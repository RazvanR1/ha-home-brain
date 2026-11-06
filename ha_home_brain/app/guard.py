"""Paznicul: verifică periodic sănătatea Home Assistant."""
from __future__ import annotations

import asyncio
import fnmatch
import logging
from datetime import datetime, timezone

from .notify import Alert

log = logging.getLogger("home_brain.guard")

# media_player lipsește intenționat: televizoarele apar „unavailable” când sunt stinse.
DEFAULT_DOMAINS = ["sensor", "binary_sensor", "switch", "light", "climate", "lock",
                   "cover", "fan", "camera", "alarm_control_panel", "vacuum"]
SUMMARY_AFTER = 3  # peste atâtea alerte noi într-o rundă -> un singur mesaj rezumat


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class Guard:
    def __init__(self, ha, notifier, cfg: dict) -> None:
        self.ha = ha
        self.notifier = notifier
        self.unavailable_minutes = int(cfg.get("unavailable_minutes", 30))
        # Ce e picat de mai mult de atât e „cunoscut” (dispozitiv scos, container șters) -> nu alertăm.
        self.max_hours = float(cfg.get("max_unavailable_hours", 24))
        self.bad_states = set(cfg.get("bad_states") or ["unavailable"])
        self.domains = set(cfg.get("domains") or DEFAULT_DOMAINS)
        self.battery_threshold = int(cfg.get("battery_threshold", 20))
        self.ignore = [p for p in cfg.get("ignore", []) if p]
        self.interval = int(cfg.get("interval_seconds", 60))
        self.findings: dict[str, Alert] = {}

    def ignored(self, entity_id: str) -> bool:
        return any(fnmatch.fnmatch(entity_id, p) for p in self.ignore)

    def battery_level(self, eid: str, st: dict) -> float | None:
        attrs = st.get("attributes", {})
        domain = eid.split(".", 1)[0]
        if domain == "sensor" and attrs.get("device_class") == "battery":
            raw = st.get("state")
        elif "battery_level" in attrs:
            raw = attrs.get("battery_level")
        elif "battery" in attrs and domain != "sensor":
            raw = attrs.get("battery")
        else:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def check(self, now: datetime | None = None) -> dict[str, Alert]:
        """Întoarce problemele curente (cheie -> alertă). Logică pură, ușor de testat."""
        now = now or datetime.now(timezone.utc)
        found: dict[str, Alert] = {}
        for eid, st in self.ha.states.items():
            if self.ignored(eid):
                continue
            domain = eid.split(".", 1)[0]
            name = st.get("attributes", {}).get("friendly_name", eid)
            state = st.get("state")

            if domain in self.domains and state in self.bad_states:
                since = _parse(st.get("last_changed"))
                if since:
                    age = (now - since).total_seconds()
                    if self.unavailable_minutes * 60 <= age <= self.max_hours * 3600:
                        found[f"unavailable:{eid}"] = Alert(
                            key=f"unavailable:{eid}", title="Dispozitiv indisponibil",
                            message=f"{name} e {state} de {int(age // 60)} minute.")

            level = self.battery_level(eid, st)
            if level is not None and level <= self.battery_threshold:
                found[f"battery:{eid}"] = Alert(
                    key=f"battery:{eid}", title="Baterie slabă",
                    message=f"{name}: baterie {level:.0f}%.",
                    level="critical" if level <= 5 else "warning")
        return found

    def fixed(self, key: str) -> bool:
        """O alertă activă se închide doar când problema chiar a dispărut."""
        kind, _, eid = key.partition(":")
        st = self.ha.states.get(eid)
        if st is None:
            return True  # entitatea a fost ștearsă
        if kind == "unavailable":
            return st.get("state") not in self.bad_states
        if kind == "battery":
            level = self.battery_level(eid, st)
            return level is None or level > self.battery_threshold
        return False

    async def run_once(self) -> None:
        found = self.check()
        new = [a for k, a in found.items() if k not in self.notifier.active]
        if len(new) > SUMMARY_AFTER:
            for alert in new:
                await self.notifier.raise_alert(alert, send=False)
            names = ", ".join(self.ha.attr(a.key.partition(":")[2], "friendly_name",
                                           a.key.partition(":")[2]) for a in new[:8])
            more = f" și încă {len(new) - 8}" if len(new) > 8 else ""
            await self.notifier.send("Mai multe probleme în casă",
                                     f"{len(new)} probleme noi: {names}{more}.", "warning")
        else:
            for alert in new:
                await self.notifier.raise_alert(alert)
        for key in list(self.notifier.active):
            if key.split(":", 1)[0] in {"unavailable", "battery"} and key not in found \
                    and self.fixed(key):
                eid = key.split(":", 1)[1]
                name = self.ha.attr(eid, "friendly_name", eid)
                await self.notifier.resolve(key, f"{name} e din nou în regulă.")
        self.findings = found

    async def run_forever(self) -> None:
        await self.ha.connected.wait()
        await asyncio.sleep(10)  # lăsăm HA să se stabilizeze după conectare
        while True:
            await self.ha.connected.wait()
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("Eroare la verificarea paznicului")
            await asyncio.sleep(self.interval)
