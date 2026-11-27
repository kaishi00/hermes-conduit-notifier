"""Chat usage: one chat's tokens and cost, for Conduit's Context sheet and the
chat list's Usage Details (hermes-conduit#2699), and each reply's speed for
the reply row's Show reply speed (reply_speed.py)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, Response

from . import core

logger = core.logger
router = core.router


# --- Chat usage (hermes-conduit#2699) -----------------------------------------
#
# Hermes keeps each session's tokens and cost on its row in state.db, but its
# routes only sum them over days and models. This reads one chat's: the row
# Conduit names, the rows a compression continued it into (and the ones it
# continued from), and the subagent runs any of them started, which Hermes
# keeps as child rows. A branch is a chat of its own and isn't counted.
#
# Cost follows Hermes' own cost_status per row: "actual" is what the provider
# billed, "estimated" Hermes' estimate from its pricing table, "included" a
# plan that doesn't bill per token, and "unknown" (or none) no price at all.
# Hermes stores an unknown price as 0, so the row's status is what tells them
# apart: a chat with no priced row answers cost_usd null, never 0.

USAGE_TIMEOUT_S = 10.0
# Far more than a chat compresses or delegates; it only bounds a damaged chain.
USAGE_MAX_ROWS = 2000
NO_STORE = {"Cache-Control": "no-store"}

_usage_limiter = core._MintLimiter(120, 60.0, message="Too many chat usage reads; try again shortly")

_COLUMNS = (
    "id", "source", "parent_session_id", "started_at", "ended_at", "end_reason", "model", "model_config",
    "tool_call_count", "api_call_count", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd",
    "actual_cost_usd", "cost_status",
)


def _row(conn: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    found = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(found) if found else None


def _children(conn: sqlite3.Connection, parent_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM sessions WHERE parent_session_id = ? "
                        "ORDER BY started_at", (parent_id,)).fetchall()
    return [dict(row) for row in rows]


def _marker(child: Dict[str, Any], name: str) -> str:
    """A ``model_config`` marker (``_branched_from``, ``_delegate_from``,
    ``_reset_from``). Continuations inherit model_config, so a marker only
    counts when it names the child's own parent (hermes_state_common.py)."""
    try:
        config = json.loads(child.get("model_config") or "{}")
    except ValueError:
        return ""
    value = config.get(name) if isinstance(config, dict) else None
    return value if isinstance(value, str) else ""


def _is_branch(child: Dict[str, Any], parent: Dict[str, Any]) -> bool:
    """A branch is a chat of its own (hermes_state's _BRANCH_CHILD_SQL), as is
    a reset's new session."""
    if parent["id"] in (_marker(child, "_branched_from"), _marker(child, "_reset_from")):
        return True
    return parent.get("end_reason") == "branched" and (child.get("started_at") or 0) >= (parent.get("ended_at") or 0)


def _continues(child: Dict[str, Any], parent: Dict[str, Any]) -> bool:
    """A compression's continuation of ``parent`` (hermes_state._COMPRESSION_CHILD_SQL):
    not a branch, a subagent's run or a tool's session."""
    return parent.get("end_reason") == "compression" and not _is_branch(child, parent) \
        and _marker(child, "_delegate_from") != parent["id"] and (child.get("source") or "") != "tool" \
        and (child.get("started_at") or 0) >= (parent.get("ended_at") or 0)


