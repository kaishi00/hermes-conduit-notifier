"""The registry of a bot's work (Bot workstreams, designs/bot-mode-coordinator).

Each bot coordinates its own work: the sessions its Bot Chat started, the asks
it sent other bots and the asks other bots sent it. One SQLite file per
profile, ``<profile home>/conduit/workstreams.db``, holds them. Only this
plugin writes it, from that profile's copy (its tools and hooks) and from the
launch profile's (the dashboard routes), so every write is a transaction and
nothing relies on a lock in one copy's memory.

Hermes keeps the sessions themselves; a row only says whose work a session
is, what it was asked to do and how it went. The model names work by ref (W1, W2 for workstreams, A1, A2 for asks), and a ref
is looked up in this bot's own file only.

Three tables beside ``work``: ``steps`` journals every tool call a workstream
starts and ends (what a restart reads to tell safe work from uncertain work),
``outbox`` keeps every turn the broker sends until Hermes takes it, and
``meta`` the bot's settings and ref counters.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

DIRECTORY = "conduit"
FILE_NAME = "workstreams.db"
SCHEMA_VERSION = 1

# What a Bot Chat may do itself (designs/bot-mode-coordinator C.4): "off" is
# today's Bot Chat; "strict" keeps only the work tools, clarify and memory;
# "lookups" adds read-only lookups.
POLICIES = ("off", "strict", "lookups")
DEFAULT_POLICY = "off"
DEFAULT_MAX_RUNNING = 2
MAX_RUNNING_LIMIT = 6

KINDS = ("workstream", "ask_out", "ask_in")
# "needs_you": waiting on an approval or a question from the user;
# "waiting": an ask_out whose answer hasn't come; "interrupted": a restart
# stopped it mid-turn.
STATUSES = ("starting", "running", "needs_you", "waiting", "done", "failed", "stopped", "interrupted")
ACTIVE = ("starting", "running", "needs_you", "waiting")
RESULT_CHARS = 2000
TITLE_CHARS = 80
GOAL_CHARS = 1000
STEP_SUMMARY_CHARS = 200
REF = re.compile(r"^([WA])(\d{1,6})$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work (
    ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    coordinator TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '{}',
    executor TEXT NOT NULL DEFAULT '{}',
    reply_to TEXT NOT NULL DEFAULT '{}',
    ask_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    last_result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    seen_at REAL NOT NULL DEFAULT 0,
    closed_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS work_session ON work (session_id);
CREATE INDEX IF NOT EXISTS work_ask ON work (ask_id);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS steps_open ON steps (ref, ended_at);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    accepted_at REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT ''
);
"""

_JSON_FIELDS = ("requested_by", "executor", "reply_to")
# A workstream holding one of its bot's running slots: closed or not, its
# session is still at work.
_RUNNING = "kind = 'workstream' AND status IN ('starting', 'running', 'needs_you')"


@dataclass
class Work:
    ref: str
    kind: str
    status: str
    title: str = ""
    goal: str = ""
    session_id: str = ""
    coordinator: str = ""
    requested_by: dict = field(default_factory=dict)
    executor: dict = field(default_factory=dict)
    reply_to: dict = field(default_factory=dict)
    ask_id: str = ""
    idempotency_key: str = ""
    last_result: str = ""
    error: str = ""
    cwd: str = ""
    repo: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    seen_at: float = 0.0
    closed_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE and not self.closed_at

    @property
    def changed_since_seen(self) -> bool:
        return self.updated_at > self.seen_at


def path_for(home: Path) -> Path:
    return Path(home) / DIRECTORY / FILE_NAME


def exists(home: Path) -> bool:
    return path_for(home).is_file()


