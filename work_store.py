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
# On for every bot until the user turns it off (C.4, M3): strict, as M0 picked.
DEFAULT_POLICY = "strict"
DEFAULT_MAX_RUNNING = 2
# What a restart does with a workstream it stopped that had nothing
# uncertain in flight (C.7): continue it once, or only tell the coordinator.
AFTER_RESTART = ("continue", "ask")
DEFAULT_AFTER_RESTART = "continue"
# What counts as the user's go-ahead in the bot's workstreams (C.11, M3):
# "chats" follows the bot's approval mode in Hermes, "ask" holds them to the
# user's own words whatever Hermes does, "off" lets them run without asking.
WORKSTREAM_APPROVALS = ("chats", "ask", "off")
DEFAULT_WORKSTREAM_APPROVALS = "chats"
MAX_RUNNING_LIMIT = 6

KINDS = ("workstream", "ask_out", "ask_in")
# "needs_you": waiting on an approval or a question from the user;
# "waiting": an ask_out whose answer hasn't come; "ready": finished with
# something for the user to look at (a draft, a diff); "interrupted": a
# restart stopped it mid-turn.
STATUSES = ("starting", "running", "needs_you", "waiting", "ready", "done", "failed", "stopped", "interrupted")
# "ready" stays active: it waits on the user to look, until it's closed.
ACTIVE = ("starting", "running", "needs_you", "waiting", "ready")
RESULT_CHARS = 2000
TITLE_CHARS = 80
GOAL_CHARS = 2000
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
    closed_at REAL NOT NULL DEFAULT 0,
    progress TEXT NOT NULL DEFAULT '{}'
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

_JSON_FIELDS = ("requested_by", "executor", "reply_to", "progress")
# Columns added after a registry's first version: (name, definition).
_ADDED_COLUMNS = (("progress", "TEXT NOT NULL DEFAULT '{}'"), ("turn_at", "REAL NOT NULL DEFAULT 0"),
                  ("pinned_at", "REAL NOT NULL DEFAULT 0"), ("resolved_by", "TEXT NOT NULL DEFAULT ''"),
                  ("reopened_at", "REAL NOT NULL DEFAULT 0"))
