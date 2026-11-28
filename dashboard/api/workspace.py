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
# File lookups (GET /workspace/resolve) share the readers, with a bucket
# of their own.
_resolve_limiter = core._MintLimiter(ARTIFACTS_LIMIT, ARTIFACTS_WINDOW_S,
                                     message="Too many file lookups; try again shortly")
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


def _workstreams(home: Any) -> List[str]:
    """The profile's bots' workstreams, left out of its Artifacts list as
    hidden chats are: Hermes shows a resolved one (notifier #540)."""
    try:
        return core._plugin_package_module("work_store").Store(home).session_ids()
    except Exception:  # an unreadable registry leaves nothing out
        logger.debug("Conduit artifacts couldn't read the bots' workstreams", exc_info=True)
        return []


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
                                    include_missing=missing != "hide",
                                    workstreams=[] if ids else _workstreams(get_hermes_home()))
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


# /artifacts/pins: the artifacts the user saved (artifact_pins.py in the
# plugin's folder, hermes-conduit#2467), shared by every device signed in to
# the profile. GET lists them, the one saved last first; POST saves one (its
# row from /artifacts, as {"item": …, "note": …}) and keeps a copy of a file
# so it opens after its chat or file is gone, or changes a saved one's note;
# DELETE ?id=<path or URL> unsaves it and removes the copy.

ARTIFACT_PINS_LIMIT = 60
ARTIFACT_PINS_WINDOW_S = 60.0
ARTIFACT_PINS_MAX_BODY_BYTES = 32 * 1024
_artifact_pins_limiter = core._MintLimiter(ARTIFACT_PINS_LIMIT, ARTIFACT_PINS_WINDOW_S,
                                           message="Too many saved-artifact requests; try again shortly")
_artifact_pins_module: Any = None
# Saves copy files: two at a time on workers of their own, so a slow copy
# never holds the shared workers; a third is turned away rather than queued.
_artifact_pins_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-artifact-pins")
_artifact_pins_slots = threading.BoundedSemaphore(2)


