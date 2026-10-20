"""Pulse's own state: its settings, every delivery attempt, the outcome ledger and memory.

Pulse (designs/proactivity.md) sends messages as turns in one home chat. This
store keeps what Pulse decided and tried; what Hermes actually did is read
back from Hermes's own state.db (``pulse_hermes``) and written here only as
the attempt's derived state. SQLite in the profile's Hermes home, so the
gateway process (hooks, the probe thread) and the CLI share it.

Never raises into a hook: callers wrap it. Off unless ``enabled`` is set.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

DB_NAME = "conduit-pulse.db"
# How long the store waits on another process's write before giving up.
BUSY_TIMEOUT_S = 5.0

# An attempt's states (designs/proactivity.md 6.4.1). Only Hermes's rows move
# an attempt past ``submitted``; ``refused`` is inject_message saying False.
SUBMITTED = "submitted"
REFUSED = "refused"
NOT_ADMITTED = "not_admitted"
QUEUED = "queued"
RUNNING = "running"
DELIVERED = "delivered"
FAILED = "failed"
IN_FLIGHT = (SUBMITTED, QUEUED, RUNNING)
FINAL = (REFUSED, NOT_ADMITTED, DELIVERED, FAILED)

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "home_title": "Pulse",
    "probe_interval_hours": 6.0,
    "probe_requested": False,
    # A session key chosen with `pulse home`, over the chat found by title.
    "home_session_key": "",
    # When the gateway's loop last ran with Pulse on (time.time()).
    "last_tick_at": None,
    # The shadow report: on while Pulse is, at these local times, in the
    # phone's time zone unless one is set here.
    "daily_report": True,
    "report_time": "08:00,13:00,18:00",
    "time_zone": "",
    "report_requested": False,
    # "all": the report and memory also read every other profile's chats,
    # memories and cron jobs (Bot Mode's bots are profiles); "this": only
    # this profile.
    "report_profiles": "all",
    # Memory (pulse_memory): on while Pulse is. It reads chats written after
    # ``memory_since`` (set when it first runs), at most every few minutes.
    "memory": True,
    "memory_since": None,
    "memory_run_at": 0.0,
    "memory_requested": False,
}
# Which process runs the loop: {"owner": ..., "until": time.time()}.
LEADER = "leader"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL,
    tag TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    session_key TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_state ON attempts(state);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    args_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error_type TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    ids_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outcomes_at ON outcomes(at);
CREATE TABLE IF NOT EXISTS report_ratings (
    attempt_id TEXT PRIMARY KEY,
    items_json TEXT NOT NULL,
    ratings_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outcomes_session ON outcomes(session_id);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    -- The model's own words: only ``evidence`` is checked against the chat.
    text TEXT NOT NULL,
    evidence TEXT NOT NULL,
    explicit INTEGER NOT NULL,
    due TEXT NOT NULL DEFAULT '',
    due_at REAL,
    mentioned_at REAL NOT NULL,
    recorded_at REAL NOT NULL,
    valid_from REAL NOT NULL,
    valid_to REAL,
    superseded_at REAL,
    superseded_by INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    closed_by TEXT NOT NULL DEFAULT '',
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, kind);
CREATE TABLE IF NOT EXISTS watermarks (
    profile TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    at REAL NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, chat_id)
);
"""
# ``items.profile`` and ``watermarks.profile`` are "" for the profile the
# store belongs to, else the other profile's name (pulse_report.profile_homes).
# ``attempts.session_key`` holds the home chat's stored session id (the
# column kept its first name so a store from the first build still opens).

_lock = threading.Lock()


