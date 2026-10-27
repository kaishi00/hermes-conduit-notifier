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
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import pulse_prefs as prefs

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
    # The morning brief (pulse_brief): every day at this local time, an hour
    # before the first report by default so the two don't arrive together.
    "brief_time": "07:00",
    # Quiet times (pulse_quiet): the weekdays Pulse keeps to itself (Monday
    # 0 to Sunday 6) and when a quiet spell ends (epoch seconds), in the
    # user's time zone.
    "quiet_days": [],
    "quiet_until": None,
    # The night (pulse_hours): from and to, "HH:MM" in the user's time zone;
    # nothing pushes in it unless it can't wait. [] for none.
    "night": ["22:00", "07:00"],
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
# Which process runs the loop: {"owner": "host:pid", "until": time.time(), "build": pulse.build_stamp()}.
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
    updated_at REAL NOT NULL,
    -- When its news may push (pulse_followup.followup_timing, JSON; "" for
    -- nothing said): when it's useful, whether it's urgent, what held it.
    timing TEXT NOT NULL DEFAULT ''
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
    -- The morning brief it went out in (that brief's attempt item id), on
    -- the brief's own card and the held cards it brought.
    brief TEXT NOT NULL DEFAULT '',
    -- Swiped away ("Not now"): off the list, nothing learned.
    dismissed_at REAL,
    -- Not now until a time (later today, tomorrow, next week): when the card
    -- comes back as new (pulse_followup.return_rested).
    rest_until REAL,
    created_at REAL NOT NULL,
    seen_at REAL,
    briefed_at REAL,
    rating TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    rated_at REAL,
    -- The biggest part learning played in bringing it up (pulse_prefs'
    -- Learned.factor, JSON; "" for none), for its Why?.
    learned TEXT NOT NULL DEFAULT '',
    -- Where it came from and how soon it was due as it came up
    -- (pulse_prefs.place and band), which its signals carry once the
    -- journal no longer has its candidate.
    place TEXT NOT NULL DEFAULT '',
    band TEXT NOT NULL DEFAULT '',
    -- Put back with Undo after Pulse marked it done: its chat isn't read
    -- again to mark it (pulse_followup.read_first, found_cards).
    put_back_at REAL
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
-- Cards the user marked done on the Pulse screen (pulse_learning.done),
-- with what the item and its checks were before, if one is behind the card,
-- so Undo puts them back.
CREATE TABLE IF NOT EXISTS card_done (
    card_id INTEGER PRIMARY KEY,
    done_at REAL NOT NULL,
    -- "user" (Done on the screen) or "pulse" (it saw the chat get it done after Do it).
    done_by TEXT NOT NULL DEFAULT 'user',
    prior_json TEXT NOT NULL DEFAULT '{}'
);
-- Do it on a card (pulse_followup.check_acts): once the chat has answered,
-- Pulse reads it and marks the card done if it got done.
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
-- Everything a report's candidate list put in front of Pulse
-- (pulse_candidates), what it decided about each, and why: kept 30 days, so
-- "why didn't you tell me about X?" has an answer as well as "why that?".
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    -- "memory": a remembered item the report listed (or kept out);
    -- "model": something else the report's model found.
    origin TEXT NOT NULL,
    item_id INTEGER,
    kind TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    -- Where the model found it: chat, calendar, reminders, notes, jobs or tools.
    source TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    due_at REAL,
    actionable INTEGER,
    -- The model's guess at how useful it is now (1-5), its own pick under
    -- the user's bar, and its one-word reason to leave it out.
    value INTEGER,
    bring_up INTEGER NOT NULL DEFAULT 0,
    skip TEXT NOT NULL DEFAULT '',
    -- What it would tell the user (or the remembered item's own text).
    text TEXT NOT NULL DEFAULT '',
    -- Why now, and where it came from, in its words to the user: the card's Why?.
    why TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    rank INTEGER,
    card_id INTEGER,
    -- The user asked to be reminded of it (a found candidate's flag).
    asked INTEGER,
    -- How the router saw it (pulse_candidates.route): how soon it was due
    -- (pulse_prefs.band), where it came from (pulse_prefs.place), its score,
    -- the biggest part learning played in it (JSON, "" for none), whether it
    -- was a floor, and the loudness it came up at ("" when it didn't).
    band TEXT NOT NULL DEFAULT '',
    place TEXT NOT NULL DEFAULT '',
    score REAL,
    factor TEXT NOT NULL DEFAULT '',
    floor INTEGER NOT NULL DEFAULT 0,
    level TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS candidates_run ON candidates(run_id);
CREATE INDEX IF NOT EXISTS candidates_created ON candidates(created_at);
-- What the user did with cards (pulse_prefs): one row per card and kind of
-- signal (a thumb, Do it by how far it got, Done), with the card's kind,
-- place and band as it came up, and when it was pushed if it was. Pulse's
-- learning is recomputed from these, so taking one back is exact.
CREATE TABLE IF NOT EXISTS signals (
    card_id INTEGER NOT NULL,
    what TEXT NOT NULL,
    at REAL NOT NULL,
    useful REAL NOT NULL DEFAULT 0,
    weight REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '',
    place TEXT NOT NULL DEFAULT '',
    band TEXT NOT NULL DEFAULT '',
    pushed REAL,
    PRIMARY KEY (card_id, what)
);
CREATE INDEX IF NOT EXISTS signals_at ON signals(at);
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
# A lesson that forgets what learning holds about something (pulse_learning.reset).
RESET = "reset"
# A thumbs down's reason that says only the timing was wrong
# (pulse_learning.WRONG_TIME): its card keeps no thumb signal.
WRONG_TIME = "wrong_time"
# Why an item memory learned after its time had passed is closed (pulse_memory.LATE_S).
LATE_BY = "expiry: learned after its time had passed"
# The report attempts split into such cards, newest last: their typed
# ratings aren't read, so an item isn't counted twice.
SPLIT_REPORTS = "split_reports"
# The items_json of a split report's ratings row: it holds no ratings and takes none.
SPLIT_ITEMS = '"split"'

CARD_COLUMNS = ("kind", "item_id", "level", "title", "quote", "news", "why", "profile", "chat_id", "chat_title",
                "message_id", "topic", "brief", "created_at", "learned", "place", "band")
# Card columns added after the first build, oldest first, with their types.
# Columns a table gained after stores existed, added to a store from before.
_ADDED_COLUMNS = (("cards", "topic", "TEXT NOT NULL DEFAULT ''"), ("cards", "brief", "TEXT NOT NULL DEFAULT ''"),
                  ("cards", "dismissed_at", "REAL"), ("cards", "rest_until", "REAL"),
                  ("candidates", "why", "TEXT NOT NULL DEFAULT ''"), ("candidates", "asked", "INTEGER"),
                  ("candidates", "band", "TEXT NOT NULL DEFAULT ''"), ("candidates", "place", "TEXT NOT NULL DEFAULT ''"),
                  ("candidates", "score", "REAL"), ("candidates", "factor", "TEXT NOT NULL DEFAULT ''"),
                  ("candidates", "floor", "INTEGER NOT NULL DEFAULT 0"),
                  ("candidates", "level", "TEXT NOT NULL DEFAULT ''"),
                  ("cards", "learned", "TEXT NOT NULL DEFAULT ''"), ("cards", "place", "TEXT NOT NULL DEFAULT ''"),
                  ("cards", "band", "TEXT NOT NULL DEFAULT ''"), ("followups", "timing", "TEXT NOT NULL DEFAULT ''"),
                  ("cards", "put_back_at", "REAL"))
# What report cards said for Why? before a candidate's reason did (an item's card,
# never shown; a whole report's card), and the setting that says a store has
# cleared them.
_STOCK_NOTICED_WHY = "From your Pulse report: something Hermes noticed in your chats, calendar or notes."
_STOCK_REPORT_WHY = "Your Pulse report: what Hermes saw in your chats since the last one."
STOCK_WHY_CLEARED = "stock_noticed_why_cleared"
LESSON_COLUMNS = ("kind", "what", "value", "profile", "chat_id", "card_id", "cause", "created_at")
CANDIDATE_COLUMNS = ("origin", "item_id", "kind", "profile", "chat_id", "source", "area", "due_at", "actionable",
                     "value", "bring_up", "skip", "text", "why", "decision", "rank", "asked", "band", "place",
                     "score", "factor", "floor", "level")
# The setting that says a store's signals were first filled in from the
# ratings, Do its and Dones it had before signals were kept (_sync_signals),
# and its two-thumb steps retired: before learning, two net thumbs moved a
# kind a step along the loudness ladder as a lesson with one of
# STEPPED_CAUSES; those thumbs are signals now, so the steps are undone
# once, and the old running score (OLD_SCORES) goes.
SIGNALS_BACKFILLED = "signals_backfilled"
STEPPED_CAUSES = ("2 thumbs up", "2 thumbs down")
OLD_SCORES = "rating_scores"
# Who closed a card's item when the user marked it Done (pulse_learning.DONE_BY's "user").
USER = "user"
# Who marked a card done when a chat showed it done (card_done's done_by).
PULSE = "pulse"


def _listed(conn: Any, key: str) -> list[str]:
    """A list setting's entries, as strings ([] when unset or not a list)."""
    found = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    value = json.loads(found["value_json"]) if found else None
    return [str(entry) for entry in value] if isinstance(value, list) else []


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # a table _ADDED_COLUMNS names


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
# What each store's signals added up to when last fitted (``learned``): its
# path's (the signals' totals, when, the fit). Fitted again once the signals
# change, or after LEARNED_FOR_S, over which fading changes next to nothing.
_learned: dict[str, tuple[tuple, float, prefs.Learned]] = {}
LEARNED_FOR_S = 600.0


def _same_host(a: Any, b: Any) -> bool:
    """Whether two lease owners (``host:pid``) name the same host."""
    return str(a).rpartition(":")[0] == str(b).rpartition(":")[0]

class PulseStore:
    def __init__(self, path: Path, busy_timeout_s: float = BUSY_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.busy_timeout_s = busy_timeout_s
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # A store from before a table had these.
            for table, column, kind in _ADDED_COLUMNS:
                if column in _columns(conn, table):
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:
                    # Another process opening the same store added it first.
                    if column not in _columns(conn, table):
                        raise
            # A report card's why is now its candidate's reason, or nothing: the stock
            # lines from before (an item's, a whole report's) would show behind Why? as
            # if they were one. Cleared once,
            # with its marker in the same transaction, so a store from before candidate
            # lists, or one a process stopped with midway, is cleared too.
            if conn.execute("SELECT 1 FROM settings WHERE key=?", (STOCK_WHY_CLEARED,)).fetchone() is None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if conn.execute("SELECT 1 FROM settings WHERE key=?", (STOCK_WHY_CLEARED,)).fetchone() is None:
                        conn.execute("UPDATE cards SET why='' WHERE kind=? AND why=?", (NOTICED, _STOCK_NOTICED_WHY))
                        conn.execute("UPDATE cards SET why='' WHERE kind='report' AND why=?", (_STOCK_REPORT_WHY,))
                        conn.execute("INSERT INTO settings(key, value_json) VALUES(?, 'true')", (STOCK_WHY_CLEARED,))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            # Signals as the cards say: each card's rating, Do it and Done, so what Pulse
            # learns starts from what the store already knows, and takes in (or back) what
            # a process from before signals, still running, wrote without them. The
            # two-thumb steps retire once.
            since = time.time() - prefs.KEEP_S
            first = conn.execute("SELECT 1 FROM settings WHERE key=?", (SIGNALS_BACKFILLED,)).fetchone() is None
            if first or any(_signal_gaps(conn, since).values()):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _sync_signals(conn, _signal_gaps(conn, since))
                    if conn.execute("SELECT 1 FROM settings WHERE key=?", (SIGNALS_BACKFILLED,)).fetchone() is None:
                        conn.execute(f"UPDATE lessons SET undone_at=? WHERE what='level' AND undone_at IS NULL "
                                     f"AND cause IN ({', '.join('?' * len(STEPPED_CAUSES))})",
                                     (time.time(), *STEPPED_CAUSES))
                        conn.execute("DELETE FROM settings WHERE key=?", (OLD_SCORES,))
                        conn.execute("INSERT INTO settings(key, value_json) VALUES(?, 'true')", (SIGNALS_BACKFILLED,))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
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
        its own, free, expired, or held on another build. Two processes
        hosting sessions for the same Hermes home never both send.

        A process stops running the loop once a newer build is installed over
        it (pulse.tick), so a lease stamped with another build than the
        claimer's is left by a process that no longer ticks: the dashboard a
        plugin update restarted takes over at once, rather than reading as
        an older copy until that lease runs out. Only on the same host: a
        process elsewhere has its own install, so its own build stamp."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (LEADER,)).fetchone()
            try:
                lease = json.loads(row["value_json"]) if row else None
            except ValueError:
                lease = None
            held = (isinstance(lease, dict) and lease.get("owner") != owner
                    and isinstance(lease.get("until"), (int, float)) and lease["until"] > now
                    and not (build and isinstance(lease.get("build"), str) and lease["build"]
                             and lease["build"] != build and _same_host(lease.get("owner"), owner)))
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
        """Stores a report's items and ratings; True when they changed. A
        report split into cards takes none: it's rated on its cards."""
        with _lock, self._connect() as conn:
            if str(attempt_id) in _listed(conn, SPLIT_REPORTS):
                return False
            row = conn.execute("SELECT items_json, ratings_json FROM report_ratings WHERE attempt_id=?",
                               (attempt_id,)).fetchone()
            if row is not None and row["items_json"] == SPLIT_ITEMS:
                return False
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
        chat was read to (MARKS), and ``window`` the backfill window's start.
        A new item marked ``late`` (pulse_memory.LATE_S) is kept as expired.
        An item ``closed`` closes whether open or reminded, and its cards get
        Pulse's done mark in the same write (_mark_cards_done), so none is
        left open behind it."""
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
                if item.get("late"):  # learned after its time had passed: too late to remind
                    conn.execute("UPDATE items SET status='expired', closed_by=?, closed_at=? WHERE id=?",
                                 (LATE_BY, now, new_id))
                # Already replaced (by an earlier chunk): replace its successor.
                target = _current(conn, item["replaces"], profile) if item.get("replaces") else None
                if target is not None and target != new_id:
                    conn.execute(f"UPDATE items SET superseded_at=?, superseded_by=?, valid_to=? WHERE id=? AND {live}",
                                 (now, new_id, item["mentioned_at"], target, profile))
            for entry in closed:  # an item replaced since closes through its successor
                target = _current(conn, entry["id"], profile, reminded=True)
                if target is None:
                    continue
                closer = f"chat {chat_id} message {entry['message_id']}: {entry['evidence']}"
                prior = _prior(conn, target)
                if conn.execute("UPDATE items SET status=?, closed_by=?, closed_at=? WHERE id=? "
                                "AND status IN ('open', 'reminded') AND superseded_at IS NULL AND profile=?",
                                (entry["status"], closer, now, target, profile)).rowcount:
                    # Undo puts the item back while this close still holds it,
                    # from a card about an item it replaced too.
                    _mark_cards_done(conn, target, prior={**prior, "closer": closer, "item": target}, now=now)
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

    def remembered_items(self, *, profile: str, limit: int, reminded_since: float) -> list[dict]:
        """What memory reads a chat beside: the profile's open items, and its
        commitments reminded since ``reminded_since``, which a chat can still
        show done; most recently mentioned first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM items WHERE superseded_at IS NULL AND profile=? AND (status='open' "
                                "OR (status='reminded' AND closed_at >= ?)) ORDER BY mentioned_at DESC, id DESC LIMIT ?",
                                (profile, reminded_since, limit)).fetchall()
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
                        next_check_at: Optional[float], now: float, failures: int = 0,
                        timing: Optional[dict[str, Any]] = None) -> None:
        """``failures``: checks failed in a row (any decision resets it).
        ``timing``: when its news may push (pulse_followup.followup_timing),
        kept as given; None clears it."""
        with _lock, self._connect() as conn:
            conn.execute("UPDATE followups SET decision=?, news=?, why=?, checks=?, failures=?, next_check_at=?, "
                         "updated_at=?, timing=? WHERE item_id=?",
                         (decision, news, why, checks, failures, next_check_at, now,
                          json.dumps(timing, sort_keys=True) if timing else "", item_id))

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
                  keep: int = 50, links: Sequence[int] = ()) -> list[int]:
        """Adds these cards in one transaction, all or none. With ``attempt``,
        it's appended to each list setting in ``marks`` (the last ``keep``)
        in the same write, so the mark can't be lost apart from the cards;
        and an attempt any of them already holds adds nothing (another
        process got there first). A report split into cards (SPLIT_REPORTS in
        ``marks``) loses its typed ratings in the same write: it's rated on
        its cards from then on, so an item counts once. ``links`` are the
        journaled candidates the cards come from, one per row, noted on each
        in the same write (pulse_candidates)."""
        for row in rows:
            _check_columns(row, CARD_COLUMNS)
        if links and len(links) != len(rows):
            raise ValueError(f"{len(links)} candidate links for {len(rows)} cards")
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                lists = {key: _listed(conn, key) for key in (marks if attempt else ())}
                if marks and attempt and any(attempt in listed for listed in lists.values()):
                    conn.execute("ROLLBACK")
                    return []
                ids = [int(conn.execute(f"INSERT INTO cards({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                                        tuple(row.values())).lastrowid) for row in rows]
                conn.executemany("UPDATE candidates SET card_id=? WHERE id=?", list(zip(ids, links)))
                if SPLIT_REPORTS in lists:
                    # Its typed ratings go, and the row stays as a mark that
                    # outlasts the trimmed list, so none are read back later.
                    conn.execute(
                        "INSERT INTO report_ratings(attempt_id, items_json, ratings_json, updated_at) "
                        "VALUES(?, ?, '[]', ?) ON CONFLICT(attempt_id) DO UPDATE SET "
                        "items_json=excluded.items_json, ratings_json='[]', updated_at=excluded.updated_at",
                        (attempt, SPLIT_ITEMS, time.time()))
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
        """``kind``'s cards the user rated useful or not: not "wrong time",
        which rates when a card came, not the card."""
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT item_id, topic, rating, rated_at FROM cards WHERE kind=? AND rating!='' AND reason!=?",
                (kind, WRONG_TIME))]

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
                                    "AND dismissed_at IS NULL "
                                    "AND id NOT IN (SELECT card_id FROM card_done)").fetchone()[0])

    def held_cards(self, limit: int) -> list[dict]:
        """Cards waiting for the morning brief, oldest first (not one the
        user already marked done or swiped away)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cards WHERE level='brief' AND briefed_at IS NULL "
                                "AND dismissed_at IS NULL AND id NOT IN (SELECT card_id FROM card_done) "
                                "ORDER BY created_at, id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def found_cards(self, since: float) -> list[dict]:
        """Report items Pulse found on its own (nothing remembered behind
        them) since ``since`` that are on the screen: not held for the brief,
        put away, rated not useful (Wrong time is only about when) or marked
        done, and not tapped Do it on (its check reads the chat) or put back
        with Undo after Pulse marked it; oldest first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cards WHERE kind=? AND item_id IS NULL AND created_at > ? "
                                "AND NOT (level='brief' AND briefed_at IS NULL) AND dismissed_at IS NULL "
                                "AND NOT (rating='down' AND reason!=?) AND id NOT IN (SELECT card_id FROM card_done) "
                                "AND id NOT IN (SELECT card_id FROM card_acts) AND put_back_at IS NULL "
                                "ORDER BY created_at, id", (NOTICED, since, WRONG_TIME)).fetchall()
        return [dict(row) for row in rows]

    def mark_done(self, card_id: int, item_id: Optional[int], *, closed_by: str, now: float,
                  by: str = "user") -> bool:
        """Marks a card done in one transaction: keeps what its item and
        checks were (for Undo), closes an open, reminded or expired item as
        done by ``closed_by``, stops its checks and marks the card seen. A
        card with no item (a report item Pulse found on its own) is only
        marked. False when it was marked already."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM card_done WHERE card_id=?", (card_id,)).fetchone():
                conn.execute("ROLLBACK")
                return False
            prior = _prior(conn, item_id) if item_id is not None else {}
            conn.execute("INSERT INTO card_done(card_id, done_at, done_by, prior_json) VALUES(?,?,?,?)",
                         (card_id, now, by, json.dumps(prior)))
            conn.execute("UPDATE card_acts SET check_at=NULL WHERE card_id=?", (card_id,))
            if item_id is not None:
                conn.execute("UPDATE items SET status='done', closed_by=?, closed_at=? WHERE id=? "
                             "AND status IN ('open', 'reminded', 'expired') AND superseded_at IS NULL",
                             (closed_by, now, item_id))
                conn.execute("UPDATE followups SET next_check_at=NULL, decision='closed', updated_at=? "
                             "WHERE item_id=? AND next_check_at IS NOT NULL", (now, item_id))
            conn.execute("UPDATE cards SET seen_at=COALESCE(seen_at, ?) WHERE id=?", (now, card_id))
            if by == USER:
                _put_signal(conn, card_id, prefs.DONE, at=now, useful=1.0, weight=prefs.DONE_WEIGHT)
            conn.execute("COMMIT")
        return True

    def unmark_done(self, card_id: int, item_id: Optional[int], *, closed_by: Sequence[str], now: float) -> bool:
        """Undo, in one transaction: takes the mark off and puts the item
        back as it was while one of ``closed_by``, or the chat's close the
        mark came with (its ``closer``, on the ``item`` it closed), still
        holds it closed, and its checks back where the mark stopped them.
        What the mark taught goes with it: the user's Done signal, and a Do
        it Pulse saw done goes back to something sent (all the check saw
        before it). A mark Pulse made, taken off, keeps the card as the user
        left it: put_back_at. False with no mark."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT done_by, prior_json FROM card_done WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            conn.execute("DELETE FROM card_done WHERE card_id=?", (card_id,))
            if row["done_by"] == PULSE:
                conn.execute("UPDATE cards SET put_back_at=? WHERE id=?", (now, card_id))
            conn.execute("DELETE FROM signals WHERE card_id=? AND what=?", (card_id, prefs.DONE))
            conn.execute("UPDATE signals SET weight=MIN(weight, ?) WHERE card_id=? AND what=?",
                         (prefs.ACT_SENT, card_id, prefs.ACT))
            try:
                prior = json.loads(row["prior_json"])
            except ValueError:
                prior = {}
            prior = prior if isinstance(prior, dict) else {}
            closers = [*closed_by, *([prior["closer"]] if isinstance(prior.get("closer"), str) else [])]
            if isinstance(prior.get("item"), int) and not isinstance(prior["item"], bool):
                item_id = prior["item"]  # the item a chat's close closed: the card's, or what replaced it
            if isinstance(prior.get("status"), str):
                conn.execute("UPDATE items SET status=?, closed_by=?, closed_at=? WHERE id=? "
                             f"AND closed_by IN ({','.join('?' * len(closers))})",
                             (prior["status"], str(prior.get("closed_by") or ""), prior.get("closed_at"),
                              item_id, *closers))
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
        starts over), and its signal, opened (one that got further keeps
        how far it got)."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT INTO card_acts(card_id, acted_at, check_at) VALUES(?,?,?) "
                             "ON CONFLICT(card_id) DO UPDATE SET acted_at=excluded.acted_at, "
                             "check_at=excluded.check_at, checks=0, outcome='', why=''", (card_id, now, check_at))
                _put_signal(conn, card_id, prefs.ACT, at=now, useful=1.0, weight=prefs.ACT_OPENED)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

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
                   why: str, stage: Optional[float] = None) -> None:
        """Only while the act is the one read (a new Do it meanwhile starts
        over). ``stage``: how far Do it got (pulse_prefs.ACT_SENT, ACT_DONE),
        raised on its signal, never lowered."""
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute("UPDATE card_acts SET check_at=?, checks=?, outcome=?, why=? "
                                       "WHERE card_id=? AND acted_at=?",
                                       (check_at, checks, outcome, why, card_id, acted_at)).rowcount
                if changed and stage is not None:
                    _put_signal(conn, card_id, prefs.ACT, at=acted_at, useful=1.0, weight=stage)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def brief_cards(self, ids: list[int], now: float, brief: str = "") -> None:
        """Stamps held cards as gone out in the brief ``brief``; one put away
        or marked done while the brief was out stays held, so putting it back
        brings it to the next brief."""
        if not ids:
            return
        with _lock, self._connect() as conn:
            conn.execute(f"UPDATE cards SET briefed_at=?, brief=? WHERE briefed_at IS NULL AND dismissed_at IS NULL "
                         f"AND id NOT IN (SELECT card_id FROM card_done) "
                         f"AND id IN ({','.join('?' * len(ids))})", (now, brief, *ids))

    def dismiss_card(self, card_id: int, dismissed: bool, now: float, rest_until: Optional[float] = None) -> bool:
        """Swipes a card away (and marks it seen), until ``rest_until`` when
        one is given, or puts it back; whether it changed."""
        with _lock, self._connect() as conn:
            if dismissed:
                return conn.execute("UPDATE cards SET dismissed_at=?, rest_until=?, seen_at=COALESCE(seen_at, ?) "
                                    "WHERE id=? AND dismissed_at IS NULL", (now, rest_until, now, card_id)).rowcount > 0
            return conn.execute("UPDATE cards SET dismissed_at=NULL, rest_until=NULL "
                                "WHERE id=? AND dismissed_at IS NOT NULL", (card_id,)).rowcount > 0

    def rested_cards(self, now: float) -> list[dict]:
        """Cards swiped away until a time that has come, the earliest first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cards WHERE dismissed_at IS NOT NULL AND rest_until IS NOT NULL "
                                "AND rest_until <= ? ORDER BY rest_until, id", (now,)).fetchall()
        return [dict(row) for row in rows]

    def return_card(self, card_id: int, now: float, rest_until: Optional[float]) -> bool:
        """A card swiped away comes back as new, at the top of the list;
        whether it did. Only while ``rest_until`` is still its time: one put
        back and swiped away again meanwhile keeps its new rest. Never one
        marked done meanwhile: end_rest brings that one back as it is."""
        with _lock, self._connect() as conn:
            return conn.execute("UPDATE cards SET dismissed_at=NULL, rest_until=NULL, seen_at=NULL, created_at=? "
                                "WHERE id=? AND dismissed_at IS NOT NULL AND rest_until IS ? "
                                "AND id NOT IN (SELECT card_id FROM card_done)",
                                (now, card_id, rest_until)).rowcount > 0

    def end_rest(self, card_id: int, rest_until: Optional[float]) -> bool:
        """A card swiped away until a time is back on the list as it was
        (for one marked done meanwhile); whether it changed. Only while
        ``rest_until`` is still its time, as return_card."""
        with _lock, self._connect() as conn:
            return conn.execute("UPDATE cards SET dismissed_at=NULL, rest_until=NULL "
                                "WHERE id=? AND dismissed_at IS NOT NULL AND rest_until IS ?",
                                (card_id, rest_until)).rowcount > 0

    def resting_items(self, now: float, rest_s: float) -> dict[int, float]:
        """Remembered items a card of was swiped away and still rests: while
        the card is away until a time (it comes back as new at that time,
        or, its chat read first, a little after: return_card, end_rest), or
        for ``rest_s`` after a Not now with no time; and when it was swiped
        away (the latest)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT item_id, MAX(dismissed_at) AS at FROM cards WHERE item_id IS NOT NULL "
                                "AND dismissed_at IS NOT NULL AND ((rest_until IS NULL AND dismissed_at >= ?) "
                                "OR rest_until IS NOT NULL) GROUP BY item_id", (now - rest_s,)).fetchall()
        return {int(row["item_id"]): float(row["at"]) for row in rows}

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
        brief unless it was made before ``held_before`` (no brief for weeks). A
        card put away with Not now is no longer held."""
        with _lock, self._connect() as conn:
            dropped = conn.execute(
                "DELETE FROM cards WHERE (NOT (level='brief' AND briefed_at IS NULL AND dismissed_at IS NULL "
                "AND id NOT IN (SELECT card_id FROM card_done)) OR created_at < ?) AND id NOT IN "
                "(SELECT id FROM cards ORDER BY created_at DESC, id DESC LIMIT ?)", (held_before, keep)).rowcount
            conn.execute("DELETE FROM card_done WHERE card_id NOT IN (SELECT id FROM cards)")
            conn.execute("DELETE FROM card_acts WHERE card_id NOT IN (SELECT id FROM cards)")
            return dropped

    def rate_card(self, card: dict, *, rating: str, reason: str, now: float) -> bool:
        """Rates ``card`` only if its rating is still the one read (another
        process may have rated it since), with its thumb signal in the same
        write; whether it did. The same thumb again (with or without a
        reason) leaves the signal as it was, so the vote fades from when it
        was first given. "Wrong time" says only the timing was wrong: no
        thumb signal, and one the card had goes."""
        counts = reason != WRONG_TIME
        counted = card["rating"] in ("up", "down") and card["reason"] != WRONG_TIME
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rated = bool(conn.execute(
                    "UPDATE cards SET rating=?, reason=?, rated_at=?, seen_at=COALESCE(seen_at, ?) "
                    "WHERE id=? AND rating=? AND reason=?",
                    (rating, reason, now, now, card["id"], card["rating"], card["reason"])).rowcount)
                if rated and counts and (rating != card["rating"] or not counted):
                    _put_signal(conn, card["id"], prefs.THUMB, at=now, useful=1.0 if rating == "up" else 0.0,
                                weight=1.0)
                elif rated and not counts and counted:
                    conn.execute("DELETE FROM signals WHERE card_id=? AND what=?", (card["id"], prefs.THUMB))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return rated

    def add_lesson(self, row: dict[str, Any], *, unless: Sequence[str] = (), limit: int = 0,
                   changes_from: Optional[str] = None) -> Optional[int]:
        """Adds a lesson; with ``limit``, only while fewer than that many in
        force match its kind, what and the ``unless`` columns; with
        ``changes_from`` (the value when none is in force), only when the
        newest in force of its kind and what has another value. All are
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
                if current == row["value"]:
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

    # The report's candidate list (pulse_candidates) ---------------------------

    def add_candidates(self, run_id: str, rows: Sequence[dict[str, Any]], *, now: float) -> list[int]:
        """Journals a run's candidates; their ids, in order."""
        for row in rows:
            _check_columns(row, CANDIDATE_COLUMNS)
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = _insert_candidates(conn, run_id, rows, now)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return ids

    def finish_run(self, key: str, run: dict, rows: Sequence[dict[str, Any]], done: dict, *, now: float) -> bool:
        """Journals a run's candidates and sets ``key`` to ``done``, in one
        write and only while ``key`` still holds ``run``: a run given up on
        meanwhile journals nothing."""
        for row in rows:
            _check_columns(row, CANDIDATE_COLUMNS)
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                found = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
                try:
                    current = json.loads(found["value_json"]) if found else None
                except ValueError:
                    current = None
                if current != run:
                    conn.execute("ROLLBACK")
                    return False
                _insert_candidates(conn, run["id"], rows, now)
                conn.execute("INSERT INTO settings(key, value_json) VALUES(?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (key, json.dumps(done)))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return True

    def card_candidate(self, card_id: int) -> Optional[dict]:
        """The journaled candidate a report card came from, while the journal
        keeps it; None for one from before candidate lists, or pruned."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE card_id=? ORDER BY id DESC LIMIT 1",
                               (card_id,)).fetchone()
        return dict(row) if row else None

    def left_out(self, since: float, limit: int, on_the_way: str = "") -> list[dict]:
        """The candidates journaled from ``since`` on that didn't come up,
        newest first, each with the thing it is about (``things``) as
        ``thing``, whether that thing came up (``_came_up_rows``, with what
        report run ``on_the_way`` picked) in the same run or later as
        ``later``, and as ``card_id`` the card it was brought up as
        (``bring_up``), or another candidate about the same thing was,
        however long ago (``_brought_rows``). A card that has been pruned
        isn't named, since bringing the thing up would make another."""
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM candidates WHERE created_at >= ? AND decision!='surfaced' "
                "ORDER BY created_at DESC, id LIMIT ?", (since, limit))]
            came = _came_up_rows(conn, since, on_the_way)
            held = _brought_rows(conn)
        thing = things([*rows, *came, *held])
        came_up = _came_up(came, thing)
        kept = {row["card_id"] for row in held}
        brought: dict[Any, int] = {}
        for row in held:
            key = thing(row)
            if key is not None:
                brought.setdefault(key, row["card_id"])
        for row in rows:
            row["thing"] = thing(row)
            if row["card_id"] not in kept:
                row["card_id"] = brought.get(row["thing"])
            row["later"] = came_up.get(row["thing"], -math.inf) >= row["created_at"]
        return rows

    def latest_candidate_at(self) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(created_at) FROM candidates").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def candidate(self, candidate_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    def bring_up(self, candidate_id: int, row: dict[str, Any], since: float,
                 on_the_way: str = "") -> Optional[int]:
        """Makes a card of a candidate that didn't come up (``row``, its
        columns) and notes it on the candidate, in one write: its signals
        carry the candidate's kind, place and band. The card it already has,
        if another tap made one first (and it's still kept), or that another
        candidate about the same thing was brought up as, however long ago,
        which ``left_out`` names; None when the journal no longer has the
        candidate, or the thing came up meanwhile, read as ``left_out``
        reads it from ``since`` (and ``on_the_way``): it hides what came up,
        but a list read before still offers it."""
        _check_columns(row, CARD_COLUMNS)
        with _lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                found = conn.execute("SELECT item_id, text, created_at, card_id, "
                                     "EXISTS(SELECT 1 FROM cards WHERE cards.id=candidates.card_id) "
                                     "AS kept FROM candidates WHERE id=?", (candidate_id,)).fetchone()
                if found is None or (found["card_id"] is not None and found["kept"]):
                    conn.execute("ROLLBACK")
                    return None if found is None else int(found["card_id"])
                came_up, made = _thing_since(conn, found, since, on_the_way)
                if came_up or made is not None:
                    conn.execute("ROLLBACK")
                    return made
                card_id = int(conn.execute(f"INSERT INTO cards({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                                           tuple(row.values())).lastrowid)
                conn.execute("UPDATE candidates SET card_id=? WHERE id=?", (card_id, candidate_id))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return card_id

    def card_chat_title(self, profile: str, chat_id: str) -> str:
        """The title the newest card from this chat gave it; "" for none."""
        with self._connect() as conn:
            row = conn.execute("SELECT chat_title FROM cards WHERE profile=? AND chat_id=? AND chat_title!='' "
                               "ORDER BY id DESC LIMIT 1", (profile, chat_id)).fetchone()
        return row["chat_title"] if row else ""

    def run_candidates(self, run_id: str) -> list[dict]:
        """A run's candidates: its picks in their order, then the rest as listed."""
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM candidates WHERE run_id=? ORDER BY rank IS NULL, rank, id", (run_id,))]

    def candidate_runs(self, limit: int) -> list[str]:
        """The runs with candidates journaled, newest first."""
        with self._connect() as conn:
            return [row["run_id"] for row in conn.execute(
                "SELECT run_id FROM candidates GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?", (limit,))]

    # What the user did with cards (pulse_prefs) ------------------------------

    def signals(self, since: float = 0.0) -> list[dict]:
        """The signals from ``since`` on, oldest first."""
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM signals WHERE at >= ? ORDER BY at, card_id, what",
                                                      (since,))]

    def learned(self, now: float) -> prefs.Learned:
        """What the signals add up to at ``now`` (pulse_prefs.learn), without
        what the resets in force forgot, fitted again only once either
        changes or LEARNED_FOR_S goes by: the tick asks every half minute
        while a follow-up waits to go out."""
        since = now - prefs.KEEP_S
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM signals WHERE at >= ? ORDER BY at, card_id, what",
                                                      (since,))]
            resets = tuple((row["kind"], row["value"], float(row["created_at"])) for row in conn.execute(
                "SELECT kind, value, created_at FROM lessons WHERE what=? AND undone_at IS NULL ORDER BY id",
                (RESET,)))
        if not rows and not resets:
            # Nothing to fit and nothing to keep. A host that never turned
            # Pulse on reads through a scratch store with a path of its
            # own, which would leave the cache an entry heavier per read.
            return prefs.learn(rows, now, resets)
        # From the rows just read, not read again: the fit and what the
        # cache says it was fitted from are then the same signals.
        totals = (len(rows), sum(row["at"] or 0 for row in rows), sum(row["useful"] or 0 for row in rows),
                  sum(row["weight"] or 0 for row in rows))
        kept = _learned.get(str(self.path))
        if kept is not None and kept[0] == (totals, resets) and 0 <= now - kept[1] < LEARNED_FOR_S:
            return kept[2]
        fitted = prefs.learn(rows, now, resets)
        _learned[str(self.path)] = ((totals, resets), now, fitted)
        return fitted

    def prune_signals(self, before: float) -> int:
        with _lock, self._connect() as conn:
            return conn.execute("DELETE FROM signals WHERE at < ?", (before,)).rowcount

    def prune_candidates(self, before: float) -> int:
        """Drops the candidates journaled before ``before``; how many went."""
        with _lock, self._connect() as conn:
            return conn.execute("DELETE FROM candidates WHERE created_at < ?", (before,)).rowcount

    def item_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT kind, COUNT(*) AS n FROM items WHERE status='open' AND superseded_at IS NULL "
                                "GROUP BY kind").fetchall()
        return {row["kind"]: row["n"] for row in rows}


