"""A chat's turn running in the gateway: whether one runs, what it's doing, its
delegate agents' own steps, and the messages waiting behind it, for the chat
Conduit has open (Eric, 2026-11-27).

``hermes dashboard`` answers Conduit's chat socket, but a chat the gateway
holds (an API chat's turn, another app's on the Webapp, a gateway restart's)
runs there. The dashboard hands a message sent into such a turn to the
gateway's queue (its receipt says ``queued`` and ``forwarded``) and keeps no
queue of its own, so after a reload, or from another app, nothing said the
message was waiting; and the turn's progress, its delegate agents' included,
streams only to the client that started it. This reads both where they are:

- the queue from the gateway's admissions in the profile's state.db (the
  rows Hermes lists as a chat's ``pending``: waiting, running, or cut off by a
  restart), only their words, read-only;
- the turn's step and its delegate agents from the plugin's live steps file
  (live_steps.py), which the hooks write in the gateway.

A chat's rows are the row Conduit names and the rows a compression continued
it into or from (session_usage's chain). A Hermes without the admissions table
says ``supported: false`` for the queue, and its turns are told only by their
live step.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, Response

from . import core, chat_tasks, live_steps

logger = core.logger

router = core.router

LIVE_WORK_TIMEOUT_S = 10.0
# The apps read this every 5 s while the chat they show has a turn running in
# the gateway or a message waiting there.
_live_work_limiter = core._MintLimiter(240, 60.0, message="Too many live-work reads; try again shortly")
NO_STORE = {"Cache-Control": "no-store"}
# A queued message's words, at most this long; the row says the rest is in the message.
TEXT_MAX = 4000
# At most this many rows; a longer queue is still told by its count.
QUEUE_MAX = 20
_PENDING = ("queued", "started", "unknown")


def _words(payload_json: Any) -> str:
    """The words a message was sent with, as Hermes shows them to a viewer: the payload's ``text``,
    else the native envelope's event text; never its routing or provenance."""
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else {}
    except (ValueError, RecursionError):
        return ""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if not isinstance(text, str):
        native = payload.get("native_text_v1")
        event = native.get("event") if isinstance(native, dict) else None
        text = event.get("text") if isinstance(event, dict) else ""
    text = text if isinstance(text, str) else ""
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def _admissions(state_path: Path, ids: List[str]) -> Optional[List[Dict[str, Any]]]:
    """The chat's pending admissions, oldest first; None on a Hermes without them."""
    conn = sqlite3.connect(f"{state_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_admissions'").fetchone() is None:
            return None
        marks = ", ".join("?" for _ in ids)
        states = ", ".join("?" for _ in _PENDING)
        rows = conn.execute(
            f"SELECT admission_id, request_id, seq, status, intent, payload_json, target_session_id "
            f"FROM session_admissions WHERE target_session_id IN ({marks}) AND status IN ({states}) "
            f"ORDER BY seq", (*ids, *_PENDING)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _queue_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"admission_id": row["admission_id"], "input_id": row.get("request_id") or "",
            "text": _words(row.get("payload_json")), "status": row["status"], "sequence": row["seq"]}


def live_work(state_path: Path, home: Any, session_id: str) -> Dict[str, Any]:
    try:
        ids = chat_tasks._chain_ids(state_path, session_id)
    except sqlite3.OperationalError as exc:
        # A sessions table without the chain's columns is from a Hermes long before the queue.
        if "no such column" not in str(exc):
            raise
        ids = [session_id]
    if ids is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        admissions = _admissions(state_path, ids)
    except sqlite3.OperationalError as exc:
        # A table without the columns this reads is from a Hermes this can't read.
        if "no such column" not in str(exc):
            raise
        admissions = None
    steps = live_steps._live_steps().read(home, agents=True)
    # The newest of the chat's rows with a running turn: a compaction mid-turn moves it there.
    step = next((steps[sid] for sid in reversed(ids) if sid in steps), None)
    started = any(row["status"] == "started" for row in admissions or [])
    waiting = [row for row in admissions or [] if row["status"] in ("queued", "unknown")]
    return {
        "session_id": session_id,
        "supported": admissions is not None,
        # A turn runs in the gateway: its admission says so, or its live step does (a Hermes without
        # admissions, or a turn that never went through them).
        "running": started or step is not None,
        "step": None if step is None else {key: step[key] for key in ("phase", "tool", "since")},
        "agents": [] if step is None else step.get("agents", []),
        "queued": [_queue_row(row) for row in waiting[:QUEUE_MAX]],
        "queued_count": len(waiting),
    }


def _read_live_work(session_id: str) -> Dict[str, Any]:
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / "state.db"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    # The steps file where the /live-steps route reads it: the profile's home.
    return live_work(path, core._calls_home(), session_id)


@router.get("/sessions/live-work")
async def get_live_work(request: Request, response: Response, session_id: Optional[str] = None,
                        profile: Optional[str] = None) -> Dict[str, Any]:
    """``running``: a turn of the chat runs in the gateway; ``step``: what it does now (``phase``,
    ``tool`` as a category, ``since``); ``agents``: its delegate agents, each with ``id``, ``goal``,
    ``phase``, ``tool``, ``since`` and ``started``; ``queued``: the messages waiting behind it (and
    a turn a restart cut off, ``status: unknown``), each with ``admission_id``, ``input_id`` (the
    submission id it was sent with), ``text``, ``status`` and ``sequence``, oldest first;
    ``supported``: whether this Hermes keeps a queue to read."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required", headers=NO_STORE)
    try:
        return await asyncio.wait_for(
            core._run_scoped(profile, core._limited(_live_work_limiter, profile, lambda: _read_live_work(sid))),
            LIVE_WORK_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Reading the chat's live work took too long", headers=NO_STORE)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("live-work", exc, feature="Chat live work")
