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
CREATE TABLE IF NOT EXISTS orders (
    ts INTEGER NOT NULL,
    mode TEXT NOT NULL,
    action TEXT NOT NULL,
    inst_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    request TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders (ts);
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    bias TEXT NOT NULL DEFAULT '',
    ref_px REAL,
    question TEXT NOT NULL DEFAULT '',
    px_24h REAL,
    px_72h REAL,
    scored INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_analyses_ts ON analyses (ts);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    inst_id TEXT NOT NULL,
    side TEXT NOT NULL,
    trigger_px REAL NOT NULL,
    target_px REAL NOT NULL,
    invalid_px REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    triggered_ts INTEGER,
    closed_ts INTEGER,
    checked_ts INTEGER,
    result_r REAL
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls (ts);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls (status);
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

    # ---------------------------------------------------------------- orders

    def record_order(
        self,
        mode: str,
        action: str,
        summary: str,
        result: str,
        inst_id: str = "",
        request: dict | list | None = None,
        detail: str = "",
        ts: float | None = None,
    ) -> None:
        """One row per instruction the bot sent to OKX on the owner's confirmation.

        `mode` is "demo" or "live"; `action` is open | close | cancel | stop;
        `result` is ok | rejected | unknown (the request may or may not have arrived).
        """
        with self._lock:
            self._db.execute(
                "INSERT INTO orders (ts, mode, action, inst_id, summary, request, result, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(ts or time.time()), mode, action, inst_id, summary,
                 json.dumps(request or {}, ensure_ascii=False), result, detail),
            )  # fmt: skip
            self._db.commit()

    def orders_between(self, start: float, end: float, mode: str | None = None) -> list[dict]:
        query = "SELECT ts, mode, action, inst_id, summary, result, detail FROM orders WHERE ts >= ? AND ts < ?"
        values: list = [int(start), int(end)]
        if mode:
            query += " AND mode = ?"
            values.append(mode)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY ts ASC", values).fetchall()
        return [dict(row) for row in rows]

    def count_opens(self, start: float, end: float, mode: str) -> int:
        """Opening orders sent in [start, end), counting the ones whose outcome is unknown."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE action = 'open' AND result IN ('ok', 'unknown')"
                " AND mode = ? AND ts >= ? AND ts < ?",
                (mode, int(start), int(end)),
            ).fetchone()
        return int(row["n"])

    # ---------------------------------------------------------- track record

    def record_analysis(
        self,
        purpose: str,
        inst_id: str,
        bias: str,
        ref_px: float | None,
        scenarios: list[dict],
        question: str = "",
        ts: float | None = None,
    ) -> int:
        """One analysis the AI gave levels in, with its scenarios (side, trigger, target, invalid).

        Scored later by track.Scorer. Returns the id of the analysis.
        """
        when = int(ts or time.time())
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO analyses (ts, purpose, inst_id, bias, ref_px, question) VALUES (?, ?, ?, ?, ?, ?)",
                (when, purpose, inst_id, bias, ref_px, question),
            )
            analysis_id = int(cursor.lastrowid)
            self._db.executemany(
                "INSERT INTO calls (analysis_id, ts, inst_id, side, trigger_px, target_px, invalid_px) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(analysis_id, when, inst_id, s["side"], s["trigger"], s["target"], s["invalid"]) for s in scenarios],
            )
            self._db.commit()
        return analysis_id

    def open_calls(self) -> list[dict]:
        """Scenarios that have no final result yet, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM calls WHERE status = 'open' ORDER BY ts ASC, id ASC").fetchall()
        return [dict(row) for row in rows]

    def update_call(self, call_id: int, **fields) -> None:
        """Writes the progress or the result of one scenario: status, triggered_ts, closed_ts, checked_ts, result_r."""
        allowed = {"status", "triggered_ts", "closed_ts", "checked_ts", "result_r"}
        names = [name for name in fields if name in allowed]
        if not names:
            return
        with self._lock:
            self._db.execute(
                f"UPDATE calls SET {', '.join(name + ' = ?' for name in names)} WHERE id = ?",
                [fields[name] for name in names] + [call_id],
            )
            self._db.commit()

    def unscored_analyses(self) -> list[dict]:
        """Analyses whose direction has not been compared with the later price yet."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM analyses WHERE scored = 0 ORDER BY ts ASC, id ASC").fetchall()
        return [dict(row) for row in rows]

    def update_analysis(self, analysis_id: int, **fields) -> None:
        allowed = {"px_24h", "px_72h", "scored"}
        names = [name for name in fields if name in allowed]
        if not names:
            return
        with self._lock:
            self._db.execute(
                f"UPDATE analyses SET {', '.join(name + ' = ?' for name in names)} WHERE id = ?",
                [fields[name] for name in names] + [analysis_id],
            )
            self._db.commit()

    def analyses_between(self, start: float, end: float, inst_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM analyses WHERE ts >= ? AND ts < ?"
        values: list = [int(start), int(end)]
        if inst_id:
            query += " AND inst_id = ?"
            values.append(inst_id)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY ts ASC, id ASC", values).fetchall()
        return [dict(row) for row in rows]

    def calls_between(self, start: float, end: float, inst_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM calls WHERE ts >= ? AND ts < ?"
        values: list = [int(start), int(end)]
        if inst_id:
            query += " AND inst_id = ?"
            values.append(inst_id)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY ts ASC, id ASC", values).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- settings

    def get_setting(self, name: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, name: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