def _signal_attrs(conn: sqlite3.Connection, card_id: int) -> Optional[dict[str, Any]]:
    """What a card's signals carry: its kind, place and band as it came up
    (from its candidate when the journal still has it, else as the card's
    earlier signals had them, else as the card kept them), and when it was
    pushed, if it was. None for a card that takes none (a report or brief as
    a whole)."""
    card = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if card is None or card["kind"] not in ("followup", NOTICED):
        return None
    pushed = float(card["created_at"]) if card["level"] == "push" else None
    found = conn.execute("SELECT * FROM candidates WHERE card_id=? ORDER BY id DESC LIMIT 1", (card_id,)).fetchone()
    if found is not None:
        return {"kind": found["kind"], "pushed": pushed,
                "place": found["place"] or prefs.place(found["profile"], found["chat_id"], found["area"], found["source"]),
                "band": found["band"] or prefs.band(found["due_at"], float(found["created_at"]))}
    # The journal keeps thirty days: a Done weeks after a thumb counts for what the thumb did.
    kept = conn.execute("SELECT kind, place, band FROM signals WHERE card_id=? LIMIT 1", (card_id,)).fetchone()
    if kept is not None:
        return {"kind": kept["kind"], "place": kept["place"], "band": kept["band"], "pushed": pushed}
    if card["band"]:
        # Kept on the card as it came up (0.84.0), for a first signal after the journal let its candidate go.
        return {"kind": "followup" if card["kind"] == "followup" else card["topic"] or "other",
                "place": card["place"], "band": card["band"], "pushed": pushed}
    item = conn.execute("SELECT due_at FROM items WHERE id=?", (card["item_id"],)).fetchone() \
        if card["item_id"] is not None else None
    band = prefs.band(item["due_at"], float(card["created_at"])) if item is not None else ""
    if card["kind"] == "followup":
        return {"kind": "followup", "place": prefs.place(card["profile"], card["chat_id"]), "band": band,
                "pushed": pushed}
    # A report item whose candidate the journal no longer has, or never had: its chat when it's about
    # something remembered or was found in one (it points there, with no message of Pulse's turn).
    in_chat = card["item_id"] is not None or bool(card["chat_id"]) and card["message_id"] is None
    return {"kind": card["topic"] or "other", "band": band, "pushed": pushed,
            "place": prefs.place(card["profile"], card["chat_id"]) if in_chat else ""}


