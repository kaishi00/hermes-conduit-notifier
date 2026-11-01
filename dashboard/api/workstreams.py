"""Bot workstreams: the Work panel's routes (designs/bot-mode-coordinator
C.6, C.8). A bot's work as Conduit's Work strip and panel show it, and the
user's own actions on a workstream, which skip the Bot Chat's model: Reply
in W3, Stop, Continue and Check first, and how they file it: Pin, Resolve
and Reopen.

A bot's work is driven in this process (Hermes' session API), so Conduit
names a secondary bot with ?conduit_profile= (PLG-3 sends a ?profile= route
to a plugin host, which has no session API). Each action goes through the
copy of work_tool Hermes loaded for that bot, whose hooks see its turns.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import core

router = core.router

WORKSTREAMS_MAX_BODY_BYTES = 16 * 1024
WORKSTREAMS_LIMIT = 120
WORKSTREAMS_WINDOW_S = 60.0
# A refusal's code -> its HTTP status. The body has the code, so Conduit
# words it, and the note.
REFUSAL_STATUS = {"no_such_work": 404, "missing": 400, "unknown_action": 404, "off": 409, "busy": 409,
                  "not_now": 409, "starting": 409, "still_running": 409, "too_long": 413, "open_elsewhere": 409, "unavailable": 503, "hermes": 502}

# The panel's reads and the user's actions count apart, so reading often
# never holds up a Stop.
_workstreams_limiter = core._MintLimiter(WORKSTREAMS_LIMIT, WORKSTREAMS_WINDOW_S,
                                         message="Too many work requests; try again shortly")
_workstream_actions_limiter = core._MintLimiter(WORKSTREAMS_LIMIT, WORKSTREAMS_WINDOW_S,
                                                message="Too many work actions; try again shortly")


def _work_tool() -> Any:
    """The copy of work_tool Hermes loaded for this request's bot (its hooks
    see that bot's turns), else the dashboard's own import of it."""
    tool = core._plugin_package_module("work_tool")
    return tool.copy_for(core._calls_home()) or tool


def workstreams_list(resolved: bool = False, before: Optional[float] = None) -> Dict[str, Any]:
    if not resolved:
        return _work_tool().app_list(core._calls_home())
    return _work_tool().app_list(core._calls_home(), resolved=True, before=before)


def workstreams_act(ref: str, action: str, body: Optional[dict]) -> Dict[str, Any]:
    return _work_tool().app_act(core._calls_home(), ref, action, body)


async def _workstreams_route(route: str, profile: Optional[str], fn: Any, limiter: Any = None) -> Any:
    no_store = {"Cache-Control": "no-store"}
    try:
        answer = await core._run_scoped(profile, core._limited(limiter or _workstreams_limiter, profile, fn))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        # The profile scope's own refusal (an unknown profile) stands.
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Bot workstreams")
    if answer.get("ok") is False:
        code = str(answer.get("error") or "refused")
        return JSONResponse({"detail": answer.get("note") or code, "code": code,
                             **({"work": answer["work"]} if "work" in answer else {})},
                            status_code=REFUSAL_STATUS.get(code, 409), headers=no_store)
    return {**answer, "ok": True}


@router.get("/workstreams")
async def get_workstreams(request: Request, response: Response, profile: Optional[str] = None,
                          resolved: bool = False, before: Optional[float] = None) -> Any:
    """The bot's work: ``work``, every open row, active first then by when
    it last changed (workstreams, and asks to and from other bots), each
    with its status, checklist, last result, the ``actions`` the user can
    take and how they can file it (``manage``: pin, unpin, resolve,
    reopen); ``counts`` for the Work strip (working, needs_you, ready,
    waiting); ``resolved_count``; ``policy`` ("off" when the bot's
    workstreams are off) and ``bot_chat``. ``resolved=1``: the Resolved
    list instead, last resolved first, a page at a time (``before``: the
    last row's ``closed_at``; ``more`` says there's another page)."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    return await _workstreams_route("workstreams", profile, lambda: workstreams_list(resolved, before))


@router.post("/workstreams/{ref}/{action}")
async def post_workstream_action(ref: str, action: str, request: Request, response: Response,
                                 profile: Optional[str] = None) -> Any:
    """The user's action on workstream ``ref``: ``send`` ({"text", "how"?}:
    their words go straight in, as if typed in its chat), ``stop``,
    ``continue`` or ``check`` (Check first, after a restart cut off a step
    that can change things); or ``pin``, ``unpin``, ``resolve`` (work
    still at it is refused, ``still_running``) and ``reopen``, for asks
    too. The answer has the row as it is now
    (``work``); a refusal is a 4xx with ``code`` and ``detail``."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, WORKSTREAMS_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    return await _workstreams_route("workstream action", profile, lambda: workstreams_act(ref, action, body),
                                    _workstream_actions_limiter)
