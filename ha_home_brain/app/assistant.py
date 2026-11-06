"""Asistentul AI (Jarvis): răspunde despre casă și execută comenzi prin Ollama."""
from __future__ import annotations

import fnmatch
import json
import logging

import aiohttp

log = logging.getLogger("home_brain.assistant")

CONTROL_DOMAINS = ("light", "switch", "climate", "cover", "fan", "media_player", "lock",
                   "vacuum", "humidifier", "alarm_control_panel", "scene", "script")
SENSOR_CLASSES = {"temperature", "humidity", "power", "energy", "battery", "co2", "pm25",
                  "illuminance", "gas", "moisture"}
BINARY_CLASSES = {"door", "window", "motion", "occupancy", "presence", "gas", "moisture",
                  "smoke", "opening", "lock", "problem"}
# Entități tehnice care doar umplu contextul (containere Docker, volume etc.).
NOISE = ["switch.*_container", "sensor.*_volume_size", "*_image*", "*_restart_count",
         "*_memory_*", "*_cpu_*", "*_out_of_memory"]

SYSTEM_PROMPT = """Ești Jarvis, asistentul casei RusHome. Răspunzi scurt, în română.
Primești starea curentă a casei (entity_id | nume | stare). Răspunde din ea, direct.
Dacă utilizatorul cere o acțiune, pune-o în "actions".
Răspunde DOAR cu JSON de forma:
{"reply": "text pentru utilizator",
 "actions": [{"domain": "light", "service": "turn_on", "entity_id": "light.x", "data": {}}]}
Folosește doar entity_id din starea casei. Fără acțiuni -> "actions": []."""


class Assistant:
    def __init__(self, ha, cfg: dict) -> None:
        self.ha = ha
        self.url = (cfg.get("ollama_url") or "").rstrip("/")
        self.model = cfg.get("model") or "llama3.1:8b"
        self.allowed = set(cfg.get("allowed_domains") or ["light", "switch", "climate", "cover",
                                                          "fan", "media_player", "scene", "script"])
        self.max_entities = int(cfg.get("max_entities", 150))
        # Același num_ctx ca integrarea Ollama din HA: altfel Ollama reîncarcă modelul.
        self.num_ctx = int(cfg.get("num_ctx", 8192))

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _relevant(self, eid: str, st: dict) -> bool:
        if any(fnmatch.fnmatch(eid, p) for p in NOISE):
            return False
        state = st.get("state")
        if state in ("unavailable", "unknown"):
            return False
        domain = eid.split(".", 1)[0]
        dc = st.get("attributes", {}).get("device_class")
        if domain == "person" or domain in CONTROL_DOMAINS:
            return True
        if domain == "sensor":
            return dc in SENSOR_CLASSES
        if domain == "binary_sensor":
            return dc in BINARY_CLASSES
        return False

    def house_context(self) -> str:
        budget = int(self.num_ctx * 0.6 * 3.5)  # ~caractere disponibile, lăsăm loc de răspuns
        order = {"person": 0, "climate": 1, "light": 2, "binary_sensor": 3, "sensor": 4}
        items = sorted(((eid, st) for eid, st in self.ha.states.items() if self._relevant(eid, st)),
                       key=lambda x: (order.get(x[0].split(".")[0], 5), x[0]))
        lines, size = [], 0
        for eid, st in items[: self.max_entities]:
            a = st.get("attributes", {})
            extra = f" {a['unit_of_measurement']}" if "unit_of_measurement" in a else ""
            if eid.startswith("climate."):
                extra = f" (setat {a.get('temperature')}, actual {a.get('current_temperature')})"
            line = f"{eid} | {a.get('friendly_name', eid)} | {st.get('state')}{extra}"
            if size + len(line) > budget:
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)

    async def ask(self, question: str, history: list[dict] | None = None) -> dict:
        if not self.enabled:
            return {"reply": "Asistentul nu e configurat (lipsește ollama_url).", "actions": []}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nStarea casei:\n" + self.house_context()},
            *(history or [])[-6:],
            {"role": "user", "content": question},
        ]
        payload = {"model": self.model, "messages": messages, "stream": False, "format": "json",
                   "think": False, "keep_alive": -1, "options": {"num_ctx": self.num_ctx}}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
                async with s.post(f"{self.url}/api/chat", json=payload) as r:
                    r.raise_for_status()
                    body = await r.json()
        except Exception as err:  # noqa: BLE001
            log.warning("Ollama indisponibil: %s", err)
            return {"reply": f"Nu pot contacta modelul AI: {err}", "actions": []}
        content = body.get("message", {}).get("content", "")
        log.debug("Ollama a răspuns: %s", content[:500])
        return await self._handle(content)

    async def _handle(self, content: str) -> dict:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {"reply": content.strip() or "(fără răspuns de la model)", "actions": []}
        if not isinstance(data, dict):
            return {"reply": str(data), "actions": []}
        reply = data.get("reply") or data.get("answer") or data.get("response") or ""
        reply = str(reply).strip()
        done, refused = [], []
        for act in data.get("actions") or []:
            if not isinstance(act, dict):
                continue
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
        if not reply:
            reply = "Gata." if done else f"(răspuns neașteptat de la model: {content[:300]})"
        return {"reply": reply, "actions": done, "refused": refused}