class Store:
    """One bot's registry. Cheap to make: each call opens its own connection,
    so a Store can be shared between threads and profile copies."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.path = path_for(self.home)

    @contextmanager
    def _db(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        """A transaction: a write takes the lock up front; a read (the hooks
        read on every tool call) never waits on one under WAL."""
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=10000")
            if write:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
            else:
                con.execute("PRAGMA query_only=1")
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    # Settings

    def get_meta(self, key: str, default: str = "") -> str:
        if not self.path.is_file():
            return default
        with self._db(write=False) as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._db() as con:
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                        (key, value, value))

    def policy(self) -> str:
        value = self.get_meta("policy", DEFAULT_POLICY)
        return value if value in POLICIES else DEFAULT_POLICY

    def set_policy(self, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
        self.set_meta("policy", policy)

    def max_running(self) -> int:
        try:
            value = int(self.get_meta("max_running", str(DEFAULT_MAX_RUNNING)))
        except ValueError:
            return DEFAULT_MAX_RUNNING
        return min(max(value, 1), MAX_RUNNING_LIMIT)

    def set_max_running(self, value: int) -> None:
        self.set_meta("max_running", str(min(max(int(value), 1), MAX_RUNNING_LIMIT)))

    # Work

    def add(self, kind: str, *, title: str, goal: str = "", coordinator: str = "", requested_by: Optional[dict] = None,
            executor: Optional[dict] = None, reply_to: Optional[dict] = None, ask_id: str = "", cwd: str = "",
            repo: str = "", status: str = "starting", running_limit: Optional[int] = None) -> Optional[Work]:
        """A new row under the next ref of its kind, with a fresh idempotency
        key for the session it will start (Hermes answers a repeated create
        under it with the same session; a new start is a new row and key).
        With ``running_limit``, None instead when that many
        workstreams are running already, counted in the same transaction."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        letter = "W" if kind == "workstream" else "A"
        now = time.time()
        with self._db() as con:
            if running_limit is not None:
                running = con.execute(f"SELECT COUNT(*) AS n FROM work WHERE {_RUNNING}").fetchone()["n"]
                if running >= running_limit:
                    return None
            counter = f"next_{letter}"
            row = con.execute("SELECT value FROM meta WHERE key = ?", (counter,)).fetchone()
            number = int(row["value"]) if row else 1
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                        (counter, str(number + 1), str(number + 1)))
            work = Work(ref=f"{letter}{number}", kind=kind, status=status, title=_clip(title, TITLE_CHARS),
                        goal=_clip(goal, GOAL_CHARS), coordinator=coordinator, requested_by=requested_by or {},
                        executor=executor or {}, reply_to=reply_to or {}, ask_id=ask_id,
                        idempotency_key=f"conduit-work-{uuid.uuid4().hex}", cwd=cwd, repo=repo, created_at=now,
                        updated_at=now, seen_at=now)
            con.execute(
                "INSERT INTO work (ref, kind, session_id, coordinator, requested_by, executor, reply_to, ask_id, "
                "idempotency_key, title, goal, status, last_result, error, cwd, repo, created_at, updated_at, "
                "seen_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (work.ref, work.kind, work.session_id, work.coordinator, json.dumps(work.requested_by),
                 json.dumps(work.executor), json.dumps(work.reply_to), work.ask_id, work.idempotency_key, work.title,
                 work.goal, work.status, "", "", work.cwd, work.repo, now, now, now, 0.0))
        return work

    def get(self, ref: Any) -> Optional[Work]:
        """The row a ref names in this bot's registry, else None. Anything
        that isn't a ref (a session id, another bot's name) names nothing."""
        ref = normalize_ref(ref)
        if not ref or not self.path.is_file():
            return None
        with self._db(write=False) as con:
            row = con.execute("SELECT * FROM work WHERE ref = ?", (ref,)).fetchone()
        return _work(row) if row else None

    def by_session(self, session_id: Any) -> Optional[Work]:
        """The workstream a session is, else None."""
        if not isinstance(session_id, str) or not session_id or not self.path.is_file():
            return None
        with self._db(write=False) as con:
            row = con.execute("SELECT * FROM work WHERE session_id = ? AND kind = 'workstream'",
                              (session_id,)).fetchone()
        return _work(row) if row else None

    def list(self, *, active_only: bool = False, include_closed: bool = False, limit: int = 50) -> list[Work]:
        if not self.path.is_file():
            return []
        query = "SELECT * FROM work"
        where = []
        if active_only:
            where.append(f"status IN ({', '.join('?' * len(ACTIVE))}) AND closed_at = 0")
        elif not include_closed:
            where.append("closed_at = 0")
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC LIMIT ?"
        params: tuple = (*ACTIVE, limit) if active_only else (limit,)
        with self._db(write=False) as con:
            rows = con.execute(query, params).fetchall()
        return [_work(row) for row in rows]

    def running_count(self) -> int:
        if not self.path.is_file():
            return 0
        with self._db(write=False) as con:
            row = con.execute(f"SELECT COUNT(*) AS n FROM work WHERE {_RUNNING}").fetchone()
        return int(row["n"])

    def update(self, ref: str, *, only_if: Optional[tuple[str, ...]] = None, **fields: Any) -> Optional[Work]:
        """Sets fields on a row and stamps it changed (unless only ``seen_at``
        moves). Unknown fields raise. With ``only_if``, only while the row's
        status is one of those (a turn's end may have settled it meanwhile)."""
        allowed = {"session_id", "status", "last_result", "error", "title", "goal", "cwd", "repo", "seen_at",
                   "closed_at", *_JSON_FIELDS}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']}")
        if "last_result" in fields:
            fields["last_result"] = _clip(fields["last_result"], RESULT_CHARS)
        if "title" in fields:
            fields["title"] = _clip(fields["title"], TITLE_CHARS)
        values = {key: (json.dumps(value) if key in _JSON_FIELDS else value) for key, value in fields.items()}
        if set(fields) != {"seen_at"}:
            values["updated_at"] = time.time()
        if not values:
            return self.get(ref)
        with self._db() as con:
            where, args = "ref = ?", [ref]
            if only_if is not None:
                where += f" AND status IN ({', '.join('?' for _ in only_if)})"
                args.extend(only_if)
            con.execute(f"UPDATE work SET {', '.join(f'{key} = ?' for key in values)} WHERE {where}",
                        (*values.values(), *args))
            row = con.execute("SELECT * FROM work WHERE ref = ?", (ref,)).fetchone()
        return _work(row) if row else None

    def mark_seen(self, refs: list[str]) -> None:
        if not refs or not self.path.is_file():
            return
        now = time.time()
        with self._db() as con:
            con.executemany("UPDATE work SET seen_at = ? WHERE ref = ?", [(now, ref) for ref in refs])

    def interrupt_running(self, live_session_ids: set[str]) -> list[str]:
        """Rows that say they're running on a session this process doesn't
        have live: a restart stopped them. Marked interrupted (never
        continued here: that's the restart sweep's call), their refs back."""
        if not self.path.is_file():
            return []
        with self._db() as con:
            rows = con.execute("SELECT ref, session_id FROM work WHERE kind = 'workstream' AND closed_at = 0 AND "
                               "status IN ('starting', 'running', 'needs_you')").fetchall()
            stopped = [row["ref"] for row in rows if row["session_id"] not in live_session_ids]
            now = time.time()
            con.executemany("UPDATE work SET status = 'interrupted', updated_at = ? WHERE ref = ?",
                            [(now, ref) for ref in stopped])
        return stopped

    # The step journal

    def step_started(self, ref: str, session_id: str, tool: str, *, turn_id: str = "", tool_call_id: str = "",
                     summary: str = "") -> int:
        with self._db() as con:
            cursor = con.execute(
                "INSERT INTO steps (ref, session_id, turn_id, tool_call_id, tool, summary, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ref, session_id, turn_id, tool_call_id, tool, _clip(summary, STEP_SUMMARY_CHARS), time.time()))
            return int(cursor.lastrowid)

    def step_ended(self, ref: str, *, tool: str, tool_call_id: str = "", outcome: str = "ok") -> bool:
        """Closes the step: by its call id, else the oldest open step of that
        tool. False when no open step matched."""
        with self._db() as con:
            row = None
            if tool_call_id:
                row = con.execute("SELECT id FROM steps WHERE ref = ? AND tool_call_id = ? AND ended_at = 0 "
                                  "ORDER BY id LIMIT 1", (ref, tool_call_id)).fetchone()
            if row is None:
                row = con.execute("SELECT id FROM steps WHERE ref = ? AND tool = ? AND ended_at = 0 "
                                  "ORDER BY id LIMIT 1", (ref, tool)).fetchone()
            if row is None:
                return False
            con.execute("UPDATE steps SET ended_at = ?, outcome = ? WHERE id = ?", (time.time(), outcome, row["id"]))
        return True

    def steps(self, ref: str, *, open_only: bool = False, limit: int = 50) -> list[dict]:
        if not self.path.is_file():
            return []
        query = "SELECT * FROM steps WHERE ref = ?" + (" AND ended_at = 0" if open_only else "")
        with self._db(write=False) as con:
            rows = con.execute(query + " ORDER BY id DESC LIMIT ?", (ref, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    # The outbox

    def outbox_add(self, ref: str, kind: str, target: str, text: str) -> str:
        """Written before Hermes is asked, so a send a restart cut off can be
        found and checked against the target's transcript."""
        item = f"out-{uuid.uuid4().hex[:16]}"
        with self._db() as con:
            con.execute("INSERT INTO outbox (id, ref, kind, target, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (item, ref, kind, target, text, time.time()))
        return item

    def outbox_done(self, item: str, outcome: str) -> None:
        with self._db() as con:
            con.execute("UPDATE outbox SET accepted_at = ?, outcome = ? WHERE id = ?",
                        (time.time() if outcome in ("accepted", "queued") else 0.0, outcome, item))

    def outbox_pending(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with self._db(write=False) as con:
            rows = con.execute("SELECT * FROM outbox WHERE accepted_at = 0 ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]


def normalize_ref(value: Any) -> str:
    """"w3", " W3 " -> "W3"; anything else -> ""."""
    if not isinstance(value, str):
        return ""
    text = value.strip().upper()
    return text if REF.match(text) else ""


def _work(row: sqlite3.Row) -> Work:
    data = dict(row)
    for key in _JSON_FIELDS:
        try:
            value = json.loads(data.get(key) or "{}")
        except ValueError:
            value = {}
        data[key] = value if isinstance(value, dict) else {}
    return Work(**data)


def _clip(value: Any, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) and limit <= TITLE_CHARS else (
        value.strip() if isinstance(value, str) else "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
