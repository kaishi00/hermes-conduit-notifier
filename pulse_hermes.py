"""Read-only lookups in Hermes's own state.db for Pulse.

What happened to a Pulse turn is only ever read here, never assumed
(designs/proactivity.md 6.4.1): the hidden user row the turn wrote, and the
rows after it. Each lookup opens its own short read-only connection, names
its columns, and raises ``HermesUnreadable`` when the database or a column
it needs isn't there, so the caller can tell "Hermes has no row" from
"can't tell".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

STATE_DB = "state.db"
# Hermes writes this display kind on the assistant row of a turn that failed
# (agent/turn_failure_copy.py): it ends the turn but delivers nothing.
FAILED_TURN_KIND = "failed_turn"


class HermesUnreadable(Exception):
    """state.db is missing, locked past the timeout, or shaped differently."""


@dataclass(frozen=True)
class HomeChat:
    chat_id: str  # the stored session id Conduit opens the chat by
    title: str
    pinned: bool


@dataclass(frozen=True)
class TurnRows:
    """The tagged user row and what the turn wrote after it."""
    session_id: str
    reply: str  # the last non-empty assistant text after the user row
    failed: bool  # Hermes marked the turn failed
    closed: bool  # a later user row exists, so the turn is over
    prompt: str = ""  # the tagged user row's own text


def _connect(home: Path) -> sqlite3.Connection:
    path = Path(home) / STATE_DB
    if not path.exists():
        raise HermesUnreadable(f"no {STATE_DB} in the Hermes home")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as error:
        raise HermesUnreadable(str(error)) from error
    conn.row_factory = sqlite3.Row
    return conn


def _run(home: Path, sql: str, params: tuple) -> list[sqlite3.Row]:
    conn = _connect(home)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as error:
        raise HermesUnreadable(str(error)) from error
    finally:
        conn.close()


def find_home_chat(home: Path, title: str) -> list[HomeChat]:
    """Chats titled exactly ``title`` (the user names the chat, the way Bot
    Mode finds "Bot Chat"), pinned and most recently active first. Only
    chats a person started count: a compression or branch child shares its
    parent's title, and reopening the parent follows it there. More than one
    means the user has to pick (``pulse home``)."""
    rows = _run(home, (
        "SELECT id, title, pinned FROM sessions WHERE title = ? AND archived = 0 AND hidden = 0 "
        "AND parent_session_id IS NULL ORDER BY pinned DESC, COALESCE(last_activity_at, started_at) DESC"
    ), (title,))
    return [HomeChat(row["id"], row["title"], bool(row["pinned"])) for row in rows]


def chat_by_id(home: Path, chat_id: str) -> Optional[HomeChat]:
    rows = _run(home, "SELECT id, title, pinned FROM sessions WHERE id = ? AND archived = 0 AND hidden = 0",
                (chat_id,))
    return HomeChat(rows[0]["id"], rows[0]["title"] or "", bool(rows[0]["pinned"])) if rows else None


def find_turn(home: Path, *, chat_id: str, tag: str, since: float) -> Optional[TurnRows]:
    """The user row carrying ``tag`` in the chat or the sessions it continued
    into (compression, branches), written at or after ``since``, and the rows
    after it. Archived rows count too: compaction keeps them as inactive.
    The latest copy wins: compressing mid-turn carries the user row into the
    new session, and the reply follows it there."""
    users = _run(home, (
        "WITH RECURSIVE chain(id) AS (SELECT ? UNION SELECT s.id FROM sessions s JOIN chain c "
        "ON s.parent_session_id = c.id) "
        "SELECT m.id, m.session_id, m.content FROM messages m WHERE m.session_id IN (SELECT id FROM chain) "
        "AND m.role = 'user' AND m.timestamp >= ? AND instr(m.content, ?) > 0 ORDER BY m.id DESC LIMIT 1"
    ), (chat_id, since, tag))
    if not users:
        return None
    user_id, session_id = users[0]["id"], users[0]["session_id"]
    prompt = users[0]["content"] if isinstance(users[0]["content"], str) else ""
    rows = _run(home, (
        "SELECT role, content, display_kind FROM messages WHERE session_id = ? AND id > ? ORDER BY id"
    ), (session_id, user_id))
    reply, failed, closed = "", False, False
    for row in rows:
        if row["role"] == "user":
            closed = True
            break
        if row["role"] != "assistant":
            continue
        if row["display_kind"] == FAILED_TURN_KIND:
            failed = True
            continue
        text = row["content"].strip() if isinstance(row["content"], str) else ""
        if text:
            reply = text
    return TurnRows(session_id, reply, failed, closed, prompt)


# The chats a person had: every session but ``excluded`` (the home chat's
# whole compression chain), cron runs and hidden chats; only their visible
# user and assistant rows. Takes the home chat's id twice.
_EXCLUDED = (
    "WITH RECURSIVE up(id, parent) AS (SELECT id, parent_session_id FROM sessions WHERE id = ? "
    "UNION SELECT s.id, s.parent_session_id FROM sessions s JOIN up ON s.id = up.parent), "
    "excluded(id) AS (SELECT ? UNION SELECT id FROM up "
    "UNION SELECT s.id FROM sessions s JOIN excluded e ON s.parent_session_id = e.id) ")
_PERSON_ROWS = (
    "m.role IN ('user', 'assistant') AND m.active = 1 AND m.display_kind IS NULL "
    "AND (s.source IS NULL OR s.source != 'cron') AND s.hidden = 0 AND s.id NOT IN (SELECT id FROM excluded)")


def recent_messages(home: Path, *, exclude_chat: str, since: float, max_chars: int = 2000,
                    limit: int = 3000) -> list[sqlite3.Row]:
    """User and assistant rows since ``since`` in the chats a person had:
    not any part of ``exclude_chat``'s compression chain, not cron runs, hidden
    chats or hidden rows. The newest ``limit``, each cut to ``max_chars``,
    oldest first, with the chat's id and title."""
    return _run(home, (
        _EXCLUDED + "SELECT * FROM (SELECT s.id AS chat_id, s.title, m.role, substr(m.content, 1, ?) AS content, "
        "m.timestamp, m.id FROM messages m JOIN sessions s ON s.id = m.session_id "
        "WHERE m.timestamp >= ? AND " + _PERSON_ROWS + " "
        "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?) ORDER BY timestamp, id"
    ), (exclude_chat, exclude_chat, max_chars, since, limit))