def _artifact_pins() -> Any:
    """artifact_pins.py in the plugin's folder, loaded once."""
    global _artifact_pins_module
    with _artifacts_module_lock:
        if _artifact_pins_module is None:
            path = os.path.join(core._plugin_root(), "artifact_pins.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_artifact_pins", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing artifact_pins.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _artifact_pins_module = module
        return _artifact_pins_module


def artifact_pins(action: str, body: Any = None, profile: Optional[str] = None) -> Dict[str, Any]:
    """Runs inside the profile scope: the pins are the profile's."""
    module = _artifact_pins()
    store = module.PinStore(core._calls_home(), sensitive=_secret_path,
                            source=_pin_source(profile) if action == "save" else (lambda path: (None, "unreadable")))
    try:
        if action == "save":
            return store.save(body)
        if action == "remove":
            return store.remove(body)
        return store.view()
    except module.PinError as exc:
        raise core.TokenError(409 if exc.code == "too_many" else 400, str(exc))



class _BoundaryUnknown(Exception):
    """Hermes has a managed-files policy but couldn't say what it is."""


def _managed_boundary() -> Optional[str]:
    """The one folder a hosted or locked install keeps its files under, as
    Hermes' own file routes do there (its real path), or None on an install
    with none: a personal one, or a Hermes from before the policy existed.
    A policy Hermes has but can't read (an unreadable file, an upstream
    change) raises _BoundaryUnknown: an unknown limit is never no limit
    (notifier issue 834)."""
    try:
        from hermes_cli.web_server_files import _managed_files_policy
    except ImportError:
        return None
    try:
        locked = _managed_files_policy(None, create_root=False).locked_root
        boundary = os.path.realpath(str(locked)) if locked is not None else None
    except Exception as exc:
        # On Conduit's plugin page too, until the policy reads again.
        _note_degraded(True)
        global _boundary_unknown_logged
        if not _boundary_unknown_logged:
            _boundary_unknown_logged = True
            logger.warning("Conduit workspace files: Hermes' managed-files policy couldn't be read (%s); "
                           "workspace files are refused until it can", type(exc).__name__)
        raise _BoundaryUnknown() from exc
    _note_degraded(False)
    return boundary


_boundary_unknown_logged = False


def _note_degraded(unknown: bool) -> None:
    """Whether the Folder is refusing for an unreadable files policy, for
    the capabilities route's ``degraded`` (degraded.py); never fails a read."""
    try:
        module = core._plugin_package_module("degraded")
        (module.note if unknown else module.clear)(module.WORKSPACE_FILES)
    except Exception:  # noqa: BLE001 — a plugin folder without degraded.py
        logger.debug("Conduit workspace files: couldn't note the Folder's state", exc_info=True)


def _pin_source(profile: Optional[str]) -> Callable[[str], tuple]:
    """Where on this host a saved file is read from for its copy: only a
    file the Folder would open, as _host_file_check decides. A profile whose
    files are on its SSH machine, or a path past a hosted install's folder,
    is saved without a copy."""
    try:
        from hermes_cli.web_routers.files import _fs_backend, _fs_profile_home
        remote = _fs_backend(profile) is not None
        home = None if remote else _fs_profile_home(profile)
    except Exception:
        # Without Hermes' own routing an SSH profile's path would be read on
        # this host: copy nothing.
        return lambda path: (None, "unreadable")
    if remote:
        return lambda path: (None, "remote")
    try:
        from hermes_cli.web_server_files import _agent_fs_path
    except Exception:
        _agent_fs_path = None
    try:
        boundary = _managed_boundary()
    except _BoundaryUnknown:
        # Saved without a copy: where the copy may come from isn't known.
        return lambda path: (None, "unreadable")

    def source(path: str) -> tuple:
        try:
            target = str(_agent_fs_path(path, profile_home=home)) if _agent_fs_path is not None else path
        except Exception:  # saved without a copy rather than not at all
            return None, "unreadable"
        if boundary is not None:
            # A path that can't be resolved can't be shown to be inside the
            # folder: it says so, rather than that it can't be read.
            try:
                real = os.path.realpath(target)
            except (OSError, ValueError):
                return None, "outside"
            if real != boundary and not real.startswith(boundary.rstrip(os.sep) + os.sep):
                return None, "outside"
        return target, None

    return source


def _secret_path(path: str) -> bool:
    """A file the Folder won't show (``.env``, keys, credentials): saved
    without a copy, so its contents never sit under another name."""
    try:
        if _workspace_files().default_sensitive(path):
            return True
    except Exception:  # without the denylist, nothing is copied
        return True
    try:
        from hermes_cli.web_routers.files import _is_sensitive_path
    except Exception:
        return False
    try:
        return bool(_is_sensitive_path(Path(path)))
    except Exception:
        return True


async def _artifact_pins_route(profile: Optional[str], run: Callable[[], Dict[str, Any]],
                               copies: bool = False) -> Dict[str, Any]:
    job = core._limited(_artifact_pins_limiter, profile, run)
    executor = None
    # A save holds its slot until its copy ends, as /artifacts holds its
    # reader: freed by the job once it started, else by this request.
    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()
    if copies:
        if not _artifact_pins_slots.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Hermes is still saving artifacts; try again shortly",
                                headers={"Cache-Control": "no-store"})
        executor = _artifact_pins_executor
        limited = job

        def job() -> Dict[str, Any]:
            with state_lock:
                if state["abandoned"]:
                    # Nobody awaits this answer any more: say so without
                    # raising, so no unretrieved exception is logged.
                    return {"cancelled": True}
                state["started"] = True
            try:
                return limited()
            finally:
                _artifact_pins_slots.release()

    try:
        return {"ok": True, **(await core._run_scoped(profile, job, executor))}
    except BaseException as exc:
        if copies:
            with state_lock:
                if not state["started"]:
                    state["abandoned"] = True
                    _artifact_pins_slots.release()
        if isinstance(exc, core.TokenError):
            raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
        if isinstance(exc, HTTPException):
            exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
            raise
        if isinstance(exc, Exception):
            raise core._unexpected("pins", exc, feature="Saved artifacts")
        raise


