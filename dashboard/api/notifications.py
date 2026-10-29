"""Cron and reply notifications, the chat Conduit has on screen, and confirm
prompts.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core

router = core.router


# --- Cron notifications ------------------------------------------------------
#
# The user turns one cron job's notifications off in Conduit's Cron tab. The
# muted job ids live in the profile's conduit-cron.json, shared with the
# plugin's turn-end hooks (agent process), which skip the push for that job's
# runs, through cron_store.py (loaded by path: this module can't import the
# plugin package). A run that answers [SILENT] never notifies either way.

CRON_LIMIT = 60
CRON_WINDOW_S = 60.0
CRON_MAX_BODY_BYTES = 1024
_cron_limiter = core._MintLimiter(CRON_LIMIT, CRON_WINDOW_S, message="Too many cron requests; try again shortly")
_cron_store_module: Any = None
_cron_store_lock = threading.Lock()


def _cron_store() -> Any:
    """cron_store.py in the plugin's folder, loaded once."""
    global _cron_store_module
    with _cron_store_lock:
        if _cron_store_module is None:
            path = os.path.join(core._plugin_root(), "cron_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_cron_store", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing cron_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _cron_store_module = module
        return _cron_store_module


def _cron_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def cron_notifications() -> Dict[str, Any]:
    return {"muted": _cron_store().CronStore(_cron_home()).muted()}


def update_cron_notifications(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object with job_id and notify")
    try:
        muted = _cron_store().CronStore(_cron_home()).set_notify(body.get("job_id"), body.get("notify"))
    except ValueError as exc:
        raise core.TokenError(400, str(exc))
    return {"muted": muted}


async def _cron_route(profile: Optional[str], response: Response, fn: Callable[[], Dict[str, Any]],
                      route: str, feature: str = "Cron notifications") -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_cron_limiter, profile, fn)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected(route, exc, feature=feature)


@router.get("/cron/notifications")
async def get_cron_notifications(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _cron_route(profile, response, cron_notifications, "status")


@router.put("/cron/notifications")
async def put_cron_notifications(request: Request, response: Response,
                                 profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CRON_MAX_BODY_BYTES)
    return await _cron_route(profile, response, lambda: update_cron_notifications(body), "settings")


# --- Reply notifications by source (#115) ------------------------------------
#
# The user picks in Conduit which sources' replies push "reply ready": Conduit,
# Hermes Desktop and the web dashboard, messaging apps, the terminal. The
# choice lives in the profile's conduit-notifications.json, read by the
# plugin's turn-end hook (agent process) through reply_sources.py (loaded by
# path: this module can't import the plugin package).

_reply_sources_module: Any = None
_reply_sources_lock = threading.Lock()


def _reply_sources() -> Any:
    """reply_sources.py in the plugin's folder, loaded once."""
    global _reply_sources_module
    with _reply_sources_lock:
        if _reply_sources_module is None:
            path = os.path.join(core._plugin_root(), "reply_sources.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_reply_sources", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing reply_sources.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _reply_sources_module = module
        return _reply_sources_module


def reply_source_settings() -> Dict[str, Any]:
    return {"sources": _reply_sources().ReplySourcesStore(_cron_home()).settings()}


def update_reply_source_settings(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise core.TokenError(400, "Expected a JSON object with source and notify")
    try:
        sources = _reply_sources().ReplySourcesStore(_cron_home()).set_notify(body.get("source"), body.get("notify"))
    except ValueError as exc:
        raise core.TokenError(400, str(exc))
    return {"sources": sources}


@router.get("/notifications/reply-sources")
async def get_reply_sources(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    return await _cron_route(profile, response, reply_source_settings, "status", feature="Reply notifications")


@router.put("/notifications/reply-sources")
async def put_reply_sources(request: Request, response: Response,
                            profile: Optional[str] = None) -> Dict[str, Any]:
    body = await core._read_json_body(request, CRON_MAX_BODY_BYTES)
    return await _cron_route(profile, response, lambda: update_reply_source_settings(body), "settings",
                             feature="Reply notifications")


# --- A chat Conduit has on screen (chat_on_screen.py) ------------------------
#
# While a turn runs in a chat open in Conduit, Conduit holds it here as on
# screen, renewing the hold while the turn runs and dropping it (hold 0) when
# the user leaves the chat or the app. A reply that lands in it then sends no
# "reply ready" push: the user watched it arrive. The holds live in the
# profile's home, read by the plugin's turn-end hook (agent process).

ON_SCREEN_LIMIT = 60
ON_SCREEN_WINDOW_S = 60.0
ON_SCREEN_MAX_BODY_BYTES = 4096
_on_screen_limiter = core._MintLimiter(ON_SCREEN_LIMIT, ON_SCREEN_WINDOW_S,
                                  message="Too many on-screen reports; try again shortly")
_chat_on_screen_module: Any = None
_chat_on_screen_lock = threading.Lock()


def _chat_on_screen() -> Any:
    """chat_on_screen.py in the plugin's folder, loaded once."""
    global _chat_on_screen_module
    with _chat_on_screen_lock:
        if _chat_on_screen_module is None:
            path = os.path.join(core._plugin_root(), "chat_on_screen.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_chat_on_screen", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing chat_on_screen.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _chat_on_screen_module = module
        return _chat_on_screen_module


def set_chat_on_screen(body: Any) -> Dict[str, Any]:
    module = _chat_on_screen()
    try:
        device, session_ids, hold_s = module.parse(body)
    except ValueError as exc:
        raise core.TokenError(400, str(exc))
    return {"on_screen": module.hold(_cron_home(), device, session_ids, hold_s)}


@router.put("/notifications/on-screen")
async def put_chat_on_screen(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    try:
        body = await core._read_json_body(request, ON_SCREEN_MAX_BODY_BYTES)
        return {"ok": True, **(await core._run_scoped(
            profile, core._limited(_on_screen_limiter, profile, lambda: set_chat_on_screen(body))))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise core._unexpected("on-screen", exc, feature="Reply notifications")


# --- Confirm prompts Hermes shows on the phone ---------------------------------
#
# Before an action the user should OK, conduit_present keeps a confirm prompt
# (present_store.py) and wakes the phone with phone.changes. Conduit reports
# what the user answered (tap or voice) and gets back the prompts still
# waiting, with the same identity fields as /conduit-settings.

PRESENT_EXCHANGE_LIMIT = 60
PRESENT_EXCHANGE_WINDOW_S = 60.0
PRESENT_MAX_BODY_BYTES = 512 * 1024  # room for 40 results with 2,000-character edits in any script, even \u-escaped

_present_exchange_limiter = core._MintLimiter(PRESENT_EXCHANGE_LIMIT, PRESENT_EXCHANGE_WINDOW_S,
                                         message="Too many confirm prompt checks; try again shortly")
_present_store_module: Any = None


def _present_store() -> Any:
    """present_store.py in the plugin's folder, loaded once."""
    global _present_store_module
    with core._calls_store_lock:
        if _present_store_module is None:
            path = os.path.join(core._plugin_root(), "present_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_present_store", path)
            if spec is None or spec.loader is None or not os.path.exists(path):
                raise core.TokenError(501, "This plugin install is missing present_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _present_store_module = module
        return _present_store_module


def exchange_present(body: Any) -> Dict[str, Any]:
    """The installation id names a device; it doesn't authenticate one. The
    dashboard's auth is the boundary, as on every route here."""
    if core._load_pairing_state(core._pairing_state_path()) is None:
        raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
    module = _present_store()
    try:
        return module.PresentStore(core._calls_home()).exchange(body)
    except module.PresentError as exc:
        raise core.TokenError(400, str(exc))
    except OSError:
        # The store couldn't be read or written right now; the phone keeps
        # its answers and exchanges again.
        raise core.TokenError(503, "Confirm prompts are unavailable right now")


@router.post("/present/exchange")
async def post_present_exchange(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, PRESENT_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await core._run_scoped(
            profile, core._limited(_present_exchange_limiter, profile, lambda: exchange_present(body))))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("present", exc, feature="Confirm prompts")
