"""Boards: the ticks on conduit_show's checklist cards, the boards agents
showed in group rooms, and the boards Hermes Desktop draws, kept on the host."""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Query, Request, Response

from . import core

router = core.router


# --- Checklist ticks ------------------------------------------------------------
#
# A checklist card (conduit_show) keeps its ticks here, so every one of the
# user's Conduits shows the same ones and Hermes hears what changed at the
# chat's next turn (checklist_store.note, from the pre_llm_call hook). Conduit
# reads a chat's ticks when it draws a checklist and sends a checklist's whole
# state on every tick.

CHECKLIST_MAX_BODY_BYTES = 32 * 1024
NO_STORE = {"Cache-Control": "no-store"}

_checklist_limiter = core._MintLimiter(240, 60.0, message="Too many checklist changes; try again shortly")
_checklist_store_module: Any = None
_checklist_store_lock = threading.Lock()


def _checklist_store() -> Any:
    """checklist_store.py in the plugin's folder, loaded once."""
    global _checklist_store_module
    with _checklist_store_lock:
        if _checklist_store_module is None:
            path = os.path.join(core._plugin_root(), "checklist_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_checklist_store", path)
            if spec is None or spec.loader is None or not os.path.exists(path):
                raise core.TokenError(501, "This plugin install is missing checklist_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _checklist_store_module = module
        return _checklist_store_module


def read_checklists(sessions: List[str]) -> Dict[str, Any]:
    module = _checklist_store()
    try:
        return {"ticks": module.ChecklistStore(core._calls_home()).ticks(sessions)}
    except module.ChecklistError as exc:
        raise core.TokenError(400, str(exc))


def save_checklist(body: Any) -> Dict[str, Any]:
    module = _checklist_store()
    try:
        return {"ticks": module.ChecklistStore(core._calls_home()).save(body)}
    except module.ChecklistError as exc:
        raise core.TokenError(400, str(exc))
    except OSError:
        raise core.TokenError(503, "Checklist ticks can't be saved right now")


@router.get("/boards/checklists")
async def get_board_checklists(request: Request, response: Response, profile: Optional[str] = None,
                               session: List[str] = Query(default=[])) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        return await core._run_scoped(profile, core._limited(_checklist_limiter, profile,
                                                             lambda: read_checklists(session)))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("board-checklists", exc, feature="Checklist ticks")


@router.post("/boards/checklists")
async def post_board_checklists(request: Request, response: Response,
                                profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, CHECKLIST_MAX_BODY_BYTES)
    try:
        return await core._run_scoped(profile, core._limited(_checklist_limiter, profile,
                                                             lambda: save_checklist(body)))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("board-checklists", exc, feature="Checklist ticks")


# --- Boards in group rooms -------------------------------------------------------
#
# A board an agent showed in a room this host runs (room_boards, kept in the
# root home for every profile's agents): Conduit reads a room's boards and
# draws each under the next message its agent posts there.

_room_boards_limiter = core._MintLimiter(240, 60.0, message="Too many room board reads; try again shortly")


def read_room_boards(room_id: Optional[str]) -> Dict[str, Any]:
    module = core._plugin_package_module("room_boards")
    root = core._plugin_package_module("room_identity").root_home(core._calls_home())
    try:
        return {"boards": module.RoomBoards(root).room(room_id)}
    except ValueError as exc:
        raise core.TokenError(400, str(exc))


@router.get("/boards/rooms")
async def get_room_boards(request: Request, response: Response, profile: Optional[str] = None,
                          room_id: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        return await core._run_scoped(profile, core._limited(_room_boards_limiter, profile,
                                                             lambda: read_room_boards(room_id)))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("room-boards", exc, feature="Boards in rooms")


# --- Boards in Hermes Desktop ----------------------------------------------------
#
# A board shown in a Desktop chat (room_boards.DesktopBoards, in the root home,
# since Desktop's half is one for every profile): the half draws it where the
# agent's reply writes ::conduit-board{id="cb-…"}, and says it's loaded, so the
# agent is asked for that line only where it will be drawn.

_desktop_boards_limiter = core._MintLimiter(240, 60.0, message="Too many board reads; try again shortly")


def _desktop_boards() -> Any:
    module = core._plugin_package_module("room_boards")
    root = core._plugin_package_module("room_identity").root_home(core._calls_home())
    return module.DesktopBoards(root)


def read_desktop_board(board_id: Optional[str]) -> Dict[str, Any]:
    try:
        board = _desktop_boards().get(board_id)
    except ValueError as exc:
        raise core.TokenError(400, str(exc))
    if board is None:
        raise core.TokenError(404, "That board is gone")
    return {"board": board}


def note_desktop_half() -> Dict[str, Any]:
    try:
        _desktop_boards().saw_half()
    except OSError:
        raise core.TokenError(503, "The plugin couldn't note Desktop's half right now")
    return {"ok": True}


@router.get("/boards/desktop")
async def get_desktop_board(request: Request, response: Response, profile: Optional[str] = None,
                            id: Optional[str] = None) -> Dict[str, Any]:  # noqa: A002 — the query's name
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        return await core._run_scoped(profile, core._limited(_desktop_boards_limiter, profile,
                                                             lambda: read_desktop_board(id)))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("desktop-boards", exc, feature="Boards in Hermes Desktop")


@router.post("/boards/desktop/seen")
async def post_desktop_half_seen(request: Request, response: Response,
                                 profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        return await core._run_scoped(profile, core._limited(_desktop_boards_limiter, profile, note_desktop_half))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("desktop-boards", exc, feature="Boards in Hermes Desktop")
