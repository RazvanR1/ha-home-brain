"""Punctul de intrare: creierul care leagă memoria, învățarea, detectorul, aprobările și agenții."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

from .agents import Agents
from .anomaly import Baselines, Detector
from .approvals import Approvals
from .assistant import Assistant
from .automations import Automations
from .guard import Guard
from .habits import Habit, HabitMiner, automation_index, build_automation, mark_covered
from .ha import HAClient
from .llm import LLM
from .notify import Alert, Notifier
from .recorder import Recorder
from .store import Store
from .web import build_app

OPTIONS = Path(os.environ.get("HB_OPTIONS", "/data/options.json"))
log = logging.getLogger("home_brain")


class Brain:
    def __init__(self, opts: dict, store: Store | None = None) -> None:
        self.opts = opts
        self.tz = ZoneInfo(opts.get("time_zone") or os.environ.get("TZ") or "Europe/Bucharest")
        self.ha = HAClient()
        self.store = store or Store()
        self.notifier = Notifier(self.ha, opts.get("notify_services", []), opts.get("ntfy_url", ""))
        self.guard = Guard(self.ha, self.notifier, opts.get("guard", {}))
        self.automations = Automations(self.ha, self.notifier, opts)
        self.recorder = Recorder(self.ha, self.store, opts.get("learning", {}))
        learn = opts.get("learning", {})
        self.persons = [p for p in learn.get("persons", []) if p]
        self.learning_enabled = bool(learn.get("enabled", True))
        self.auto_propose = bool(learn.get("propose_automations", True))
        self.max_proposals_per_day = int(learn.get("max_proposals_per_day", 2))
        self.miner = HabitMiner(self.tz, self.persons,
                                min_support=int(learn.get("min_support_days", 4)),
                                min_confidence=float(learn.get("min_confidence", 0.6)))
        self.detector = Detector(self.ha, opts.get("unusual", {}), self.tz, self.persons)
        self.detector.muted = set(self.store.get("muted", []))
        self.detector.dwell_factor = self.store.get("dwell_factor", {})
        self.llm = LLM(opts.get("assistant", {}))
        self.approvals = Approvals(self.ha, self.store, self.notifier, opts.get("safety", {}))
        site = opts.get("site_context", "")
        self.agents = Agents(self.ha, self.store, self.llm, self.approvals, self.notifier,
                             {**opts.get("agents", {}), "site_context": site}, self.tz)
        self.assistant = Assistant(self.ha, self.llm, opts.get("assistant", {}), self.approvals,
                                   site, self.brain_summary)
        self.approvals.on("action", self.approvals.handle_action)
        self.approvals.on("undo", self.approvals.handle_undo)
        self.approvals.on("automation", self._decide_habit)
        self.approvals.on("finding", self._decide_finding)
        self.last_learning: float | None = None
        self.findings: dict[str, dict] = {}

    # ---------- învățare ----------
    def names(self) -> dict[str, str]:
        return {e: s.get("attributes", {}).get("friendly_name", e) for e, s in self.ha.states.items()}

    async def learn(self) -> list[Habit]:
        since = time.time() - 30 * 86400
        transitions = self.store.transitions(since)
        habits = self.miner.mine(transitions, self.names())
        try:
            index = automation_index(await self.ha.automation_configs())
        except Exception as err:  # noqa: BLE001
            log.warning("Nu am putut citi automatizările: %s", err)
            index = []
        mark_covered(habits, index)
        for h in habits:
            old = self.store.item("habit", h.id)
            status = old["status"] if old else ("covered" if h.covered_by else "new")
            if h.covered_by and status == "new":
                status = "covered"
            self.store.put_item("habit", h.id, h.to_dict(), status=status)
        self.detector.base = Baselines(self.tz).build(
            self.store.samples(int(since // 3600)), transitions)
        self.last_learning = time.time()
        self.store.log("învățare", f"{len(habits)} obiceiuri găsite "
                                   f"({sum(1 for h in habits if h.covered_by)} deja automatizate) "
                                   f"din {len(transitions)} schimbări.")
        if self.auto_propose:
            await self._propose_strong(habits)
        return habits

    def _strong(self, h: Habit) -> bool:
        return (not h.covered_by and h.confidence >= 0.75 and h.support_days >= 5
                and h.backtest.get("precision", 0) >= 0.8)

    async def _propose_strong(self, habits: list[Habit]) -> None:
        today = datetime.now(self.tz).date().isoformat()
        sent = self.store.get(f"proposals:{today}", 0)
        for h in habits:
            if sent >= self.max_proposals_per_day:
                break
            item = self.store.item("habit", h.id)
            if item and item["status"] == "new" and self._strong(h):
                await self.propose(h.id)
                sent += 1
        self.store.set(f"proposals:{today}", sent)

    async def propose(self, habit_id: str, notify: bool = True) -> str | None:
        item = self.store.item("habit", habit_id)
        if not item:
            return None
        bt = item.get("backtest", {})
        rid = await self.approvals.ask(
            "automation", habit_id, "Obicei nou găsit",
            f"{item['description']} Testat pe istoric: {bt.get('hits')} din {bt.get('fired')} zile corect. "
            f"Fac o automatizare?", {"habit_id": habit_id},
            buttons=[("ok", "Creează"), ("no", "Nu e obicei")], notify=notify)
        self.store.set_status("habit", habit_id, "proposed")
        return rid

    async def create_automation(self, habit_id: str) -> str:
        item = self.store.item("habit", habit_id)
        if not item:
            return "Obiceiul nu mai există."
        fields = {k: item[k] for k in Habit.__dataclass_fields__ if k in item}
        h = Habit(**fields)
        auto = build_automation(h, self.names(), self.persons)
        await self.ha.save_automation(auto["id"], auto)
        self.store.set_status("habit", habit_id, "created")
        self.store.put_item("done", f"auto_{habit_id}", {
            "source": "Învățare", "domain": "automation", "service": "create", "entity_id": "",
            "name": auto["alias"], "automation_id": auto["id"], "reason": h.description}, status="done")
        self.store.log("automatizare", f"Am creat „{auto['alias']}”.")
        return f"Am creat automatizarea „{auto['alias']}”."

    async def reject_habit(self, habit_id: str) -> str:
        self.store.set_status("habit", habit_id, "rejected")
        return "Am notat: nu e obicei, nu-l mai propun."

    async def _decide_habit(self, item: dict, decision: str) -> str:
        hid = item["payload"]["habit_id"]
        if decision in ("ok", "always"):
            return await self.create_automation(hid)
        if decision == "no":
            return await self.reject_habit(hid)
        return "—"

    # ---------- neobișnuit ----------
    async def check_unusual(self) -> None:
        recent = self.store.transitions(time.time() - 1800)
        found = {f.key: f for f in self.detector.check(recent)}
        now = time.time()
        quiet = self.agents._quiet_now()
        for key, f in found.items():
            if key in self.findings or self.store.get(f"snooze:{key}", 0) > now:
                continue
            self.findings[key] = f.to_dict()
            await self.approvals.ask("finding", f"{key}:{int(now // 86400)}", f.title, f.message,
                                     {"key": key, "kind": f.kind, "entity_id": f.entity_id},
                                     buttons=[("seen", "Am văzut"), ("mute", "Nu e problemă")],
                                     notify=f.level == "critical" or not quiet)
        for key in list(self.findings):
            if key not in found:
                self.findings.pop(key)
                for a in self.approvals.pending():
                    if a["kind"] == "finding" and a["payload"].get("key") == key:
                        self.store.set_status("approval", a["id"], "resolved")

    async def _decide_finding(self, item: dict, decision: str) -> str:
        p = item["payload"]
        key, eid, kind = p["key"], p["entity_id"], p["kind"]
        if decision == "mute":
            self.detector.muted.add(f"{eid}:{kind}")
            self.store.set("muted", sorted(self.detector.muted))
            return "Nu mai semnalez asta pentru acest dispozitiv."
        self.store.set(f"snooze:{key}", time.time() + 86400)
        if kind == "dwell":
            self.detector.dwell_factor[eid] = self.detector.dwell_factor.get(eid, 1.0) * 2
            self.store.set("dwell_factor", self.detector.dwell_factor)
            return "Am înțeles; data viitoare aștept de două ori mai mult."
        return "Am înțeles; nu mai insist azi."

    # ---------- rezumat pentru Jarvis ----------
    def brain_summary(self) -> str:
        lines = [f"- {f['message']}" for f in list(self.findings.values())[:5]]
        lines += [f"- {i['agent_name']}: {i['title']}" for i in self.agents.insights(5)]
        lines += [f"- obicei: {h['description']}" for h in self.store.items("habit", ("new", "proposed", "created"), 5)]
        return "\n".join(lines)

    # ---------- bucle ----------
    async def learning_loop(self) -> None:
        await self.ha.connected.wait()
        while self.recorder.import_status in ("neînceput", "în curs"):
            await asyncio.sleep(10)
        while True:
            if self.learning_enabled:
                try:
                    await self.learn()
                except Exception:  # noqa: BLE001
                    log.exception("Învățarea a eșuat")
            await asyncio.sleep(6 * 3600)

    async def unusual_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            if self.detector.base is not None and self.ha.connected.is_set():
                try:
                    await self.check_unusual()
                except Exception:  # noqa: BLE001
                    log.exception("Detectorul a eșuat")

    async def guard_bridge(self) -> None:
        """Alertele paznicului apar și în panou (ca istoric); cele noi le trimite el însuși."""
        await self.guard.run_forever()


async def amain() -> None:
    opts = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    logging.basicConfig(level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    brain = Brain(opts)
    runner = web.AppRunner(build_app(brain))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("HB_PORT", "8099"))).start()
    log.info("Home Brain pornit, panou pe portul 8099")
    await asyncio.gather(
        brain.ha.run_forever(),
        brain.recorder.run_forever(),
        brain.guard_bridge(),
        brain.automations.run_forever(),
        brain.learning_loop(),
        brain.unusual_loop(),
        brain.agents.run_forever(),
    )


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
