"""Memoria locală a creierului (SQLite în /data). Scrieri grupate, curățare automată."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

log = logging.getLogger("home_brain.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transitions (
    ts REAL NOT NULL, entity_id TEXT NOT NULL, old TEXT, new TEXT, origin TEXT
);
CREATE INDEX IF NOT EXISTS ix_tr_entity_ts ON transitions(entity_id, ts);
CREATE INDEX IF NOT EXISTS ix_tr_ts ON transitions(ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tr ON transitions(entity_id, ts, new);
CREATE TABLE IF NOT EXISTS samples (
    entity_id TEXT NOT NULL, hour INTEGER NOT NULL, n INTEGER, total REAL, sq REAL,
    mn REAL, mx REAL, PRIMARY KEY (entity_id, hour)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS items (
    kind TEXT NOT NULL, id TEXT NOT NULL, status TEXT, ts REAL, data TEXT,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS activity (ts REAL, kind TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_act_ts ON activity(ts);
"""


class Store:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("HB_DB", "/data/brain.db")
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._tr_buf: list[tuple] = []
        self._num_buf: dict[tuple[str, int], list[float]] = {}

    # ---------- scriere grupată ----------
    def add_transition(self, ts: float, entity_id: str, old: str | None, new: str | None,
                       origin: str) -> None:
        self._tr_buf.append((ts, entity_id, old, new, origin))
        if len(self._tr_buf) > 500:
            self.flush()

    def add_sample(self, ts: float, entity_id: str, value: float) -> None:
        self._num_buf.setdefault((entity_id, int(ts // 3600)), []).append(value)

    def flush(self) -> None:
        with self._lock:
            tr, self._tr_buf = self._tr_buf, []
            nums, self._num_buf = self._num_buf, {}
            if not tr and not nums:
                return
            cur = self.db.cursor()
            cur.execute("BEGIN")
            cur.executemany("INSERT OR IGNORE INTO transitions VALUES (?,?,?,?,?)", tr)
            for (eid, hour), vals in nums.items():
                cur.execute(
                    """INSERT INTO samples VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(entity_id, hour) DO UPDATE SET
                       n=n+excluded.n, total=total+excluded.total, sq=sq+excluded.sq,
                       mn=min(mn, excluded.mn), mx=max(mx, excluded.mx)""",
                    (eid, hour, len(vals), sum(vals), sum(v * v for v in vals), min(vals), max(vals)))
            cur.execute("COMMIT")

    def add_transitions(self, rows: Iterable[tuple]) -> int:
        rows = list(rows)
        with self._lock:
            cur = self.db.cursor()
            cur.execute("BEGIN")
            before = self.db.total_changes
            cur.executemany("INSERT OR IGNORE INTO transitions VALUES (?,?,?,?,?)", rows)
            cur.execute("COMMIT")
            return self.db.total_changes - before

    # ---------- citire ----------
    def transitions(self, since: float, entity_ids: Iterable[str] | None = None) -> list[tuple]:
        """(ts, entity_id, old, new, origin) ordonate după timp."""
        with self._lock:
            if entity_ids is None:
                return self.db.execute(
                    "SELECT ts, entity_id, old, new, origin FROM transitions WHERE ts>=? ORDER BY ts",
                    (since,)).fetchall()
            ids = list(entity_ids)
            if not ids:
                return []
            q = ",".join("?" * len(ids))
            return self.db.execute(
                f"SELECT ts, entity_id, old, new, origin FROM transitions "
                f"WHERE ts>=? AND entity_id IN ({q}) ORDER BY ts", (since, *ids)).fetchall()

    def samples(self, since_hour: int) -> list[tuple]:
        """(entity_id, hour, n, total, sq, mn, mx)"""
        with self._lock:
            return self.db.execute("SELECT * FROM samples WHERE hour>=?", (since_hour,)).fetchall()

    def data_span(self) -> tuple[float | None, float | None]:
        with self._lock:
            return self.db.execute("SELECT min(ts), max(ts) FROM transitions").fetchone()

    def count_transitions(self) -> int:
        with self._lock:
            return self.db.execute("SELECT count(*) FROM transitions").fetchone()[0]

    # ---------- chei/valori, obiecte, jurnal ----------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def put_item(self, kind: str, item_id: str, data: dict, status: str | None = None) -> None:
        with self._lock:
            if status is None:
                row = self.db.execute("SELECT status FROM items WHERE kind=? AND id=?",
                                      (kind, item_id)).fetchone()
                status = row[0] if row else "new"
            self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)",
                            (kind, item_id, status, time.time(), json.dumps(data, ensure_ascii=False)))

    def set_status(self, kind: str, item_id: str, status: str) -> None:
        with self._lock:
            self.db.execute("UPDATE items SET status=?, ts=? WHERE kind=? AND id=?",
                            (status, time.time(), kind, item_id))

    def delete_items(self, kind: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM items WHERE kind=?", (kind,))

    def item(self, kind: str, item_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT status, ts, data FROM items WHERE kind=? AND id=?",
                                  (kind, item_id)).fetchone()
        if not row:
            return None
        return {"id": item_id, "status": row[0], "ts": row[1], **json.loads(row[2])}

    def items(self, kind: str, status: str | Iterable[str] | None = None, limit: int = 500) -> list[dict]:
        with self._lock:
            if status is None:
                rows = self.db.execute(
                    "SELECT id, status, ts, data FROM items WHERE kind=? ORDER BY ts DESC LIMIT ?",
                    (kind, limit)).fetchall()
            else:
                sts = [status] if isinstance(status, str) else list(status)
                q = ",".join("?" * len(sts))
                rows = self.db.execute(
                    f"SELECT id, status, ts, data FROM items WHERE kind=? AND status IN ({q}) "
                    f"ORDER BY ts DESC LIMIT ?", (kind, *sts, limit)).fetchall()
        return [{"id": r[0], "status": r[1], "ts": r[2], **json.loads(r[3])} for r in rows]

    def log(self, kind: str, message: str, data: dict | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT INTO activity VALUES (?,?,?,?)",
                            (time.time(), kind, message, json.dumps(data or {}, ensure_ascii=False)))
        log.info("[%s] %s", kind, message)

    def activity(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT ts, kind, message FROM activity ORDER BY ts DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]

    # ---------- întreținere ----------
    def cleanup(self, transitions_days: int = 60, samples_days: int = 120, activity_days: int = 60) -> None:
        now = time.time()
        with self._lock:
            self.db.execute("DELETE FROM transitions WHERE ts<?", (now - transitions_days * 86400,))
            self.db.execute("DELETE FROM samples WHERE hour<?", (int((now - samples_days * 86400) // 3600),))
            self.db.execute("DELETE FROM activity WHERE ts<?", (now - activity_days * 86400,))
