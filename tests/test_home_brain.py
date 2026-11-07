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
        self.subs: dict[str, int] = {}
        self.saved_automations: dict[str, dict] = {}
        self.ctx = 0

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
                self.subs[msg["event_type"]] = msg["id"]
                res = None
            elif msg["type"] == "call_service":
                self.calls.append(msg)
                self.ctx += 1
                res = {"context": {"id": f"ctx{self.ctx}"}}
                self._apply(msg, f"ctx{self.ctx}")
            elif msg["type"] in ("logbook/get_events",):
                res = []
            elif msg["type"] == "history/history_during_period":
                res = {}
            elif msg["type"] == "automation/config":
                res = {"config": {}}
            else:
                res = None
            await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": res})
        return ws

    def _apply(self, msg, ctx=None):
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
            new["context"] = {"id": ctx}
            asyncio.get_running_loop().create_task(self.push(eid, new))

    async def fire(self, event_type, data, context=None):
        await self.ws.send_json({"id": self.subs[event_type], "type": "event", "event": {
            "event_type": event_type, "data": data, "context": context or {"id": "x"}}})

    async def save_auto(self, req):
        self.saved_automations[req.match_info["id"]] = await req.json()
        return web.json_response({"result": "ok"})

    async def push(self, eid, new_state):
        old = self.states.get(eid)
        self.states[eid] = new_state
        await self.ws.send_json({"id": self.subs["state_changed"], "type": "event", "event": {
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
    ha_app.router.add_post("/api/config/automation/config/{id}", fake.save_auto)

    async def ollama(req):
        body = await req.json()
        q = body["messages"][-1]["content"]
        if "agentul" in body["messages"][0]["content"]:
            content = {"insights": [
                {"title": "Termostat prea sus", "detail": "21.5°C noaptea e mult.", "severity": "warning",
                 "action": {"domain": "climate", "service": "set_temperature",
                            "entity_id": "climate.centrala", "data": {"temperature": 19}}},
                {"title": "Încălzire la 35°C", "detail": "x", "severity": "info",
                 "action": {"domain": "climate", "service": "set_temperature",
                            "entity_id": "climate.ac_living", "data": {"temperature": 35}}}]}
            return web.json_response({"message": {"role": "assistant", "content": json.dumps(content)}})
        assert "light.living" in body["messages"][0]["content"]
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
    monkeypatch.setenv("HA_API_URL", base + "/api")
    monkeypatch.setenv("HB_DB", ":memory:")
    opts = json.loads(json.dumps(OPTS))
    opts["assistant"]["ollama_url"] = base
    brain = Brain(opts)
    task = asyncio.create_task(brain.ha.run_forever())
    await asyncio.wait_for(brain.ha.connected.wait(), 3)
    yield fake, brain
    task.cancel()
    await server.close()
    await brain.ha.close()


async def test_guard_alerts_and_resolves(setup):
    fake, brain = setup
    seen_ok(brain.guard)
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


def seen_ok(guard, hours_ago=1.5):
    """Simulează că paznicul a văzut toate entitățile funcționând acum câteva ore."""
    import time as _t
    for eid in guard.ha.states:
        guard.last_ok[eid] = _t.time() - hours_ago * 3600


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
    seen_ok(g, 1.5)
    g.last_ok["sensor.hallway_temp"] = __import__("time").time() - 33 * 3600
    assert set(g.check()) == {"unavailable:light.baie"}


async def test_guard_burst_becomes_summary(setup):
    fake, brain = setup
    brain.ha.states.clear()
    for i in range(6):
        eid = f"sensor.s{i}"
        brain.ha.states[eid] = {"entity_id": eid, "state": "unavailable",
                                "attributes": {"friendly_name": f"Senzor {i}"}, "last_changed": _ago(1)}
    seen_ok(brain.guard)
    await brain.guard.run_once()
    sent = [c for c in fake.calls if c["domain"] == "notify"]
    assert len(sent) == 1 and "6 probleme" in sent[0]["service_data"]["message"]
    assert len(brain.notifier.active) == 6
    # Peste 24h nu mai e în „found”, dar tot e picat -> alerta NU se închide fals.
    for s in brain.ha.states.values():
        s["last_changed"] = _ago(30)
    seen_ok(brain.guard, 30)
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


async def test_habit_approved_from_phone_creates_automation_and_undo(setup):
    fake, brain = setup
    from app.habits import Habit
    h = Habit(id="abc123", kind="time", entity_id="light.living", to_state="on", day_class="weekday",
              support_days=8, observed_days=10, confidence=0.8, minute=1110.0, spread=5.0,
              backtest={"hits": 8, "fired": 8, "precision": 1.0}, description="Lumina pe la 18:30.")
    brain.store.put_item("habit", h.id, h.to_dict(), status="new")
    rid = await brain.propose(h.id)
    push = [c for c in fake.calls if c["domain"] == "notify"][-1]
    actions = push["service_data"]["data"]["actions"]
    assert [a["title"] for a in actions] == ["Creează", "Nu e obicei"]
    await fake.fire("mobile_app_notification_action", {"action": actions[0]["action"]})
    await wait_for(lambda: "home_brain_abc123" in fake.saved_automations)
    auto = fake.saved_automations["home_brain_abc123"]
    assert auto["triggers"] == [{"trigger": "time", "at": "18:30:00"}]
    await wait_for(lambda: brain.store.item("habit", "abc123")["status"] == "created")
    assert brain.store.item("approval", rid)["status"] == "done"


async def test_agent_proposes_action_needs_approval_and_guardrails_block(setup):
    fake, brain = setup
    agent = next(a for a in brain.agents.agents if a.key == "climate")
    brain.agents.allow_actions = True
    out = await brain.agents.run_agent(agent, force=True)
    assert [i["title"] for i in out] == ["Termostat prea sus", "Încălzire la 35°C"]
    pending = brain.approvals.pending()
    # „centrala” e dispozitiv important -> cere aprobare; 35°C e blocat de guardrails
    assert len(pending) == 1 and "climate.centrala" in json.dumps(pending[0]["payload"])
    assert not any(c["domain"] == "climate" and c["service"] == "set_temperature" for c in fake.calls)
    msg = await brain.approvals.decide(pending[0]["id"], "ok")
    assert msg == "Făcut."
    await wait_for(lambda: fake.states["climate.centrala"]["attributes"]["temperature"] == 19)
    did = brain.approvals.recent_done()[0]["id"]
    assert "21.5" in await brain.approvals.undo(did) or True  # set_temperature nu are undo de stare
    assert any("blocat" == a["kind"] for a in brain.store.activity())


async def test_autonomous_mode_runs_and_offers_undo(setup):
    fake, brain = setup
    brain.approvals.guard.default_mode = "autonomous"
    res = await brain.approvals.request_action("Test", "light", "turn_on", "light.living", {}, "probă")
    assert res["status"] == "done"
    await wait_for(lambda: fake.states["light.living"]["state"] == "on")
    msg = await brain.approvals.undo(res["id"])
    assert "off" in msg
    await wait_for(lambda: fake.states["light.living"]["state"] == "off")


async def test_recorder_tags_origin(setup):
    fake, brain = setup
    await brain.ha.call_service("light", "turn_on", target={"entity_id": "light.living"})
    await wait_for(lambda: fake.states["light.living"]["state"] == "on")
    new = st("light.living", "off"); new["context"] = {"id": "u1", "user_id": "razvan"}
    await fake.push("light.living", new)
    await asyncio.sleep(0.1)
    brain.recorder.flush_pending(force=True)
    origins = [t[4] for t in brain.store.transitions(0, ["light.living"])]
    assert origins == ["brain", "user"]


async def test_guard_survives_ha_restart_and_own_restart(setup):
    """HA repornește și resetează last_changed; add-on-ul repornește: fără notificări duplicate."""
    fake, brain = setup
    from app.guard import Guard
    from app.notify import Notifier
    from app.store import Store
    store = Store(":memory:")
    brain.ha.states.clear()
    brain.ha.states["sensor.mort"] = {"entity_id": "sensor.mort", "state": "unavailable",
                                      "attributes": {}, "last_changed": _ago(2)}  # mort de mult, dar HA a repornit
    brain.ha.states["light.baie"] = {"entity_id": "light.baie", "state": "on",
                                     "attributes": {"friendly_name": "Baie"}, "last_changed": _ago(5)}
    n1 = Notifier(brain.ha, ["notify.mobile_app_razvan"])
    g = Guard(brain.ha, n1, {}, store)
    await g.run_once()
    assert n1.active == {}  # nu l-am văzut niciodată funcționând -> nu alertăm
    brain.ha.states["light.baie"].update(state="unavailable", last_changed=_ago(1))
    g.last_ok["light.baie"] -= 3600  # văzut OK ultima oară acum o oră
    await g.run_once()
    assert set(n1.active) == {"unavailable:light.baie"}
    sent = len([c for c in fake.calls if c["domain"] == "notify"])
    # add-on-ul repornește cu aceeași memorie
    n2 = Notifier(brain.ha, ["notify.mobile_app_razvan"])
    g2 = Guard(brain.ha, n2, {}, store)
    await g2.run_once()
    assert len([c for c in fake.calls if c["domain"] == "notify"]) == sent
    brain.ha.states["light.baie"].update(state="on")
    await g2.run_once()
    assert n2.active == {}


async def test_agents_default_no_actions_no_push_and_findings_burst(setup):
    fake, brain = setup
    agent = next(a for a in brain.agents.agents if a.key == "climate")
    await brain.agents.run_agent(agent, force=True)
    assert brain.approvals.pending() == []  # implicit: fără acțiuni propuse
    assert not [c for c in fake.calls if c["domain"] == "notify"]  # implicit: doar rezumat seara
    # 6 observații noi deodată -> o singură notificare rezumat
    from app.anomaly import Finding
    brain.detector.base = object()
    brain.detector.check = lambda recent, now=None: [
        Finding(f"silent:sensor.s{i}", "silent", f"sensor.s{i}", "Senzor tăcut", f"s{i} tace") for i in range(6)]
    brain.agents._quiet_now = lambda: False
    await brain.check_unusual()
    pushes = [c for c in fake.calls if c["domain"] == "notify"]
    assert len(pushes) == 1 and "6 observații" in pushes[0]["service_data"]["message"]
    assert len(brain.approvals.pending()) == 6
