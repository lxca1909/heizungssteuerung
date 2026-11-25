"""SQLite: Messverlauf, Alarme, Befehlsprotokoll."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL, heater TEXT NOT NULL, on_ INTEGER, mode TEXT,
    room_temp REAL, case_temp REAL, voltage REAL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS alarms (
    ts REAL NOT NULL, level TEXT, heater TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS commands (
    ts REAL NOT NULL, user TEXT, heater TEXT, action TEXT, params TEXT, ok INTEGER, message TEXT
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, args)
            self._conn.commit()

    def _query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def add_sample(self, heater: str, status: dict[str, Any], ts: float) -> None:
        self._exec(
            "INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
            (ts, heater, int(bool(status.get("on"))), status.get("mode"),
             status.get("room_temp"), status.get("case_temp"), status.get("voltage")),
        )

    def add_alarm(self, alarm: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO alarms VALUES (?,?,?,?)",
            (alarm.get("ts") or time.time(), alarm.get("level"), alarm.get("heater"), alarm.get("message")),
        )

    def log_command(self, user: str, heater: str, action: str, params: str, ok: bool, message: str) -> None:
        self._exec("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                   (time.time(), user, heater, action, params, int(ok), message))

    def history(self, since: float) -> list[dict[str, Any]]:
        return self._query(
            "SELECT ts, heater, on_ AS on_, mode, room_temp, case_temp, voltage FROM samples WHERE ts >= ? ORDER BY ts",
            (since,),
        )

    def recent_alarms(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM alarms ORDER BY ts DESC LIMIT ?", (limit,))

    def recent_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM commands ORDER BY ts DESC LIMIT ?", (limit,))

    def prune(self, keep_days: int = 90) -> None:
        cutoff = time.time() - keep_days * 86400
        for table in ("samples", "alarms", "commands"):
            self._exec(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