# A workstream holding one of its bot's running slots: closed or not, its
# session is still at work.
# Who can start a job of their own that's kept here too (M5): a call's
# job runs as the call says, never under the coordinator's limit.
CALL_VIA = ("phone", "watch")
_FROM_CALL = "COALESCE(json_extract(requested_by, '$.by'), '') IN ('phone', 'watch')"
_RUNNING = f"kind = 'workstream' AND status IN ('starting', 'running', 'needs_you') AND NOT ({_FROM_CALL})"


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
    # The worker's own checklist (work_progress): {"steps": [{"step", "state"}],
    # "ready": bool}.
    progress: dict = field(default_factory=dict)
    # When the workstream's latest turn started: a restart reads the steps
    # since then as the turn it stopped.
    turn_at: float = 0.0
    # The user pinned it in Conduit's Work panel (0: not pinned).
    pinned_at: float = 0.0
    # Who resolved it (closed it): "user" from the Work panel, "bot" with
    # work_close; empty while it's open.
    resolved_by: str = ""
    # When the user last reopened it from the Work panel's Resolved list.
    reopened_at: float = 0.0

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
                if write:
                    _add_columns(con)  # under the write lock: two first writers can't both add it
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

    def meta_keys(self, prefix: str) -> list[str]:
        """The keys that start with ``prefix`` and hold a value."""
        if not self.path.is_file():
            return []
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._db(write=False) as con:
            rows = con.execute("SELECT key FROM meta WHERE key LIKE ? ESCAPE '\\' AND value != ''",
                               (escaped + "%",)).fetchall()
        return [row["key"] for row in rows]

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

    def after_restart(self) -> str:
        value = self.get_meta("after_restart", DEFAULT_AFTER_RESTART)
        return value if value in AFTER_RESTART else DEFAULT_AFTER_RESTART

    def set_after_restart(self, value: str) -> None:
        if value not in AFTER_RESTART:
            raise ValueError(f"after_restart must be one of {', '.join(AFTER_RESTART)}")
        self.set_meta("after_restart", value)

    def workstream_approvals(self) -> str:
        value = self.get_meta("workstream_approvals", DEFAULT_WORKSTREAM_APPROVALS)
        return value if value in WORKSTREAM_APPROVALS else DEFAULT_WORKSTREAM_APPROVALS

    def set_workstream_approvals(self, value: str) -> None:
        if value not in WORKSTREAM_APPROVALS:
            raise ValueError(f"workstream_approvals must be one of {', '.join(WORKSTREAM_APPROVALS)}")
        self.set_meta("workstream_approvals", value)

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

    def list(self, *, active_only: bool = False, include_closed: bool = False, limit: int = 50,
             active_or_changed: bool = False, changed_since: float | None = None,
             by_change: bool = False) -> list[Work]:
        """Newest first (``by_change``: last changed first). A ``limit`` of
        -1 takes every row. ``active_or_changed``: open rows that are active
        or changed since the coordinator last saw them, chosen before the
        limit. ``changed_since``: only rows changed at or after that time,
        also chosen before the limit."""
        if not self.path.is_file():
            return []
        query = "SELECT * FROM work"
        where = []
        if active_only:
            where.append(f"status IN ({', '.join('?' * len(ACTIVE))}) AND closed_at = 0")
        elif active_or_changed:
            where.append(f"closed_at = 0 AND (status IN ({', '.join('?' * len(ACTIVE))}) OR updated_at > seen_at)")
        elif not include_closed:
            where.append("closed_at = 0")
        if changed_since is not None:
            where.append("updated_at >= ?")
        if where:
            query += " WHERE " + " AND ".join(where)
        order = "updated_at DESC, created_at DESC" if by_change else "created_at DESC"
        if active_or_changed:
            # Active work first, so newer changes never push it out.
            order = f"(status IN ({', '.join('?' * len(ACTIVE))})) DESC, " + order
        query += f" ORDER BY {order} LIMIT ?"
        since = () if changed_since is None else (changed_since,)
        params: tuple = ((*ACTIVE, *since, *ACTIVE, limit) if active_or_changed else (*ACTIVE, *since, limit)
                         if active_only else (*since, limit))
        with self._db(write=False) as con:
            rows = con.execute(query, params).fetchall()
        return [_work(row) for row in rows]

    def resolved(self, *, before: float | None = None, limit: int = 50) -> list[Work]:
        """Closed rows, last closed first; ``before``: only those closed
        before that time (the next page)."""
        if not self.path.is_file():
            return []
        query = "SELECT * FROM work WHERE closed_at > 0"
        params: tuple = ()
        if before is not None:
            query += " AND closed_at < ?"
            params = (before,)
        query += " ORDER BY closed_at DESC, created_at DESC LIMIT ?"
        with self._db(write=False) as con:
            rows = con.execute(query, (*params, limit)).fetchall()
        return [_work(row) for row in rows]

    def session_ids(self) -> list[str]:
        """Every workstream's chat, open or resolved, but a call's job's."""
        if not self.path.is_file():
            return []
        with self._db(write=False) as con:
            # A call's job stays in the chat lists, under Voice Jobs.
            rows = con.execute("SELECT session_id FROM work WHERE kind = 'workstream' AND session_id != '' "
                               f"AND NOT ({_FROM_CALL})").fetchall()
        return [row[0] for row in rows]

    def count(self, *, closed: bool) -> int:
        """How many rows are closed (``closed``) or open."""
        if not self.path.is_file():
            return 0
        with self._db(write=False) as con:
            return con.execute("SELECT COUNT(*) FROM work WHERE closed_at " + ("> 0" if closed else "= 0")).fetchone()[0]

    def by_ask(self, ask_id: Any, kind: str) -> Optional[Work]:
        """The row of this kind an ask between bots shares its id with."""
        if not isinstance(ask_id, str) or not ask_id or not self.path.is_file():
            return None
        with self._db(write=False) as con:
            row = con.execute("SELECT * FROM work WHERE ask_id = ? AND kind = ?", (ask_id, kind)).fetchone()
        return _work(row) if row else None

    def asks_since(self, kind: str, since: float) -> list[Work]:
        """Asks of this kind made at or after ``since``, newest first."""
        if not self.path.is_file():
            return []
        with self._db(write=False) as con:
            rows = con.execute("SELECT * FROM work WHERE kind = ? AND created_at >= ? ORDER BY created_at DESC",
                               (kind, since)).fetchall()
        return [_work(row) for row in rows]

    def running_count(self) -> int:
        if not self.path.is_file():
            return 0
        with self._db(write=False) as con:
            row = con.execute(f"SELECT COUNT(*) AS n FROM work WHERE {_RUNNING}").fetchone()
        return int(row["n"])

    def reopen(self, ref: str, *, running_limit: int, if_open: bool = False) -> bool:
        """An idle workstream back to running, if the bot has a slot for it,
        counted in the same transaction (as ``add`` does). ``if_open``: not
        one that's been closed (a send reopens it; recovery doesn't)."""
        with self._db() as con:
            running = con.execute(f"SELECT COUNT(*) FROM work WHERE {_RUNNING} AND ref != ?", (ref,)).fetchone()[0]
            if running >= running_limit:
                return False
            changed = con.execute("UPDATE work SET status = 'running', closed_at = 0, resolved_by = '', error = '', "
                                  "updated_at = ? "
                                  "WHERE ref = ?" + (" AND closed_at = 0" if if_open else ""),
                                  (time.time(), ref)).rowcount
        return bool(changed)

    def update(self, ref: str, *, only_if: Optional[tuple[str, ...]] = None, **fields: Any) -> Optional[Work]:
        """Sets fields on a row and stamps it changed (unless only ``seen_at``
        or ``progress`` moves). Unknown fields raise. With ``only_if``, only while the row's
        status is one of those (a turn's end may have settled it meanwhile).
        The row as written, or None when nothing was (no row, or not in
        those states)."""
        allowed = {"session_id", "status", "last_result", "error", "title", "goal", "cwd", "repo", "seen_at",
                   "closed_at", "turn_at", "pinned_at", "resolved_by", "reopened_at", *_JSON_FIELDS}
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
        # Seen marks, a worker's checklist and the user's pin aren't changes
        # the coordinator's "new since your last turn" is about.
        if set(fields) - {"seen_at", "progress", "turn_at", "pinned_at"}:
            values["updated_at"] = time.time()
        if not values:
            return self.get(ref)
        with self._db() as con:
            where, args = "ref = ?", [ref]
            if only_if is not None:
                where += f" AND status IN ({', '.join('?' for _ in only_if)})"
                args.extend(only_if)
            changed = con.execute(f"UPDATE work SET {', '.join(f'{key} = ?' for key in values)} WHERE {where}",
                                  (*values.values(), *args)).rowcount
            row = con.execute("SELECT * FROM work WHERE ref = ?", (ref,)).fetchone() if changed else None
        return _work(row) if row else None

    def mark_seen(self, refs: list[str]) -> None:
        if not refs or not self.path.is_file():
            return
        now = time.time()
        with self._db() as con:
            con.executemany("UPDATE work SET seen_at = ? WHERE ref = ?", [(now, ref) for ref in refs])

    def interrupt_running(self, live_session_ids: set[str], *, before: Optional[float] = None) -> list[str]:
        """Rows that say they're running on a session this process doesn't
        have live: a restart stopped them. Marked interrupted (never
        continued here: that's the restart sweep's call), their refs back.
        With ``before``, only rows made before then (when the live set was
        read), so a start made since isn't caught."""
        if not self.path.is_file():
            return []
        with self._db() as con:
            rows = con.execute("SELECT ref, session_id FROM work WHERE kind = 'workstream' AND closed_at = 0 AND "
                               "status IN ('starting', 'running', 'needs_you') AND created_at < ?",
                               (before if before is not None else float("inf"),)).fetchall()
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

    def step_ended(self, ref: str, *, tool: str, tool_call_id: str = "", outcome: str = "ok", prefix: str = "") -> bool:
        """Closes the step: by its call id, else the oldest open step of that
        tool (or, with a prefix, journaled under a name that starts with it).
        False when no open step matched."""
        with self._db() as con:
            row = None
            if tool_call_id:
                row = con.execute("SELECT id FROM steps WHERE ref = ? AND tool_call_id = ? AND ended_at = 0 "
                                  "ORDER BY id LIMIT 1", (ref, tool_call_id)).fetchone()
            if row is None and prefix:
                row = con.execute("SELECT id FROM steps WHERE ref = ? AND substr(tool, 1, ?) = ? AND ended_at = 0 "
                                  "ORDER BY id LIMIT 1", (ref, len(prefix), prefix)).fetchone()
            if row is None:
                row = con.execute("SELECT id FROM steps WHERE ref = ? AND tool = ? AND ended_at = 0 "
                                  "ORDER BY id LIMIT 1", (ref, tool)).fetchone()
            if row is None:
                return False
            con.execute("UPDATE steps SET ended_at = ?, outcome = ? WHERE id = ?", (time.time(), outcome, row["id"]))
        return True

    def close_open_steps(self, ref: str, outcome: str) -> int:
        """Closes every step of the row still open (a restart cut them off)."""
        if not self.path.is_file():
            return 0
        with self._db() as con:
            return con.execute("UPDATE steps SET ended_at = ?, outcome = ? WHERE ref = ? AND ended_at = 0",
                               (time.time(), outcome, ref)).rowcount

    def steps(self, ref: str, *, open_only: bool = False, limit: int = 50) -> list[dict]:
        if not self.path.is_file():
            return []
        query = "SELECT * FROM steps WHERE ref = ?" + (" AND ended_at = 0" if open_only else "")
        with self._db(write=False) as con:
            rows = con.execute(query + " ORDER BY id DESC LIMIT ?", (ref, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def unsettled_steps(self, ref: str, since: float) -> list[dict]:
        """Every step since then that's still open or timed out, however
        many steps came after it."""
        if not self.path.is_file():
            return []
        with self._db(write=False) as con:
            rows = con.execute("SELECT * FROM steps WHERE ref = ? AND started_at >= ? AND "
                               "(ended_at = 0 OR outcome = 'timed_out') ORDER BY id", (ref, since)).fetchall()
        return [dict(row) for row in rows]

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

    def outbox_rows(self, ref: str, kind: str) -> list[dict]:
        if not self.path.is_file():
            return []
        with self._db(write=False) as con:
            rows = con.execute("SELECT * FROM outbox WHERE ref = ? AND kind = ? ORDER BY created_at",
                               (ref, kind)).fetchall()
        return [dict(row) for row in rows]

    def outbox_pending(self) -> list[dict]:
        """Rows nothing has been heard of yet: not taken, refused, lost or dropped."""
        if not self.path.is_file():
            return []
        with self._db(write=False) as con:
            rows = con.execute("SELECT * FROM outbox WHERE accepted_at = 0 AND outcome = '' ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]


def _add_columns(con: sqlite3.Connection) -> None:
    """A registry from before a column was added gets it (a write opens the
    file this way first, so reads after any write see it)."""
    have = {row[1] for row in con.execute("PRAGMA table_info(work)")}
    for name, definition in _ADDED_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE work ADD COLUMN {name} {definition}")


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
