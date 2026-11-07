"""Jarvis: răspunde despre casă și execută comenzi, prin același model local ca agenții."""
from __future__ import annotations

import fnmatch
import logging

log = logging.getLogger("home_brain.assistant")

CONTROL_DOMAINS = ("light", "switch", "climate", "cover", "fan", "media_player", "lock",
                   "vacuum", "humidifier", "alarm_control_panel", "scene", "script")
SENSOR_CLASSES = {"temperature", "humidity", "power", "energy", "battery", "co2", "pm25",
                  "illuminance", "gas", "moisture"}
BINARY_CLASSES = {"door", "window", "motion", "occupancy", "presence", "gas", "moisture",
                  "smoke", "opening", "lock", "problem"}
NOISE = ["switch.*_container", "sensor.*_volume_size", "*_image*", "*_restart_count",
         "*_memory_*", "*_cpu_*", "*_out_of_memory"]

SYSTEM_PROMPT = """Ești Jarvis, asistentul casei RusHome. Răspunzi scurt, în română.
Despre casă: {site}
Primești starea curentă a casei (entity_id | nume | stare) și, dacă există, ce a observat creierul casei.
Răspunde din ele, direct. Dacă utilizatorul cere o acțiune, pune-o în "actions".
Răspunde DOAR cu JSON de forma:
{{"reply": "text pentru utilizator",
 "actions": [{{"domain": "light", "service": "turn_on", "entity_id": "light.x", "data": {{}}}}]}}
Folosește doar entity_id din starea casei. Fără acțiuni -> "actions": []."""


class Assistant:
    def __init__(self, ha, llm, cfg: dict, approvals=None, site: str = "", extra_context=None) -> None:
        self.ha = ha
        self.llm = llm
        self.approvals = approvals
        self.site = site or "o casă obișnuită"
        self.extra_context = extra_context  # funcție -> text (obiceiuri, alerte, observații)
        self.allowed = set(cfg.get("allowed_domains") or ["light", "switch", "climate", "cover",
                                                          "fan", "media_player", "scene", "script"])
        self.max_entities = int(cfg.get("max_entities", 150))

    @property
    def enabled(self) -> bool:
        return self.llm.enabled

    @property
    def model(self) -> str:
        return self.llm.model

    def _relevant(self, eid: str, st: dict) -> bool:
        if any(fnmatch.fnmatch(eid, p) for p in NOISE):
            return False
        if st.get("state") in ("unavailable", "unknown"):
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
        budget = self.llm.budget_chars(0.5)
        order = {"person": 0, "climate": 1, "light": 2, "binary_sensor": 3, "sensor": 4}
        items = sorted(((e, s) for e, s in self.ha.states.items() if self._relevant(e, s)),
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
        ctx = "Starea casei:\n" + self.house_context()
        if self.extra_context:
            more = self.extra_context()
            if more:
                ctx += "\n\nCe a observat creierul casei:\n" + more[:1500]
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(site=self.site) + "\n\n" + ctx},
                    *(history or [])[-6:], {"role": "user", "content": question}]
        try:
            data, raw = await self.llm.chat_json(messages, timeout=120)
        except Exception as err:  # noqa: BLE001
            log.warning("Ollama indisponibil: %s", err)
            return {"reply": f"Nu pot contacta modelul AI: {err}", "actions": []}
        if data is None:
            return {"reply": raw.strip() or "(fără răspuns de la model)", "actions": []}
        return await self._handle(data, raw)

    async def _handle(self, data: dict, raw: str = "") -> dict:
        reply = str(data.get("reply") or data.get("answer") or data.get("response") or "").strip()
        done, refused = [], []
        for act in data.get("actions") or []:
            if not isinstance(act, dict):
                continue
            domain, service, eid = act.get("domain", ""), act.get("service", ""), act.get("entity_id", "")
            if domain not in self.allowed or eid not in self.ha.states or eid.split(".", 1)[0] != domain:
                refused.append(f"{domain}.{service} {eid}")
                continue
            data_ = act.get("data") if isinstance(act.get("data"), dict) else {}
            if self.approvals is not None:
                res = await self.approvals.request_action("Jarvis", domain, service, eid, data_,
                                                          "Cerut în chat", user_initiated=True)
                if res["status"] == "done":
                    done.append(f"{domain}.{service} → {eid}")
                elif res["status"] == "pending":
                    refused.append(f"{eid}: am trimis cerere de aprobare pe telefon")
                else:
                    refused.append(f"{eid}: {res.get('reason', res['status'])}")
                continue
            try:
                await self.ha.call_service(domain, service, data_ or None, target={"entity_id": eid})
                done.append(f"{domain}.{service} → {eid}")
            except Exception as err:  # noqa: BLE001
                refused.append(f"{eid}: {err}")
        if not reply:
            reply = "Gata." if done else f"(răspuns neașteptat de la model: {raw[:300]})"
        return {"reply": reply, "actions": done, "refused": refused}
