"""Conduit settings Hermes may change, and preferences synced across devices.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core

router = core.router


# --- Conduit settings Hermes may change --------------------------------------
#
# Each Conduit uploads the settings it lets Hermes see (and the user's "Let
# Hermes change settings" mode), reports what became of the requests it took,
# and gets back the ones still waiting for it (settings_store.py). Conduit
# stays the owner: it re-checks every request and asks the user as its mode
# says.

CONDUIT_SETTINGS_LIMIT = 30
CONDUIT_SETTINGS_WINDOW_S = 60.0
# Room for the store's worst case: 80 settings, each with a description and
# up to 40 choices.
CONDUIT_SETTINGS_MAX_BODY_BYTES = 640 * 1024

_conduit_settings_limiter = core._MintLimiter(CONDUIT_SETTINGS_LIMIT, CONDUIT_SETTINGS_WINDOW_S,
                                         message="Too many settings syncs; try again shortly")
_settings_store_module: Any = None


def _settings_store() -> Any:
    """settings_store.py in the plugin's folder, loaded once."""
    global _settings_store_module
    with core._calls_store_lock:
        if _settings_store_module is None:
            path = os.path.join(core._plugin_root(), "settings_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_settings_store", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing settings_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _settings_store_module = module
        return _settings_store_module


def sync_conduit_settings(body: Any) -> Dict[str, Any]:
    """The installation id names a device; it doesn't authenticate one. The
    dashboard's auth is the boundary, as on every route here."""
    if core._load_pairing_state(core._pairing_state_path()) is None:
        raise core.TokenError(409, "This Hermes profile isn't paired with Conduit")
    module = _settings_store()
    try:
        return module.SettingsStore(core._calls_home()).sync(body)
    except module.SettingsError as exc:
        raise core.TokenError(400, str(exc))


@router.put("/conduit-settings")
async def put_conduit_settings(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CONDUIT_SETTINGS_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await core._run_scoped(
            profile, core._limited(_conduit_settings_limiter, profile, lambda: sync_conduit_settings(body))))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("settings", exc, feature="Conduit settings")


# --- Conduit preferences synced across devices --------------------------------
#
# Each iPhone and iPad signed in to this profile keeps its preferences here
# (prefs_store.py) so the others pick them up (hermes-conduit#1089). Conduit
# chooses the keys and never sends credentials. Export and import move the
# whole set to another host.

CONDUIT_PREFS_LIMIT = 30
CONDUIT_PREFS_WINDOW_S = 60.0
# The store's whole map, with room for the request around it.
CONDUIT_PREFS_MAX_BODY_BYTES = 600 * 1024

_conduit_prefs_limiter = core._MintLimiter(CONDUIT_PREFS_LIMIT, CONDUIT_PREFS_WINDOW_S,
                                      message="Too many preference syncs; try again shortly")
_prefs_store_module: Any = None


def _prefs_store() -> Any:
    """prefs_store.py in the plugin's folder, loaded once."""
    global _prefs_store_module
    with core._calls_store_lock:
        if _prefs_store_module is None:
            path = os.path.join(core._plugin_root(), "prefs_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_prefs_store", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing prefs_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _prefs_store_module = module
        return _prefs_store_module


def conduit_prefs(action: str, body: Any = None, *, replace: bool = False) -> Dict[str, Any]:
    """The installation id names a device; it doesn't authenticate one. The
    dashboard's auth is the boundary, as on every route here. Unlike
    /conduit-settings this needs no pairing: preferences can be imported on a
    new host, and synced by a device that never turned on notifications."""
    module = _prefs_store()
    store = module.PrefsStore(core._calls_home())
    try:
        if action == "sync":
            return store.sync(body)
        if action == "import":
            return store.import_(body, replace=replace)
        if action == "export":
            return {"export": store.export()}
        return store.view()
    except module.PrefsError as exc:
        raise core.TokenError(413 if exc.code in ("too_large", "too_many") else 400, str(exc))


async def _prefs_route(profile: Optional[str], run: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_conduit_prefs_limiter, profile, run)))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("prefs", exc, feature="Conduit preferences")


@router.get("/conduit-prefs")
async def get_conduit_prefs(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _prefs_route(profile, lambda: conduit_prefs("view"))


@router.put("/conduit-prefs")
async def put_conduit_prefs(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CONDUIT_PREFS_MAX_BODY_BYTES)
    return await _prefs_route(profile, lambda: conduit_prefs("sync", body))


@router.get("/conduit-prefs/export")
async def get_conduit_prefs_export(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _prefs_route(profile, lambda: conduit_prefs("export"))


@router.post("/conduit-prefs/import")
async def post_conduit_prefs_import(request: Request, response: Response, profile: Optional[str] = None,
                                    replace: bool = False) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CONDUIT_PREFS_MAX_BODY_BYTES)
    return await _prefs_route(profile, lambda: conduit_prefs("import", body, replace=replace))
