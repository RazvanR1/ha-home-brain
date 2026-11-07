"""Panoul web (ingress) din sidebar-ul Home Assistant."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

log = logging.getLogger("home_brain.web")
STATIC = Path(__file__).parent / "static"
INGRESS_IPS = {"172.30.32.2", "127.0.0.1", "::1"}


def build_app(brain, allowed_ips: set[str] | None = INGRESS_IPS) -> web.Application:
    @web.middleware
    async def only_ingress(req: web.Request, handler):
        # Panoul poate comanda dispozitive, deci acceptăm doar cereri venite prin ingress-ul HA.
        if allowed_ips is not None and req.remote not in allowed_ips:
            raise web.HTTPForbidden(text="Acces doar prin Home Assistant")
        return await handler(req)

    app = web.Application(middlewares=[only_ingress])
    chat_history: list[dict] = []
    J = web.json_response

    async def index(_req):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def status(_req):
        ha = brain.ha
        store = getattr(brain, "store", None)
        span = store.data_span() if store else (None, None)
        return J({
            "connected": ha.connected.is_set(),
            "entities": len(ha.states),
            "active_alerts": [asdict(a) for a in brain.notifier.active.values()],
            "history": [asdict(a) for a in list(brain.notifier.history)[:50]],
            "automations": brain.automations.status(),
            "assistant": {"enabled": brain.assistant.enabled, "model": brain.assistant.model},
            "memory": {
                "transitions": store.count_transitions() if store else 0,
                "days": round((span[1] - span[0]) / 86400, 1) if span and span[0] else 0,
                "import": getattr(getattr(brain, "recorder", None), "import_status", "—"),
                "last_learning": getattr(brain, "last_learning", None),
            },
            "pending": len(brain.approvals.pending()) if hasattr(brain, "approvals") else 0,
            "findings": list(getattr(brain, "findings", {}).values()),
            "agents": getattr(getattr(brain, "agents", None), "status", {}),
            "safety": brain.approvals.summary() if hasattr(brain, "approvals") else {},
            "llm": {"calls": brain.llm.calls, "last_seconds": round(brain.llm.last_seconds, 1)}
            if hasattr(brain, "llm") else {},
        })

    async def chat(req):
        body = await req.json()
        question = str(body.get("message", "")).strip()
        if not question:
            return J({"reply": "Scrie o întrebare.", "actions": []})
        result = await brain.assistant.ask(question, chat_history)
        chat_history.extend([{"role": "user", "content": question},
                             {"role": "assistant", "content": result.get("reply", "")}])
        del chat_history[:-12]
        return J(result)

    async def test_notify(_req):
        await brain.notifier.send("Home Brain", "Notificare de test din Home Brain.", "info")
        return J({"ok": True})

    # ---------- obiceiuri ----------
    async def habits(_req):
        return J(brain.store.items("habit", limit=200))

    async def habit_action(req):
        hid, act = req.match_info["id"], req.match_info["action"]
        if act == "propose":
            await brain.propose(hid, notify=True)
            return J({"message": "Am trimis propunerea pe telefon și în Aprobări."})
        if act == "create":
            return J({"message": await brain.create_automation(hid)})
        if act == "reject":
            return J({"message": await brain.reject_habit(hid)})
        if act == "restore":
            brain.store.set_status("habit", hid, "new")
            return J({"message": "Repus în listă."})
        raise web.HTTPBadRequest()

    async def relearn(_req):
        hs = await brain.learn()
        return J({"message": f"Am reînvățat: {len(hs)} obiceiuri."})

    # ---------- aprobări, acțiuni ----------
    async def approvals(_req):
        return J(brain.approvals.pending())

    async def decide(req):
        msg = await brain.approvals.decide(req.match_info["id"], req.match_info["decision"])
        return J({"message": msg})

    async def done(_req):
        return J(brain.approvals.recent_done())

    async def undo(req):
        return J({"message": await brain.approvals.undo(req.match_info["id"])})

    # ---------- agenți, jurnal ----------
    async def insights(_req):
        return J(brain.agents.insights())

    async def run_agent(req):
        key = req.match_info["key"]
        agent = next((a for a in brain.agents.agents if a.key == key), None)
        if not agent:
            raise web.HTTPNotFound()
        out = await brain.agents.run_agent(agent, force=True)
        return J({"message": f"{agent.name}: {len(out)} observații.", "insights": out})

    async def activity(_req):
        return J(brain.store.activity(150))

    async def health(_req):
        return J({"ok": brain.ha.connected.is_set(), "ts": time.time()})

    r = app.router
    r.add_get("/", index)
    r.add_get("/api/health", health)
    r.add_get("/api/status", status)
    r.add_post("/api/chat", chat)
    r.add_post("/api/test-notify", test_notify)
    if hasattr(brain, "store"):
        r.add_get("/api/habits", habits)
        r.add_post("/api/habits/{id}/{action}", habit_action)
        r.add_post("/api/learn", relearn)
        r.add_get("/api/approvals", approvals)
        r.add_post("/api/approvals/{id}/{decision}", decide)
        r.add_get("/api/done", done)
        r.add_post("/api/undo/{id}", undo)
        r.add_get("/api/insights", insights)
        r.add_post("/api/agents/{key}/run", run_agent)
        r.add_get("/api/activity", activity)
    return app
