"""Ce e neobișnuit în casă — învățat din istoric, fără listă de dispozitive și fără AI."""
from __future__ import annotations

import fnmatch
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, tzinfo

from .recorder import is_discrete_tracked, is_numeric_tracked

OPEN_CLASSES = {"door", "window", "opening", "garage_door"}
# Pragurile minime sub care nu are sens să alertăm, oricât de „anormal” ar fi statistic.
MIN_SPREAD = {"temperature": 1.5, "humidity": 6, "power": 30, "illuminance": 50, "co2": 150,
              "pressure": 5, "voltage": 8, "current": 1, "battery": 10}
MIN_DWELL = {"door": 15 * 60, "window": 6 * 3600, "opening": 30 * 60, "garage_door": 15 * 60,
             "light": 5 * 3600, "switch": 8 * 3600, "media_player": 6 * 3600, "fan": 8 * 3600,
             "climate": 12 * 3600, "vacuum": 3 * 3600}
LONG_STATES = {"light": "on", "switch": "on", "fan": "on", "media_player": "on", "vacuum": "cleaning"}


@dataclass
class Finding:
    key: str
    kind: str          # range | silent | flapping | dwell | open_away
    entity_id: str
    title: str
    message: str
    level: str = "warning"

    def to_dict(self) -> dict:
        return asdict(self)


