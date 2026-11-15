"""The trade journal: a local SQLite file with account snapshots and events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .models import AccountState

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts INTEGER NOT NULL,
    total_eq REAL,
    upl REAL,
    mgn_ratio REAL,
    positions TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);
CREATE TABLE IF NOT EXISTS events (
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    inst_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""


class Journal:
    def __init__(self, path: str | Path = "data/bot.db"):
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def record_snapshot(self, state: AccountState, ts: float | None = None) -> None:
        positions = json.dumps([asdict(position) for position in state.positions], ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT INTO snapshots (ts, total_eq, upl, mgn_ratio, positions) VALUES (?, ?, ?, ?, ?)",
                (int(ts or time.time()), state.snapshot.total_eq, state.snapshot.upl, state.snapshot.mgn_ratio, positions),
            )
            self._db.commit()

    def record_event(self, kind: str, summary: str, inst_id: str = "", payload: dict | None = None, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, kind, inst_id, summary, payload) VALUES (?, ?, ?, ?, ?)",
                (int(ts or time.time()), kind, inst_id, summary, json.dumps(payload or {}, ensure_ascii=False)),
            )
            self._db.commit()

    def first_equity_since(self, ts: float) -> float | None:
        """Equity of the first snapshot at or after `ts` (used as "start of day")."""
        with self._lock:
            row = self._db.execute(
                "SELECT total_eq FROM snapshots WHERE ts >= ? AND total_eq IS NOT NULL ORDER BY ts ASC LIMIT 1",
                (int(ts),),
            ).fetchone()
        return row["total_eq"] if row else None

    def events_between(self, start: float, end: float) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, inst_id, summary FROM events WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (int(start), int(end)),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_event(self, kind: str, start: float, end: float) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM events WHERE kind = ? AND ts >= ? AND ts < ? LIMIT 1", (kind, int(start), int(end))
            ).fetchone()
        return row is not None

    def equity_series(self, start: float, end: float) -> list[tuple[int, float]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, total_eq FROM snapshots WHERE ts >= ? AND ts < ? AND total_eq IS NOT NULL ORDER BY ts ASC",
                (int(start), int(end)),
            ).fetchall()
        return [(row["ts"], row["total_eq"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
