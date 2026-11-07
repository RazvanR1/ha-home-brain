"""Client pentru Home Assistant (WebSocket + REST prin Supervisor)."""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("home_brain.ha")

# listener(entity_id, old_state, new_state, origin)
StateListener = Callable[[str, dict | None, dict | None, str], Awaitable[None]]
EventListener = Callable[[dict], Awaitable[None]]

# De unde vine o schimbare de stare.
ORIGIN_BRAIN = "brain"            # Home Brain însuși
ORIGIN_AUTOMATION = "automation"  # o automatizare / un script din HA
ORIGIN_USER = "user"              # un om din aplicație / dashboard
ORIGIN_DEVICE = "device"          # fizic (întrerupător, telecomandă) sau integrare

EXTRA_EVENTS = ("automation_triggered", "script_started", "mobile_app_notification_action")


class _TTLSet:
    """Set mic de context-id-uri recente (automatizări, acțiunile noastre)."""

    def __init__(self, ttl: float = 600, max_items: int = 5000) -> None:
        self.ttl, self.max = ttl, max_items
        self._d: OrderedDict[str, float] = OrderedDict()

    def add(self, key: str | None) -> None:
        if not key:
            return
        self._d[key] = time.monotonic()
        self._d.move_to_end(key)
        while len(self._d) > self.max:
            self._d.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        ts = self._d.get(key)  # type: ignore[arg-type]
        return ts is not None and time.monotonic() - ts < self.ttl


class HAClient:
    def __init__(self, url: str | None = None, token: str | None = None,
                 api_url: str | None = None) -> None:
        self.url = url or os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
        self.api_url = (api_url or os.environ.get("HA_API_URL", "http://supervisor/core/api")).rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.states: dict[str, dict] = {}
        self.connected = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[StateListener] = []
        self._event_listeners: dict[str, list[EventListener]] = {}
        self._sub_types: dict[int, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self.automation_contexts = _TTLSet()
        self.brain_contexts = _TTLSet()

    # ---------- abonări ----------
    def on_state_change(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def on_event(self, event_type: str, listener: EventListener) -> None:
        self._event_listeners.setdefault(event_type, []).append(listener)

    # ---------- citire ----------
    def state(self, entity_id: str) -> str | None:
        s = self.states.get(entity_id)
        return s["state"] if s else None

    def attr(self, entity_id: str, name: str, default: Any = None) -> Any:
        s = self.states.get(entity_id)
        return s.get("attributes", {}).get(name, default) if s else default

    def name(self, entity_id: str) -> str:
        return self.attr(entity_id, "friendly_name", entity_id)

    def classify(self, context: dict | None) -> str:
        context = context or {}
        cid = context.get("id")
        if cid in self.brain_contexts or context.get("parent_id") in self.brain_contexts:
            return ORIGIN_BRAIN
        if cid in self.automation_contexts or context.get("parent_id"):
            return ORIGIN_AUTOMATION
        if context.get("user_id"):
            return ORIGIN_USER
        return ORIGIN_DEVICE

    # ---------- acțiuni ----------
    async def call_service(self, domain: str, service: str, data: dict | None = None,
                           target: dict | None = None) -> Any:
        msg: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service}
        if data:
            msg["service_data"] = data
        if target:
            msg["target"] = target
        log.info("Serviciu: %s.%s %s %s", domain, service, target or "", data or "")
        res = await self._request(msg)
        if isinstance(res, dict):
            self.brain_contexts.add((res.get("context") or {}).get("id"))
        return res

    async def ws(self, msg: dict, timeout: float = 30) -> Any:
        return await self._request(msg, timeout)

    async def rest(self, method: str, path: str, json_body: Any = None) -> Any:
        self._session = self._session or aiohttp.ClientSession()
        async with self._session.request(
                method, f"{self.api_url}{path}", json=json_body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=30)) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HA {method} {path}: {r.status} {text[:200]}")
            try:
                return await r.json(content_type=None)
            except Exception:  # noqa: BLE001
                return text

    async def logbook(self, start_iso: str, end_iso: str, entity_ids: list[str]) -> list[dict]:
        return await self._request({"type": "logbook/get_events", "start_time": start_iso,
                                    "end_time": end_iso, "entity_ids": entity_ids}, timeout=120) or []

    async def automation_configs(self) -> dict[str, dict]:
        """entity_id -> configurația automatizării (doar cele făcute din UI au id)."""
        out: dict[str, dict] = {}
        for eid in [e for e in self.states if e.startswith("automation.")]:
            try:
                res = await self._request({"type": "automation/config", "entity_id": eid})
                out[eid] = (res or {}).get("config") or {}
            except Exception:  # noqa: BLE001
                out[eid] = {}
        return out

    async def save_automation(self, auto_id: str, config: dict) -> None:
        await self.rest("POST", f"/config/automation/config/{auto_id}", config)

    async def delete_automation(self, auto_id: str) -> None:
        await self.rest("DELETE", f"/config/automation/config/{auto_id}")

    # ---------- conexiune ----------
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

    async def close(self) -> None:
        if self._session:
            await self._session.close()

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
                self._sub_types.clear()
                for et in ("state_changed", *EXTRA_EVENTS):
                    await self._subscribe(et)
                log.info("Conectat la Home Assistant (%d entități)", len(self.states))
                self.connected.set()
                await reader
            finally:
                reader.cancel()
                self._ws = None

    async def _subscribe(self, event_type: str) -> None:
        msg_id = next(self._ids)
        self._sub_types[msg_id] = event_type
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({"id": msg_id, "type": "subscribe_events", "event_type": event_type})
        await asyncio.wait_for(fut, timeout=30)

    async def _request(self, msg: dict, timeout: float = 30) -> Any:
        if self._ws is None:
            raise ConnectionError("HA nu e conectat")
        msg_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({"id": msg_id, **msg})
        return await asyncio.wait_for(fut, timeout=timeout)

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
                event = msg["event"]
                et = event.get("event_type") or self._sub_types.get(msg.get("id"), "")
                if et == "state_changed":
                    data = event["data"]
                    await self._handle_state(data["entity_id"], data.get("old_state"),
                                             data.get("new_state"))
                else:
                    if et in ("automation_triggered", "script_started"):
                        self.automation_contexts.add((event.get("context") or {}).get("id"))
                    for listener in self._event_listeners.get(et, []):
                        self._spawn(listener(event))

    async def _handle_state(self, entity_id: str, old: dict | None, new: dict | None) -> None:
        if new is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = new
        origin = self.classify((new or {}).get("context"))
        # Listenerii rulează ca task-uri separate: dacă ar fi așteptați aici, un listener care
        # cheamă call_service ar bloca cititorul care trebuie să primească răspunsul (deadlock).
        for listener in self._listeners:
            self._spawn(listener(entity_id, old, new, origin))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(self._safe(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("Eroare într-un listener")