def _put_signal(conn: sqlite3.Connection, card_id: int, what: str, *, at: float, useful: float,
                weight: float) -> None:
    """Keeps a card's signal of one kind: a thumb or Done replaces the one
    before; Do it keeps the furthest it got and when it was first done.
    Nothing for a card that takes none."""
    attrs = _signal_attrs(conn, card_id)
    if attrs is None:
        return
    keep = ("at=MIN(at, excluded.at), weight=MAX(weight, excluded.weight)" if what == prefs.ACT
            else "at=excluded.at, useful=excluded.useful, weight=excluded.weight")
    conn.execute("INSERT INTO signals(card_id, what, at, useful, weight, kind, place, band, pushed) "
                 f"VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(card_id, what) DO UPDATE SET {keep}",
                 (card_id, what, at, useful, weight, attrs["kind"], attrs["place"], attrs["band"], attrs["pushed"]))


def _signal_gaps(conn: sqlite3.Connection, since: float) -> dict[str, list[sqlite3.Row]]:
    """Where the signals of the cards the store still has differ from what
    the cards say, from ``since`` on: a store from before signals were kept,
    or a process from before still running, writes ratings, Do its, Dones
    and Undos without them. "thumb": cards rated without their thumb (or
    with the other one), but for "wrong time", which keeps none; "act": Do
    its without theirs; "done": the user's Dones without theirs; "undone":
    Done signals whose Done was undone; "unmarked": Do its kept as done,
    though the mark Pulse made of it was undone. Only cards that take
    signals (follow-ups and report items)."""
    kinds = ("followup", NOTICED)
    return {
        "thumb": conn.execute(
            "SELECT c.id, c.rating, COALESCE(c.rated_at, c.created_at) AS at FROM cards c "
            "LEFT JOIN signals s ON s.card_id=c.id AND s.what=? WHERE c.kind IN (?, ?) "
            "AND c.rating IN ('up', 'down') AND c.reason != ? AND COALESCE(c.rated_at, c.created_at) >= ? "
            "AND (s.card_id IS NULL OR s.useful != (CASE c.rating WHEN 'up' THEN 1.0 ELSE 0.0 END))",
            (prefs.THUMB, *kinds, WRONG_TIME, since)).fetchall(),
        "act": conn.execute(
            "SELECT a.* FROM card_acts a JOIN cards c ON c.id=a.card_id "
            "LEFT JOIN signals s ON s.card_id=a.card_id AND s.what=? "
            "WHERE c.kind IN (?, ?) AND a.acted_at >= ? AND s.card_id IS NULL",
            (prefs.ACT, *kinds, since)).fetchall(),
        "done": conn.execute(
            "SELECT d.card_id, d.done_at FROM card_done d JOIN cards c ON c.id=d.card_id "
            "LEFT JOIN signals s ON s.card_id=d.card_id AND s.what=? "
            "WHERE d.done_by=? AND c.kind IN (?, ?) AND d.done_at >= ? AND s.card_id IS NULL",
            (prefs.DONE, USER, *kinds, since)).fetchall(),
        "undone": conn.execute(
            "SELECT s.card_id FROM signals s JOIN cards c ON c.id=s.card_id WHERE s.what=? "
            "AND NOT EXISTS (SELECT 1 FROM card_done d WHERE d.card_id=s.card_id AND d.done_by=?)",
            (prefs.DONE, USER)).fetchall(),
        "unmarked": conn.execute(
            "SELECT s.card_id FROM signals s JOIN card_acts a ON a.card_id=s.card_id "
            "WHERE s.what=? AND s.weight > ? AND a.outcome='done' "
            "AND NOT EXISTS (SELECT 1 FROM card_done d WHERE d.card_id=s.card_id)",
            (prefs.ACT, prefs.ACT_SENT)).fetchall(),
    }


