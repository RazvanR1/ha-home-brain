import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ha_home_brain"))

from app.habits import HabitMiner, automation_index, build_automation, mark_covered  # noqa: E402

TZ = ZoneInfo("Europe/Bucharest")
START = datetime(2026, 10, 1, tzinfo=TZ)  # joi


def ts(day, hh, mm, ss=0):
    return (START + timedelta(days=day, hours=hh, minutes=mm, seconds=ss)).timestamp()


def build_history():
    tr = []
    for d in range(21):
        wd = (START + timedelta(days=d)).weekday()
        # Lampa: aprinsă manual pe la 22:30 în zilele lucrătoare, cu mici abateri
        if wd < 5:
            tr.append((ts(d, 22, 25 + d % 10), "light.lamp", "off", "on", "device"))
            tr.append((ts(d, 23, 50), "light.lamp", "on", "off", "device"))
        # Ușa se deschide la 17:00, lumina de la intrare se aprinde după ~20 s (manual)
        tr.append((ts(d, 17, 0), "binary_sensor.door", "off", "on", "device"))
        tr.append((ts(d, 17, 0, 20), "light.entrance", "off", "on", "user"))
        tr.append((ts(d, 17, 2), "binary_sensor.door", "on", "off", "device"))
        tr.append((ts(d, 17, 30), "light.entrance", "on", "off", "user"))
        # Ceva comandat de o automatizare existentă nu e obicei
        tr.append((ts(d, 7, 0), "switch.boiler", "off", "on", "automation"))
        tr.append((ts(d, 8, 0), "switch.boiler", "on", "off", "automation"))
        # Zgomot: TV pornit la ore aleatoare
        tr.append((ts(d, 9 + (d * 7) % 12, (d * 13) % 60), "light.tv_backlight", "off", "on", "user"))
        tr.append((ts(d, 9 + (d * 7) % 12, (d * 13) % 60 + 1), "light.tv_backlight", "on", "off", "user"))
    tr.sort()
    return tr


def test_mines_time_and_sequence_habits():
    habits = HabitMiner(TZ).mine(build_history(), {"light.lamp": "Lampă"})
    by = {(h.kind, h.entity_id, h.to_state, h.day_class): h for h in habits}
    lamp = by.get(("time", "light.lamp", "on", "weekday"))
    assert lamp is not None, [h.description for h in habits]
    assert 1345 <= lamp.minute <= 1360 and lamp.backtest["precision"] >= 0.9
    assert "Lampă" in lamp.description and "zilele lucrătoare" in lamp.description
    seq = [h for h in habits if h.entity_id == "light.entrance" and h.to_state == "on"]
    assert seq, [h.description for h in habits]
    # Ce face o automatizare nu devine obicei; zgomotul nici atât
    assert not any(h.entity_id == "switch.boiler" for h in habits)
    assert not any(h.entity_id == "light.tv_backlight" for h in habits)


def test_covered_and_automation_yaml():
    habits = HabitMiner(TZ).mine(build_history())
    lamp = next(h for h in habits if h.entity_id == "light.lamp" and h.to_state == "on")
    idx = automation_index({"automation.lamp": {"alias": "Lampa seara", "triggers": [
        {"trigger": "time", "at": "22:40:00"}], "actions": [
        {"action": "light.turn_on", "target": {"entity_id": "light.lamp"}}]}})
    mark_covered([lamp], idx)
    assert lamp.covered_by == "Lampa seara"
    auto = build_automation(lamp, {}, ["person.razvan"])
    assert auto["triggers"][0]["trigger"] == "time"
    assert auto["actions"][0] == {"action": "light.turn_on", "target": {"entity_id": "light.lamp"}}
    assert auto["conditions"][0]["weekday"] == ["mon", "tue", "wed", "thu", "fri"]
    assert auto["id"].startswith("home_brain_")