def chat_activity(home: Path, *, exclude_chat: str, since: float,
                  until: Optional[float] = None, max_chars: int = 2000) -> list[sqlite3.Row]:
    """Per chat a person wrote in after ``since`` (and up to ``until``, as
    in ``recent_messages``): its id, the chat it continued from, its newest
    such row's id and time, and how many characters its rows hold, each
    counted up to ``max_chars``."""
    return _run(home, (
        _EXCLUDED + "SELECT s.id AS chat_id, s.parent_session_id AS parent, MAX(m.id) AS last_id, "
        "MAX(m.timestamp) AS last_at, SUM(MIN(LENGTH(m.content), ?)) AS chars FROM messages m "
        "JOIN sessions s ON s.id = m.session_id WHERE m.timestamp > ? AND m.timestamp <= ? AND "
        + _PERSON_ROWS + " GROUP BY s.id ORDER BY last_at"
    ), (exclude_chat, exclude_chat, max_chars, since, float("inf") if until is None else until))


def chat_messages(home: Path, *, chat_id: str, after_id: int, since: float, max_chars: int,
                  limit: int, until: Optional[float] = None, newest: bool = False) -> list[sqlite3.Row]:
    """One chat's visible user and assistant rows after row ``after_id`` and
    time ``since`` (up to ``until``), oldest first, each cut to ``max_chars``:
    the first ``limit`` of them, or with ``newest`` the last."""
    rows = _run(home, (
        "SELECT id, role, substr(content, 1, ?) AS content, timestamp FROM messages "
        "WHERE session_id = ? AND id > ? AND timestamp > ? AND timestamp <= ? AND role IN ('user', 'assistant') "
        f"AND active = 1 AND display_kind IS NULL ORDER BY id {'DESC' if newest else ''} LIMIT ?"
    ), (max_chars, chat_id, after_id, since, float("inf") if until is None else until, limit))
    return rows[::-1] if newest else rows


def user_text_after(home: Path, *, session_id: str, tag: str) -> list[str]:
    """What the user wrote after the user row carrying ``tag``, up to the
    next hidden (Pulse) row."""
    rows = _run(home, (
        "SELECT content, display_kind FROM messages WHERE session_id = ? AND role = 'user' AND id > ("
        "SELECT MAX(id) FROM messages WHERE session_id = ? AND role = 'user' AND instr(content, ?) > 0) "
        "ORDER BY id"
    ), (session_id, session_id, tag))
    text = []
    for row in rows:
        if row["display_kind"] is not None:
            break
        text.append(row["content"] if isinstance(row["content"], str) else "")
    return text
