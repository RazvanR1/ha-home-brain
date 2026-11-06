"""Panoul web (ingress) din sidebar-ul Home Assistant."""
from __future__ import annotations

import logging
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

    async def index(_req: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def status(_req: web.Request) -> web.Response:
        ha = brain.ha
        return web.json_response({
            "connected": ha.connected.is_set(),
            "entities": len(ha.states),
            "active_alerts": [asdict(a) for a in brain.notifier.active.values()],
            "history": [asdict(a) for a in list(brain.notifier.history)[:50]],
            "automations": brain.automations.status(),
            "assistant": {"enabled": brain.assistant.enabled, "model": brain.assistant.model},
        })

    async def chat(req: web.Request) -> web.Response:
        body = await req.json()
        question = str(body.get("message", "")).strip()
        if not question:
            return web.json_response({"reply": "Scrie o întrebare.", "actions": []})
        result = await brain.assistant.ask(question, chat_history)
        chat_history.extend([{"role": "user", "content": question},
                             {"role": "assistant", "content": result.get("reply", "")}])
        del chat_history[:-12]
        return web.json_response(result)

    async def test_notify(_req: web.Request) -> web.Response:
        await brain.notifier.send("Home Brain", "Notificare de test din Home Brain.", "info")
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/test-notify", test_notify)
    return app