def _sync_signals(conn: sqlite3.Connection, gaps: dict[str, list[sqlite3.Row]]) -> None:
    """Closes ``_signal_gaps``: a rating, Do it or Done without its signal
    gets it, as it would have then; an undone Done's signal goes, and a Do
    it Pulse saw done whose mark was undone goes back to something sent, as
    Undo does now (``unmark_done``). A Do it counts by how far its check got:
    done while Pulse's mark of it stands, something sent once a check ran or
    the chat kept going, else opened."""
    for card in gaps["thumb"]:
        _put_signal(conn, card["id"], prefs.THUMB, at=float(card["at"]),
                    useful=1.0 if card["rating"] == "up" else 0.0, weight=1.0)
    marked = {row["card_id"] for row in conn.execute("SELECT card_id FROM card_done WHERE done_by != ?", (USER,))}
    for act in gaps["act"]:
        stage = (prefs.ACT_DONE if act["outcome"] == "done" and act["card_id"] in marked
                 else prefs.ACT_SENT if act["checks"] or act["why"] == "the chat kept going" else prefs.ACT_OPENED)
        _put_signal(conn, act["card_id"], prefs.ACT, at=float(act["acted_at"]), useful=1.0, weight=stage)
    for done in gaps["done"]:
        _put_signal(conn, done["card_id"], prefs.DONE, at=float(done["done_at"]), useful=1.0,
                    weight=prefs.DONE_WEIGHT)
    for row in gaps["undone"]:
        conn.execute("DELETE FROM signals WHERE card_id=? AND what=?", (row["card_id"], prefs.DONE))
    for row in gaps["unmarked"]:
        conn.execute("UPDATE signals SET weight=? WHERE card_id=? AND what=?",
                     (prefs.ACT_SENT, row["card_id"], prefs.ACT))


