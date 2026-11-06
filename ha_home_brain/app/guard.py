"""Paznicul: verifică periodic sănătatea Home Assistant."""
from __future__ import annotations

import asyncio
import fnmatch
import logging
from datetime import datetime, timezone

from .notify import Alert

log = logging.getLogger("home_brain.guard")

BAD_STATES = {"unavailable", "unknown"}
WATCH_DOMAINS = {"sensor", "binary_sensor", "switch", "light", "climate", "lock",
                 "cover", "fan", "camera", "media_player", "alarm_control_panel"}


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
        self.battery_threshold = int(cfg.get("battery_threshold", 20))
        self.ignore = [p for p in cfg.get("ignore", []) if p]
        self.interval = int(cfg.get("interval_seconds", 60))
        self.findings: dict[str, Alert] = {}

    def ignored(self, entity_id: str) -> bool:
        return any(fnmatch.fnmatch(entity_id, p) for p in self.ignore)

    def check(self, now: datetime | None = None) -> dict[str, Alert]:
        """Întoarce problemele curente (cheie -> alertă). Logică pură, ușor de testat."""
        now = now or datetime.now(timezone.utc)
        found: dict[str, Alert] = {}
        for eid, st in self.ha.states.items():
            if self.ignored(eid):
                continue
            domain = eid.split(".", 1)[0]
            attrs = st.get("attributes", {})
            name = attrs.get("friendly_name", eid)
            state = st.get("state")

            if domain in WATCH_DOMAINS and state in BAD_STATES:
                since = _parse(st.get("last_changed"))
                if since and (now - since).total_seconds() >= self.unavailable_minutes * 60:
                    mins = int((now - since).total_seconds() // 60)
                    found[f"unavailable:{eid}"] = Alert(
                        key=f"unavailable:{eid}", title="Dispozitiv indisponibil",
                        message=f"{name} ({eid}) e {state} de {mins} minute.")

            battery = None
            if domain == "sensor" and attrs.get("device_class") == "battery":
                battery = state
            elif "battery_level" in attrs:
                battery = attrs.get("battery_level")
            elif "battery" in attrs and domain != "sensor":
                battery = attrs.get("battery")
            try:
                level = float(battery) if battery is not None else None
            except (TypeError, ValueError):
                level = None
            if level is not None and level <= self.battery_threshold:
                found[f"battery:{eid}"] = Alert(
                    key=f"battery:{eid}", title="Baterie slabă",
                    message=f"{name}: baterie {level:.0f}%.",
                    level="critical" if level <= 5 else "warning")
        return found

    async def run_once(self) -> None:
        found = self.check()
        for key, alert in found.items():
            await self.notifier.raise_alert(alert)
        for key in list(self.notifier.active):
            if key.split(":", 1)[0] in {"unavailable", "battery"} and key not in found:
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
