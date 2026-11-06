"""Asistentul AI (Jarvis): răspunde despre casă și execută comenzi prin Ollama."""
from __future__ import annotations

import json
import logging

import aiohttp

log = logging.getLogger("home_brain.assistant")

CONTEXT_DOMAINS = ("person", "light", "switch", "climate", "sensor", "binary_sensor",
                   "cover", "fan", "media_player", "lock", "alarm_control_panel", "camera")

SYSTEM_PROMPT = """Ești Jarvis, asistentul casei RusHome. Răspunzi scurt, în română.
Primești starea curentă a casei. Dacă utilizatorul cere o acțiune, o propui în "actions".
Răspunde DOAR cu JSON de forma:
{"reply": "text pentru utilizator",
 "actions": [{"domain": "light", "service": "turn_on", "entity_id": "light.x", "data": {}}]}
Folosește doar entity_id care apar în starea casei. Dacă nu e nevoie de acțiuni, "actions": []."""


class Assistant:
    def __init__(self, ha, cfg: dict) -> None:
        self.ha = ha
        self.url = (cfg.get("ollama_url") or "").rstrip("/")
        self.model = cfg.get("model") or "llama3.1:8b"
        self.allowed = set(cfg.get("allowed_domains") or ["light", "switch", "climate", "cover",
                                                          "fan", "media_player", "scene", "script"])
        self.max_entities = int(cfg.get("max_entities", 300))

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def house_context(self) -> str:
        lines = []
        for eid, st in sorted(self.ha.states.items()):
            if not eid.startswith(CONTEXT_DOMAINS):
                continue
            a = st.get("attributes", {})
            if eid.startswith("sensor.") and "unit_of_measurement" not in a \
                    and a.get("device_class") is None:
                continue
            extra = ""
            if "unit_of_measurement" in a:
                extra = f" {a['unit_of_measurement']}"
            if eid.startswith("climate."):
                extra = f" (setat {a.get('temperature')}, actual {a.get('current_temperature')})"
            lines.append(f"{eid} | {a.get('friendly_name', eid)} | {st.get('state')}{extra}")
            if len(lines) >= self.max_entities:
                break
        return "\n".join(lines)

    async def ask(self, question: str, history: list[dict] | None = None) -> dict:
        if not self.enabled:
            return {"reply": "Asistentul nu e configurat (lipsește ollama_url).", "actions": []}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "Starea casei:\n" + self.house_context()},
            *(history or [])[-6:],
            {"role": "user", "content": question},
        ]
        payload = {"model": self.model, "messages": messages, "stream": False, "format": "json",
                   "think": False, "keep_alive": -1}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
                async with s.post(f"{self.url}/api/chat", json=payload) as r:
                    r.raise_for_status()
                    body = await r.json()
        except Exception as err:  # noqa: BLE001
            log.warning("Ollama indisponibil: %s", err)
            return {"reply": f"Nu pot contacta modelul AI: {err}", "actions": []}
        return await self._handle(body.get("message", {}).get("content", ""))

    async def _handle(self, content: str) -> dict:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {"reply": content.strip() or "(fără răspuns)", "actions": []}
        reply = str(data.get("reply", "")).strip()
        done, refused = [], []
        for act in data.get("actions") or []:
            domain = act.get("domain", "")
            eid = act.get("entity_id", "")
            if domain not in self.allowed or eid not in self.ha.states \
                    or eid.split(".", 1)[0] != domain:
                refused.append(f"{domain}.{act.get('service')} {eid}")
                continue
            try:
                await self.ha.call_service(domain, act.get("service", ""), act.get("data") or None,
                                           target={"entity_id": eid})
                done.append(f"{domain}.{act.get('service')} → {eid}")
            except Exception as err:  # noqa: BLE001
                refused.append(f"{eid}: {err}")
        return {"reply": reply or "Gata.", "actions": done, "refused": refused}