def fmt_dur(sec: float) -> str:
    if sec < 3600:
        return f"{int(sec // 60)} min"
    if sec < 86400:
        h, m = divmod(int(sec // 60), 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{sec / 86400:.1f} zile"


def _pct(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _ts(v) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Baselines:
    """Ce e normal: valori pe oră, cât stă fiecare dispozitiv în fiecare stare, cât de des se schimbă."""

    def __init__(self, tz: tzinfo) -> None:
        self.tz = tz
        self.hourly: dict[tuple[str, int], tuple[int, float, float, float, float, int]] = {}
        self.overall: dict[str, tuple[float, float]] = {}
        self.rate: dict[str, float] = {}  # eșantioane pe oră, în medie
        self.dwell: dict[tuple[str, str], list[float]] = defaultdict(list)
        self.max_burst: dict[str, int] = {}

    def build(self, samples: list[tuple], transitions: list[tuple]) -> "Baselines":
        acc: dict[tuple[str, int], list] = {}
        hours_seen: dict[str, int] = defaultdict(int)
        total_n: dict[str, int] = defaultdict(int)
        mins: dict[str, float] = {}
        maxs: dict[str, float] = {}
        for eid, hour, n, total, sq, mn, mx in samples:
            hod = datetime.fromtimestamp(hour * 3600, self.tz).hour
            a = acc.setdefault((eid, hod), [0, 0.0, 0.0, math.inf, -math.inf, 0])
            a[0] += n; a[1] += total; a[2] += sq
            a[3] = min(a[3], mn); a[4] = max(a[4], mx); a[5] += 1
            hours_seen[eid] += 1
            total_n[eid] += n
            mins[eid] = min(mins.get(eid, math.inf), mn)
            maxs[eid] = max(maxs.get(eid, -math.inf), mx)
        for (eid, hod), (n, total, sq, mn, mx, days) in acc.items():
            mean = total / n
            std = math.sqrt(max(sq / n - mean * mean, 0))
            self.hourly[(eid, hod)] = (n, mean, std, mn, mx, days)
        for eid in hours_seen:
            self.rate[eid] = total_n[eid] / hours_seen[eid]
            self.overall[eid] = (mins[eid], maxs[eid])

        last: dict[str, tuple[float, str]] = {}
        buckets: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for ts, eid, _old, new, _o in transitions:
            if eid in last:
                pts, pst = last[eid]
                if pst not in ("unavailable", "unknown"):
                    self.dwell[(eid, pst)].append(ts - pts)
            last[eid] = (ts, new)
            buckets[eid][int(ts // 900)] += 1
        for eid, b in buckets.items():
            self.max_burst[eid] = max(b.values())
        return self


class Detector:
    def __init__(self, ha, cfg: dict, tz: tzinfo, persons: list[str]) -> None:
        self.ha = ha
        self.tz = tz
        self.persons = persons
        self.ignore = [p for p in cfg.get("ignore", []) if p]
        self.enabled = {k: bool(cfg.get(k, True)) for k in ("range", "silent", "flapping", "dwell")}
        self.enabled["open_away"] = bool(cfg.get("open_away", False))
        self.min_days = int(cfg.get("min_days", 5))
        self.base: Baselines | None = None
        self.muted: set[str] = set()          # entity:kind respinse definitiv
        self.dwell_factor: dict[str, float] = {}  # crescut la fiecare „ignoră”

    def ignored(self, eid: str, kind: str) -> bool:
        return f"{eid}:{kind}" in self.muted or any(fnmatch.fnmatch(eid, p) for p in self.ignore)

    def check(self, recent: list[tuple], now: float | None = None) -> list[Finding]:
        """recent = tranzițiile din ultimele ~30 min (pentru flapping)."""
        if self.base is None:
            return []
        now = now or time.time()
        out: list[Finding] = []
        for eid, st in self.ha.states.items():
            if is_numeric_tracked(eid, st):
                if self.enabled["range"] and not self.ignored(eid, "range"):
                    f = self._range(eid, st, now)
                    if f:
                        out.append(f)
                if self.enabled["silent"] and not self.ignored(eid, "silent"):
                    f = self._silent(eid, st, now)
                    if f:
                        out.append(f)
            elif is_discrete_tracked(eid, st):
                if self.enabled["dwell"] and not self.ignored(eid, "dwell"):
                    f = self._dwell(eid, st, now)
                    if f:
                        out.append(f)
        if self.enabled["flapping"]:
            out += self._flapping(recent, now)
        if self.enabled["open_away"]:
            f = self._open_away(now)
            if f:
                out.append(f)
        return out

    def _range(self, eid: str, st: dict, now: float) -> Finding | None:
        try:
            v = float(st.get("state"))
        except (TypeError, ValueError):
            return None
        hod = datetime.fromtimestamp(now, self.tz).hour
        h = self.base.hourly.get((eid, hod))
        if not h or h[5] < self.min_days or h[0] < 10:
            return None
        n, mean, std, mn, mx, _days = h
        dc = st.get("attributes", {}).get("device_class", "")
        floor = max(MIN_SPREAD.get(dc, 0), abs(mean) * 0.05)
        spread = max(std, floor)
        lo_all, hi_all = self.base.overall.get(eid, (mn, mx))
        if abs(v - mean) / spread < 5 or (lo_all - floor <= v <= hi_all + floor):
            return None
        unit = st.get("attributes", {}).get("unit_of_measurement", "")
        name = st.get("attributes", {}).get("friendly_name", eid)
        sens = "mare" if v > mean else "mică"
        return Finding(f"range:{eid}", "range", eid, "Valoare neobișnuită",
                       f"{name} arată {v:g}{unit}, neobișnuit de {sens} pentru ora asta "
                       f"(de obicei {mean:.1f}{unit}, între {lo_all:g} și {hi_all:g}).")

    def _silent(self, eid: str, st: dict, now: float) -> Finding | None:
        rate = self.base.rate.get(eid, 0)
        if rate < 1 or st.get("state") in ("unavailable", "unknown"):
            return None  # raportează rar oricum; „unavailable” e treaba paznicului
        last = _ts(st.get("last_reported")) or _ts(st.get("last_updated"))
        if last is None:
            return None
        limit = max(6 * 3600, 10 * 3600 / rate)
        if now - last < limit:
            return None
        name = st.get("attributes", {}).get("friendly_name", eid)
        return Finding(f"silent:{eid}", "silent", eid, "Senzor tăcut",
                       f"{name} n-a mai raportat de {fmt_dur(now - last)} "
                       f"(de obicei de ~{rate:.0f} ori pe oră).")

    def _dwell(self, eid: str, st: dict, now: float) -> Finding | None:
        domain = eid.split(".", 1)[0]
        a = st.get("attributes", {})
        state = st.get("state")
        dc = a.get("device_class")
        if domain == "binary_sensor":
            if dc not in OPEN_CLASSES or state != "on":
                return None
            key = dc
        elif LONG_STATES.get(domain) == state:
            key = domain
        else:
            return None
        since = _ts(st.get("last_changed"))
        if since is None:
            return None
        hist = self.base.dwell.get((eid, state), [])
        if len(hist) < 5:
            return None
        limit = max(_pct(hist, 0.95) * 3, MIN_DWELL.get(key, 4 * 3600)) * self.dwell_factor.get(eid, 1.0)
        dur = now - since
        if dur < limit:
            return None
        name = a.get("friendly_name", eid)
        what = "deschis(ă)" if domain == "binary_sensor" else ("pornit(ă)" if state == "on" else state)
        return Finding(f"dwell:{eid}", "dwell", eid, "Stă prea mult așa",
                       f"{name} e {what} de {fmt_dur(dur)}; de obicei cel mult {fmt_dur(_pct(hist, 0.95))}.")

    def _flapping(self, recent: list[tuple], now: float) -> list[Finding]:
        counts: dict[str, int] = defaultdict(int)
        for ts, eid, _o, _n, origin in recent:
            if now - ts <= 900 and origin != "brain":
                counts[eid] += 1
        out = []
        for eid, c in counts.items():
            if self.ignored(eid, "flapping"):
                continue
            usual = self.base.max_burst.get(eid, 0)
            if c >= max(10, usual * 2):
                out.append(Finding(f"flapping:{eid}", "flapping", eid, "Se aprinde și se stinge întruna",
                                   f"{self.ha.name(eid)} și-a schimbat starea de {c} ori în 15 min "
                                   f"(maxim obișnuit: {usual})."))
        return out

    def _open_away(self, now: float) -> Finding | None:
        if not self.persons or any(self.ha.state(p) == "home" for p in self.persons):
            return None
        opened = []
        for eid, st in self.ha.states.items():
            if eid.startswith("binary_sensor.") and st.get("state") == "on" \
                    and st.get("attributes", {}).get("device_class") in OPEN_CLASSES:
                since = _ts(st.get("last_changed")) or now
                if now - since > 120:
                    opened.append(st.get("attributes", {}).get("friendly_name", eid))
        if not opened:
            return None
        return Finding("open_away:house", "open_away", "house", "Casă deschisă, nimeni acasă",
                       "Nu e nimeni acasă, dar e deschis: " + ", ".join(opened) + ".", "critical")