def same_text(text: str) -> str:
    """A candidate's text spaced and cased alike, so the same thing found
    twice reads as one."""
    return " ".join(text.split()).casefold()


def same_thing(row: Any) -> list[tuple[str, Any]]:
    """What a candidate is about: its remembered item, and its text."""
    keys: list[tuple[str, Any]] = [("item", row["item_id"])] if row["item_id"] is not None else []
    text = same_text(row["text"] or "")
    return keys + [("text", text)] if text else keys


def things(rows: Sequence[Any]) -> Callable[[Any], Optional[tuple[str, Any]]]:
    """The thing each of ``rows`` is about, as a key its rows share (None for
    a row with neither item nor text): rows sharing a remembered item or a
    text (``same_thing``) are one thing, and so is every row joined to them
    that way, so an item whose words changed between reports, and words a
    report found on their own, are still the one thing."""
    parent: dict[tuple[str, Any], tuple[str, Any]] = {}

    def root(key: tuple[str, Any]) -> tuple[str, Any]:
        while parent.setdefault(key, key) != key:
            parent[key] = key = parent[parent[key]]
        return key

    for row in rows:
        keys = same_thing(row)
        for key in keys[1:]:
            parent[root(key)] = root(keys[0])

    def thing(row: Any) -> Optional[tuple[str, Any]]:
        keys = same_thing(row)
        return root(keys[0]) if keys else None
    return thing


