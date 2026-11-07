import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_home_brain"))

from app.anomaly import Baselines, Detector  # noqa: E402

TZ = ZoneInfo("Europe/Bucharest")
NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FakeHA:
    def __init__(self, states):
        self.states = states

    def state(self, eid):
        return self.states.get(eid, {}).get("state")

    def name(self, eid):
        return self.states.get(eid, {}).get("attributes", {}).get("friendly_name", eid)


def temp_samples(eid, value=21.0, days=14, per_hour=12):
    hour_now = int(NOW // 3600)
    return [(eid, h, per_hour, value * per_hour, (value * value + 0.04) * per_hour, value - 0.3, value + 0.3)
            for h in range(hour_now - days * 24, hour_now)]


def door_history(days=14):
    tr = []
    for d in range(days):
        t = NOW - (d + 1) * 86400
        tr += [(t, "binary_sensor.usa", "off", "on", "device"), (t + 60, "binary_sensor.usa", "on", "off", "device")]
    return sorted(tr)


def make(states, samples=(), transitions=(), cfg=None):
    det = Detector(FakeHA(states), cfg or {}, TZ, ["person.razvan"])
    det.base = Baselines(TZ).build(list(samples), list(transitions))
    return det


def st(state, ago, **attrs):
    return {"state": state, "attributes": attrs, "last_changed": iso(NOW - ago),
            "last_reported": iso(NOW - ago), "last_updated": iso(NOW - ago)}


def test_out_of_range_temperature():
    s = {"sensor.t": st("35", 60, device_class="temperature", unit_of_measurement="°C",
                        state_class="measurement", friendly_name="Temp living")}
    f = make(s, temp_samples("sensor.t")).check([], NOW)
    assert [x.kind for x in f] == ["range"] and "Temp living" in f[0].message
    s["sensor.t"]["state"] = "21.4"
    assert make(s, temp_samples("sensor.t")).check([], NOW) == []


def test_silent_sensor():
    s = {"sensor.t": st("21", 9 * 3600, device_class="temperature", state_class="measurement")}
    assert [x.kind for x in make(s, temp_samples("sensor.t")).check([], NOW)] == ["silent"]


def test_door_open_too_long_and_dismiss_factor():
    s = {"binary_sensor.usa": st("on", 40 * 60, device_class="door", friendly_name="Ușa")}
    det = make(s, transitions=door_history())
    f = det.check([], NOW)
    assert [x.kind for x in f] == ["dwell"] and "Ușa" in f[0].message
    det.dwell_factor["binary_sensor.usa"] = 3.0  # utilizatorul a zis „ignoră” -> așteaptă mai mult
    assert det.check([], NOW) == []


def test_flapping_and_mute():
    s = {"switch.x": st("on", 10)}
    recent = [(NOW - i * 30, "switch.x", "off", "on", "device") for i in range(12)]
    det = make(s, transitions=[])
    assert [x.kind for x in det.check(recent, NOW)] == ["flapping"]
    det.muted.add("switch.x:flapping")
    assert det.check(recent, NOW) == []
