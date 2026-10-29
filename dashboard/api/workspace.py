"""Artifacts and workspace files."""

from __future__ import annotations

import asyncio
import base64
import binascii
import importlib.util
import math
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException, Query, Request, Response

from . import core

logger = core.logger
router = core.router


# --- Artifacts ---------------------------------------------------------------
#
# GET /artifacts lists what a profile's chats produced (artifacts_index.py
# in the plugin's folder): files Hermes sent or wrote, the HTML pages it drew,
# files the user attached and links, newest first. Conduit's Artifacts screen
# reads every chat (no session_id) and a chat's own sheet reads one (each id
# it's known by as a session_id, with the sessions it continues). Paged by
# message id: pass the previous page's next_before as before. missing=hide
# leaves out files no longer on the host (an older plugin ignores it).

ARTIFACTS_LIMIT = 60
ARTIFACTS_WINDOW_S = 60.0
# The ids one chat is known by (its runtime and stored ids).
ARTIFACTS_MAX_SESSIONS = 16
ARTIFACTS_MAX_ID_LENGTH = 200
# Each read is bounded (rows scanned, sqlite's busy wait); this backstops it.
ARTIFACTS_TIMEOUT_S = 25.0
_artifacts_limiter = core._MintLimiter(ARTIFACTS_LIMIT, ARTIFACTS_WINDOW_S,
                                  message="Too many artifact requests; try again shortly")
_artifacts_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-artifacts")
# A timed-out read keeps its worker until it ends, so a request finding both
# busy is turned away rather than queued behind them.
_artifacts_slots = threading.BoundedSemaphore(2)
_artifacts_module: Any = None
_artifacts_module_lock = threading.Lock()