def _came_up(came: Sequence[Any], thing: Callable[[Any], Optional[tuple[str, Any]]]) -> dict[Any, float]:
    """When each thing last came up, by what came up (``_came_up_rows``)."""
    came_up: dict[Any, float] = {}
    for row in came:
        key = thing(row)
        if key is not None:
            came_up[key] = max(came_up.get(key, row["created_at"]), row["created_at"])
    return came_up


def _came_up_rows(conn: sqlite3.Connection, since: float, on_the_way: str) -> list[Any]:
    """What came up from ``since`` on, as rows ``things`` reads: the
    candidates reports brought up as cards, and those report run
    ``on_the_way`` picked, whose cards are on their way (what a report that
    was dropped, or that Hermes refused, picked never came up), and the
    cards no candidate made (a report sent without its candidate list, a
    follow-up), by their item and words."""
    return [*conn.execute("SELECT item_id, text, created_at FROM candidates WHERE created_at >= ? "
                          "AND decision='surfaced' AND (card_id IS NOT NULL OR run_id=?)", (since, on_the_way)),
            *conn.execute("SELECT item_id, news AS text, created_at FROM cards WHERE created_at >= ? AND id NOT IN "
                          "(SELECT card_id FROM candidates WHERE card_id IS NOT NULL)", (since,))]


