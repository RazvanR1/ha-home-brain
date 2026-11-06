"""Trimiterea alertelor: servicii notify din HA și/sau ntfy."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("home_brain.notify")


@dataclass
class Alert:
    key: str
    title: str
    message: str
    level: str = "warning"  # info | warning | critical
    time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    resolved: bool = False


class Notifier:
    def __init__(self, ha, services: list[str], ntfy_url: str = "", history: int = 200) -> None:
        self.ha = ha
        self.services = [s for s in services if s]
        self.ntfy_url = ntfy_url.rstrip("/")
        self.history: deque[Alert] = deque(maxlen=history)
        self.active: dict[str, Alert] = {}

    async def raise_alert(self, alert: Alert, send: bool = True) -> None:
        """Trimite alerta o singură dată per cheie, până se rezolvă."""
        if alert.key in self.active:
            return
        self.active[alert.key] = alert
        self.history.appendleft(alert)
        if send:
            await self.send(alert.title, alert.message, alert.level)

    async def resolve(self, key: str, message: str | None = None) -> None:
        alert = self.active.pop(key, None)
        if alert is None:
            return
        alert.resolved = True
        if message:
            ok = Alert(key=key, title=f"Rezolvat: {alert.title}", message=message, level="info")
            ok.resolved = True
            self.history.appendleft(ok)
            await self.send(ok.title, message, "info")

    async def send(self, title: str, message: str, level: str = "info") -> None:
        for svc in self.services:
            domain, _, name = svc.partition(".")
            if not name:
                domain, name = "notify", domain
            try:
                await self.ha.call_service(domain, name, {"title": title, "message": message})
            except Exception as err:  # noqa: BLE001
                log.warning("Nu am putut trimite prin %s: %s", svc, err)
        if self.ntfy_url:
            prio = {"info": "default", "warning": "high", "critical": "urgent"}.get(level, "default")
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(self.ntfy_url, data=message.encode(),
                                 headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                                          "Priority": prio, "Tags": "house"})
            except Exception as err:  # noqa: BLE001
                log.warning("Nu am putut trimite pe ntfy: %s", err)
        log.info("[%s] %s: %s", level, title, message)