def _chain(conn: sqlite3.Connection, start: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The chat's rows in order: back to the row its first compression
    continued, then forward to the newest continuation."""
    seen = {start["id"]}
    root = start
    while root.get("parent_session_id") and len(seen) < USAGE_MAX_ROWS:
        parent = _row(conn, root["parent_session_id"])
        if parent is None or parent["id"] in seen or not _continues(root, parent):
            break
        seen.add(parent["id"])
        root = parent
    chain = [root]
    in_chain = {root["id"]}
    while chain[-1].get("end_reason") == "compression" and len(in_chain) < USAGE_MAX_ROWS:
        tail = chain[-1]
        nexts = [child for child in _children(conn, tail["id"])
                 if child["id"] not in in_chain and _continues(child, tail)]
        if not nexts:
            break
        # Hermes continues a compression once; the latest wins if it ever did twice.
        chain.append(nexts[-1])
        in_chain.add(nexts[-1]["id"])
    return chain


def _subagents(conn: sqlite3.Connection, chain: List[Dict[str, Any]]) -> tuple:
    """The rows under the chat's rows that aren't its own (subagent runs, their
    continuations and tools' sessions), and theirs. Returns (rows, runs): a run
    is a child that is not a branch, not a tool's session and not a compression
    continuation of its parent."""
    in_chain = {row["id"] for row in chain}
    found: List[Dict[str, Any]] = []
    runs = 0
    seen = set(in_chain)
    queue = list(chain)
    while queue and len(seen) < USAGE_MAX_ROWS:
        parent = queue.pop(0)
        for child in _children(conn, parent["id"]):
            if child["id"] in seen or _is_branch(child, parent):
                continue
            seen.add(child["id"])
            found.append(child)
            # A compression's continuation (a subagent's own, or a chat row
            # Hermes ever continued twice) continues its run, and a tool's
            # session is the tool's: their tokens count, but not as runs.
            if (child.get("source") or "") != "tool" and not _continues(child, parent):
                runs += 1
            queue.append(child)
    return found, runs


def _ran(row: Dict[str, Any]) -> bool:
    return any((row.get(key) or 0) > 0 for key in ("api_call_count", "input_tokens", "output_tokens"))


def _row_cost(row: Dict[str, Any]) -> tuple:
    """(amount, kind) for one row, kind one of actual, estimated, included, unknown."""
    status = (row.get("cost_status") or "").strip().lower()
    actual, estimated = row.get("actual_cost_usd"), row.get("estimated_cost_usd")
    if actual is not None and status in ("actual", ""):
        return float(actual), "actual"
    if status == "actual":
        return (float(estimated), "estimated") if estimated is not None else (None, "unknown")
    if status == "estimated" and estimated is not None:
        return float(estimated), "estimated"
    if status == "included":
        return 0.0, "included"
    return None, "unknown"


def chat_usage(conn: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    """The chat's usage, or None when Hermes has no row for ``session_id``."""
    start = _row(conn, session_id)
    if start is None:
        return None
    chain = _chain(conn, start)
    subagents, runs = _subagents(conn, chain)
    rows = chain + subagents
    totals = {key: sum(int(row.get(key) or 0) for row in rows)
              for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                          "reasoning_tokens", "api_call_count", "tool_call_count")}
    amount = 0.0
    kinds = set()
    for row in rows:
        if not _ran(row):
            continue
        cost, kind = _row_cost(row)
        kinds.add(kind)
        if cost is not None:
            amount += cost
    if not kinds or kinds == {"unknown"}:
        cost_usd, cost_status = None, "unknown"
    elif kinds == {"included"}:
        cost_usd, cost_status = 0.0, "included"
    else:
        cost_usd = round(amount, 6)
        # "partial": some calls were priced and some weren't, so the sum is a floor.
        cost_status = "partial" if "unknown" in kinds else "estimated" if "estimated" in kinds else "actual"
    newest = chain[-1]
    return {
        "session_id": start["id"],
        "newest_session_id": newest["id"],
        "model": newest.get("model") or "",
        "started_at": chain[0].get("started_at"),
        "compressions": len(chain) - 1,
        "subagent_runs": runs,
        **totals,
        "cost_usd": cost_usd,
        "cost_status": cost_status,
    }


def _read_chat_usage(session_id: str) -> Dict[str, Any]:
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "state.db"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        try:
            conn.row_factory = sqlite3.Row
            usage = chat_usage(conn, session_id)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # A Hermes whose sessions table predates the cost columns.
        if "no such column" in str(exc):
            raise HTTPException(status_code=501, detail="Chat usage needs a newer Hermes on this host.")
        raise
    if usage is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return usage


@router.get("/sessions/usage")
async def get_session_usage(request: Request, response: Response, session_id: Optional[str] = None,
                            profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required", headers=NO_STORE)
    try:
        return await asyncio.wait_for(
            core._run_scoped(profile, core._limited(_usage_limiter, profile, lambda: _read_chat_usage(sid))),
            USAGE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Reading the chat's usage took too long", headers=NO_STORE)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("session-usage", exc, feature="Chat usage")


# --- Reply speeds (reply_speed.py) --------------------------------------------
#
# The plugin's post_api_request hook (agent process) adds each model call's
# output tokens and writing time to its turn in the profile's
# conduit-reply-speeds.db. This ties each kept turn to the reply it ended on:
# the chat's last assistant row with words between the turn's first call and
# its end, across the sessions its compactions continued it into, so Conduit
# shows the speed beside that reply. A turn whose reply isn't saved yet comes
# with row_id null: the reply a client is still drawing.

SPEEDS_MAX_ROWS = 20000
_speeds_limiter = core._MintLimiter(120, 60.0, message="Too many reply speed reads; try again shortly")
_reply_speed_module: Any = None
_reply_speed_lock = threading.Lock()


def _reply_speed() -> Any:
    """reply_speed.py in the plugin's folder, loaded once."""
    global _reply_speed_module
    with _reply_speed_lock:
        if _reply_speed_module is None:
            path = os.path.join(core._plugin_root(), "reply_speed.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_reply_speed", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing reply_speed.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _reply_speed_module = module
        return _reply_speed_module


def chat_reply_speeds(conn: sqlite3.Connection, home: Any, session_id: str) -> Optional[Dict[str, Any]]:
    """The chat's replies with a kept speed, oldest first, or None when Hermes
    has no row for ``session_id``."""
    start = _row(conn, session_id)
    if start is None:
        return None
    module = _reply_speed()
    ids = [row["id"] for row in _chain(conn, start)]
    turns = module.ReplySpeedStore(home).turns(ids)
    rows: List[tuple] = []
    if turns:
        marks = ", ".join("?" for _ in ids)
        rows = [(int(row[0]), float(row[1])) for row in conn.execute(
            f"SELECT id, timestamp FROM messages WHERE session_id IN ({marks}) AND role = 'assistant' "
            f"AND timestamp >= ? AND TRIM(COALESCE(content, '')) != '' ORDER BY id DESC LIMIT ?",
            (*ids, float(turns[0]["started_at"]) - 1.0, SPEEDS_MAX_ROWS)).fetchall()]
    return {"session_id": start["id"], "replies": module.replies(turns, rows)}


def _read_reply_speeds(session_id: str) -> Dict[str, Any]:
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    path = home / "state.db"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        speeds = chat_reply_speeds(conn, home, session_id)
    finally:
        conn.close()
    if speeds is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return speeds


@router.get("/sessions/reply-speeds")
async def get_reply_speeds(request: Request, response: Response, session_id: Optional[str] = None,
                           profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required", headers=NO_STORE)
    try:
        return await asyncio.wait_for(
            core._run_scoped(profile, core._limited(_speeds_limiter, profile, lambda: _read_reply_speeds(sid))),
            USAGE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Reading the chat's reply speeds took too long", headers=NO_STORE)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("reply-speeds", exc, feature="Reply speed")
