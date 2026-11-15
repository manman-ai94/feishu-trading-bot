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
CREATE TABLE IF NOT EXISTS ai_usage (
    ts INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage (ts);
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

    def record_usage(
        self,
        purpose: str,
        model: str,
        requests: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        web_searches: int = 0,
        cost_usd: float = 0.0,
        ts: float | None = None,
    ) -> None:
        """One row per piece of AI work (a question, a brief), however many API requests it took."""
        with self._lock:
            self._db.execute(
                "INSERT INTO ai_usage (ts, purpose, model, requests, input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_tokens, web_searches, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(ts or time.time()), purpose, model, requests, input_tokens, output_tokens, cache_read_tokens,
                 cache_write_tokens, web_searches, cost_usd),
            )
            self._db.commit()

    def usage_between(self, start: float, end: float) -> dict:
        """AI use in [start, end): pieces of work, web searches and estimated cost in USD.

        Sorting a message (purpose 'routing') and self-checks count towards the
        cost but not as pieces of work.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(CASE WHEN purpose IN ('routing', 'self-check') THEN 0 ELSE 1 END), 0) AS jobs,"
                " COALESCE(SUM(web_searches), 0) AS web_searches, COALESCE(SUM(cost_usd), 0) AS cost_usd"
                " FROM ai_usage WHERE ts >= ? AND ts < ?",
                (int(start), int(end)),
            ).fetchone()
        return {"jobs": row["jobs"], "web_searches": row["web_searches"], "cost_usd": row["cost_usd"]}

    def close(self) -> None:
        with self._lock:
            self._db.close()
