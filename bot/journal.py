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
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    inst_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    opened_at INTEGER,
    closed_at INTEGER NOT NULL,
    open_px REAL,
    close_px REAL,
    realized REAL,
    ccy TEXT NOT NULL DEFAULT '',
    how TEXT NOT NULL DEFAULT '',
    first_stop REAL,
    stop_moves INTEGER,
    risk_pct REAL,
    r_multiple REAL,
    mae_r REAL,
    mfe_r REAL,
    facts TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    lesson TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reviews_closed ON reviews (closed_at);
CREATE TABLE IF NOT EXISTS dialog (
    chat_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dialog_chat ON dialog (chat_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    inst_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    said TEXT NOT NULL DEFAULT '',
    chat TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    done_ts INTEGER,
    done_price REAL
);
CREATE TABLE IF NOT EXISTS wishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'owner',
    chat TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT NOT NULL DEFAULT ''
);
"""

# Columns added after a table was first created: (table, column, declaration).
ADDED_COLUMNS = (
    ("calls", "confidence", "INTEGER"),
    ("orders", "source", "TEXT NOT NULL DEFAULT ''"),  # where an order came from: see trading.SOURCE_NAMES
    ("reviews", "source", "TEXT NOT NULL DEFAULT ''"),
)


class Journal:
    def __init__(self, path: str | Path = "data/bot.db"):
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            for table, column, declaration in ADDED_COLUMNS:
                known = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in known:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
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

    def snapshots_between(self, start: float, end: float) -> list[dict]:
        """Account snapshots in [start, end), oldest first: ts, total_eq and the positions as they were stored."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, total_eq, positions FROM snapshots WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (int(start), int(end)),
            ).fetchall()
        snapshots = []
        for row in rows:
            try:
                positions = json.loads(row["positions"])
            except ValueError:
                positions = []
            snapshots.append({"ts": row["ts"], "total_eq": row["total_eq"], "positions": positions if isinstance(positions, list) else []})
        return snapshots

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
        source: str = "",
    ) -> None:
        """One row per instruction the bot sent to OKX on the owner's confirmation.

        `mode` is "demo" or "live"; `action` is open | close | cancel | stop;
        `result` is ok | rejected | unknown (the request may or may not have arrived);
        `source` is where the order came from (trading.SOURCE_NAMES).
        """
        with self._lock:
            self._db.execute(
                "INSERT INTO orders (ts, mode, action, inst_id, summary, request, result, detail, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(ts or time.time()), mode, action, inst_id, summary,
                 json.dumps(request or {}, ensure_ascii=False), result, detail, source),
            )  # fmt: skip
            self._db.commit()

    def orders_between(self, start: float, end: float, mode: str | None = None) -> list[dict]:
        query = "SELECT ts, mode, action, inst_id, summary, result, detail, source FROM orders WHERE ts >= ? AND ts < ?"
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
                "INSERT INTO calls (analysis_id, ts, inst_id, side, trigger_px, target_px, invalid_px, confidence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (analysis_id, when, inst_id, s["side"], s["trigger"], s["target"], s["invalid"], s.get("confidence"))
                    for s in scenarios
                ],
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

    # ------------------------------------------------------- trade reviews

    REVIEW_FIELDS = (
        "inst_id", "direction", "opened_at", "closed_at", "open_px", "close_px", "realized", "ccy", "how",
        "first_stop", "stop_moves", "risk_pct", "r_multiple", "mae_r", "mfe_r", "facts", "text", "lesson", "source",
    )  # fmt: skip

    def record_review(self, ts: float | None = None, **fields) -> int:
        """One closed position as it was reviewed: the program's figures, the AI's few lines and its lesson."""
        names = [name for name in self.REVIEW_FIELDS if name in fields]
        with self._lock:
            cursor = self._db.execute(
                f"INSERT INTO reviews (ts, {', '.join(names)}) VALUES (?, {', '.join('?' for _ in names)})",
                [int(ts or time.time())] + [fields[name] for name in names],
            )
            self._db.commit()
        return int(cursor.lastrowid)

    def has_review(self, inst_id: str, closed_at: float) -> bool:
        """Whether this close was reviewed already (the monitor can see one close twice across a restart)."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM reviews WHERE inst_id = ? AND closed_at = ? LIMIT 1", (inst_id, int(closed_at))
            ).fetchone()
        return row is not None

    def reviews_between(self, start: float, end: float) -> list[dict]:
        """Reviews of positions closed in [start, end), oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reviews WHERE closed_at >= ? AND closed_at < ? ORDER BY closed_at ASC", (int(start), int(end))
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_lessons(self, limit: int, since: float) -> list[dict]:
        """The newest lessons, newest first: closed_at, inst_id, direction, r_multiple, lesson."""
        with self._lock:
            rows = self._db.execute(
                "SELECT closed_at, inst_id, direction, r_multiple, lesson FROM reviews"
                " WHERE lesson != '' AND closed_at >= ? ORDER BY closed_at DESC LIMIT ?",
                (int(since), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- dialog

    def record_dialog(self, chat_id: str, question: str, answer: str, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO dialog (chat_id, ts, question, answer) VALUES (?, ?, ?, ?)",
                (chat_id, int(ts or time.time()), question, answer),
            )
            self._db.commit()

    def recent_dialog(self, chat_id: str, since: float, limit: int) -> list[tuple[str, str]]:
        """The last `limit` questions and answers of one chat since `since`, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT question, answer FROM dialog WHERE chat_id = ? AND ts >= ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (chat_id, int(since), int(limit)),
            ).fetchall()
        return [(row["question"], row["answer"]) for row in reversed(rows)]

    def forget_dialog_before(self, ts: float) -> None:
        with self._lock:
            self._db.execute("DELETE FROM dialog WHERE ts < ?", (int(ts),))
            self._db.commit()

    # ---------------------------------------------------------------- alerts

    def add_alert(
        self, inst_id: str, direction: str, price: float, said: str = "", chat: str = "", ts: float | None = None
    ) -> int:
        """A price alert: `direction` is "below" or "above" `price`. Returns its number."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO alerts (ts, inst_id, direction, price, said, chat) VALUES (?, ?, ?, ?, ?, ?)",
                (int(ts or time.time()), inst_id, direction, float(price), said, chat),
            )
            self._db.commit()
            return int(cursor.lastrowid)

    def alerts(self, status: str = "active") -> list[dict]:
        """Price alerts with this status, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM alerts WHERE status = ? ORDER BY id", (status,)).fetchall()
        return [dict(row) for row in rows]

    def close_alert(self, alert_id: int, status: str, price: float | None = None, ts: float | None = None) -> bool:
        """Marks an active alert fired, cancelled or expired. False when it was no longer active."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE alerts SET status = ?, done_ts = ?, done_price = ? WHERE id = ? AND status = 'active'",
                (status, int(ts or time.time()), price, int(alert_id)),
            )
            self._db.commit()
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- wishes

    def add_wish(self, text: str, source: str = "owner", chat: str = "", ts: float | None = None) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO wishes (ts, text, source, chat) VALUES (?, ?, ?, ?)",
                (int(ts or time.time()), text, source, chat),
            )
            self._db.commit()
            return int(cursor.lastrowid)

    def wishes(self, since: float = 0, status: str | None = None) -> list[dict]:
        """The owner's requests and suggestions recorded since `since`, oldest first."""
        query, values = "SELECT * FROM wishes WHERE ts >= ?", [int(since)]
        if status:
            query += " AND status = ?"
            values.append(status)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id", values).fetchall()
        return [dict(row) for row in rows]

    def dialog_between(self, start: float, end: float) -> list[dict]:
        """Every question and answer kept for [start, end), all chats, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT chat_id, ts, question, answer FROM dialog WHERE ts >= ? AND ts < ? ORDER BY ts, rowid",
                (int(start), int(end)),
            ).fetchall()
        return [dict(row) for row in rows]

    def wish(self, wish_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM wishes WHERE id = ?", (int(wish_id),)).fetchone()
        return dict(row) if row else None

    def set_wish_status(self, wish_id: int, status: str, note: str = "") -> bool:
        with self._lock:
            cursor = self._db.execute("UPDATE wishes SET status = ?, note = ? WHERE id = ?", (status, note, int(wish_id)))
            self._db.commit()
            return cursor.rowcount > 0

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

    def backup_to(self, path: str | Path) -> None:
        """Writes a complete, consistent copy of the journal to `path`."""
        target = sqlite3.connect(str(path))
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()
