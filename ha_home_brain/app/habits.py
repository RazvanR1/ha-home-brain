"""Învățarea obiceiurilor din acțiunile făcute de om. Totul local, fără AI.

Un obicei e fie de oră („aprinzi lampa pe la 22:40 în zilele lucrătoare”), fie de secvență
(„după ce se deschide ușa, aprinzi lumina de la intrare”). Fiecare e testat pe istoric
(backtest) înainte să fie propus, iar automatizarea rezultată e una nativă din HA.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from .ha import ORIGIN_DEVICE, ORIGIN_USER

HUMAN = {ORIGIN_USER, ORIGIN_DEVICE}
NON_STATES = {"unavailable", "unknown", "", "none", None}
TRIGGER_DOMAINS = {"binary_sensor", "person", "light", "switch", "input_boolean", "device_tracker"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
WEEKEND = ["sat", "sun"]
ZILE = {"weekday": "în zilele lucrătoare", "weekend": "în weekend", "all": "zilnic"}
STARE = {"on": "pornești", "off": "oprești", "open": "deschizi", "closed": "închizi",
         "cleaning": "pornești", "docked": "trimiți la bază", "heat": "pui pe încălzire",
         "cool": "pui pe răcire"}


def service_for(domain: str, state: str) -> tuple[str, dict] | None:
    """Serviciul HA care aduce entitatea în starea dată (sau None dacă nu știm sigur)."""
    if domain in ("light", "switch", "fan", "input_boolean", "humidifier"):
        return ({"on": (f"{domain}.turn_on", {}), "off": (f"{domain}.turn_off", {})}).get(state)
    if domain == "cover":
        return ({"open": ("cover.open_cover", {}), "closed": ("cover.close_cover", {})}).get(state)
    if domain == "script":
        return ("script.turn_on", {}) if state == "on" else None
    if domain == "vacuum":
        return ({"cleaning": ("vacuum.start", {}), "docked": ("vacuum.return_to_base", {})}).get(state)
    if domain == "climate":
        if state in ("off", "heat", "cool", "auto", "dry", "fan_only", "heat_cool"):
            return ("climate.set_hvac_mode", {"hvac_mode": state})
    if domain == "media_player" and state == "off":
        return ("media_player.turn_off", {})
    return None


@dataclass
class Habit:
    id: str
    kind: str                 # time | sequence
    entity_id: str
    to_state: str
    day_class: str            # weekday | weekend | all
    support_days: int
    observed_days: int
    confidence: float
    minute: float | None = None
    spread: float | None = None
    trigger_entity: str | None = None
    trigger_state: str | None = None
    lag_seconds: float | None = None
    require_home: bool = False
    backtest: dict = field(default_factory=dict)
    covered_by: str | None = None
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- utilitare de timp ----------
def local(ts: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def minute_of_day(dt: datetime) -> float:
    return dt.hour * 60 + dt.minute + dt.second / 60


def day_class_of(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


def fmt_minute(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def circ_mean(mins: list[float]) -> float:
    s = sum(math.sin(2 * math.pi * m / 1440) for m in mins)
    c = sum(math.cos(2 * math.pi * m / 1440) for m in mins)
    return (math.atan2(s, c) * 1440 / (2 * math.pi)) % 1440


def circ_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def observed_days(start_ts: float, end_ts: float, tz: tzinfo) -> dict[str, list[date]]:
    """Zilele (aproape) complete din istoric, pe clase."""
    s, e = local(start_ts, tz).date(), local(end_ts, tz).date()
    if (e - s).days >= 2:
        s += timedelta(days=1)  # prima zi e de obicei incompletă
    out: dict[str, list[date]] = {"weekday": [], "weekend": [], "all": []}
    d = s
    while d <= e:
        out[day_class_of(d)].append(d)
        out["all"].append(d)
        d += timedelta(days=1)
    return out


def best_cluster(points: list[tuple[date, float]], tol: float) -> list[tuple[date, float]]:
    """Cel mai dens grup de (zi, minut) în fereastra ±tol, cel mult un punct pe zi."""
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[1])
    ext = pts + [(d, m + 1440) for d, m in pts]
    best: list[tuple[date, float]] = []
    j = 0
    for i in range(len(pts)):
        j = max(j, i)
        while j < len(ext) and j - i < len(pts) and ext[j][1] - ext[i][1] <= 2 * tol:
            j += 1
        window = ext[i:j]
        per_day: dict[date, float] = {}
        center = ext[i][1] + tol
        for d, m in window:  # dacă sunt mai multe în aceeași zi, păstrăm cea mai apropiată de centru
            if d not in per_day or abs(m - center) < abs(per_day[d] - center):
                per_day[d] = m
        if len(per_day) > len(best):
            best = [(d, m % 1440) for d, m in per_day.items()]
    return best


class Timeline:
    """Starea fiecărei entități la orice moment, din tranziții."""

    def __init__(self, transitions: list[tuple]) -> None:
        self._ts: dict[str, list[float]] = defaultdict(list)
        self._st: dict[str, list[str]] = defaultdict(list)
        for ts, eid, _old, new, _o in transitions:
            self._ts[eid].append(ts)
            self._st[eid].append(new)

    def state_at(self, eid: str, ts: float) -> str | None:
        arr = self._ts.get(eid)
        if not arr:
            return None
        i = bisect.bisect_right(arr, ts) - 1
        return self._st[eid][i] if i >= 0 else None

    def changes(self, eid: str, start: float, end: float) -> list[tuple[float, str]]:
        arr = self._ts.get(eid, [])
        i, j = bisect.bisect_left(arr, start), bisect.bisect_right(arr, end)
        return [(arr[k], self._st[eid][k]) for k in range(i, j)]


def _hid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


# ---------- minerit ----------
class HabitMiner:
    def __init__(self, tz: tzinfo, persons: list[str] | None = None, *, tolerance_min: float = 20,
                 min_support: int = 4, min_confidence: float = 0.6, seq_window_s: float = 600,
                 min_precision: float = 0.6) -> None:
        self.tz = tz
        self.persons = persons or []
        self.tol = tolerance_min
        self.min_support = min_support
        self.min_conf = min_confidence
        self.window = seq_window_s
        self.min_precision = min_precision

    @staticmethod
    def is_habit_transition(t: tuple) -> bool:
        ts, eid, old, new, origin = t
        domain = eid.split(".", 1)[0]
        return (origin in HUMAN and new not in NON_STATES and old not in NON_STATES
                and old != new and service_for(domain, new) is not None
                and domain not in ("lock", "alarm_control_panel"))

    def mine(self, transitions: list[tuple], names: dict[str, str] | None = None) -> list[Habit]:
        if not transitions:
            return []
        names = names or {}
        days = observed_days(transitions[0][0], transitions[-1][0], self.tz)
        tl = Timeline(transitions)
        human = [t for t in transitions if self.is_habit_transition(t)]
        habits = self._time_habits(human, days, tl, names) + self._sequence_habits(
            transitions, human, days, tl, names)
        # Dacă același lucru apare și ca obicei de oră și de secvență, îl păstrăm pe cel mai precis.
        best: dict[tuple, Habit] = {}
        for h in habits:
            key = (h.entity_id, h.to_state, h.day_class)
            if key not in best or h.backtest.get("precision", 0) > best[key].backtest.get("precision", 0):
                best[key] = h
        return sorted(best.values(), key=lambda h: (-h.confidence, -h.support_days))

    def _someone_home(self, tl: Timeline, ts: float) -> bool | None:
        if not self.persons:
            return None
        states = [tl.state_at(p, ts) for p in self.persons]
        if all(s is None for s in states):
            return None
        return any(s == "home" for s in states)

    def _time_habits(self, human, days, tl, names) -> list[Habit]:
        by_key: dict[tuple[str, str], list[tuple[date, float, float]]] = defaultdict(list)
        for ts, eid, _o, new, _or in human:
            dt = local(ts, self.tz)
            by_key[(eid, new)].append((dt.date(), minute_of_day(dt), ts))
        out = []
        for (eid, state), pts in by_key.items():
            for dc in ("weekday", "weekend", "all"):
                obs = days[dc]
                if len(obs) < self.min_support:
                    continue
                obs_set = set(obs)
                cand = [(d, m) for d, m, _ in pts if d in obs_set]
                cluster = best_cluster(cand, self.tol)
                support = len(cluster)
                conf = support / len(obs)
                if support < self.min_support or conf < self.min_conf:
                    continue
                mins = [m for _, m in cluster]
                center = circ_mean(mins)
                spread = statistics.pstdev([circ_dist(m, center) for m in mins]) if len(mins) > 1 else 0
                hit_ts = [ts for d, m, ts in pts if d in obs_set and circ_dist(m, center) <= self.tol]
                home = [self._someone_home(tl, t) for t in hit_ts]
                require_home = bool(self.persons) and eid.split(".")[0] in ("light", "media_player") \
                    and home and sum(1 for x in home if x) / len(home) >= 0.9
                h = Habit(id=_hid("time", eid, state, dc), kind="time", entity_id=eid, to_state=state,
                          day_class=dc, support_days=support, observed_days=len(obs),
                          confidence=round(conf, 2), minute=round(center, 1), spread=round(spread, 1),
                          require_home=require_home)
                h.backtest = self.backtest_time(h, obs, tl)
                if h.backtest["precision"] < self.min_precision:
                    continue
                verb = STARE.get(state, f"pui pe {state}")
                h.description = (f"{ZILE[dc].capitalize()} pe la {fmt_minute(center)} {verb} "
                                 f"{names.get(eid, eid)} ({support} din {len(obs)} zile).")
                out.append(h)
        # „zilnic” doar dacă e mai bun decât lucrătoare + weekend separat
        keep = []
        for h in out:
            if h.day_class == "all" and any(o.entity_id == h.entity_id and o.to_state == h.to_state
                                            and o.day_class != "all" and o.confidence >= h.confidence
                                            for o in out):
                continue
            if h.day_class != "all" and any(o.entity_id == h.entity_id and o.to_state == h.to_state
                                            and o.day_class == "all" and o.confidence > h.confidence + 0.05
                                            for o in out):
                continue
            keep.append(h)
        return keep

    def backtest_time(self, h: Habit, obs: list[date], tl: Timeline) -> dict:
        """Dacă automatizarea ar fi existat: în câte zile ar fi făcut ce ai făcut și tu?"""
        hits = extra = already = skipped = 0
        for d in obs:
            fire = datetime(d.year, d.month, d.day, tzinfo=self.tz) + timedelta(minutes=h.minute)
            ft = fire.timestamp()
            if h.require_home and self._someone_home(tl, ft) is False:
                skipped += 1
                continue
            if tl.state_at(h.entity_id, ft) == h.to_state:
                already += 1
                continue
            near = tl.changes(h.entity_id, ft - self.tol * 60, ft + self.tol * 60)
            if any(s == h.to_state for _, s in near):
                hits += 1
            else:
                extra += 1
        fired = hits + extra
        return {"fired": fired, "hits": hits, "extra": extra, "already": already, "skipped": skipped,
                "precision": round(hits / fired, 2) if fired else 0.0}

    def _sequence_habits(self, transitions, human, days, tl, names) -> list[Habit]:
        trig = [t for t in transitions
                if t[1].split(".")[0] in TRIGGER_DOMAINS and t[3] not in NON_STATES
                and t[2] not in NON_STATES and t[4] != "brain"]
        trig_ts = [t[0] for t in trig]
        trig_count: dict[tuple[str, str], int] = defaultdict(int)
        for t in trig:
            trig_count[(t[1], t[3])] += 1
        pairs: dict[tuple, list[tuple[float, date]]] = defaultdict(list)
        target_count: dict[tuple[str, str], int] = defaultdict(int)
        for ts, eid, _o, new, _or in human:
            target_count[(eid, new)] += 1
            i, j = bisect.bisect_left(trig_ts, ts - self.window), bisect.bisect_left(trig_ts, ts - 3)
            seen = set()
            for k in range(i, j):
                a = trig[k]
                if a[1] == eid or (a[1], a[3]) in seen:
                    continue
                seen.add((a[1], a[3]))
                pairs[(a[1], a[3], eid, new)].append((ts - a[0], local(ts, self.tz).date()))
        out = []
        for (ae, as_, be, bs), occ in pairs.items():
            support = len({d for _, d in occ})
            if support < self.min_support:
                continue
            p_b_given_a = len(occ) / max(trig_count[(ae, as_)], 1)
            p_a_given_b = len(occ) / max(target_count[(be, bs)], 1)
            if p_b_given_a < self.min_conf * 0.75 or p_a_given_b < 0.5:
                continue
            lag = statistics.median(l for l, _ in occ)
            h = Habit(id=_hid("seq", ae, as_, be, bs), kind="sequence", entity_id=be, to_state=bs,
                      day_class="all", support_days=support, observed_days=len(days["all"]),
                      confidence=round(p_b_given_a, 2), trigger_entity=ae, trigger_state=as_,
                      lag_seconds=round(lag, 1))
            h.backtest = self.backtest_sequence(h, trig, tl)
            if h.backtest["precision"] < self.min_precision:
                continue
            verb = STARE.get(bs, f"pui pe {bs}")
            h.description = (f"După ce {names.get(ae, ae)} devine „{as_}”, {verb} {names.get(be, be)} "
                             f"în ~{int(lag)} s ({len(occ)} din {trig_count[(ae, as_)]} ori).")
            out.append(h)
        return out

    def backtest_sequence(self, h: Habit, trig: list[tuple], tl: Timeline) -> dict:
        hits = extra = already = 0
        for ts, eid, _o, new, _or in trig:
            if eid != h.trigger_entity or new != h.trigger_state:
                continue
            if tl.state_at(h.entity_id, ts) == h.to_state:
                already += 1
                continue
            after = tl.changes(h.entity_id, ts, ts + self.window)
            if any(s == h.to_state for _, s in after):
                hits += 1
            else:
                extra += 1
        fired = hits + extra
        return {"fired": fired, "hits": hits, "extra": extra, "already": already, "skipped": 0,
                "precision": round(hits / fired, 2) if fired else 0.0}


# ---------- automatizări existente ----------
def _walk_entities(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("entity_id", "entity_ids") and isinstance(v, (str, list)):
                for e in ([v] if isinstance(v, str) else v):
                    if isinstance(e, str):
                        out.add(e)
            else:
                _walk_entities(v, out)
    elif isinstance(node, list):
        for x in node:
            _walk_entities(x, out)


def automation_index(configs: dict[str, dict]) -> list[dict]:
    """Rezumatul automatizărilor existente: ce declanșează, ce comandă, la ce oră."""
    idx = []
    for eid, cfg in configs.items():
        if not cfg:
            continue
        triggers = cfg.get("triggers", cfg.get("trigger", [])) or []
        triggers = triggers if isinstance(triggers, list) else [triggers]
        actions = cfg.get("actions", cfg.get("action", [])) or []
        t_ent, a_ent = set(), set()
        _walk_entities(triggers, t_ent)
        _walk_entities(actions, a_ent)
        times = []
        for t in triggers:
            at = t.get("at") if isinstance(t, dict) else None
            for a in ([at] if isinstance(at, str) else at or []):
                try:
                    hh, mm = a.split(":")[:2]
                    times.append(int(hh) * 60 + int(mm))
                except (ValueError, AttributeError):
                    pass
        idx.append({"entity_id": eid, "alias": cfg.get("alias", eid), "id": cfg.get("id"),
                    "trigger_entities": t_ent, "action_entities": a_ent, "times": times,
                    "has_sun": any(isinstance(t, dict) and t.get("trigger", t.get("platform")) == "sun"
                                   for t in triggers)})
    return idx


def mark_covered(habits: list[Habit], index: list[dict]) -> None:
    for h in habits:
        for a in index:
            if h.entity_id not in a["action_entities"]:
                continue
            if h.kind == "sequence" and h.trigger_entity in a["trigger_entities"]:
                h.covered_by = a["alias"]
            elif h.kind == "time" and (a["has_sun"] or any(circ_dist(t, h.minute) <= 45 for t in a["times"])):
                h.covered_by = a["alias"]
            if h.covered_by:
                break


# ---------- automatizarea nativă ----------
def build_automation(h: Habit, names: dict[str, str], persons: list[str]) -> dict:
    domain = h.entity_id.split(".", 1)[0]
    service, data = service_for(domain, h.to_state)
    action = {"action": service, "target": {"entity_id": h.entity_id}}
    if data:
        action["data"] = data
    conditions: list[dict] = []
    if h.kind == "time":
        triggers = [{"trigger": "time", "at": fmt_minute(h.minute) + ":00"}]
        if h.day_class != "all":
            conditions.append({"condition": "time",
                               "weekday": WEEKDAYS if h.day_class == "weekday" else WEEKEND})
    else:
        triggers = [{"trigger": "state", "entity_id": h.trigger_entity, "to": h.trigger_state}]
    if h.require_home and persons:
        conditions.append({"condition": "or", "conditions": [
            {"condition": "state", "entity_id": p, "state": "home"} for p in persons]})
    conditions.append({"condition": "not", "conditions": [
        {"condition": "state", "entity_id": h.entity_id, "state": h.to_state}]})
    return {
        "id": f"home_brain_{h.id}",
        "alias": f"Home Brain – {names.get(h.entity_id, h.entity_id)} ({h.to_state})",
        "description": f"Creat de Home Brain din obiceiul: {h.description} "
                       f"Backtest: {h.backtest.get('hits')}/{h.backtest.get('fired')} corecte.",
        "triggers": triggers, "conditions": conditions, "actions": [action], "mode": "single",
    }
