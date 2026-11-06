"""Automatizări inteligente. Fiecare regulă se activează doar dacă are entități configurate."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("home_brain.automations")

HOME = "home"


class Rule:
    name = "regulă"

    def __init__(self, ha, notifier) -> None:
        self.ha = ha
        self.notifier = notifier
        self.last_action = "—"

    @property
    def enabled(self) -> bool:
        return bool(self.entities())

    def entities(self) -> set[str]:
        return set()

    async def evaluate(self) -> None:  # pragma: no cover - suprascris
        raise NotImplementedError

    def status(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "last_action": self.last_action}


class PresenceCamera(Rule):
    """Camerele (Frigate) pornite doar când nu e nimeni acasă."""
    name = "Camere doar când nu e nimeni acasă"

    def __init__(self, ha, notifier, cfg: dict) -> None:
        super().__init__(ha, notifier)
        self.persons = [p for p in cfg.get("persons", []) if p]
        self.targets = [c for c in cfg.get("camera_switches", []) if c]
        self._last: bool | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.persons and self.targets)

    def entities(self) -> set[str]:
        return set(self.persons) | set(self.targets)

    def someone_home(self) -> bool | None:
        states = [self.ha.state(p) for p in self.persons]
        if any(s is None or s in {"unavailable", "unknown"} for s in states):
            return None  # nu știm sigur -> nu facem nimic
        return any(s == HOME for s in states)

    async def evaluate(self) -> None:
        home = self.someone_home()
        if home is None:
            return
        want_on = not home
        wrong = [t for t in self.targets if self.ha.state(t) != ("on" if want_on else "off")]
        if not wrong:
            self._last = home
            return
        service = "turn_on" if want_on else "turn_off"
        await self.ha.call_service("homeassistant", service, target={"entity_id": wrong})
        self.last_action = ("Nimeni acasă: camere pornite" if want_on
                            else "Cineva acasă: camere oprite")
        if self._last is not None and self._last != home:
            await self.notifier.send("Camere", self.last_action, "info")
        self._last = home


class HeatingAcLock(Rule):
    """Centrala nu pornește cât timp AC-ul încălzește; revine apoi la modul anterior."""
    name = "Centrală oprită cât încălzește AC-ul"

    def __init__(self, ha, notifier, cfg: dict) -> None:
        super().__init__(ha, notifier)
        self.acs = [a for a in cfg.get("ac_climates", []) if a]
        self.boiler = cfg.get("boiler_climate", "") or ""
        self.saved_mode: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.acs and self.boiler)

    def entities(self) -> set[str]:
        return set(self.acs) | ({self.boiler} if self.boiler else set())

    def ac_heating(self) -> bool:
        return any(self.ha.state(a) == "heat" for a in self.acs)

    async def evaluate(self) -> None:
        boiler_mode = self.ha.state(self.boiler)
        if boiler_mode in (None, "unavailable", "unknown"):
            return
        if self.ac_heating():
            if boiler_mode != "off":
                self.saved_mode = boiler_mode
                await self.ha.call_service("climate", "set_hvac_mode", {"hvac_mode": "off"},
                                           target={"entity_id": self.boiler})
                self.last_action = f"AC încălzește: centrala oprită (era {boiler_mode})"
        elif self.saved_mode and boiler_mode == "off":
            mode, self.saved_mode = self.saved_mode, None
            await self.ha.call_service("climate", "set_hvac_mode", {"hvac_mode": mode},
                                       target={"entity_id": self.boiler})
            self.last_action = f"AC nu mai încălzește: centrala revine pe {mode}"


class FollowThermostat(Rule):
    """Robinetele termostatate urmează temperatura setată pe termostatul centralei."""
    name = "Robinete urmează termostatul"

    def __init__(self, ha, notifier, cfg: dict) -> None:
        super().__init__(ha, notifier)
        self.leader = cfg.get("leader_climate", "") or ""
        self.followers = [f for f in cfg.get("follower_climates", []) if f]

    @property
    def enabled(self) -> bool:
        return bool(self.leader and self.followers)

    def entities(self) -> set[str]:
        return ({self.leader} if self.leader else set()) | set(self.followers)

    async def evaluate(self) -> None:
        target = self.ha.attr(self.leader, "temperature")
        if target is None:
            return
        for f in self.followers:
            if self.ha.state(f) in (None, "unavailable", "unknown"):
                continue
            current = self.ha.attr(f, "temperature")
            if current is None or abs(float(current) - float(target)) >= 0.25:
                await self.ha.call_service("climate", "set_temperature", {"temperature": target},
                                           target={"entity_id": f})
                self.last_action = f"{f} setat la {target}°C"


class Automations:
    def __init__(self, ha, notifier, cfg: dict) -> None:
        self.ha = ha
        self.rules: list[Rule] = [
            PresenceCamera(ha, notifier, cfg.get("presence_camera", {})),
            HeatingAcLock(ha, notifier, cfg.get("heating_ac_lock", {})),
            FollowThermostat(ha, notifier, cfg.get("follow_thermostat", {})),
        ]
        self._lock = asyncio.Lock()
        ha.on_state_change(self._on_change)

    async def _on_change(self, entity_id: str, old, new) -> None:
        for rule in self.rules:
            if rule.enabled and entity_id in rule.entities():
                await self._run(rule)

    async def _run(self, rule: Rule) -> None:
        async with self._lock:
            try:
                await rule.evaluate()
            except Exception:  # noqa: BLE001
                log.exception("Eroare în regula %s", rule.name)

    async def run_forever(self) -> None:
        while True:
            await self.ha.connected.wait()
            for rule in self.rules:
                if rule.enabled:
                    await self._run(rule)
            await asyncio.sleep(300)

    def status(self) -> list[dict]:
        return [r.status() for r in self.rules]