def _artifacts_index() -> Any:
    """artifacts_index.py in the plugin's folder, loaded once."""
    global _artifacts_module
    with _artifacts_module_lock:
        if _artifacts_module is None:
            path = os.path.join(core._plugin_root(), "artifacts_index.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_artifacts_index", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing artifacts_index.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _artifacts_module = module
        return _artifacts_module


def list_artifacts(before: Optional[int] = None, session_ids: Optional[List[str]] = None,
                   kind: Optional[str] = None, q: str = "", limit: Optional[int] = None,
                   missing: Optional[str] = None) -> Dict[str, Any]:
    """One page of the profile's artifacts (called inside the profile scope)."""
    from hermes_constants import get_hermes_home

    index = _artifacts_index()
    if kind and kind not in index.GROUPS:
        raise core.TokenError(400, "kind must be images, pages, files or links")
    if missing not in (None, "", "show", "hide"):
        raise core.TokenError(400, "missing must be show or hide")
    try:
        conn = index.open_db(get_hermes_home())
    except FileNotFoundError:
        return {"items": [], "next_before": None}
    try:
        ids = [sid.strip() for sid in session_ids or []
               if sid and sid.strip() and len(sid) <= ARTIFACTS_MAX_ID_LENGTH][:ARTIFACTS_MAX_SESSIONS]
        if session_ids and not ids:
            # A chat asked for by ids none of which can name one: never
            # every chat instead.
            return {"items": [], "next_before": None}
        return index.list_artifacts(conn, before=before, session_ids=ids, group=kind or None,
                                    query=q or "", limit=index.DEFAULT_LIMIT if limit is None else limit,
                                    include_missing=missing != "hide")
    finally:
        conn.close()


@router.get("/artifacts")
async def get_artifacts(response: Response, profile: Optional[str] = None, before: Optional[int] = None,
                        session_id: Optional[List[str]] = Query(None), kind: Optional[str] = None,
                        q: Optional[str] = None, limit: Optional[int] = None,
                        missing: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    # The slot is freed exactly once: by the read when it ran, else by this
    # request (a read it gave up on before it started never runs).
    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()

    def job() -> Dict[str, Any]:
        with state_lock:
            if state["abandoned"]:
                return {"items": [], "next_before": None}
            state["started"] = True
        try:
            return list_artifacts(before, session_id, kind, q or "", limit, missing)
        finally:
            _artifacts_slots.release()

    try:
        await core._scoped_acquire(_artifacts_limiter, profile)
        if not _artifacts_slots.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Hermes is still reading artifacts; try again shortly",
                                headers={"Cache-Control": "no-store"})
        try:
            page = await asyncio.wait_for(core._run_scoped(profile, job, _artifacts_executor),
                                          timeout=ARTIFACTS_TIMEOUT_S)
        except BaseException:
            with state_lock:
                if not state["started"]:
                    state["abandoned"] = True
                    _artifacts_slots.release()
            raise
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.warning("Conduit artifacts list timed out after %ss", ARTIFACTS_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Listing artifacts took too long; try again",
                            headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except sqlite3.Error as exc:
        logger.warning("Conduit artifacts couldn't read the session store: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Hermes' session store is busy; try again shortly",
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("list", exc, feature="Artifacts")
    return {"ok": True, **page}


# --- Workspace files -----------------------------------------------------------
#
# Conduit's Folder tab (workspace_files.py in the plugin's folder): one root at a
# time (the chat's working folder or a project folder), listed with sizes and
# dates, searched by name and inside files, its recent files, and new
# folders and files, saves that refuse a file changed meanwhile, rename and
# move, duplicate, delete and upload. Every path stays inside the root it
# names, and files Hermes keeps from its own file routes stay out. An SSH
# profile works through Hermes' SSH adapter, a Docker profile's container
# paths through its host mount, as Hermes' /api/fs routes resolve them.

WORKSPACE_LIMIT = 240
WORKSPACE_WINDOW_S = 60.0
WORKSPACE_TIMEOUT_S = 30.0
WORKSPACE_MAX_BODY_BYTES = 36 * 1024 * 1024
_workspace_limiter = core._MintLimiter(WORKSPACE_LIMIT, WORKSPACE_WINDOW_S,
                                  message="Too many file requests; try again shortly")
_workspace_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conduit-workspace")
# Searches walk whole trees: two at a time, a third is turned away.
_workspace_walks = threading.BoundedSemaphore(2)
_workspace_module: Any = None
_workspace_module_lock = threading.Lock()
_workspace_unmapped_logged = False


def _workspace_files() -> Any:
    """workspace_files.py in the plugin's folder, loaded once."""
    global _workspace_module
    with _workspace_module_lock:
        if _workspace_module is None:
            path = os.path.join(core._plugin_root(), "workspace_files.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_workspace_files", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing workspace_files.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _workspace_module = module
        return _workspace_module


def _workspace(profile: Optional[str]) -> Any:
    """The profile's workspace backend: its SSH machine, or this host."""
    module = _workspace_files()
    try:
        from hermes_cli.web_routers.files import _fs_backend, _fs_profile_home, _is_sensitive_path
    except Exception:
        # Without Hermes' own routing an SSH profile would land on this host
        # and its secret-file check would be lost: refuse instead.
        raise core.TokenError(501, "This Hermes can't route workspace files; update Hermes")

    def sensitive(path: str) -> bool:
        return module.default_sensitive(path) or _is_sensitive_path(Path(path))

    remote = _fs_backend(profile)
    if remote is not None:
        return module.SshWorkspace(remote, sensitive)
    home = _fs_profile_home(profile)
    try:
        from hermes_cli.web_server_files import _agent_fs_path
    except Exception:
        # An older Hermes without Docker path mapping: paths are taken as
        # this host's, which they are there.
        global _workspace_unmapped_logged
        if not _workspace_unmapped_logged:
            _workspace_unmapped_logged = True
            logger.warning("Conduit workspace files: Hermes has no _agent_fs_path; Docker paths aren't mapped")
        _agent_fs_path = None

    def resolve(raw: str) -> str:
        return str(_agent_fs_path(raw, profile_home=home)) if _agent_fs_path is not None else raw

    # A hosted or locked install keeps its files under one folder, as
    # Hermes' own file routes do there; a personal install has none.
    try:
        from hermes_cli.web_server_files import _managed_files_policy
        boundary = _managed_files_policy(None, create_root=False).locked_root
    except Exception:
        boundary = None
    return module.LocalWorkspace(resolve, sensitive, boundary=str(boundary) if boundary is not None else None)


async def _workspace_call(profile: Optional[str], op: Callable[[Any], Dict[str, Any]], *, walks: bool = False,
                          name: str = "request", limited: bool = False) -> Dict[str, Any]:
    no_store = {"Cache-Control": "no-store"}
    if not limited:
        await core._scoped_acquire(_workspace_limiter, profile)
    if walks and not _workspace_walks.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Hermes is still searching; try again shortly", headers=no_store)
    # A walk's slot is freed exactly once: by the walk when it ran (even
    # after this request stopped waiting), else by this request.
    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()

    def job() -> Dict[str, Any]:
        with state_lock:
            if state["abandoned"]:
                return {}
            state["started"] = True
        try:
            return op(_workspace(profile))
        finally:
            if walks:
                _workspace_walks.release()

    try:
        _workspace_files()  # loaded first, so its error type is known below
        try:
            result = await asyncio.wait_for(core._run_scoped(profile, job, _workspace_executor),
                                            timeout=WORKSPACE_TIMEOUT_S)
        except BaseException:
            with state_lock:
                if not state["started"]:
                    state["abandoned"] = True
                    if walks:
                        _workspace_walks.release()
            raise
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="That took too long; try again", headers=no_store)
    except Exception as exc:
        workspace_error = getattr(_workspace_module, "WorkspaceError", None)
        if workspace_error is not None and isinstance(exc, workspace_error):
            raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
        raise core._unexpected(name, exc, feature="Workspace files")
    return {"ok": True, **result}


@router.get("/workspace/list")
async def get_workspace_list(response: Response, root: str, path: Optional[str] = None,
                             profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _workspace_call(profile, lambda ws: ws.list_dir(root, path or root), name="list")


@router.get("/workspace/search")
async def get_workspace_search(response: Response, root: str, q: str = "", contents: bool = True,
                               profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _workspace_call(profile, lambda ws: ws.search(root, q, contents), walks=True, name="search")


@router.get("/workspace/recent")
async def get_workspace_recent(response: Response, root: str, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _workspace_call(profile, lambda ws: ws.recent(root), walks=True, name="recent")


def _workspace_text(body: Dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{key} is required", headers={"Cache-Control": "no-store"})
    return value


def _workspace_mtime(body: Dict[str, Any]) -> Optional[float]:
    value = body.get("expected_mtime")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(status_code=400, detail="expected_mtime must be a number",
                            headers={"Cache-Control": "no-store"})
    return float(value)


@router.post("/workspace/{action}")
async def post_workspace(action: str, request: Request, response: Response,
                         profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    if action not in {"mkdir", "new-file", "save", "move", "duplicate", "delete", "upload"}:
        raise HTTPException(status_code=404, detail="Not found", headers=no_store)
    # Counted before the body is read, so a flood of large bodies is turned
    # away before any of it is buffered.
    await core._scoped_acquire(_workspace_limiter, profile)
    body = await core._read_json_body(request, WORKSPACE_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object", headers=no_store)
    root = _workspace_text(body, "root")
    overwrite = body.get("overwrite") is True
    if action == "mkdir":
        folder, name = _workspace_text(body, "path"), _workspace_text(body, "name")
        op = lambda ws: ws.mkdir(root, folder, name)
    elif action == "new-file":
        folder, name = _workspace_text(body, "path"), _workspace_text(body, "name")
        content = body.get("content") if isinstance(body.get("content"), str) else ""
        op = lambda ws: ws.new_file(root, folder, name, content)
    elif action == "save":
        path, content = _workspace_text(body, "path"), body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content is required", headers=no_store)
        expected = _workspace_mtime(body)
        op = lambda ws: ws.save(root, path, content, expected)
    elif action == "move":
        path, folder, name = _workspace_text(body, "path"), _workspace_text(body, "to"), _workspace_text(body, "name")
        op = lambda ws: ws.move(root, path, folder, name, overwrite)
    elif action == "duplicate":
        path = _workspace_text(body, "path")
        op = lambda ws: ws.duplicate(root, path)
    elif action == "delete":
        path = _workspace_text(body, "path")
        op = lambda ws: ws.delete(root, path)
    else:
        folder, name = _workspace_text(body, "path"), _workspace_text(body, "name")
        try:
            data = base64.b64decode(_workspace_text(body, "data"), validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=400, detail="data must be base64", headers=no_store)
        op = lambda ws: ws.upload(root, folder, name, data, overwrite)
    return await _workspace_call(profile, op, name=action, limited=True)
