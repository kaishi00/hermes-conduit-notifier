"""How fast each reply was written, in tokens per second, for Conduit's
reply row ("Show reply speed", hermes-conduit's feature map: Reply speed).

Hermes' own figure (the ``avg_tps`` its desktop status bar shows) divides
the provider's output tokens by the whole time of the chat's last ten model
calls: the wait before the first token, prefill and the short tool-call steps
included, so it reads low and belongs to no one reply. This keeps a reply's
own: Hermes' ``post_api_request`` hook reports every model call of a turn
with the provider's token counts and its times, and each turn adds up its
calls here. A call is timed from its first streamed chunk to its end, the
model writing. A call with no streamed chunk, or one whose provider reports
reasoning tokens (hidden thinking can come before the first chunk, and its
tokens are counted), is timed from the request instead, so a speed can read
low but never higher than the model wrote.

The dashboard's route (dashboard/api/session_usage.py) ties each turn to the
reply it ended on in state.db: the last assistant row with words written
between the turn's first call and its end (or the next turn's start).

State lives in the profile's ``conduit-reply-speeds.db``, the newest
MAX_TURNS turns of every chat together, the oldest dropped first. Failing
here only leaves a reply without a speed.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

FILE_NAME = "conduit-reply-speeds.db"
# Thousands of chats' recent replies; the file stays a few MB.
MAX_TURNS = 20000
MAX_ID_LENGTH = 200
# Shorter than this a call's writing time is noise (a cached or empty answer).
MIN_SECONDS = 0.05
# A row is the turn's reply when written up to this long after its last call
# ended (Hermes saves a turn's rows as it finishes).
REPLY_GRACE_S = 120.0
_lock = threading.Lock()


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= MAX_ID_LENGTH else ""


def call_measure(started_at: Any, ended_at: Any, first_chunk_at: Any, usage: Any) -> tuple[int, float] | None:
    """(output tokens, seconds writing) for one model call, or None when it
    can't give a speed (no token count from the provider, no times)."""
    if not isinstance(usage, dict):
        return None
    tokens = _number(usage.get("output_tokens"))
    started, ended, first = _number(started_at), _number(ended_at), _number(first_chunk_at)
    if tokens is None or tokens < 1 or started is None or ended is None or ended <= started:
        return None
    reasoning = _number(usage.get("reasoning_tokens")) or 0
    begin = first if first is not None and started <= first < ended and reasoning <= 0 else started
    seconds = ended - begin
    if seconds < MIN_SECONDS:
        seconds = ended - started
    if seconds < MIN_SECONDS:
        return None
    return int(tokens), seconds


class ReplySpeedStore:
    def __init__(self, home: Any) -> None:
        self.path = Path(home) / FILE_NAME

    def add_call(self, session_id: Any, turn_id: Any, started_at: Any, ended_at: Any,
                 first_chunk_at: Any = None, usage: Any = None) -> bool:
        """Adds one model call to its turn; whether it counted."""
        session, turn = _text(session_id), _text(turn_id)
        measured = call_measure(started_at, ended_at, first_chunk_at, usage)
        if not session or not turn or measured is None:
            return False
        tokens, seconds = measured
        started, ended = float(started_at), float(ended_at)
        with _lock, closing(self._open()) as db, db:
            db.execute(
                "INSERT INTO turns (session_id, turn_id, started_at, ended_at, output_tokens, seconds, calls) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT (session_id, turn_id) DO UPDATE SET "
                "started_at = MIN(started_at, excluded.started_at), ended_at = MAX(ended_at, excluded.ended_at), "
                "output_tokens = output_tokens + excluded.output_tokens, seconds = seconds + excluded.seconds, "
                "calls = calls + 1",
                (session, turn, started, ended, tokens, seconds))
            # The turns that ended longest ago go, never by insertion order: a long turn still reporting
            # calls keeps its row and its totals.
            db.execute("DELETE FROM turns WHERE rowid IN (SELECT rowid FROM turns ORDER BY ended_at DESC "
                       "LIMIT -1 OFFSET ?)", (MAX_TURNS,))
        return True

    def turns(self, session_ids: Iterable[Any]) -> list[dict[str, Any]]:
        """The kept turns of ``session_ids`` (a chat and its compactions'
        continuations), oldest first."""
        ids = [value for value in dict.fromkeys(_text(item) for item in session_ids) if value]
        if not ids or not self.path.exists():
            return []
        marks = ", ".join("?" for _ in ids)
        try:
            with closing(sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as db:
                rows = db.execute(
                    f"SELECT session_id, started_at, ended_at, output_tokens, seconds, calls FROM turns "
                    f"WHERE session_id IN ({marks}) ORDER BY started_at", ids).fetchall()
        except sqlite3.Error:
            return []
        return [{"session_id": row[0], "started_at": row[1], "ended_at": row[2], "output_tokens": row[3],
                 "seconds": row[4], "calls": row[5]} for row in rows]

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS turns (session_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
                   "started_at REAL NOT NULL, ended_at REAL NOT NULL, output_tokens INTEGER NOT NULL, "
                   "seconds REAL NOT NULL, calls INTEGER NOT NULL, PRIMARY KEY (session_id, turn_id))")
        db.execute("CREATE INDEX IF NOT EXISTS turns_by_session ON turns (session_id, started_at)")
        return db


def speed(turn: dict[str, Any]) -> float | None:
    seconds = _number(turn.get("seconds"))
    tokens = _number(turn.get("output_tokens"))
    if not seconds or seconds < MIN_SECONDS or not tokens:
        return None
    return round(tokens / seconds, 1)


def replies(turns: list[dict[str, Any]], rows: list[tuple[int, float]]) -> list[dict[str, Any]]:
    """Each turn's speed with the reply it ended on: ``rows`` are the chat's
    assistant rows with words, as (row id, timestamp). A turn whose reply isn't
    saved yet has ``row_id`` None (the reply a client is still drawing)."""
    ordered = sorted(rows, key=lambda row: (row[1], row[0]))
    out = []
    claimed: set = set()
    first = 0  # turns run oldest first: rows before an earlier turn's window aren't any later turn's
    for index, turn in enumerate(turns):
        rate = speed(turn)
        start = float(turn["started_at"]) - 1.0
        end = float(turn["ended_at"]) + REPLY_GRACE_S
        if index + 1 < len(turns):
            end = min(end, float(turns[index + 1]["started_at"]))
        while first < len(ordered) and ordered[first][1] < start:
            first += 1
        found = None
        at = first
        while at < len(ordered) and ordered[at][1] < end:
            if ordered[at][0] not in claimed:
                found = ordered[at][0]
            at += 1
        if rate is None:
            continue
        # A row an earlier turn ended on is that turn's reply, never a second one's.
        if found is not None:
            claimed.add(found)
        out.append({"row_id": found, "tokens_per_s": rate,
                    "output_tokens": int(turn["output_tokens"]), "seconds": round(float(turn["seconds"]), 2),
                    "ended_at": float(turn["ended_at"])})
    return out
