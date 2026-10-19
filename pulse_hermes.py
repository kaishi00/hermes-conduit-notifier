"""Read-only lookups in Hermes's own state.db for Pulse.

What Hermes did with a Pulse turn is only ever read here, never assumed
(designs/proactivity.md 6.4.1): the admission row the gateway committed for
an injection, and the transcript rows the turn wrote. Each lookup opens its
own short read-only connection, names its columns, and raises
``HermesUnreadable`` when the database or a column it needs isn't there, so
the caller can tell "Hermes has no row" from "can't tell".
"""

from __future__ import annotations

import json
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
    session_key: str
    session_id: str
    title: str
    pinned: bool


@dataclass(frozen=True)
class Admission:
    request_id: str
    target_session_id: str
    status: str  # queued, started, unknown, terminal
    outcome: Optional[str]  # completed, failed, interrupted, rejected, cancelled


@dataclass(frozen=True)
class TurnRows:
    """The tagged user row and what the turn wrote after it."""
    session_id: str
    reply: str  # the last non-empty assistant text after the user row
    failed: bool  # Hermes marked the turn failed
    closed: bool  # a later user row exists, so the turn is over


def _connect(home: Path) -> sqlite3.Connection:
    path = Path(home) / STATE_DB
    if not path.exists():
        raise HermesUnreadable(f"no {STATE_DB} in the Hermes home")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
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
    """Local chats titled exactly ``title`` (the user names the chat, the way
    Bot Mode finds "Bot Chat"), one per session key, pinned and most recently
    active first. More than one means the user has to pick (``pulse home``)."""
    rows = _run(home, (
        "SELECT id, session_key, title, pinned, COALESCE(last_activity_at, started_at) AS active_at "
        "FROM sessions WHERE title = ? AND session_key LIKE '%:local:%' "
        "AND archived = 0 AND hidden = 0 ORDER BY pinned DESC, active_at DESC"
    ), (title,))
    chats: dict[str, HomeChat] = {}
    for row in rows:
        key = row["session_key"]
        if key and key not in chats:
            chats[key] = HomeChat(key, row["id"], row["title"], bool(row["pinned"]))
    return list(chats.values())


def chat_for_key(home: Path, session_key: str) -> Optional[HomeChat]:
    rows = _run(home, (
        "SELECT id, session_key, title, pinned FROM sessions WHERE session_key = ? "
        "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1"
    ), (session_key,))
    if not rows:
        return None
    row = rows[0]
    return HomeChat(row["session_key"], row["id"], row["title"] or "", bool(row["pinned"]))


def find_admission(home: Path, *, plugin_id: str, session_key: str, tag: str) -> Optional[Admission]:
    """The gateway's admission row for the injection carrying ``tag``.

    The gateway names a plugin injection ``["plugin", <plugin id>, <session
    key>, <random hex>]`` under the principal ``automation:<session key>``
    (gateway/run_plugin_injection.py), and keeps the text in the payload."""
    prefix = json.dumps(["plugin", plugin_id, session_key], separators=(",", ":"))[:-1] + ","
    rows = _run(home, (
        "SELECT request_id, target_session_id, status, outcome, payload_json FROM session_admissions "
        "WHERE principal_id = ? AND substr(request_id, 1, ?) = ? ORDER BY seq DESC"
    ), ("automation:" + session_key, len(prefix), prefix))
    for row in rows:
        try:
            text = json.loads(row["payload_json"]).get("text")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(text, str) and tag in text:
            return Admission(row["request_id"], row["target_session_id"], row["status"], row["outcome"])
    return None


def find_turn(home: Path, *, session_key: str, tag: str, request_id: str = "") -> Optional[TurnRows]:
    """The user row the tagged turn wrote, and the rows after it in the same
    session. Archived rows count too: compaction keeps them as inactive."""
    if request_id:
        users = _run(home, (
            "SELECT m.id, m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.session_key = ? AND m.platform_message_id = ? AND m.role = 'user' ORDER BY m.id LIMIT 1"
        ), (session_key, request_id))
    else:
        users = []
    if not users:
        # A turn that didn't go through an admission (a host injecting on its
        # own path) is found by its tag.
        users = _run(home, (
            "SELECT m.id, m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.session_key = ? AND m.role = 'user' AND instr(m.content, ?) > 0 ORDER BY m.id LIMIT 1"
        ), (session_key, tag))
    if not users:
        return None
    user_id, session_id = users[0]["id"], users[0]["session_id"]
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
        text = (row["content"] or "").strip() if isinstance(row["content"], str) else ""
        if text:
            reply = text
    return TurnRows(session_id, reply, failed, closed)