class PulseStore:
    def __init__(self, path: Path, busy_timeout_s: float = BUSY_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.busy_timeout_s = busy_timeout_s
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_s, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return _Closing(conn)

    # Settings ---------------------------------------------------------------

    def get(self, key: str) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULTS.get(key)
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return DEFAULTS.get(key)

    def set(self, key: str, value: Any) -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value_json) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, json.dumps(value)),
            )

    def take_probe_request(self) -> bool:
        """Whether a probe was asked for (``pulse probe``), clearing the ask."""
        return self.take_request("probe_requested")

    def take_request(self, key: str) -> bool:
        """Whether the flag ``key`` was set (a CLI ask), clearing it."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            try:
                asked = bool(row and json.loads(row["value_json"]))
            except ValueError:
                asked = False
            if asked:
                conn.execute("UPDATE settings SET value_json='false' WHERE key=?", (key,))
            conn.execute("COMMIT")
        return asked

    def claim_leader(self, owner: str, ttl_s: float, now: float, build: str = "") -> bool:
        """Whether ``owner`` runs the loop until ``now + ttl_s``: the lease is
        its own, free, or expired. Two processes hosting sessions for the
        same Hermes home never both send."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (LEADER,)).fetchone()
            try:
                lease = json.loads(row["value_json"]) if row else None
            except ValueError:
                lease = None
            held = (isinstance(lease, dict) and lease.get("owner") != owner
                    and isinstance(lease.get("until"), (int, float)) and lease["until"] > now)
            if not held:
                conn.execute(
                    "INSERT INTO settings(key, value_json) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (LEADER, json.dumps({"owner": owner, "until": now + ttl_s, "build": build})),
                )
            conn.execute("COMMIT")
        return not held

    # Attempts ---------------------------------------------------------------

    def add_attempt(self, *, attempt_id: str, item_id: str, attempt: int, kind: str, tag: str, text: str,
                    session_key: str) -> None:
        """Written before inject_message is called, so a crash right after the
        call still leaves a record for the reconciler to look up."""
        now = time.time()
        with _lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id, item_id, attempt, kind, tag, text, session_key, state, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, item_id, attempt, kind, tag, text, session_key, SUBMITTED, now, now),
            )

    def update_attempt(self, attempt_id: str, *, state: Optional[str] = None, detail: Optional[str] = None,
                       turn_id: Optional[str] = None) -> None:
        sets, values = ["updated_at=?"], [time.time()]
        for column, value in (("state", state), ("detail", detail), ("turn_id", turn_id)):
            if value is not None:
                sets.append(f"{column}=?")
                values.append(value)
        with _lock, self._connect() as conn:
            conn.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE attempt_id=?", (*values, attempt_id))

    def attempt_by_tag(self, tag: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM attempts WHERE tag=?", (tag,)).fetchone()
        return dict(row) if row else None

    def in_flight(self) -> list[dict]:
        marks = ",".join("?" * len(IN_FLIGHT))
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM attempts WHERE state IN ({marks}) ORDER BY created_at",
                                IN_FLIGHT).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM attempts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def last_attempt_at(self, kind: str) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(created_at) AS at FROM attempts WHERE kind=?", (kind,)).fetchone()
        return row["at"] if row and row["at"] is not None else None

    def trim_finished(self, kind: str) -> None:
        """Cuts finished attempts of ``kind`` down to their first line: the
        full text was only kept to recognise the turn, and Hermes's own
        transcript has it."""
        marks = ",".join("?" * len(FINAL))
        with _lock, self._connect() as conn:
            conn.execute(f"UPDATE attempts SET text = substr(text, 1, instr(text || char(10), char(10)) - 1) "
                         f"WHERE kind=? AND state IN ({marks}) AND instr(text, char(10)) > 0", (kind, *FINAL))

    def attempts_since(self, kind: str, since: float) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM attempts WHERE kind=? AND created_at >= ? ORDER BY created_at",
                                (kind, since)).fetchall()
        return [dict(row) for row in rows]

    def counts(self, kind: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM attempts WHERE kind=? GROUP BY state",
                                (kind,)).fetchall()
        return {row["state"]: row["n"] for row in rows}

    # Report ratings -------------------------------------------------------

    def set_ratings(self, attempt_id: str, items_json: str, ratings_json: str) -> bool:
        """Stores a report's items and ratings; True when they changed."""
        with _lock, self._connect() as conn:
            row = conn.execute("SELECT items_json, ratings_json FROM report_ratings WHERE attempt_id=?",
                               (attempt_id,)).fetchone()
            if row is not None and (row["items_json"], row["ratings_json"]) == (items_json, ratings_json):
                return False
            conn.execute(
                "INSERT INTO report_ratings(attempt_id, items_json, ratings_json, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(attempt_id) DO UPDATE SET items_json=excluded.items_json, "
                "ratings_json=excluded.ratings_json, updated_at=excluded.updated_at",
                (attempt_id, items_json, ratings_json, time.time()))
        return True

    def all_ratings(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM report_ratings ORDER BY attempt_id").fetchall()
        return [dict(row) for row in rows]

    # Outcomes (designs/proactivity.md 3.6) --------------------------------

    def add_outcome(self, row: dict[str, Any]) -> None:
        columns = ("at", "session_id", "turn_id", "task_id", "tool_call_id", "tool", "action", "label",
                   "args_digest", "status", "error_type", "error", "ids_json", "duration_ms")
        with _lock, self._connect() as conn:
            conn.execute(f"INSERT INTO outcomes({', '.join(columns)}) VALUES({','.join('?' * len(columns))})",
                         tuple(row[column] for column in columns))

    def recent_outcomes(self, limit: int = 20, *, session_id: Optional[str] = None) -> list[dict]:
        where, params = ("WHERE session_id=? ", (session_id,)) if session_id else ("", ())
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM outcomes {where}ORDER BY id DESC LIMIT ?",
                                (*params, limit)).fetchall()
        return [dict(row) for row in rows]

    def outcomes_between(self, session_id: str, start: float, end: float, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            # The latest win a cut: they sit nearest the rows they're shown with.
            rows = conn.execute("SELECT * FROM outcomes WHERE session_id=? AND at >= ? AND at <= ? ORDER BY at DESC "
                                "LIMIT ?", (session_id, start, end, limit)).fetchall()
        return [dict(row) for row in rows]

    def outcome_counts(self, since: float) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM outcomes WHERE at >= ? GROUP BY status",
                                (since,)).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def prune_outcomes(self, *, before: float, keep: int) -> int:
        """Drops outcomes older than ``before``, and the oldest past ``keep``."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            removed = conn.execute("DELETE FROM outcomes WHERE at < ?", (before,)).rowcount
            removed += conn.execute("DELETE FROM outcomes WHERE id <= (SELECT id FROM outcomes ORDER BY id DESC "
                                    "LIMIT 1 OFFSET ?)", (keep,)).rowcount
            conn.execute("COMMIT")
        return removed


    # Memory (designs/pulse-slice-1.md) -----------------------------------

    def watermark(self, profile: str, chat_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM watermarks WHERE profile=? AND chat_id=?",
                               (profile, chat_id)).fetchone()
        return dict(row) if row else None

    def watermarks(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM watermarks").fetchall()
        return [dict(row) for row in rows]

    def advance(self, profile: str, chat_id: str, *, message_id: int, at: float,
                items: Sequence[dict[str, Any]] = (), after_id: int = 0) -> Optional[list[int]]:
        """Records a read chunk: its items, and the chat read up to row
        ``message_id`` (written at ``at``), in one transaction, so a chunk is
        never half kept. Only if the chat was still read up to ``after_id``,
        so a chunk two processes read is kept once. Returns the new items'
        ids, or None when another reader got there first."""
        columns = ("profile", "chat_id", "message_id", "kind", "text", "evidence", "explicit", "due", "due_at",
                   "mentioned_at", "recorded_at", "valid_from")
        now, ids = time.time(), []
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT message_id FROM watermarks WHERE profile=? AND chat_id=?",
                               (profile, chat_id)).fetchone()
            if (row["message_id"] if row else 0) != after_id:
                conn.execute("ROLLBACK")
                return None
            for item in items:
                row = {**item, "profile": profile, "chat_id": chat_id, "recorded_at": now,
                       "valid_from": item["mentioned_at"]}
                ids.append(conn.execute(
                    f"INSERT INTO items({', '.join(columns)}) VALUES({','.join('?' * len(columns))})",
                    tuple(row[column] for column in columns)).lastrowid)
            conn.execute(
                "INSERT INTO watermarks(profile, chat_id, message_id, at) VALUES(?,?,?,?) "
                "ON CONFLICT(profile, chat_id) DO UPDATE SET message_id=excluded.message_id, at=excluded.at, "
                "failures=0, retry_at=0", (profile, chat_id, message_id, at))
            conn.execute("COMMIT")
        return ids

    def chunk_failed(self, profile: str, chat_id: str, *, retry_at: float, after_id: int = 0,
                     now: float = 0.0) -> int:
        """Counts a failed read of the chat's next chunk, unless another
        reader moved the chat past ``after_id`` or already counted a failure
        that is still waiting for its retry; returns the count (0 when
        another reader moved it on)."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT message_id, failures, retry_at FROM watermarks WHERE profile=? AND chat_id=?",
                               (profile, chat_id)).fetchone()
            if (row["message_id"] if row else 0) != after_id:
                conn.execute("ROLLBACK")
                return 0
            if row and row["failures"] and row["retry_at"] > now:
                conn.execute("ROLLBACK")
                return row["failures"]  # this round's failure is already counted
            conn.execute(
                "INSERT INTO watermarks(profile, chat_id, message_id, at, failures, retry_at) VALUES(?,?,0,0,1,?) "
                "ON CONFLICT(profile, chat_id) DO UPDATE SET failures=failures+1, retry_at=excluded.retry_at",
                (profile, chat_id, retry_at))
            failures = conn.execute("SELECT failures FROM watermarks WHERE profile=? AND chat_id=?",
                                    (profile, chat_id)).fetchone()["failures"]
            conn.execute("COMMIT")
        return failures

    def open_items(self, kind: str = "", limit: int = -1) -> list[dict]:
        where, params = ("AND kind=? ", (kind,)) if kind else ("", ())
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM items WHERE status='open' AND superseded_at IS NULL {where}"
                                f"ORDER BY mentioned_at DESC, id DESC LIMIT ?", (*params, limit)).fetchall()
        return [dict(row) for row in rows]

    def close_item(self, item_id: int, *, status: str, closed_by: str) -> bool:
        with _lock, self._connect() as conn:
            changed = conn.execute(
                "UPDATE items SET status=?, closed_by=?, closed_at=? WHERE id=? AND status='open'",
                (status, closed_by, time.time(), item_id)).rowcount
        return bool(changed)

    def item_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, COUNT(*) AS n FROM items WHERE status='open' AND superseded_at IS NULL "
                                "GROUP BY kind").fetchall()
        return {row["kind"]: row["n"] for row in rows}


class _Closing:
    """A connection that closes when its ``with`` block ends (sqlite3's own
    context manager only commits)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, *exc: Any) -> None:
        self._conn.close()


def open_store(home: Path) -> PulseStore:
    return PulseStore(Path(home) / DB_NAME)
