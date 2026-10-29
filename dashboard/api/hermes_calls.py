"""Hermes calls you."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import threading
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core

router = core.router


# --- Hermes calls you (#449) -------------------------------------------------
#
# Conduit asks to be called when a job the user handed to a Live Voice call
# ends. As soon as the user asks ("call me when it's done"), Conduit registers
# a watch on the job's own Hermes session ids here, held while the call goes
# on: Conduit renews the hold during the call and releases it at hang-up. The
# plugin's turn-end hooks (agent process) fire it once that session's turn
# ends, and send the call request through the push relay. The watches and the per-profile call
# settings live in the profile's conduit-calls.json, shared with the hooks
# through calls_store.py (loaded by path: this module can't import the plugin
# package). (hermes-conduit designs/hermes-calls-you-449.md)
#
# A call the user declines or doesn't answer comes back here from Conduit as
# an outcome; the next turn of its chat tells Hermes (calls.outcome_note).

CALLS_LIMIT = 30
CALLS_WINDOW_S = 60.0
CALLS_MAX_BODY_BYTES = 4096
_CALL_WATCH_ID = re.compile(r"^[0-9a-f]{24}\Z")
_calls_limiter = core._MintLimiter(CALLS_LIMIT, CALLS_WINDOW_S, message="Too many call requests; try again shortly")
_calls_store_module: Any = None


def _calls_store() -> Any:
    """calls_store.py in the plugin's folder, loaded once."""
    global _calls_store_module
    with core._calls_store_lock:
        if _calls_store_module is None:
            path = os.path.join(core._plugin_root(), "calls_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_calls_store", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing calls_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _calls_store_module = module
        return _calls_store_module


def calls_status() -> Dict[str, Any]:
    module = _calls_store()
    store = module.CallStore(core._calls_home())
    return {
        "paired": core._load_pairing_state(core._pairing_state_path()) is not None,
        "settings": store.settings(),
        "bounds": {key: {"min": low, "max": high} for key, (low, high) in module.INT_BOUNDS.items()},
        # The longest "what's worth a call" note the host keeps.
        "rules_max": module.MAX_RULES_CHARS,
        "watches": store.watch_count(),
    }


def update_call_settings(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("settings"), dict):
        raise core.TokenError(400, "Expected a JSON object with settings")
    store = _calls_store().CallStore(core._calls_home())
    try:
        return {"settings": store.update_settings(body["settings"])}
    except ValueError as exc:
        raise core.TokenError(400, str(exc))


def add_call_watch(body: Any) -> Dict[str, Any]:
    """Watches a job for the user's call, held for ``hold_s`` seconds.
    ``status`` is ``watching``, or ``ended`` (with ``outcome``) when the
    job's turn ended within the last ``ended_within_s`` seconds: Conduit then
    tells the user itself."""
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    if core._load_pairing_state(core._pairing_state_path()) is None:
        raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
    store = _calls_store().CallStore(core._calls_home())
    try:
        return store.add_watch(body.get("session_ids"), body.get("title", ""),
                               hold_s=body.get("hold_s", 0), ended_within_s=body.get("ended_within_s"))
    except ValueError as exc:
        reason = str(exc)
        if reason == "calls_off":
            raise core.TokenError(409, "Calls are off for this Hermes profile")
        if reason == "too_many":
            raise core.TokenError(429, "Too many jobs are already waiting to call you")
        raise core.TokenError(400, reason)


def hold_call_watch(watch_id: str, body: Any) -> Dict[str, Any]:
    """Renews a watch's hold (``hold_s`` > 0) or releases it (0). Releasing
    one whose job ended during the hold answers ``ended``."""
    if not _CALL_WATCH_ID.match(watch_id):
        raise core.TokenError(400, "Unknown watch id")
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    try:
        return _calls_store().CallStore(core._calls_home()).hold(watch_id, body.get("hold_s"))
    except ValueError as exc:
        raise core.TokenError(400, str(exc))


def remove_call_watch(watch_id: str) -> Dict[str, Any]:
    if not _CALL_WATCH_ID.match(watch_id):
        raise core.TokenError(400, "Unknown watch id")
    return {"removed": _calls_store().CallStore(core._calls_home()).remove_watch(watch_id)}


def set_call_presence(body: Any) -> Dict[str, Any]:
    """The user is in a Live Voice call for ``hold_s`` more seconds (Conduit
    renews it during the call), or not (0): nothing rings meanwhile."""
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    if core._load_pairing_state(core._pairing_state_path()) is None:
        raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
    try:
        return _calls_store().CallStore(core._calls_home()).set_presence(body.get("hold_s"))
    except ValueError as exc:
        raise core.TokenError(400, str(exc))


def record_call_outcome(body: Any) -> Dict[str, Any]:
    """The user declined a call from Hermes, or didn't answer it: the next
    turn of its chat hears so."""
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    if core._load_pairing_state(core._pairing_state_path()) is None:
        raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
    try:
        return _calls_store().CallStore(core._calls_home()).record_outcome(
            body.get("call_id"), body.get("session_ids"), body.get("outcome"), kind=body.get("kind"),
            title=body.get("title", ""), reason=body.get("reason", ""), age_s=body.get("age_s", 0))
    except ValueError as exc:
        raise core.TokenError(400, str(exc))


def call_waiting(session_id: Any) -> Dict[str, Any]:
    """What Hermes waits on in a chat a call is about (a question batch's
    choices, an approval's command and description), for a call answered
    with no chat open: the Apple Watch's. Kept only while alert calls are
    on (calls_store.note_waiting); ``waiting`` is null otherwise."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise core.TokenError(400, "Name the chat's session_id")
    return {"waiting": _calls_store().CallStore(core._calls_home()).waiting_for(session_id)}


def answer_call_waiting(body: Any) -> Dict[str, Any]:
    """The user's spoken answer, on a call from Hermes, to what Hermes waits
    on in its chat: a question's ``answer`` (a choice's number or label, "the
    recommended one", their own words, or "skip" for one of a form) for
    ``question`` (its number in the batch), or an approval's ``choice`` (once
    or deny, with ``reason``). The
    agent's process passes it to Hermes within seconds. An approval's answer
    waits to hear that it did: "answered" only once Hermes took it."""
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    store = _calls_store().CallStore(core._calls_home())
    return _approval_delivered(store, _answer_waiting(store, body))


def _answer_waiting(store: Any, body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object")
    try:
        return store.answer_waiting(
            body.get("session_id"), answer=body.get("answer"), choice=body.get("choice"),
            question=body.get("question"), reason=body.get("reason"))
    except ValueError as exc:
        raise core.TokenError(400, str(exc))


# How a call's approval answer fared, for the call to say (calls_store
# DELIVERY_STATES). Read by the model, so not localized.
_APPROVAL_NOT_DELIVERED = {
    "missed": ("too_late", "It didn't go through: Hermes wasn't waiting on that approval any more (it was "
                              "answered in Conduit, or timed out), so nothing was {done}. The user can check the chat "
                              "in Conduit."),
    "withdrawn": ("failed", "It didn't go through: Hermes didn't pick the answer up in time, so nothing was {done}. "
                            "The user can answer it in Conduit."),
    "claimed": ("unconfirmed", "Hermes hasn't confirmed the answer yet, so it may not have gone through. The user can "
                          "check the chat in Conduit."),
}


# Watch calls waiting at once to hear how their approval answer fared: each
# holds a worker (the Watch tools' pool, or its Local Live call's own) for up
# to calls_store.APPROVAL_DELIVERY_WAIT_S. One user's Watch answers one at a
# time; the bound only keeps a burst from taking the pool. The HTTP route
# waits on the event loop instead (_approval_delivered_async).
_APPROVAL_DELIVERY_WAITS = threading.BoundedSemaphore(2)


def _approval_delivered(store: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    """``answer_waiting``'s result, with an approval's answer as it fared:
    the call says it went only when Hermes took it."""
    answer_id = result.pop("answer_id", None)
    if answer_id is None or result.get("status") != "answered":
        return result
    # A moment for a slot first: a fresh answer is taken back only past that.
    if _APPROVAL_DELIVERY_WAITS.acquire(timeout=1.0):
        try:
            fared = store.approval_delivery(answer_id)
        finally:
            _APPROVAL_DELIVERY_WAITS.release()
    else:
        # Too many waiting already: no wait, so an answer nothing took yet
        # is taken back and the user answers in Conduit.
        fared = store.approval_delivery(answer_id, wait_s=0)
    return _approval_outcome(result, fared)


async def _approval_delivered_async(store: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    """_approval_delivered for a route: it waits on the event loop, holding
    no worker meanwhile."""
    answer_id = result.pop("answer_id", None)
    if answer_id is None or result.get("status") != "answered":
        return result
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _calls_store().APPROVAL_DELIVERY_WAIT_S
    while True:
        fared = await asyncio.to_thread(store.approval_fared, answer_id)
        if fared is not None:
            break
        if loop.time() >= deadline:
            fared = await asyncio.to_thread(store.approval_delivery, answer_id, wait_s=0)
            break
        await asyncio.sleep(0.2)
    return _approval_outcome(result, fared)


def _approval_outcome(result: Dict[str, Any], fared: str) -> Dict[str, Any]:
    if fared == "resolved":
        return result
    status, message = _APPROVAL_NOT_DELIVERED.get(fared, _APPROVAL_NOT_DELIVERED["missed"])
    return {"status": status, "message": message.format(done="approved" if result.get("choice") == "once" else "denied")}


async def _calls_route(profile: Optional[str], response: Response, fn: Callable[[], Dict[str, Any]],
                       route: str) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_calls_limiter, profile, fn)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Calls")


@router.get("/calls")
async def get_calls(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _calls_route(profile, response, calls_status, "status")


@router.put("/calls")
async def put_calls(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: update_call_settings(body), "settings")


@router.post("/calls/watches")
async def post_call_watch(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: add_call_watch(body), "watch")


@router.put("/calls/watches/{watch_id}")
async def put_call_watch(watch_id: str, request: Request, response: Response,
                         profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: hold_call_watch(watch_id, body), "watch")


@router.delete("/calls/watches/{watch_id}")
async def delete_call_watch(watch_id: str, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _calls_route(profile, response, lambda: remove_call_watch(watch_id), "watch")


@router.put("/calls/presence")
async def put_call_presence(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: set_call_presence(body), "presence")


@router.get("/calls/waiting")
async def get_call_waiting(response: Response, session_id: Optional[str] = None,
                           profile: Optional[str] = None) -> Dict[str, Any]:
    return await _calls_route(profile, response, lambda: call_waiting(session_id), "waiting")


@router.post("/calls/waiting/answer")
async def post_call_waiting_answer(request: Request, response: Response,
                                   profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CALLS_MAX_BODY_BYTES)
    stores: list = []

    def answer() -> Dict[str, Any]:
        stores.append(_calls_store().CallStore(core._calls_home()))
        return _answer_waiting(stores[0], body)

    result = await _calls_route(profile, response, answer, "answer")
    if "answer_id" not in result:
        return result
    try:
        return {"ok": True, **(await _approval_delivered_async(stores[0], result))}
    except Exception as exc:
        raise core._unexpected("answer", exc, feature="Calls")


@router.post("/calls/outcomes")
async def post_call_outcome(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CALLS_MAX_BODY_BYTES)
    return await _calls_route(profile, response, lambda: record_call_outcome(body), "outcome")