def _brought_rows(conn: sqlite3.Connection) -> list[Any]:
    """The candidates brought up (``bring_up``) as a card Pulse still keeps,
    newest first, as far back as the journal goes: a thing brought up from
    an older report's list is still that card when the latest leaves it out
    again."""
    return conn.execute("SELECT item_id, text, card_id FROM candidates WHERE decision!='surfaced' "
                        "AND card_id IN (SELECT id FROM cards) ORDER BY created_at DESC, id").fetchall()


def _thing_since(conn: sqlite3.Connection, found: Any, since: float,
                 on_the_way: str) -> tuple[bool, Optional[int]]:
    """For the thing candidate ``found`` is about, read from ``since`` (or
    from ``found``, if older) as ``left_out`` reads it: whether it came up in
    ``found``'s own run or later, which ``left_out`` leaves out as ``later``,
    and the kept card another candidate about it was brought up as, however
    long ago."""
    since = min(since, found["created_at"])
    rows = conn.execute("SELECT item_id, text FROM candidates WHERE created_at >= ? AND decision!='surfaced'",
                        (since,)).fetchall()
    came = _came_up_rows(conn, since, on_the_way)
    held = _brought_rows(conn)
    thing = things([*rows, *came, *held, found])
    key = thing(found)
    if key is None:
        return False, None
    if _came_up(came, thing).get(key, -math.inf) >= found["created_at"]:
        return True, None
    return False, next((int(row["card_id"]) for row in held if thing(row) == key), None)


