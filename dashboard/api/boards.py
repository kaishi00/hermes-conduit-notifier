"""Boards: the ticks on conduit_show's checklist cards, kept on the host."""

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
