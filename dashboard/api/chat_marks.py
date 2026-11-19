"""Done chats: the chats the user or their agent marked done (chat_marks_store),
for every Conduit's Sessions list."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core

router = core.router

NO_STORE = {"Cache-Control": "no-store"}
CHAT_MARKS_MAX_BODY_BYTES = 16 * 1024
# A list reads them with each catalog read, and every row's menu may mark one.
_chat_marks_limiter = core._MintLimiter(240, 60.0, message="Too many Done changes; try again shortly")


def _store() -> Any:
    module = core._plugin_package_module("chat_marks_store")
    return module, module.ChatMarksStore(core._calls_home())


def read_chat_marks() -> Dict[str, Any]:
    _, store = _store()
    return {"done": store.done()}


def save_chat_mark(body: Any) -> Dict[str, Any]:
    module, store = _store()
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    try:
        return {"mark": store.mark(body.get("ids"), body.get("done"), by="user")}
    except module.ChatMarksError as exc:
        raise core.TokenError(400, str(exc))
    except OSError:
        raise core.TokenError(503, "The chat can't be marked right now")


async def _route(request: Request, profile: Optional[str], run: Any) -> Dict[str, Any]:
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_chat_marks_limiter, profile, run)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except Exception as exc:
        raise core._unexpected("chat-marks", exc, feature="Done chats")


@router.get("/chats/done")
async def get_chats_done(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _route(request, profile, read_chat_marks)


@router.post("/chats/done")
async def post_chats_done(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CHAT_MARKS_MAX_BODY_BYTES)
    return await _route(request, profile, lambda: save_chat_mark(body))
