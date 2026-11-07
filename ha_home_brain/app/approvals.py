"""Aprobări din notificare, reguli de siguranță (guardrails) și Undo."""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Awaitable, Callable

from .habits import service_for

log = logging.getLogger("home_brain.approvals")

SHADOW, COPILOT, AUTONOMOUS = "shadow", "copilot", "autonomous"
# Nu le execută niciodată, indiferent ce cere un agent.
BLOCKED = {"shell_command", "python_script", "pyscript", "hassio", "rest_command", "recorder",
           "system_log", "logger", "backup", "update"}
BLOCKED_SERVICES = {("homeassistant", "stop"), ("homeassistant", "restart"),
                    ("homeassistant", "reload_all"), ("automation", "turn_off"),
                    ("automation", "reload")}
CRITICAL_SERVICES = {("lock", "unlock"), ("lock", "open"), ("valve", "open_valve"),
                     ("alarm_control_panel", "alarm_disarm"), ("cover", "open_cover")}
CRITICAL_WORDS = ("centrala", "central_heating", "boiler", "gaz", "gas", "pompa", "robinet",
                  "valve", "frigider", "refrigerator", "server", "nas", "proxmox", "truenas")


def _sid(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


class Guardrails:
    def __init__(self, cfg: dict) -> None:
        self.min_temp = float(cfg.get("min_temperature", 16))
        self.max_temp = float(cfg.get("max_temperature", 26))
        self.default_mode = cfg.get("autonomy", COPILOT)
        self.overrides: dict[str, str] = {}
        for item in cfg.get("autonomy_overrides", []) or []:
            if ":" in item:
                d, m = item.split(":", 1)
                self.overrides[d.strip()] = m.strip()

    def mode_for(self, domain: str) -> str:
        return self.overrides.get(domain, self.default_mode)

    def evaluate(self, domain: str, service: str, entity_id: str, data: dict | None) -> tuple[str, str]:
        """Întoarce (decizie, motiv): blocked | approve | allowed."""
        data = data or {}
        if domain in BLOCKED or (domain, service) in BLOCKED_SERVICES:
            return "blocked", f"{domain}.{service} nu e permis niciodată"
        if entity_id and entity_id.split(".", 1)[0] != domain and domain not in ("homeassistant",):
            return "blocked", "entitatea nu corespunde domeniului serviciului"
        for key in ("temperature", "target_temp_high", "target_temp_low"):
            if key in data:
                try:
                    t = float(data[key])
                except (TypeError, ValueError):
                    return "blocked", "temperatură invalidă"
                if not self.min_temp <= t <= self.max_temp:
                    return "blocked", f"temperatura {t} e în afara limitelor {self.min_temp}–{self.max_temp}°C"
        if (domain, service) in CRITICAL_SERVICES or domain in ("lock", "alarm_control_panel", "valve"):
            return "approve", "acțiune critică: cere mereu aprobare"
        if any(w in entity_id.lower() for w in CRITICAL_WORDS):
            return "approve", "dispozitiv important: cere mereu aprobare"
        return "allowed", ""


class Approvals:
    def __init__(self, ha, store, notifier, cfg: dict) -> None:
        self.ha = ha
        self.store = store
        self.notifier = notifier
        self.guard = Guardrails(cfg)
        self.actionable = bool(cfg.get("actionable_notifications", True))
        self.handlers: dict[str, Callable[[dict, str], Awaitable[str]]] = {}
        ha.on_event("mobile_app_notification_action", self._on_action)

    # ---------- cereri ----------
    def on(self, kind: str, handler: Callable[[dict, str], Awaitable[str]]) -> None:
        """handler(item, decizie) -> mesaj; decizie: ok | always | no | undo."""
        self.handlers[kind] = handler

    async def ask(self, kind: str, item_id: str, title: str, message: str, payload: dict,
                  buttons: list[tuple[str, str]] | None = None, notify: bool = True) -> str:
        rid = _sid(kind, item_id)
        existing = self.store.item("approval", rid)
        if existing and existing["status"] in ("pending", "rejected", "done"):
            return rid
        buttons = buttons or [("ok", "Aprobă"), ("always", "Mereu"), ("no", "Respinge")]
        self.store.put_item("approval", rid, {"kind": kind, "item_id": item_id, "title": title,
                                              "message": message, "payload": payload,
                                              "buttons": buttons}, status="pending")
        if notify:
            await self._push(rid, title, message, buttons)
        return rid

    async def _push(self, rid: str, title: str, message: str, buttons: list[tuple[str, str]]) -> None:
        extra = {"tag": f"hb_{rid}", "actions": [{"action": f"HB_{b.upper()}_{rid}", "title": t}
                                                 for b, t in buttons]} if self.actionable else None
        await self.notifier.send(title, message, "info", data=extra)

    async def _on_action(self, event: dict) -> None:
        action = (event.get("data") or {}).get("action", "")
        if not action.startswith("HB_"):
            return
        _, decision, rid = action.split("_", 2)
        await self.decide(rid, decision.lower())

    async def decide(self, rid: str, decision: str) -> str:
        item = self.store.item("approval", rid)
        if not item:
            return "Cererea nu mai există."
        if decision != "undo" and item["status"] != "pending":
            return "Deja decis."
        handler = self.handlers.get(item["kind"])
        msg = await handler(item, decision) if handler else "Nimic de făcut."
        status = {"ok": "done", "always": "done", "no": "rejected", "undo": "undone",
                  "seen": "done", "mute": "rejected"}.get(decision, "done")
        self.store.set_status("approval", rid, status)
        self.store.log("decizie", f"{item['title']}: {decision} – {msg}")
        return msg

    def pending(self) -> list[dict]:
        return self.store.items("approval", "pending", limit=100)

    # ---------- acțiuni pe dispozitive ----------
    def _signature(self, domain: str, service: str, entity_id: str) -> str:
        return f"{domain}.{service}:{entity_id}"

    async def request_action(self, source: str, domain: str, service: str, entity_id: str,
                             data: dict | None, reason: str, user_initiated: bool = False) -> dict:
        """Calea unică prin care un agent (sau Jarvis) poate atinge un dispozitiv."""
        decision, why = self.guard.evaluate(domain, service, entity_id, data)
        sig = self._signature(domain, service, entity_id)
        if decision == "blocked":
            self.store.log("blocat", f"{source}: {sig} – {why}")
            return {"status": "blocked", "reason": why}
        if user_initiated and decision == "allowed":
            # Omul a cerut-o explicit în chat: nu mai întrebăm, dar trecem prin guardrails.
            return await self.execute({"domain": domain, "service": service, "entity_id": entity_id,
                                       "data": data or {}, "source": source, "reason": reason})
        if not user_initiated and sig in set(self.store.get("rejected_signatures", [])):
            return {"status": "rejected_before"}
        mode = COPILOT if user_initiated else self.guard.mode_for(domain)
        allowed_always = sig in set(self.store.get("allowed_signatures", []))
        payload = {"domain": domain, "service": service, "entity_id": entity_id, "data": data or {},
                   "source": source, "reason": reason}
        name = self.ha.name(entity_id)
        if mode == SHADOW:
            self.store.log("umbră", f"{source} ar fi făcut {service} pe {name}: {reason}")
            return {"status": "shadow"}
        if decision == "allowed" and (mode == AUTONOMOUS or allowed_always):
            return await self.execute(payload, notify_undo=True)
        rid = await self.ask("action", sig + str(int(time.time() // 3600)),
                             f"{source}: {service.replace('_', ' ')} {name}?",
                             f"{reason}" + (f" ({why})" if why else ""), payload)
        return {"status": "pending", "request": rid}

    async def execute(self, payload: dict, notify_undo: bool = False) -> dict:
        eid = payload["entity_id"]
        before = self.ha.state(eid)
        await self.ha.call_service(payload["domain"], payload["service"], payload.get("data") or None,
                                   target={"entity_id": eid})
        did = _sid("done", eid, time.time())
        self.store.put_item("done", did, {**payload, "before": before, "name": self.ha.name(eid)},
                            status="done")
        self.store.log("acțiune", f"{payload['source']}: {payload['service']} pe {self.ha.name(eid)}")
        if notify_undo:
            await self._push(f"undo{did}", f"Am făcut: {payload['service'].replace('_', ' ')} "
                                           f"{self.ha.name(eid)}", payload.get("reason", ""),
                             [("undo", "Anulează")])
            self.store.put_item("approval", f"undo{did}", {"kind": "undo", "item_id": did,
                                                           "title": "Anulează", "message": "",
                                                           "payload": {}, "buttons": []}, status="pending")
        return {"status": "done", "id": did}

    async def undo(self, did: str) -> str:
        done = self.store.item("done", did)
        if not done or done["status"] != "done":
            return "Nu am ce anula."
        if done.get("automation_id"):
            await self.ha.delete_automation(done["automation_id"])
            self.store.set_status("done", did, "undone")
            return "Automatizarea a fost ștearsă."
        before = done.get("before")
        svc = service_for(done["domain"], before) if before else None
        if not svc:
            return f"Nu știu cum să readuc {done['name']} la „{before}”."
        service, data = svc
        d, s = service.split(".")
        await self.ha.call_service(d, s, data or None, target={"entity_id": done["entity_id"]})
        self.store.set_status("done", did, "undone")
        return f"{done['name']} a revenit la „{before}”."

    async def handle_action(self, item: dict, decision: str) -> str:
        p = item["payload"]
        sig = self._signature(p["domain"], p["service"], p["entity_id"])
        if decision == "no":
            self.store.set("rejected_signatures", sorted(set(self.store.get("rejected_signatures", [])) | {sig}))
            return "Respins; nu mai propun asta."
        if decision == "always":
            if self.guard.evaluate(p["domain"], p["service"], p["entity_id"], p["data"])[0] == "allowed":
                self.store.set("allowed_signatures", sorted(set(self.store.get("allowed_signatures", [])) | {sig}))
        res = await self.execute(p)
        return "Făcut." if res["status"] == "done" else str(res)

    async def handle_undo(self, item: dict, decision: str) -> str:
        return await self.undo(item["item_id"]) if decision == "undo" else "—"

    def recent_done(self, limit: int = 30) -> list[dict]:
        return self.store.items("done", limit=limit)

    def summary(self) -> dict[str, Any]:
        return {"mode": self.guard.default_mode, "overrides": self.guard.overrides,
                "allowed": self.store.get("allowed_signatures", []),
                "rejected": self.store.get("rejected_signatures", [])}
