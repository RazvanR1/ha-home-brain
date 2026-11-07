"""Înregistrează ce se întâmplă în casă și importă istoricul HA la pornire."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from .ha import ORIGIN_AUTOMATION, ORIGIN_DEVICE, ORIGIN_USER

log = logging.getLogger("home_brain.recorder")

ACTUATORS = {"light", "switch", "fan", "cover", "climate", "media_player", "vacuum",
             "humidifier", "lock", "input_boolean", "script", "alarm_control_panel", "water_heater"}
EVENT_BINARY = {"door", "window", "opening", "garage_door", "motion", "occupancy", "presence",
                "gas", "moisture", "smoke", "lock", "problem", "safety", "vibration"}
NUMERIC_CLASSES = {"temperature", "humidity", "power", "illuminance", "co2", "pm25", "pm10",
                   "pressure", "voltage", "current", "battery", "moisture", "carbon_dioxide",
                   "volatile_organic_compounds", "signal_strength"}
SKIP_PATTERNS = ("_container", "_volume_size", "_image", "_restart_count", "_memory_", "_cpu_",
                 "_out_of_memory", "_led_ring")
NON_STATES = {"unavailable", "unknown", "", "none", None}


def _ok_name(eid: str) -> bool:
    return not any(p in eid for p in SKIP_PATTERNS)


def is_group(st: dict) -> bool:
    return isinstance(st.get("attributes", {}).get("entity_id"), list)


def is_discrete_tracked(eid: str, st: dict) -> bool:
    domain = eid.split(".", 1)[0]
    if not _ok_name(eid) or is_group(st):
        return False
    if domain in ACTUATORS or domain in ("person", "device_tracker"):
        return True
    if domain == "binary_sensor":
        return st.get("attributes", {}).get("device_class") in EVENT_BINARY
    return False


def is_numeric_tracked(eid: str, st: dict) -> bool:
    if not eid.startswith("sensor.") or not _ok_name(eid):
        return False
    a = st.get("attributes", {})
    if a.get("device_class") not in NUMERIC_CLASSES:
        return False
    return a.get("state_class") in ("measurement", None)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def logbook_origin(entry: dict) -> str:
    if entry.get("context_event_type") in ("automation_triggered", "script_started") \
            or entry.get("context_domain") in ("automation", "script"):
        return ORIGIN_AUTOMATION
    if entry.get("context_entity_id") and entry.get("context_entity_id") != entry.get("entity_id"):
        return ORIGIN_AUTOMATION  # schimbat ca efect al altei entități (grupuri, oglinzi)
    if entry.get("context_user_id"):
        return ORIGIN_USER
    return ORIGIN_DEVICE


def _ts(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class Recorder:
    def __init__(self, ha, store, cfg: dict) -> None:
        self.ha = ha
        self.store = store
        self.import_days = int(cfg.get("history_import_days", 10))
        self.import_status = "neînceput"
        # Originea se stabilește cu o mică întârziere: HA poate trimite schimbarea de stare
        # înaintea răspunsului la comanda noastră, deci contextul nostru nu e încă cunoscut.
        self._pending: list[tuple] = []
        ha.on_state_change(self._on_change)

    async def _on_change(self, entity_id: str, old, new, origin: str) -> None:
        if new is None:
            return
        ts = _ts(new.get("last_updated")) or time.time()
        if is_numeric_tracked(entity_id, new):
            v = _num(new.get("state"))
            if v is not None:
                self.store.add_sample(ts, entity_id, v)
            return
        if not is_discrete_tracked(entity_id, new):
            return
        o, n = (old or {}).get("state"), new.get("state")
        if o == n:
            return
        self._pending.append((time.monotonic(), ts, entity_id, o, n, new.get("context")))

    def flush_pending(self, force: bool = False) -> None:
        now, keep = time.monotonic(), []
        for item in self._pending:
            if force or now - item[0] >= 1.5:
                _, ts, eid, o, n, ctx = item
                self.store.add_transition(ts, eid, o, n, self.ha.classify(ctx))
            else:
                keep.append(item)
        self._pending = keep
        self.store.flush()

    def tracked(self) -> tuple[list[str], list[str]]:
        disc = [e for e, s in self.ha.states.items() if is_discrete_tracked(e, s)]
        num = [e for e, s in self.ha.states.items() if is_numeric_tracked(e, s)]
        return disc, num

    async def import_history(self) -> None:
        """Importă o singură dată ultimele N zile (logbook pentru origine, history pentru valori)."""
        if self.store.get("history_imported"):
            self.import_status = "făcut"
            return
        disc, num = self.tracked()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.import_days)
        self.import_status = "în curs"
        added = 0
        day = start
        while day < end:
            nxt = min(day + timedelta(days=1), end)
            for i in range(0, len(disc), 60):
                chunk = disc[i:i + 60]
                try:
                    entries = await self.ha.logbook(day.isoformat(), nxt.isoformat(), chunk)
                except Exception as err:  # noqa: BLE001
                    log.warning("Logbook %s: %s", day.date(), err)
                    continue
                added += self.store.add_transitions(self._rows_from_logbook(entries))
            for i in range(0, len(num), 40):
                chunk = num[i:i + 40]
                try:
                    hist = await self.ha.ws({"type": "history/history_during_period",
                                             "start_time": day.isoformat(), "end_time": nxt.isoformat(),
                                             "entity_ids": chunk, "minimal_response": True,
                                             "no_attributes": True, "significant_changes_only": False},
                                            timeout=120)
                except Exception as err:  # noqa: BLE001
                    log.warning("History %s: %s", day.date(), err)
                    continue
                for eid, points in (hist or {}).items():
                    for p in points:
                        v, t = _num(p.get("s")), _ts(p.get("lu") or p.get("lc"))
                        if v is not None and t is not None:
                            self.store.add_sample(t, eid, v)
                self.store.flush()
            day = nxt
            await asyncio.sleep(0)
        self.store.flush()
        self.store.set("history_imported", True)
        self.import_status = "făcut"
        self.store.log("memorie", f"Am importat istoricul pe {self.import_days} zile "
                                  f"({added} schimbări, {len(num)} senzori numerici).")

    def _rows_from_logbook(self, entries: list[dict]) -> list[tuple]:
        rows, last = [], {}
        for e in sorted(entries, key=lambda x: _ts(x.get("when")) or 0):
            eid, state, t = e.get("entity_id"), e.get("state"), _ts(e.get("when"))
            if not eid or t is None or state is None:
                continue
            old = last.get(eid)
            last[eid] = state
            if old == state:
                continue
            rows.append((t, eid, old, state, logbook_origin(e)))
        return rows

    async def run_forever(self) -> None:
        await self.ha.connected.wait()
        try:
            await self.import_history()
        except Exception:  # noqa: BLE001
            log.exception("Importul istoricului a eșuat")
            self.import_status = "eșuat"
        n = 0
        while True:
            await asyncio.sleep(5)
            self.flush_pending()
            n += 1
            if n % 2400 == 0:  # ~zilnic
                self.store.cleanup()
