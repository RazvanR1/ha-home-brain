"""Agenți AI pe domenii: climă, energie, securitate, server. Rulează local prin Ollama,
doar când s-a schimbat ceva, unul câte unul."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from datetime import datetime, tzinfo

log = logging.getLogger("home_brain.agents")

AGENT_PROMPT = """Ești agentul „{name}” din creierul casei RusHome. Răspunzi în română, concret și scurt.
Despre casă: {site}
Sarcina ta: {task}
Primești mai jos datele (entity_id | nume | stare | statistici pe 24 h). Entitățile indisponibile
NU apar aici: de ele se ocupă paznicul, nu le comenta.
Reguli stricte:
- Folosește DOAR cifrele din date. Nu inventa cauze, nu ghici, nu presupune. Dacă nu ești sigur, nu raporta.
- Numele unei automatizări sau al unui senzor NU înseamnă că acel lucru s-a întâmplat.
- Temperaturi de 40-70°C la procesoare/discuri de server sunt normale; nu le raporta decât peste 75°C.
- „critical” DOAR pentru pericol imediat: apă, gaz, fum, casă deschisă când nu e nimeni, server oprit.
- Cel mult 3 observații, doar ce chiar contează. Dacă totul e normal, întoarce lista goală (asta e bine).
{actions}
Răspunde DOAR cu JSON:
{{"insights": [{{"title": "...", "detail": "...", "severity": "info|warning|critical",
  "action": null | {{"domain": "...", "service": "...", "entity_id": "...", "data": {{}}}}}}]}}"""


class Agent:
    key = ""
    name = ""
    task = ""

    def select(self, eid: str, st: dict) -> bool:
        raise NotImplementedError


class ClimateAgent(Agent):
    key, name = "climate", "Climă"
    task = ("confort și încălzire: temperaturi și umiditate pe camere, termostat, AC, ferestre deschise "
            "cu încălzirea pornită, risc de condens/mucegai (umiditate mare + temperatură scăzută), "
            "încălzire care nu încălzește.")

    def select(self, eid, st):
        d, dc = eid.split(".")[0], st.get("attributes", {}).get("device_class")
        return d in ("climate", "weather") or (d == "sensor" and dc in ("temperature", "humidity")) \
            or (d == "binary_sensor" and dc in ("window", "door"))


class EnergyAgent(Agent):
    key, name = "energy", "Energie"
    task = ("consum de energie: ce consumă mult, ce a rămas pornit degeaba, centrala (priza cu măsurare), "
            "consum anormal față de ziua obișnuită, idei concrete de economie.")

    def select(self, eid, st):
        dc = st.get("attributes", {}).get("device_class")
        return eid.startswith("sensor.") and dc in ("power", "energy") and "_container" not in eid


NO_CONTEXT_DOMAINS = ("automation.", "script.", "input_", "scene.", "button.", "event.", "select.",
                      "number.", "text.", "time.", "image.", "notify.", "tts.", "stt.", "conversation.")


class SecurityAgent(Agent):
    key, name = "security", "Securitate"
    task = ("siguranța casei: cine e acasă, uși, mișcare, camere, alarmă, scurgeri de apă/gaz/fum, "
            "activitate când nu e nimeni acasă, dispozitive de securitate care nu funcționează.")

    def select(self, eid, st):
        d, dc = eid.split(".")[0], st.get("attributes", {}).get("device_class")
        return d in ("person", "alarm_control_panel", "camera", "lock") or (
            d == "binary_sensor" and dc in ("door", "window", "motion", "occupancy", "gas", "moisture",
                                            "smoke", "safety", "problem", "tamper"))


class ServerAgent(Agent):
    key, name = "server", "Server & homelab"
    task = ("sănătatea homelab-ului: Proxmox, TrueNAS (pool-uri, discuri), containere Docker oprite, "
            "spațiu pe disc aproape plin (peste 85%), temperaturi de disc peste 55°C, actualizări disponibile.")
    pattern = re.compile(r"proxmox|pve|truenas|portainer|docker|scrutiny|uptime|netdata|"
                         r"_container$|pool|disk|smart|^update\.", re.I)

    def select(self, eid, st):
        if not self.pattern.search(eid):
            return False
        return not any(x in eid for x in ("_image", "_restart_count", "_memory_limit", "_volume_size"))


ALL_AGENTS = [ClimateAgent(), EnergyAgent(), SecurityAgent(), ServerAgent()]
ACTION_DOMAINS = {"light", "switch", "climate", "fan", "humidifier", "media_player", "vacuum"}


class Agents:
    def __init__(self, ha, store, llm, approvals, notifier, cfg: dict, tz: tzinfo) -> None:
        self.ha = ha
        self.store = store
        self.llm = llm
        self.approvals = approvals
        self.notifier = notifier
        self.tz = tz
        self.site = cfg.get("site_context") or "o casă obișnuită"
        self.interval = float(cfg.get("agents_interval_hours", 6)) * 3600
        enabled = set(cfg.get("agents") or [a.key for a in ALL_AGENTS])
        self.agents = [a for a in ALL_AGENTS if a.key in enabled]
        self.quiet = cfg.get("quiet_hours", "23-7")
        self.allow_actions = bool(cfg.get("allow_actions", False))
        self.notify_mode = cfg.get("notify", "digest")  # none | digest | critical
        self.digest_hour = int(cfg.get("digest_hour", 19))
        self.status: dict[str, dict] = {a.key: {"last_run": None, "skipped": "încă nu a rulat"}
                                        for a in self.agents}

    # ---------- date ----------
    def _stats24(self, eids: set[str]) -> dict[str, tuple[float, float, float]]:
        since = int(time.time() // 3600) - 24
        acc: dict[str, list] = {}
        for eid, _h, n, total, _sq, mn, mx in self.store.samples(since):
            if eid in eids:
                a = acc.setdefault(eid, [0, 0.0, math.inf, -math.inf])
                a[0] += n; a[1] += total; a[2] = min(a[2], mn); a[3] = max(a[3], mx)
        return {e: (a[2], a[1] / a[0], a[3]) for e, a in acc.items() if a[0]}

    def context(self, agent: Agent) -> tuple[str, str]:
        rows, down = [], 0
        for e, s in sorted(self.ha.states.items()):
            if e.startswith(NO_CONTEXT_DOMAINS) or not agent.select(e, s):
                continue
            if s.get("state") in ("unavailable", "unknown", None):
                down += 1
                continue
            rows.append((e, s))
        stats = self._stats24({e for e, _ in rows})
        since = time.time() - 86400
        changes: dict[str, int] = {}
        for _ts, eid, _o, _n, _or in self.store.transitions(since, [e for e, _ in rows]):
            changes[eid] = changes.get(eid, 0) + 1
        lines, fp_parts = [], []
        budget = self.llm.budget_chars(0.55)
        size = 0
        for eid, st in rows:
            a = st.get("attributes", {})
            unit = a.get("unit_of_measurement", "")
            line = f"{eid} | {a.get('friendly_name', eid)} | {st.get('state')}{unit}"
            if eid.startswith("climate."):
                line += f" (setat {a.get('temperature')}, actual {a.get('current_temperature')}, " \
                        f"acțiune {a.get('hvac_action')})"
            if eid in stats:
                mn, mean, mx = stats[eid]
                line += f" | 24h: min {mn:.1f} medie {mean:.1f} max {mx:.1f}"
            if changes.get(eid):
                line += f" | {changes[eid]} schimbări/24h"
            if size + len(line) > budget:
                break
            lines.append(line)
            size += len(line) + 1
            try:  # amprenta: valori rotunjite, ca să nu rulăm agentul pentru zgomot
                fp_parts.append(f"{eid}={round(float(st.get('state')))}")
            except (TypeError, ValueError):
                fp_parts.append(f"{eid}={st.get('state')}")
        fp = hashlib.sha1("|".join(fp_parts).encode()).hexdigest()
        if down and lines:
            lines.append(f"(+ {down} entități indisponibile, urmărite de paznic)")
        return "\n".join(lines), fp

    def _quiet_now(self) -> bool:
        try:
            a, b = (int(x) for x in self.quiet.split("-"))
        except ValueError:
            return False
        h = datetime.now(self.tz).hour
        return (a <= h or h < b) if a > b else (a <= h < b)

    # ---------- rulare ----------
    async def run_agent(self, agent: Agent, force: bool = False) -> list[dict]:
        ctx, fp = self.context(agent)
        st = self.status[agent.key]
        if not ctx:
            st.update(skipped="nu are date", last_run=time.time())
            return []
        if not force and self.store.get(f"agent_fp:{agent.key}") == fp:
            st.update(skipped="nimic nou de la ultima analiză", last_run=time.time())
            return []
        anomalies = [i for i in self.store.items("approval", "pending") if i["kind"] == "finding"]
        extra = ""
        if anomalies:
            extra = "\nObservații locale deschise:\n" + "\n".join(f"- {i['message']}" for i in anomalies[:5])
        actions_rule = ("Poți propune o acțiune doar pe entity_id din date; va fi aprobată de om."
                        if self.allow_actions else 'Nu propune acțiuni: pune mereu "action": null.')
        messages = [{"role": "system", "content": AGENT_PROMPT.format(name=agent.name, site=self.site,
                                                                     task=agent.task, actions=actions_rule)},
                    {"role": "user", "content": f"Ora: {datetime.now(self.tz):%A %H:%M}\nDate:\n{ctx}{extra}"}]
        try:
            data, raw = await self.llm.chat_json(messages)
        except Exception as err:  # noqa: BLE001
            st.update(skipped=f"eroare AI: {err}", last_run=time.time())
            return []
        self.store.set(f"agent_fp:{agent.key}", fp)
        insights = (data or {}).get("insights") or []
        out = []
        for ins in insights[:3]:
            if not isinstance(ins, dict) or not ins.get("title"):
                continue
            iid = hashlib.sha1(f"{agent.key}|{ins['title']}|{datetime.now(self.tz).date()}".encode()).hexdigest()[:12]
            sev = ins.get("severity") if ins.get("severity") in ("info", "warning", "critical") else "info"
            item = {"agent": agent.key, "agent_name": agent.name, "title": str(ins["title"])[:120],
                    "detail": str(ins.get("detail", ""))[:600], "severity": sev}
            is_new = self.store.item("insight", iid) is None
            self.store.put_item("insight", iid, item, status="new")
            out.append(item)
            if is_new and self.notify_mode == "critical" and sev == "critical":
                await self.notifier.send(f"{agent.name}: {item['title']}", item["detail"], sev)
            act = ins.get("action")
            if self.allow_actions and isinstance(act, dict) and act.get("entity_id") in self.ha.states \
                    and str(act.get("domain")) in ACTION_DOMAINS and act.get("service") \
                    and act["entity_id"].split(".")[0] == act.get("domain"):
                await self.approvals.request_action(agent.name, str(act.get("domain", "")),
                                                    str(act.get("service", "")), act["entity_id"],
                                                    act.get("data") if isinstance(act.get("data"), dict) else {},
                                                    item["title"])
        st.update(skipped=None, last_run=time.time(), insights=len(out), seconds=round(self.llm.last_seconds, 1))
        self.store.log("agent", f"{agent.name}: {len(out)} observații" + ("" if data else f" (răspuns brut: {raw[:120]})"))
        return out

    async def run_forever(self) -> None:
        if not self.llm.enabled:
            return
        await self.ha.connected.wait()
        await asyncio.sleep(300)  # lăsăm importul istoricului și statisticile să se facă întâi
        while True:
            for agent in self.agents:
                try:
                    await self.run_agent(agent)
                except Exception:  # noqa: BLE001
                    log.exception("Agentul %s a eșuat", agent.name)
                await asyncio.sleep(5)
            await asyncio.sleep(self.interval)

    async def digest_loop(self) -> None:
        """Un singur rezumat pe zi cu observațiile noi ale agenților, în loc de notificări răzlețe."""
        if self.notify_mode != "digest":
            return
        while True:
            await asyncio.sleep(600)
            now = datetime.now(self.tz)
            day = now.date().isoformat()
            if now.hour < self.digest_hour or self.store.get("digest_sent") == day:
                continue
            start = datetime(now.year, now.month, now.day, tzinfo=self.tz).timestamp()
            fresh = [i for i in self.insights(100) if i["ts"] >= start and i["severity"] != "info"]
            self.store.set("digest_sent", day)
            if not fresh:
                continue
            lines = [f"• {i['agent_name']}: {i['title']}" for i in fresh[:5]]
            more = f"\n…și încă {len(fresh) - 5}" if len(fresh) > 5 else ""
            await self.notifier.send("Home Brain – rezumatul zilei", "\n".join(lines) + more, "info")

    def insights(self, limit: int = 40) -> list[dict]:
        return self.store.items("insight", limit=limit)
