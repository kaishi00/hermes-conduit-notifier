"""A chat's Kanban tasks: the cards a chat handed to the board, on every
board, for the line above Conduit's composer (hermes-conduit-desktop#553).

Hermes stamps each task ``kanban_create`` makes with the session that made it
(``tasks.session_id``), and a worker's own cards inherit it from the task it
works, so a squad's fan-out counts as the chat's too. A chat's rows are the
row Conduit names and the rows a compression continued it into or from
(session_usage's chain), since the session id changes as a chat compresses.

The board is shared across profiles (``kanban_home``), so every board is read;
the chain comes from the profile's own state.db. Archived tasks and boards are
left out. Each board is opened read-only: this never creates a board or runs
Hermes' migrations. A board whose tasks table has no ``session_id`` (a Hermes
from before the stamp) is skipped, and an answer where no board has it says
``supported: false``, so the apps show nothing rather than "no tasks".
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core, session_usage

router = core.router

CHAT_TASKS_TIMEOUT_S = 10.0
# The newest this many; a chat that made more than this is a board of its own.
CHAT_TASKS_LIMIT = 50
NO_STORE = {"Cache-Control": "no-store"}
# The apps read this while a chat with an unfinished task shows, every 15 s.
_chat_tasks_limiter = core._MintLimiter(240, 60.0, message="Too many chat task reads; try again shortly")

_COLUMNS = ("id", "title", "status", "assignee", "created_at", "started_at", "completed_at",
            "last_failure_error", "consecutive_failures")


def _chain_ids(state_path: Path, session_id: str) -> Optional[List[str]]:
    """The chat's session ids, oldest first; None when Hermes has no row for it."""
    conn = sqlite3.connect(f"{state_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        start = session_usage._row(conn, session_id)
        if start is None:
            return None
        return [row["id"] for row in session_usage._chain(conn, start)]
    finally:
        conn.close()


def _boards() -> List[Tuple[str, str, Path]]:
    """(slug, name, db path) for each board Hermes lists, archived ones left out."""
    try:
        from hermes_cli import kanban_db

        listed = kanban_db.list_boards(include_archived=False)
    except (ImportError, AttributeError):  # a Hermes without the board, or without boards
        return []
    found = []
    for meta in listed:
        slug = meta.get("slug") if isinstance(meta, dict) else None
        if not isinstance(slug, str) or not slug:
            continue
        name = meta.get("name") if isinstance(meta.get("name"), str) and meta.get("name") else slug
        path = meta.get("db_path") or kanban_db.kanban_db_path(board=slug)
        found.append((slug, name, Path(path)))
    return found


def _board_tasks(path: Path, ids: List[str]) -> Optional[List[Dict[str, Any]]]:
    """The board's tasks made by any of ``ids``; None when its tasks carry no
    session (an older Hermes) or it can't be opened."""
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "session_id" not in columns:
            return None
        wanted = [name for name in _COLUMNS if name in columns]
        marks = ", ".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM tasks WHERE session_id IN ({marks}) AND status != 'archived' "
            f"ORDER BY created_at DESC LIMIT {CHAT_TASKS_LIMIT}", tuple(ids)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _task(row: Dict[str, Any], slug: str, name: str) -> Dict[str, Any]:
    error = row.get("last_failure_error")
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "status": row.get("status") or "",
        "assignee": row.get("assignee") or None,
        "board": slug,
        "board_name": name,
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        # Only while it's still blocked by it: a task that went on to finish
        # keeps the column, but it no longer describes the task.
        "error": error if isinstance(error, str) and error.strip() and row.get("status") == "blocked" else None,
    }


def chat_tasks(state_path: Path, session_id: str) -> Dict[str, Any]:
    ids = _chain_ids(state_path, session_id)
    if ids is None:
        raise HTTPException(status_code=404, detail="Session not found")
    supported = False
    tasks: List[Dict[str, Any]] = []
    for slug, name, path in _boards():
        rows = _board_tasks(path, ids)
        if rows is None:
            continue
        supported = True
        tasks.extend(_task(row, slug, name) for row in rows)
    tasks.sort(key=lambda task: (task.get("created_at") or 0, task["id"]))
    return {"session_id": session_id, "supported": supported, "tasks": tasks[-CHAT_TASKS_LIMIT:]}


def _read_chat_tasks(session_id: str) -> Dict[str, Any]:
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "state.db"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return chat_tasks(path, session_id)


@router.get("/sessions/kanban-tasks")
async def get_chat_kanban_tasks(request: Request, response: Response, session_id: Optional[str] = None,
                                profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required", headers=NO_STORE)
    try:
        return await asyncio.wait_for(
            core._run_scoped(profile, core._limited(_chat_tasks_limiter, profile, lambda: _read_chat_tasks(sid))),
            CHAT_TASKS_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Reading the chat's Kanban tasks took too long", headers=NO_STORE)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("chat-kanban-tasks", exc, feature="Chat Kanban tasks")