@router.get("/artifacts/pins")
async def get_artifact_pins(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _artifact_pins_route(profile, lambda: artifact_pins("view"))


@router.post("/artifacts/pins")
async def post_artifact_pin(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, ARTIFACT_PINS_MAX_BODY_BYTES)
    return await _artifact_pins_route(profile, lambda: artifact_pins("save", body, profile), copies=True)


@router.delete("/artifacts/pins")
async def delete_artifact_pin(response: Response, id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _artifact_pins_route(profile, lambda: artifact_pins("remove", id))



# GET /workspace/resolve: where a file a chat's reply names by its name or a
# relative path is (hermes-conduit#1809), from the chat's folder, its git
# repository's top, the terminal's folder for a chat with none, and its own
# tool calls (artifacts_index.resolve_name). Conduit asks when such a link is
# tapped, with each id the chat goes by, and opens the Folder at the answer.

def resolve_file(name: str, session_ids: Optional[List[str]], profile: Optional[str]) -> Dict[str, Any]:
    """Where ``name`` is, for the profile's chat (called inside the profile scope)."""
    from hermes_constants import get_hermes_home

    ids = [sid.strip() for sid in session_ids or []
           if sid and sid.strip() and len(sid) <= ARTIFACTS_MAX_ID_LENGTH][:ARTIFACTS_MAX_SESSIONS]
    index = _artifacts_index()
    try:
        conn = index.open_db(get_hermes_home())
    except FileNotFoundError:
        return {"path": None}
    try:
        exists = _host_file_check(profile)
        found = index.resolve_name(conn, ids, name, exists=exists, default_folder=_terminal_folder())
    finally:
        conn.close()
    if found.get("path"):
        # A folder opens on its entries (desktop issue 686). A profile whose
        # files are on its SSH machine asks that machine (notifier#1139); if
        # it can't, a name written with its trailing slash is a folder there.
        is_folder = getattr(exists, "is_folder", None)
        if is_folder is not None:
            found["isDirectory"] = bool(is_folder(os.path.expanduser(found["path"])))
        else:
            found["isDirectory"] = _remote_is_folder(profile, found["path"], name)
    return found


def _remote_is_folder(profile: Optional[str], path: str, name: str) -> bool:
    guess = name.strip().endswith(("/", "\\"))
    try:
        workspace = _workspace(profile)
        check = getattr(workspace, "is_folder", None)
        return bool(check(path)) if check is not None else guess
    except Exception:  # the SSH machine didn't answer: the name's own slash says
        logger.debug("Conduit workspace resolve: couldn't ask the SSH machine about %s", path, exc_info=True)
        return guess


def _terminal_folder() -> Optional[str]:
    """The profile's configured terminal folder (``terminal.cwd``), which
    Hermes' file tools resolve a relative path against when a chat recorded
    no folder of its own; None when it isn't set to an absolute folder on
    this host (unset, ``.``, ``auto``, or a sandbox's own path). Read inside
    the profile scope."""
    try:
        from hermes_cli.config import load_config
        terminal = load_config().get("terminal") or {}
    except Exception:
        return None
    if not isinstance(terminal, dict):
        return None
    backend = str(terminal.get("backend") or "local").strip().lower()
    raw = terminal.get("cwd")
    if backend != "local" or not isinstance(raw, str):
        return None
    folder = os.path.expanduser(raw.strip())
    return folder if os.path.isabs(folder) else None


def _host_file_check(profile: Optional[str]) -> Callable[[str], bool]:
    """Whether a path the chat named is one the Folder would open: not a
    secret file, inside a hosted install's folder, and (on this host, through
    Hermes' own mapping of a Docker profile's paths) a file or a folder
    (desktop issue 686; ``is_folder`` tells which). A profile whose
    files are on its SSH machine checks only the first, and the folder's
    join, else the newest path, is taken."""
    # Without the plugin's own denylist, refuse as the Folder's routes do.
    sensitive_default = _workspace_files().default_sensitive
    try:
        from hermes_cli.web_routers.files import _fs_backend, _fs_profile_home, _is_sensitive_path
        remote = _fs_backend(profile) is not None
        home = None if remote else _fs_profile_home(profile)
    except Exception:
        # Without Hermes' own routing an SSH profile's paths would be checked
        # on this host and its secret-file check lost: refuse, as the Folder does.
        raise core.TokenError(501, "This Hermes can't route workspace files; update Hermes")
    try:
        from hermes_cli.web_server_files import _agent_fs_path
    except Exception:
        global _workspace_unmapped_logged
        if not _workspace_unmapped_logged:
            _workspace_unmapped_logged = True
            logger.warning("Conduit workspace files: Hermes has no _agent_fs_path; Docker paths aren't mapped")
        _agent_fs_path = None
    boundary = None
    if not remote:
        try:
            boundary = _managed_boundary()
        except _BoundaryUnknown:
            # No file is one the Folder would open while its limit is unknown.
            unknown = lambda path: False
            unknown.checks_host = True  # type: ignore[attr-defined]
            return unknown

    def host_path(path: str) -> str:
        return str(_agent_fs_path(path, profile_home=home)) if _agent_fs_path is not None else path

    def exists(path: str) -> bool:
        try:
            if sensitive_default is not None and sensitive_default(path):
                return False
            if _is_sensitive_path is not None and _is_sensitive_path(Path(path)):
                return False
            if remote:
                return True
            target = host_path(path)
            if boundary is not None:
                real = os.path.realpath(target)
                if real != boundary and not real.startswith(boundary.rstrip(os.sep) + os.sep):
                    return False
            return os.path.isfile(target) or os.path.isdir(target)
        except Exception:
            return False

    if not remote:
        def is_folder(path: str) -> bool:
            try:
                return os.path.isdir(host_path(path))
            except Exception:
                return False

        exists.is_folder = is_folder  # type: ignore[attr-defined]

    # An SSH profile's answer isn't a check of the file, so quoted prose
    # ending in a name can't be told from a quoted path there.
    exists.checks_host = not remote
    return exists


@router.get("/workspace/resolve")
async def get_workspace_resolve(response: Response, path: str, session_id: Optional[List[str]] = Query(None),
                                profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    # The slot is freed exactly once, as for /artifacts.
    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()

    def job() -> Dict[str, Any]:
        with state_lock:
            if state["abandoned"]:
                return {"path": None}
            state["started"] = True
        try:
            return resolve_file(path, session_id, profile)
        finally:
            _artifacts_slots.release()

    try:
        await core._scoped_acquire(_resolve_limiter, profile)
        if not _artifacts_slots.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Hermes is still reading chats; try again shortly",
                                headers=no_store)
        try:
            result = await asyncio.wait_for(core._run_scoped(profile, job, _artifacts_executor),
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
        raise HTTPException(status_code=504, detail="Finding that file took too long; try again", headers=no_store)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except sqlite3.Error as exc:
        logger.warning("Conduit couldn't read the session store to find a file: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Hermes' session store is busy; try again shortly",
                            headers=no_store)
    except Exception as exc:
        raise core._unexpected("resolve", exc, feature="Workspace files")
    return {"ok": True, **result}

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
        boundary = _managed_boundary()
    except _BoundaryUnknown:
        raise core.TokenError(503, "Hermes couldn't read which folder its files are kept in; "
                                   "check the host's files settings and try again")
    return module.LocalWorkspace(resolve, sensitive, boundary=boundary)


async def _workspace_call(profile: Optional[str], op: Callable[[Any], Dict[str, Any]], *, walks: bool = False,
                          name: str = "request", limited: bool = False, changes: bool = False) -> Dict[str, Any]:
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
        try:
            _workspace_files()  # loaded first, so its error type is known below
        except BaseException:
            # No walk ran to free its slot: two such failures would
            # otherwise refuse every search until the dashboard restarts.
            if walks:
                _workspace_walks.release()
            raise
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
        # A change that started keeps running on its thread and may still
        # land: the answer says so, and Conduit reads the folder again
        # rather than repeating a change whose result it doesn't know
        # (notifier issue 834).
        if changes and state["started"]:
            raise HTTPException(status_code=504, detail="That's taking too long and may still finish; "
                                                        "check the folder before trying again", headers=no_store)
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
        # Paste: a copy in another folder (capability workspace-copy).
        to = body.get("to")
        if to is not None and not isinstance(to, str):
            raise HTTPException(status_code=400, detail="to must be a folder's path", headers=no_store)
        to = _workspace_text(body, "to") if to is not None else None
        op = lambda ws: ws.duplicate(root, path, to)
    elif action == "delete":
        path = _workspace_text(body, "path")
        op = lambda ws: ws.delete(root, path)
    else:
        folder, name = _workspace_text(body, "path"), _workspace_text(body, "name")
        # An empty file is a file: "" decodes to no bytes and uploads as one (issue 1708).
        encoded = body.get("data")
        if not isinstance(encoded, str):
            raise HTTPException(status_code=400, detail="data is required", headers=no_store)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=400, detail="data must be base64", headers=no_store)
        op = lambda ws: ws.upload(root, folder, name, data, overwrite)
    return await _workspace_call(profile, op, name=action, limited=True, changes=True)
