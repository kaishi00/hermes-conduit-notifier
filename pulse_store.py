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
    # Settings > Pulse's model ({"provider", "model"}): Pulse's chat stays on
    # it, put back before each Pulse turn. None leaves the chat's own.
    "turn_model": None,
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
    # Follow-ups (pulse_followup): Pulse checks on what Hermes promised and
    # brings the news to the home chat. On while Pulse and memory are.
    "followups": True,
    # The morning brief (pulse_brief): when cards held for it go out, local time.
    "brief_time": "08:00",
    "memory_since": None,
    "memory_run_at": 0.0,
    "memory_requested": False,
    "followups_now_requested": None,  # when `pulse followups now` asked (pulse_followup)
    # Memory stops for the day once its model calls used this many tokens.
    "memory_daily_tokens": 300_000,
    # Follow-up checks have their own day, so a backfill that spends
    # memory's can't keep promises and to-dos from being checked.
    "followup_daily_tokens": None,  # None: the nudge level's
    "followup_today": None,  # {"day", "tokens"}
    "nudge": "balanced",  # gentle, balanced or eager: pulse_learning.NUDGES
    # A backfill of older history: {"from", "to", "days", "active", "done"},
    # read after the chats as they happen (pulse_memory.run).
    "memory_backfill": None,
    "memory_backfill_days": 30,  # what `pulse memory backfill start` reads
    "memory_backfill_unreadable": [],  # profiles the backfill couldn't read on its last run
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
-- Pulse follow-ups (pulse_followup): a check schedule per open promise and
-- the last decision, which `pulse followups` shows as the journal.
CREATE TABLE IF NOT EXISTS followups (
    item_id INTEGER PRIMARY KEY,
    next_check_at REAL,
    checks INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT '',
    news TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS followups_due ON followups(next_check_at);
-- What Pulse brought the user (pulse_learning), for Conduit's Pulse screen,
-- with the user's rating of each.
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    item_id INTEGER,
    -- The loudness it went out at: push, screen or brief.
    level TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    news TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    chat_title TEXT NOT NULL DEFAULT '',
    message_id INTEGER,
    -- A report item's kind (pulse_report.KINDS) on a "noticed" card.
    topic TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    seen_at REAL,
    briefed_at REAL,
    rating TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    rated_at REAL
);
CREATE INDEX IF NOT EXISTS cards_created ON cards(created_at);
-- What ratings taught Pulse (pulse_learning): each row changes what Pulse
-- does until it's undone.
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    what TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    card_id INTEGER,
    cause TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    undone_at REAL
);
-- Follow-up cards the user marked done on the Pulse screen
-- (pulse_learning.done), with what the item and its checks were before, so
-- Undo puts them back.
CREATE TABLE IF NOT EXISTS card_done (
    card_id INTEGER PRIMARY KEY,
    done_at REAL NOT NULL,
    -- "user" (Done on the screen) or "pulse" (it saw the chat get it done after Do it).
    done_by TEXT NOT NULL DEFAULT 'user',
    prior_json TEXT NOT NULL DEFAULT '{}'
);
-- Do it on a follow-up card (pulse_followup.check_acts): once the chat has
-- answered, Pulse reads it and marks the card done if it got done.
CREATE TABLE IF NOT EXISTS card_acts (
    card_id INTEGER PRIMARY KEY,
    acted_at REAL NOT NULL,
    check_at REAL,
    checks INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS backfill_marks (
    profile TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    at REAL NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    window_from REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, chat_id)
);
"""
# Where memory has read each chat to: ``watermarks`` for chats as they
# happen, ``backfill_marks`` for a backfill of older history (same shape, plus
# the start of the window each mark belongs to: a mark from another window,
# say one written as a backfill was cancelled, counts as no mark).
MARKS = ("watermarks", "backfill_marks")


def _check_marks(marks: str) -> None:
    if marks not in MARKS:  # named into SQL
        raise ValueError(f"unknown marks table {marks!r}")

# A report's item on its own card (pulse_learning.add_noticed_cards).
NOTICED = "noticed"
# The report attempts split into such cards, newest last: their typed
# ratings aren't read, so an item isn't counted twice.
SPLIT_REPORTS = "split_reports"

CARD_COLUMNS = ("kind", "item_id", "level", "title", "quote", "news", "why", "profile", "chat_id", "chat_title",
                "message_id", "topic", "created_at")
LESSON_COLUMNS = ("kind", "what", "value", "profile", "chat_id", "card_id", "cause", "created_at")


def _check_columns(row: dict, allowed: tuple) -> None:
    unknown = set(row) - set(allowed)
    if unknown:  # named into SQL
        raise ValueError(f"unknown columns {sorted(unknown)!r}")


# How far a replacement follows earlier replacements to the item still current.
MAX_CHAIN = 20
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
            # A store from before cards had a topic.
            if "topic" not in {row[1] for row in conn.execute("PRAGMA table_info(cards)")}:
                try:
                    conn.execute("ALTER TABLE cards ADD COLUMN topic TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError:
                    # Another process opening the same store added it first.
                    if "topic" not in {row[1] for row in conn.execute("PRAGMA table_info(cards)")}:
                        raise

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

    def swap(self, key: str, old: Any, new: Any) -> bool:
        """Sets ``key`` to ``new`` only if it still holds ``old`` (another
        process may have changed it since it was read)."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            try:
                current = json.loads(row["value_json"]) if row else DEFAULTS.get(key)
            except ValueError:
                current = DEFAULTS.get(key)
            if current != old:
                conn.execute("ROLLBACK")
                return False
            conn.execute("INSERT INTO settings(key, value_json) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (key, json.dumps(new)))
            conn.execute("COMMIT")
        return True

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

    def attempts_for_suffix(self, suffix: str) -> list[dict]:
        """Attempts whose item id ends with ``suffix`` (a follow-up's attempts
        each get a fresh id ending with its item), oldest first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM attempts WHERE substr(item_id, -?) = ? ORDER BY created_at, attempt",
                                (len(suffix), suffix)).fetchall()
        return [dict(row) for row in rows]

    def retire_attempts(self, suffix: str) -> None:
        """Moves an item's attempts out of ``attempts_for_suffix``'s reach: a
        follow-up's next news is sent and settled on its own attempts."""
        with _lock, self._connect() as conn:
            conn.execute("UPDATE attempts SET item_id = item_id || '-done' WHERE substr(item_id, -?) = ?",
                         (len(suffix), suffix))

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

    def watermarks(self, marks: str = "watermarks", window: Optional[float] = None) -> list[dict]:
        """Where each chat was read to; with ``window``, only that backfill window's marks."""
        _check_marks(marks)
        with self._connect() as conn:
            if marks == "backfill_marks" and window is not None:
                rows = conn.execute("SELECT * FROM backfill_marks WHERE window_from=?", (window,)).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {marks}").fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _mark(conn: Any, marks: str, profile: str, chat_id: str, window: float) -> Optional[Any]:
        row = conn.execute(f"SELECT * FROM {marks} WHERE profile=? AND chat_id=?", (profile, chat_id)).fetchone()
        if row is not None and marks == "backfill_marks" and row["window_from"] != window:
            conn.execute("DELETE FROM backfill_marks WHERE profile=? AND chat_id=?", (profile, chat_id))
            return None  # another window's mark
        return row

    def clear_backfill_marks(self, keep_window: Optional[float] = None) -> None:
        """Drops backfill marks; with ``keep_window``, all but that window's."""
        with _lock, self._connect() as conn:
            if keep_window is None:
                conn.execute("DELETE FROM backfill_marks")
            else:
                conn.execute("DELETE FROM backfill_marks WHERE window_from!=?", (keep_window,))

    def advance(self, profile: str, chat_id: str, *, message_id: int, at: float,
                items: Sequence[dict[str, Any]] = (), closed: Sequence[dict[str, Any]] = (),
                after_id: int = 0, marks: str = "watermarks", window: float = 0.0) -> Optional[list[int]]:
        """Records a read chunk: its items, the remembered items it repeats,
        replaces or closes, and the chat read up to row ``message_id``
        (written at ``at``), in one transaction, so a chunk is never half
        kept. Only if the chat was still read up to ``after_id``, so a chunk
        two processes read is kept once. Returns the new items' ids, or None
        when another reader got there first.

        An item with ``same_as`` only brings that item's last mention (and a
        due date it lacked) up to date; one with ``replaces`` supersedes the
        old item, which is kept with when and by what it stopped being true.
        Nothing is deleted. ``marks`` names the table that says where the
        chat was read to (MARKS), and ``window`` the backfill window's start."""
        _check_marks(marks)
        columns = ("profile", "chat_id", "message_id", "kind", "text", "evidence", "explicit", "due", "due_at",
                   "mentioned_at", "recorded_at", "valid_from")
        live = "status='open' AND superseded_at IS NULL AND profile=?"
        now, ids = time.time(), []
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._mark(conn, marks, profile, chat_id, window)
            if (row["message_id"] if row else 0) != after_id:
                conn.execute("ROLLBACK")
                return None
            for item in items:
                if item.get("same_as"):
                    # A repeat of an item replaced since it was shown repeats its successor;
                    # one of an item closed since is already on record.
                    target = _current(conn, item["same_as"], profile)
                    if target is None or conn.execute(
                            # A date said again, or a later one, moves the item's date.
                            f"UPDATE items SET mentioned_at=MAX(mentioned_at, ?), "
                            f"due=CASE WHEN ? IS NOT NULL AND (due_at IS NULL OR ? > due_at) THEN ? ELSE due END, "
                            f"due_at=CASE WHEN ? IS NOT NULL AND (due_at IS NULL OR ? > due_at) THEN ? ELSE due_at END "
                            f"WHERE id=? AND {live}",
                            (item["mentioned_at"], item["due_at"], item["due_at"], item["due"], item["due_at"],
                             item["due_at"], item["due_at"], target, profile)).rowcount:
                        continue
                row = {**item, "profile": profile, "chat_id": chat_id, "recorded_at": now,
                       "valid_from": item["mentioned_at"]}
                new_id = conn.execute(
                    f"INSERT INTO items({', '.join(columns)}) VALUES({','.join('?' * len(columns))})",
                    tuple(row[column] for column in columns)).lastrowid
                ids.append(new_id)
                # Already replaced (by an earlier chunk): replace its successor.
                target = _current(conn, item["replaces"], profile) if item.get("replaces") else None
                if target is not None and target != new_id:
                    conn.execute(f"UPDATE items SET superseded_at=?, superseded_by=?, valid_to=? WHERE id=? AND {live}",
                                 (now, new_id, item["mentioned_at"], target, profile))
            for entry in closed:  # an item replaced since closes through its successor
                target = _current(conn, entry["id"], profile)
                if target is not None:
                    conn.execute(f"UPDATE items SET status=?, closed_by=?, closed_at=? WHERE id=? AND {live}",
                                 (entry["status"], f"chat {chat_id} message {entry['message_id']}: {entry['evidence']}",
                                  now, target, profile))
            conn.execute(
                f"INSERT INTO {marks}(profile, chat_id, message_id, at) VALUES(?,?,?,?) "
                "ON CONFLICT(profile, chat_id) DO UPDATE SET message_id=excluded.message_id, at=excluded.at, "
                "failures=0, retry_at=0", (profile, chat_id, message_id, at))
            if marks == "backfill_marks":
                conn.execute("UPDATE backfill_marks SET window_from=? WHERE profile=? AND chat_id=?",
                             (window, profile, chat_id))
            conn.execute("COMMIT")
        return ids

    def chunk_failed(self, profile: str, chat_id: str, *, retry_at: float, after_id: int = 0,
                     now: float = 0.0, marks: str = "watermarks", window: float = 0.0) -> int:
        """Counts a failed read of the chat's next chunk, unless another
        reader moved the chat past ``after_id`` or already counted a failure
        that is still waiting for its retry; returns the count, or 0 when
        it didn't count this one."""
        _check_marks(marks)
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._mark(conn, marks, profile, chat_id, window)
            if (row["message_id"] if row else 0) != after_id:
                conn.execute("ROLLBACK")
                return 0
            if row and row["failures"] and row["retry_at"] > now:
                conn.execute("ROLLBACK")
                return 0  # another reader already counted this round's failure
            conn.execute(
                f"INSERT INTO {marks}(profile, chat_id, message_id, at, failures, retry_at) VALUES(?,?,0,0,1,?) "
                "ON CONFLICT(profile, chat_id) DO UPDATE SET failures=failures+1, retry_at=excluded.retry_at",
                (profile, chat_id, retry_at))
            if marks == "backfill_marks":
                conn.execute("UPDATE backfill_marks SET window_from=? WHERE profile=? AND chat_id=?",
                             (window, profile, chat_id))
            failures = conn.execute(f"SELECT failures FROM {marks} WHERE profile=? AND chat_id=?",
                                    (profile, chat_id)).fetchone()["failures"]
            conn.execute("COMMIT")
        return failures

    def open_items(self, kind: str = "", limit: int = -1, *, profile: Optional[str] = None,
                   soonest: bool = False, stale_before: Optional[float] = None) -> list[dict]:
        """Open items, most recently mentioned first, or with ``soonest``
        dated ones soonest due first and then the rest. With ``stale_before``,
        not the ones ``expire_items(due_before=stale_before)`` would close."""
        where, params = "", []
        if stale_before is not None:
            where, params = where + "AND NOT (due_at IS NOT NULL AND due_at < ? AND mentioned_at < ?) ", \
                params + [stale_before, stale_before]
        if kind:
            where, params = where + "AND kind=? ", params + [kind]
        if profile is not None:
            where, params = where + "AND profile=? ", params + [profile]
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM items WHERE status='open' AND superseded_at IS NULL {where}"
                                f"ORDER BY {'due_at IS NULL, due_at, ' if soonest else ''}mentioned_at DESC, id DESC LIMIT ?",
                                (*params, limit)).fetchall()
        return [dict(row) for row in rows]

    def expire_items(self, *, due_before: float, now: float) -> int:
        """Open items due before ``due_before`` and not mentioned since."""
        with _lock, self._connect() as conn:
            return conn.execute(
                "UPDATE items SET status='expired', closed_by='expiry', closed_at=? WHERE status='open' "
                "AND superseded_at IS NULL AND due_at IS NOT NULL AND due_at < ? AND mentioned_at < ?",
                (now, due_before, due_before)).rowcount

    def close_item(self, item_id: int, *, status: str, closed_by: str) -> bool:
        with _lock, self._connect() as conn:
            changed = conn.execute(
                "UPDATE items SET status=?, closed_by=?, closed_at=? WHERE id=? AND status='open' "
                "AND superseded_at IS NULL",
                (status, closed_by, time.time(), item_id)).rowcount
        return bool(changed)

    def current_item(self, item_id: int) -> Optional[int]:
        """The open item standing for ``item_id`` now: itself, or what
        replaced it; None once it's closed."""
        with self._connect() as conn:
            row = conn.execute("SELECT profile FROM items WHERE id=?", (item_id,)).fetchone()
            return _current(conn, item_id, row["profile"]) if row else None

    def item(self, item_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    # Follow-ups -------------------------------------------------------------

    def add_followup(self, item_id: int, *, next_check_at: float, now: float) -> None:
        """A check schedule for an item that has none yet."""
        with _lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO followups(item_id, next_check_at, created_at, updated_at) "
                         "VALUES(?,?,?,?)", (item_id, next_check_at, now, now))

    def end_followups(self, *, keep: set, now: float, only: Optional[int] = None) -> None:
        """Stops checking items no longer open (``only``: just that one).
        A follow-up waiting to be told, told or undelivered keeps its row."""
        with _lock, self._connect() as conn:
            rows = conn.execute("SELECT item_id FROM followups WHERE next_check_at IS NOT NULL"
                                + (" AND item_id=?" if only is not None else ""),
                                (only,) if only is not None else ()).fetchall()
            for row in rows:
                if row["item_id"] not in keep:
                    conn.execute("UPDATE followups SET next_check_at=NULL, decision='closed', updated_at=? "
                                 "WHERE item_id=?", (now, row["item_id"]))

    def sooner_followups(self, factor: float, now: float) -> None:
        """Brings every waiting check closer by ``factor`` (never before now)."""
        with _lock, self._connect() as conn:
            conn.execute("UPDATE followups SET next_check_at = ? + (next_check_at - ?) * ?, updated_at=? "
                         "WHERE next_check_at > ? AND decision IN ('', 'wait', 'check failed')",
                         (now, now, factor, now, now))

    def followup(self, item_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM followups WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def due_followups(self, now: float) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM followups WHERE next_check_at IS NOT NULL AND next_check_at <= ? "
                                "ORDER BY next_check_at", (now,)).fetchall()
        return [dict(row) for row in rows]

    def decide_followup(self, item_id: int, *, decision: str, news: str, why: str, checks: int,
                        next_check_at: Optional[float], now: float, failures: int = 0) -> None:
        """``failures``: checks failed in a row (any decision resets it)."""
        with _lock, self._connect() as conn:
            conn.execute("UPDATE followups SET decision=?, news=?, why=?, checks=?, failures=?, next_check_at=?, "
                         "updated_at=? WHERE item_id=?",
                         (decision, news, why, checks, failures, next_check_at, now, item_id))

    def followups_to_tell(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM followups WHERE decision='tell' ORDER BY updated_at").fetchall()
        return [dict(row) for row in rows]

    def followups(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM followups ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def followup_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT decision, COUNT(*) AS n FROM followups GROUP BY decision").fetchall()
        return {(row["decision"] or "waiting"): row["n"] for row in rows}

    # Cards and lessons (pulse_learning) --------------------------------------

    def add_card(self, row: dict[str, Any]) -> int:
        _check_columns(row, CARD_COLUMNS)
        with _lock, self._connect() as conn:
            columns = ", ".join(row)
            return int(conn.execute(f"INSERT INTO cards({columns}) VALUES({', '.join('?' * len(row))})",
                                    tuple(row.values())).lastrowid)

    def add_cards(self, rows: Sequence[dict[str, Any]], *, attempt: str = "", marks: Sequence[str] = (),
                  keep: int = 50) -> list[int]:
        """Adds these cards in one transaction, all or none. With ``attempt``,
        it's appended to each list setting in ``marks`` (the last ``keep``)
        in the same write, so the mark can't be lost apart from the cards;
        and an attempt the first of them already holds adds nothing (another
        process got there first)."""
        for row in rows:
            _check_columns(row, CARD_COLUMNS)
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                lists = {}
                for key in marks if attempt else ():
                    found = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
                    value = json.loads(found["value_json"]) if found else None
                    lists[key] = [str(entry) for entry in value] if isinstance(value, list) else []
                if marks and attempt and attempt in lists[marks[0]]:
                    conn.execute("ROLLBACK")
                    return []
                ids = [int(conn.execute(f"INSERT INTO cards({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                                        tuple(row.values())).lastrowid) for row in rows]
                for key, value in lists.items():
                    conn.execute("INSERT INTO settings(key, value_json) VALUES(?, ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                                 (key, json.dumps((value + [attempt])[-keep:])))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return ids

    def rated_cards(self, kind: str) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT item_id, topic, rating, rated_at FROM cards WHERE kind=? AND rating!=''", (kind,))]

    def card(self, card_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        return dict(row) if row else None

    def cards(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cards ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def unseen_cards(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM cards WHERE seen_at IS NULL").fetchone()[0])

    def see_cards(self, ids: Optional[list[int]], now: float) -> int:
        """Marks cards seen: these, or every one when ``ids`` is None."""
        with _lock, self._connect() as conn:
            if ids is None:
                return conn.execute("UPDATE cards SET seen_at=? WHERE seen_at IS NULL", (now,)).rowcount
            if not ids:
                return 0
            return conn.execute(f"UPDATE cards SET seen_at=? WHERE seen_at IS NULL AND id IN ({','.join('?' * len(ids))})",
                                (now, *ids)).rowcount

    def card_for(self, kind: str, item_id: int, news: str) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM cards WHERE kind=? AND item_id=? AND news=?",
                               (kind, item_id, news)).fetchone()
        return int(row["id"]) if row else None

    def card_for_message(self, kind: str, chat_id: str, message_id: int) -> Optional[int]:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM cards WHERE kind=? AND chat_id=? AND message_id=?",
                               (kind, chat_id, message_id)).fetchone()
        return int(row["id"]) if row else None

    def held_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM cards WHERE level='brief' AND briefed_at IS NULL "
                                    "AND id NOT IN (SELECT card_id FROM card_done)").fetchone()[0])

    def held_cards(self, limit: int) -> list[dict]:
        """Cards waiting for the morning brief, oldest first (not one the
        user already marked done)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cards WHERE level='brief' AND briefed_at IS NULL "
                                "AND id NOT IN (SELECT card_id FROM card_done) "
                                "ORDER BY created_at, id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def mark_done(self, card_id: int, item_id: int, *, closed_by: str, now: float, by: str = "user") -> bool:
        """Marks a follow-up card done in one transaction: keeps what its item
        and checks were (for Undo), closes an open, reminded or expired item
        as done by ``closed_by``, stops its checks and marks the card seen.
        False when it was marked already."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM card_done WHERE card_id=?", (card_id,)).fetchone():
                conn.execute("ROLLBACK")
                return False
            item = conn.execute("SELECT status, closed_by, closed_at FROM items WHERE id=?", (item_id,)).fetchone()
            check = conn.execute("SELECT decision, next_check_at FROM followups WHERE item_id=?", (item_id,)).fetchone()
            prior = {**(dict(item) if item else {}), **(dict(check) if check else {})}
            conn.execute("INSERT INTO card_done(card_id, done_at, done_by, prior_json) VALUES(?,?,?,?)",
                         (card_id, now, by, json.dumps(prior)))
            conn.execute("UPDATE card_acts SET check_at=NULL WHERE card_id=?", (card_id,))
            conn.execute("UPDATE items SET status='done', closed_by=?, closed_at=? WHERE id=? "
                         "AND status IN ('open', 'reminded', 'expired') AND superseded_at IS NULL",
                         (closed_by, now, item_id))
            conn.execute("UPDATE followups SET next_check_at=NULL, decision='closed', updated_at=? "
                         "WHERE item_id=? AND next_check_at IS NOT NULL", (now, item_id))
            conn.execute("UPDATE cards SET seen_at=COALESCE(seen_at, ?) WHERE id=?", (now, card_id))
            conn.execute("COMMIT")
        return True

    def unmark_done(self, card_id: int, item_id: int, *, closed_by: Sequence[str], now: float) -> bool:
        """Undo, in one transaction: takes the mark off and puts the item
        back as it was while one of ``closed_by`` still holds it closed, and its
        checks back where the mark stopped them. False with no mark."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT prior_json FROM card_done WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            conn.execute("DELETE FROM card_done WHERE card_id=?", (card_id,))
            try:
                prior = json.loads(row["prior_json"])
            except ValueError:
                prior = {}
            prior = prior if isinstance(prior, dict) else {}
            if isinstance(prior.get("status"), str):
                conn.execute("UPDATE items SET status=?, closed_by=?, closed_at=? WHERE id=? "
                             f"AND closed_by IN ({','.join('?' * len(closed_by))})",
                             (prior["status"], str(prior.get("closed_by") or ""), prior.get("closed_at"),
                              item_id, *closed_by))
            if isinstance(prior.get("decision"), str) and prior["decision"] != "closed":
                due = prior.get("next_check_at")
                conn.execute("UPDATE followups SET decision=?, next_check_at=?, updated_at=? "
                             "WHERE item_id=? AND decision='closed'",
                             (prior["decision"], max(float(due), now) if isinstance(due, (int, float)) else None,
                              now, item_id))
            conn.execute("COMMIT")
        return True

    def done_marks(self, ids: list[int]) -> dict[int, dict]:
        """Each of these cards' done mark ({done_at, done_by}), for the ones
        marked."""
        if not ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute("SELECT card_id, done_at, done_by FROM card_done "
                                f"WHERE card_id IN ({','.join('?' * len(ids))})", tuple(ids)).fetchall()
        return {int(row["card_id"]): {"done_at": float(row["done_at"]), "done_by": row["done_by"]} for row in rows}

    def act_on_card(self, card_id: int, *, now: float, check_at: float) -> None:
        """Do it on a card: a check of its chat at ``check_at`` (a second Do it
        starts over)."""
        with _lock, self._connect() as conn:
            conn.execute("INSERT INTO card_acts(card_id, acted_at, check_at) VALUES(?,?,?) "
                         "ON CONFLICT(card_id) DO UPDATE SET acted_at=excluded.acted_at, check_at=excluded.check_at, "
                         "checks=0, outcome='', why=''", (card_id, now, check_at))

    def due_acts(self, now: float) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM card_acts WHERE check_at IS NOT NULL AND check_at <= ? "
                                "ORDER BY check_at", (now,)).fetchall()
        return [dict(row) for row in rows]

    def act(self, card_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM card_acts WHERE card_id=?", (card_id,)).fetchone()
        return dict(row) if row else None

    def decide_act(self, card_id: int, *, acted_at: float, check_at: Optional[float], checks: int, outcome: str,
                   why: str) -> None:
        """Only while the act is the one read (a new Do it meanwhile starts over)."""
        with _lock, self._connect() as conn:
            conn.execute("UPDATE card_acts SET check_at=?, checks=?, outcome=?, why=? WHERE card_id=? AND acted_at=?",
                         (check_at, checks, outcome, why, card_id, acted_at))

    def brief_cards(self, ids: list[int], now: float) -> None:
        if not ids:
            return
        with _lock, self._connect() as conn:
            conn.execute(f"UPDATE cards SET briefed_at=? WHERE briefed_at IS NULL AND id IN ({','.join('?' * len(ids))})",
                         (now, *ids))

    def turn_cards(self, kind: str) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT id, news FROM cards WHERE kind=?", (kind,))]

    def drop_cards(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with _lock, self._connect() as conn:
            dropped = conn.execute(f"DELETE FROM cards WHERE id IN ({marks})", tuple(ids)).rowcount
            conn.execute(f"DELETE FROM card_done WHERE card_id IN ({marks})", tuple(ids))
            conn.execute(f"DELETE FROM card_acts WHERE card_id IN ({marks})", tuple(ids))
            return dropped

    def prune_cards(self, keep: int, held_before: float = 0.0) -> int:
        """Drops the oldest cards past ``keep``, but not one still held for the
        brief unless it was made before ``held_before`` (no brief for weeks)."""
        with _lock, self._connect() as conn:
            dropped = conn.execute(
                "DELETE FROM cards WHERE (NOT (level='brief' AND briefed_at IS NULL "
                "AND id NOT IN (SELECT card_id FROM card_done)) OR created_at < ?) AND id NOT IN "
                "(SELECT id FROM cards ORDER BY created_at DESC, id DESC LIMIT ?)", (held_before, keep)).rowcount
            conn.execute("DELETE FROM card_done WHERE card_id NOT IN (SELECT id FROM cards)")
            conn.execute("DELETE FROM card_acts WHERE card_id NOT IN (SELECT id FROM cards)")
            return dropped

    def rate_card(self, card: dict, *, rating: str, reason: str, now: float) -> bool:
        """Rates ``card`` only if its rating is still the one read (another
        process may have rated it since); whether it did."""
        with _lock, self._connect() as conn:
            return bool(conn.execute(
                "UPDATE cards SET rating=?, reason=?, rated_at=?, seen_at=COALESCE(seen_at, ?) "
                "WHERE id=? AND rating=? AND reason=?",
                (rating, reason, now, now, card["id"], card["rating"], card["reason"])).rowcount)

    def add_lesson(self, row: dict[str, Any], *, unless: Sequence[str] = (), limit: int = 0,
                   changes_from: Optional[str] = None, expect: Optional[str] = None) -> Optional[int]:
        """Adds a lesson; with ``limit``, only while fewer than that many in
        force match its kind, what and the ``unless`` columns; with
        ``changes_from`` (the value when none is in force), only when the
        newest in force of its kind and what has another value (and, with
        ``expect``, has that one: a step from it isn't laid over a move
        made meanwhile). All are
        checked in the same transaction, so two writers at once can't both
        add one."""
        _check_columns(row, LESSON_COLUMNS)
        _check_columns(dict.fromkeys(unless), tuple(row))
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if limit:
                match = ["kind", "what", *unless]
                count = conn.execute("SELECT COUNT(*) FROM lessons WHERE undone_at IS NULL AND "
                                     + " AND ".join(f"{column}=?" for column in match),
                                     tuple(row[column] for column in match)).fetchone()[0]
                if count >= limit:
                    conn.execute("ROLLBACK")
                    return None
            if changes_from is not None:
                newest = conn.execute("SELECT value FROM lessons WHERE undone_at IS NULL AND kind=? AND what=? "
                                      "ORDER BY id DESC LIMIT 1", (row["kind"], row["what"])).fetchone()
                current = newest["value"] if newest else changes_from
                if current == row["value"] or (expect is not None and current != expect):
                    conn.execute("ROLLBACK")
                    return None
            columns = ", ".join(row)
            lesson_id = int(conn.execute(f"INSERT INTO lessons({columns}) VALUES({', '.join('?' * len(row))})",
                                         tuple(row.values())).lastrowid)
            conn.execute("COMMIT")
        return lesson_id

    def lessons(self, kind: Optional[str] = None, what: Optional[str] = None) -> list[dict]:
        """The lessons still in force, oldest first."""
        query, args = "SELECT * FROM lessons WHERE undone_at IS NULL", []
        if kind is not None:
            query, args = query + " AND kind=?", args + [kind]
        if what is not None:
            query, args = query + " AND what=?", args + [what]
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY id", args).fetchall()
        return [dict(row) for row in rows]

    def undo_lesson(self, lesson_id: int, now: float) -> bool:
        with _lock, self._connect() as conn:
            return bool(conn.execute("UPDATE lessons SET undone_at=? WHERE id=? AND undone_at IS NULL",
                                     (now, lesson_id)).rowcount)

    def item_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, COUNT(*) AS n FROM items WHERE status='open' AND superseded_at IS NULL "
                                "GROUP BY kind").fetchall()
        return {row["kind"]: row["n"] for row in rows}


def _current(conn: sqlite3.Connection, item_id: int, profile: str) -> Optional[int]:
    """The item still standing for ``item_id``: itself, or what replaced
    it; None once it's closed (or isn't this profile's)."""
    for _ in range(MAX_CHAIN):
        row = conn.execute("SELECT status, superseded_by FROM items WHERE id=? AND profile=?",
                           (item_id, profile)).fetchone()
        if row is None or row["status"] != "open":
            return None
        if row["superseded_by"] is None:
            return item_id
        item_id = row["superseded_by"]
    return None


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