def _insert_candidates(conn: sqlite3.Connection, run_id: str, rows: Sequence[dict[str, Any]], now: float) -> list[int]:
    return [int(conn.execute(f"INSERT INTO candidates(run_id, created_at, {', '.join(row)}) "
                             f"VALUES(?, ?, {', '.join('?' * len(row))})", (run_id, now, *row.values())).lastrowid)
            for row in rows]


def _current(conn: sqlite3.Connection, item_id: int, profile: str, *, reminded: bool = False) -> Optional[int]:
    """The item still standing for ``item_id``: itself, or what replaced
    it; None once it's closed (or isn't this profile's). With ``reminded``,
    a commitment whose reminder went out still stands: a chat can close it."""
    standing = ("open", "reminded") if reminded else ("open",)
    for _ in range(MAX_CHAIN):
        row = conn.execute("SELECT status, superseded_by FROM items WHERE id=? AND profile=?",
                           (item_id, profile)).fetchone()
        if row is None or row["status"] not in standing:
            return None
        if row["superseded_by"] is None:
            return item_id
        item_id = row["superseded_by"]
    return None


def _prior(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    """What an item and its checks are now, for a done mark's Undo."""
    item = conn.execute("SELECT status, closed_by, closed_at FROM items WHERE id=?", (item_id,)).fetchone()
    check = conn.execute("SELECT decision, next_check_at FROM followups WHERE item_id=?", (item_id,)).fetchone()
    return {**(dict(item) if item else {}), **(dict(check) if check else {})}


def _mark_cards_done(conn: sqlite3.Connection, item_id: int, *, prior: dict[str, Any], now: float) -> list[int]:
    """Pulse's done mark, as mark_done makes it, on the cards Done takes
    (follow-ups and report items) about ``item_id`` or an item it replaced
    that aren't marked yet, each keeping ``prior`` for Undo; their ids."""
    ids = [row["id"] for row in conn.execute(
        "WITH RECURSIVE chain(id) AS (SELECT ? UNION SELECT items.id FROM items JOIN chain "
        "ON items.superseded_by=chain.id) SELECT id FROM cards WHERE item_id IN (SELECT id FROM chain) "
        "AND kind IN ('followup', ?) AND id NOT IN (SELECT card_id FROM card_done) ORDER BY id",
        (item_id, NOTICED))]
    for card_id in ids:
        conn.execute("INSERT INTO card_done(card_id, done_at, done_by, prior_json) VALUES(?,?,?,?)",
                     (card_id, now, PULSE, json.dumps(prior)))
        conn.execute("UPDATE card_acts SET check_at=NULL WHERE card_id=?", (card_id,))
        conn.execute("UPDATE cards SET seen_at=COALESCE(seen_at, ?) WHERE id=?", (now, card_id))
    return ids


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
