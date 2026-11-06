"""Client WebSocket pentru Home Assistant (prin Supervisor)."""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("home_brain.ha")

StateListener = Callable[[str, dict | None, dict | None], Awaitable[None]]


class HAClient:
    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = url or os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.states: dict[str, dict] = {}
        self.connected = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[StateListener] = []
        self._tasks: set[asyncio.Task] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None

    def on_state_change(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def state(self, entity_id: str) -> str | None:
        s = self.states.get(entity_id)
        return s["state"] if s else None

    def attr(self, entity_id: str, name: str, default: Any = None) -> Any:
        s = self.states.get(entity_id)
        return s.get("attributes", {}).get(name, default) if s else default

    async def call_service(self, domain: str, service: str, data: dict | None = None,
                           target: dict | None = None) -> Any:
        msg: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service}
        if data:
            msg["service_data"] = data
        if target:
            msg["target"] = target
        log.info("Serviciu: %s.%s %s %s", domain, service, target or "", data or "")
        return await self._request(msg)

    async def run_forever(self) -> None:
        delay = 2
        while True:
            try:
                await self._run_once()
                delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                log.warning("Conexiune HA pierdută: %s (reîncerc în %ss)", err, delay)
            self.connected.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("HA deconectat"))
            self._pending.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _run_once(self) -> None:
        self._session = self._session or aiohttp.ClientSession()
        async with self._session.ws_connect(self.url, heartbeat=30, max_msg_size=0) as ws:
            self._ws = ws
            hello = await ws.receive_json()
            if hello.get("type") == "auth_required":
                await ws.send_json({"type": "auth", "access_token": self.token})
                res = await ws.receive_json()
                if res.get("type") != "auth_ok":
                    raise PermissionError(f"Autentificare HA eșuată: {res}")
            reader = asyncio.create_task(self._reader(ws))
            try:
                states = await self._request({"type": "get_states"})
                self.states = {s["entity_id"]: s for s in states}
                await self._request({"type": "subscribe_events", "event_type": "state_changed"})
                log.info("Conectat la Home Assistant (%d entități)", len(self.states))
                self.connected.set()
                await reader
            finally:
                reader.cancel()
                self._ws = None

    async def _request(self, msg: dict) -> Any:
        if self._ws is None:
            raise ConnectionError("HA nu e conectat")
        msg_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({"id": msg_id, **msg})
        return await asyncio.wait_for(fut, timeout=30)

    async def _reader(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            msg = raw.json()
            if msg.get("type") == "result":
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if msg.get("success"):
                        fut.set_result(msg.get("result"))
                    else:
                        fut.set_exception(RuntimeError(str(msg.get("error"))))
            elif msg.get("type") == "event":
                data = msg["event"]["data"]
                await self._handle_state(data["entity_id"], data.get("old_state"), data.get("new_state"))

    async def _handle_state(self, entity_id: str, old: dict | None, new: dict | None) -> None:
        if new is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = new
        # Listenerii rulează ca task-uri separate: dacă ar fi așteptați aici, un listener care
        # cheamă call_service ar bloca cititorul care trebuie să primească răspunsul (deadlock).
        for listener in self._listeners:
            task = asyncio.create_task(self._safe(listener, entity_id, old, new))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe(self, listener: StateListener, entity_id: str, old, new) -> None:
        try:
            await listener(entity_id, old, new)
        except Exception:  # noqa: BLE001
            log.exception("Eroare în listener pentru %s", entity_id)
