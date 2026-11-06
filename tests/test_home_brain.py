"""Teste end-to-end cu un Home Assistant fals (server WebSocket) și un Ollama fals."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_home_brain"))

from app.main import Brain  # noqa: E402
from app.web import build_app  # noqa: E402

TOKEN = "test-token"


def st(eid, state, **attrs):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": old}


class FakeHA:
    def __init__(self, states):
        self.states = {s["entity_id"]: s for s in states}
        self.calls: list[dict] = []
        self.ws: web.WebSocketResponse | None = None
        self.sub_id = None

    async def handler(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws = ws
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"})
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for raw in ws:
            msg = json.loads(raw.data)
            if msg["type"] == "get_states":
                res = list(self.states.values())
            elif msg["type"] == "subscribe_events":
                self.sub_id = msg["id"]
                res = None
            elif msg["type"] == "call_service":
                self.calls.append(msg)
                res = {}
                self._apply(msg)
            else:
                res = None
            await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": res})
        return ws

    def _apply(self, msg):
        """Simulează efectul unor servicii, ca HA să trimită state_changed înapoi."""
        targets = (msg.get("target") or {}).get("entity_id") or []
        targets = [targets] if isinstance(targets, str) else targets
        for eid in targets:
            if eid not in self.states:
                continue
            new = dict(self.states[eid])
            new["attributes"] = dict(new["attributes"])
            svc = msg["service"]
            if svc in ("turn_on", "turn_off"):
                new["state"] = "on" if svc == "turn_on" else "off"
            elif svc == "set_hvac_mode":
                new["state"] = msg["service_data"]["hvac_mode"]
            elif svc == "set_temperature":
                new["attributes"]["temperature"] = msg["service_data"]["temperature"]
            asyncio.get_running_loop().create_task(self.push(eid, new))

    async def push(self, eid, new_state):
        old = self.states.get(eid)
        self.states[eid] = new_state
        await self.ws.send_json({"id": self.sub_id, "type": "event", "event": {
            "event_type": "state_changed",
            "data": {"entity_id": eid, "old_state": old, "new_state": new_state}}})


OPTS = {
    "notify_services": ["notify.mobile_app_razvan"],
    "guard": {"unavailable_minutes": 30, "battery_threshold": 20, "ignore": ["sensor.ignored_*"]},
    "presence_camera": {"persons": ["person.razvan", "person.hajni"],
                        "camera_switches": ["switch.frigate_detect"]},
    "heating_ac_lock": {"ac_climates": ["climate.ac_living"], "boiler_climate": "climate.centrala"},
    "follow_thermostat": {"leader_climate": "climate.centrala",
                          "follower_climates": ["climate.robinet_bucatarie"]},
    "assistant": {"model": "test"},
}

STATES = [
    st("person.razvan", "home"), st("person.hajni", "not_home"),
    st("switch.frigate_detect", "on", friendly_name="Frigate"),
    st("climate.ac_living", "off"), st("climate.centrala", "heat", temperature=21.5),
    st("climate.robinet_bucatarie", "heat", temperature=18),
    st("sensor.usa_baterie", "12", device_class="battery", friendly_name="Ușă"),
    st("sensor.temp_dormitor", "unavailable", friendly_name="Temp dormitor"),
    st("sensor.ignored_x", "unavailable"),
    st("light.living", "off", friendly_name="Lumină living"),
]


async def wait_for(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condiția nu s-a îndeplinit")
        await asyncio.sleep(0.02)


@pytest.fixture
async def setup(monkeypatch):
    fake = FakeHA([json.loads(json.dumps(s)) for s in STATES])
    ha_app = web.Application()
    ha_app.router.add_get("/ws", fake.handler)

    async def ollama(req):
        body = await req.json()
        q = body["messages"][-1]["content"]
        assert "light.living" in body["messages"][1]["content"]
        if "aprinde" in q:
            content = {"reply": "Aprind lumina.", "actions": [
                {"domain": "light", "service": "turn_on", "entity_id": "light.living"},
                {"domain": "lock", "service": "unlock", "entity_id": "lock.usa"}]}
        else:
            content = {"reply": "Răzvan e acasă.", "actions": []}
        return web.json_response({"message": {"role": "assistant", "content": json.dumps(content)}})

    ha_app.router.add_post("/api/chat", ollama)
    server = TestServer(ha_app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setenv("HA_WS_URL", base.replace("http", "ws") + "/ws")
    monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
    opts = json.loads(json.dumps(OPTS))
    opts["assistant"]["ollama_url"] = base
    brain = Brain(opts)
    task = asyncio.create_task(brain.ha.run_forever())
    await asyncio.wait_for(brain.ha.connected.wait(), 3)
    yield fake, brain
    task.cancel()
    await server.close()
    if brain.ha._session:
        await brain.ha._session.close()


async def test_guard_alerts_and_resolves(setup):
    fake, brain = setup
    await brain.guard.run_once()
    keys = set(brain.notifier.active)
    assert keys == {"battery:sensor.usa_baterie", "unavailable:sensor.temp_dormitor"}
    sent = [c for c in fake.calls if c["domain"] == "notify"]
    assert len(sent) == 2 and sent[0]["service"] == "mobile_app_razvan"
    await brain.guard.run_once()  # fără duplicate
    assert len([c for c in fake.calls if c["domain"] == "notify"]) == 2
    await fake.push("sensor.temp_dormitor", st("sensor.temp_dormitor", "21.3", friendly_name="Temp dormitor"))
    await wait_for(lambda: brain.ha.state("sensor.temp_dormitor") == "21.3")
    await brain.guard.run_once()
    assert "unavailable:sensor.temp_dormitor" not in brain.notifier.active
    assert any("Rezolvat" in c["service_data"]["title"] for c in fake.calls if c["domain"] == "notify")


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


async def test_guard_real_world_noise(setup):
    """Situația reală din casă: multe entități picate de mult, TV-uri stinse, `unknown` normale."""
    fake, brain = setup
    g = brain.guard
    brain.ha.states.clear()
    brain.ha.states.update({
        "sensor.hallway_temp": {"entity_id": "sensor.hallway_temp", "state": "unavailable",
                                "attributes": {}, "last_changed": _ago(33)},   # prea vechi
        "media_player.tv": {"entity_id": "media_player.tv", "state": "unavailable",
                            "attributes": {}, "last_changed": _ago(2)},        # TV stins
        "sensor.vol_size": {"entity_id": "sensor.vol_size", "state": "unknown",
                            "attributes": {}, "last_changed": _ago(2)},        # unknown e normal
        "light.baie": {"entity_id": "light.baie", "state": "unavailable",
                       "attributes": {"friendly_name": "Baie"}, "last_changed": _ago(1)},
    })
    assert set(g.check()) == {"unavailable:light.baie"}


async def test_guard_burst_becomes_summary(setup):
    fake, brain = setup
    brain.ha.states.clear()
    for i in range(6):
        eid = f"sensor.s{i}"
        brain.ha.states[eid] = {"entity_id": eid, "state": "unavailable",
                                "attributes": {"friendly_name": f"Senzor {i}"}, "last_changed": _ago(1)}
    await brain.guard.run_once()
    sent = [c for c in fake.calls if c["domain"] == "notify"]
    assert len(sent) == 1 and "6 probleme" in sent[0]["service_data"]["message"]
    assert len(brain.notifier.active) == 6
    # Peste 24h nu mai e în „found”, dar tot e picat -> alerta NU se închide fals.
    for s in brain.ha.states.values():
        s["last_changed"] = _ago(30)
    await brain.guard.run_once()
    assert len(brain.notifier.active) == 6
    assert len([c for c in fake.calls if c["domain"] == "notify"]) == 1


async def test_presence_camera(setup):
    fake, brain = setup
    rule = brain.automations.rules[0]
    await rule.evaluate()  # Răzvan acasă -> camera oprită
    await wait_for(lambda: fake.states["switch.frigate_detect"]["state"] == "off")
    await fake.push("person.razvan", st("person.razvan", "not_home"))
    await wait_for(lambda: fake.states["switch.frigate_detect"]["state"] == "on")
    await fake.push("person.hajni", st("person.hajni", "home"))
    await wait_for(lambda: fake.states["switch.frigate_detect"]["state"] == "off")


async def test_heating_ac_lock(setup):
    fake, brain = setup
    await fake.push("climate.ac_living", st("climate.ac_living", "heat"))
    await wait_for(lambda: fake.states["climate.centrala"]["state"] == "off")
    await fake.push("climate.ac_living", st("climate.ac_living", "off"))
    await wait_for(lambda: fake.states["climate.centrala"]["state"] == "heat")


async def test_follow_thermostat(setup):
    fake, brain = setup
    await brain.automations.rules[2].evaluate()
    await wait_for(lambda: fake.states["climate.robinet_bucatarie"]["attributes"]["temperature"] == 21.5)
    await fake.push("climate.centrala", st("climate.centrala", "heat", temperature=19))
    await wait_for(lambda: fake.states["climate.robinet_bucatarie"]["attributes"]["temperature"] == 19)


async def test_assistant_and_panel(setup):
    fake, brain = setup
    client = TestClient(TestServer(build_app(brain, allowed_ips=None)))
    await client.start_server()
    try:
        r = await (await client.get("/api/status")).json()
        assert r["connected"] and r["assistant"]["enabled"]
        assert all(a["enabled"] for a in r["automations"])
        assert (await client.get("/")).status == 200
        r = await (await client.post("/api/chat", json={"message": "aprinde lumina din living"})).json()
        assert r["actions"] == ["light.turn_on → light.living"]
        assert r["refused"]  # lock nu e permis
        await wait_for(lambda: fake.states["light.living"]["state"] == "on")
        r = await (await client.post("/api/chat", json={"message": "cine e acasă?"})).json()
        assert r["reply"] == "Răzvan e acasă." and r["actions"] == []
    finally:
        await client.close()


async def test_panel_blocks_outside_ingress(setup):
    _, brain = setup
    client = TestClient(TestServer(build_app(brain)))  # 127.0.0.1 e permis
    await client.start_server()
    try:
        assert (await client.get("/api/status")).status == 200
    finally:
        await client.close()
    client = TestClient(TestServer(build_app(brain, allowed_ips={"172.30.32.2"})))
    await client.start_server()
    try:
        assert (await client.get("/api/status")).status == 403
    finally:
        await client.close()
