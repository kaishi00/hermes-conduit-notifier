"""Pulse's own state: its settings, every delivery attempt, and the outcome ledger.

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
from typing import Any, Optional

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
CREATE INDEX IF NOT EXISTS outcomes_session ON outcomes(session_id);
"""
# ``attempts.session_key`` holds the home chat's stored session id (the
# column kept its first name so a store from the first build still opens).

_lock = threading.Lock()


class PulseStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
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
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value_json FROM settings WHERE key='probe_requested'").fetchone()
            try:
                asked = bool(row and json.loads(row["value_json"]))
            except ValueError:
                asked = False
            if asked:
                conn.execute("UPDATE settings SET value_json='false' WHERE key='probe_requested'")
            conn.execute("COMMIT")
        return asked

    def claim_leader(self, owner: str, ttl_s: float, now: float) -> bool:
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
                    (LEADER, json.dumps({"owner": owner, "until": now + ttl_s})),
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

    def counts(self, kind: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM attempts WHERE kind=? GROUP BY state",
                                (kind,)).fetchall()
        return {row["state"]: row["n"] for row in rows}


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

    def outcome_counts(self, since: float) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM outcomes WHERE at >= ? GROUP BY status",
                                (since,)).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def prune_outcomes(self, *, before: float, keep: int) -> int:
        """Drops outcomes older than ``before``, and the oldest past ``keep``."""
        with _lock, self._connect() as conn:
            removed = conn.execute("DELETE FROM outcomes WHERE at < ?", (before,)).rowcount
            removed += conn.execute("DELETE FROM outcomes WHERE id <= (SELECT id FROM outcomes ORDER BY id DESC "
                                    "LIMIT 1 OFFSET ?)", (keep,)).rowcount
        return removed


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
