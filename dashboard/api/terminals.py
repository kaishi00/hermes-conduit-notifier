"""Terminals: shells on the Hermes host in a chat's folder, for the coding workspace."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from . import core
from . import grok_live
from . import workspace

logger = core.logger
router = core.router


# --- Terminals -----------------------------------------------------------------
#
# Conduit for desktop's workbench (issue 450) opens shells on the Hermes host,
# started in the open chat's folder (terminal_host.py in the plugin's folder).
# The folder is a root the Folder tab reaches, checked by the same workspace
# (workspace._workspace): a terminal is a full shell, so it is refused where
# that workspace keeps to one folder (a hosted or locked install) and on an
# SSH profile, whose machine this process can't give a terminal. The person
# can turn terminals off for this Hermes (Conduit's Settings > Terminals).
#
# GET    /terminals            ?root=          this profile's terminals (in root)
# POST   /terminals            {root, title?, cols?, rows?}   a new one
# POST   /terminals/rename     {id, title}
# DELETE /terminals            ?id=            closes it and ends its shell
# GET    /terminals/settings   POST /terminals/settings {enabled}
# WS     /terminals/socket     ?id=&since=     output from byte ``since`` on;
#        frames: {type: hello|output|exit} out, {type: input|resize} in
#
# A terminal keeps running with no socket open, its last 256 KB kept, until
# it's closed, its shell exits, a week passes with nothing showing it, or the
# dashboard stops.

TERMINALS_LIMIT = 240
TERMINALS_WINDOW_S = 60.0
TERMINALS_TIMEOUT_S = 15.0
TERMINALS_MAX_BODY_BYTES = 16 * 1024
TERMINALS_SETTINGS_FILE = "conduit-terminals.json"
# Queued output chunks a socket may fall behind by before it's dropped; the
# client reconnects from the last byte it drew.
TERMINALS_SOCKET_BACKLOG = 2000
TERMINALS_MAX_FRAME_CHARS = 128 * 1024
CLOSE_UNAUTHORIZED, CLOSE_REFUSED, CLOSE_GONE, CLOSE_SLOW, CLOSE_FAILED = 4401, 4403, 4404, 4408, 4500
_terminals_limiter = core._MintLimiter(TERMINALS_LIMIT, TERMINALS_WINDOW_S,
                                       message="Too many terminal requests; try again shortly")
_terminals_auth_failure_limiter = core._MintLimiter(30, 60.0, message="Too many refused terminal sockets")
_terminals_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-terminals")
# Typing goes through its own workers, so a shell that isn't reading never holds up the routes.
_terminal_input_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conduit-terminal-input")
_settings_lock = threading.Lock()
NO_STORE = {"Cache-Control": "no-store"}


def _host_module() -> Any:
    return core._plugin_package_module("terminal_host")


def _settings_path() -> str:
    root = core._plugin_package_module("room_identity").root_home(core._calls_home())
    return os.path.join(str(root), TERMINALS_SETTINGS_FILE)


def terminals_enabled() -> bool:
    """On unless the person turned them off for this Hermes."""
    try:
        with open(_settings_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        # Unreadable: an unknown switch is an off switch.
        return False
    return not (isinstance(data, dict) and data.get("enabled") is False)


def _save_enabled(enabled: bool) -> None:
    path = _settings_path()
    temp = f"{path}.{os.getpid()}.tmp"
    with _settings_lock:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"enabled": enabled}, handle)
        os.replace(temp, path)


def _why_not(profile: Optional[str]) -> Optional[tuple]:
    """(status, reason) when this profile can't have terminals, else None."""
    if not _host_module().supported():
        return 501, "Terminals need a Linux or Mac Hermes host"
    if not terminals_enabled():
        return 403, "Terminals are turned off on this Hermes"
    try:
        boundary = workspace._managed_boundary()
    except Exception:
        return 503, "Hermes couldn't read which folder its files are kept in"
    if boundary is not None:
        return 403, "Terminals are off on a hosted Hermes"
    return None


def _root(profile: Optional[str], raw: Any) -> str:
    ws = workspace._workspace(profile)
    if getattr(ws, "_fs", None) is not None:
        raise core.TokenError(501, "Terminals aren't available for SSH profiles yet")
    try:
        return ws.root(raw)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status is not None:
            raise core.TokenError(status, str(exc))
        raise


async def _call(profile: Optional[str], op, name: str, check: bool = True) -> Dict[str, Any]:
    await core._scoped_acquire(_terminals_limiter, profile)

    def job() -> Dict[str, Any]:
        if check:
            refused = _why_not(profile)
            if refused is not None:
                raise core.TokenError(*refused)
        return op()

    try:
        result = await asyncio.wait_for(core._run_scoped(profile, job, _terminals_executor),
                                        timeout=TERMINALS_TIMEOUT_S)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **NO_STORE}
        raise
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="The terminal took too long to answer", headers=NO_STORE)
    except Exception as exc:
        module = _host_module()
        if isinstance(exc, module.TerminalError):
            raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
        workspace_error = getattr(workspace._workspace_module, "WorkspaceError", None)
        if workspace_error is not None and isinstance(exc, workspace_error):
            raise HTTPException(status_code=exc.status, detail=str(exc), headers=NO_STORE)
        raise core._unexpected(f"terminals-{name}", exc, feature="Terminals")
    return {"ok": True, **result}


@router.get("/terminals")
async def get_terminals(request: Request, response: Response, root: Optional[str] = None,
                        profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    owner = core._limiter_key(profile)

    def op() -> Dict[str, Any]:
        refused = _why_not(profile)
        if refused is not None:
            return {"available": False, "reason": refused[1], "enabled": terminals_enabled(), "terminals": []}
        folder = _root(profile, root) if root else None
        return {"available": True, "reason": None, "enabled": True,
                "terminals": _host_module().host().list(owner, folder)}

    return await _call(profile, op, "list", check=False)


@router.post("/terminals")
async def post_terminal(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, TERMINALS_MAX_BODY_BYTES)
    if not isinstance(body, dict) or not isinstance(body.get("root"), str) or not body["root"].strip():
        raise HTTPException(status_code=400, detail="root is required", headers=NO_STORE)
    owner = core._limiter_key(profile)

    def op() -> Dict[str, Any]:
        folder = _root(profile, body["root"])
        terminal = _host_module().host().create(owner, folder, body.get("title"), body.get("cols", 80),
                                                body.get("rows", 24))
        return {"terminal": terminal.row()}

    return await _call(profile, op, "create")


@router.post("/terminals/rename")
async def post_terminal_rename(request: Request, response: Response,
                               profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    body = await core._read_json_body(request, TERMINALS_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object", headers=NO_STORE)
    owner = core._limiter_key(profile)
    return await _call(profile, lambda: {"terminal": _host_module().host().rename(
        owner, body.get("id"), body.get("title")).row()}, "rename", check=False)


@router.delete("/terminals")
async def delete_terminal(request: Request, response: Response, id: Optional[str] = None,  # noqa: A002
                          profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    owner = core._limiter_key(profile)

    def op() -> Dict[str, Any]:
        _host_module().host().close(owner, id)
        return {}

    return await _call(profile, op, "close", check=False)


@router.get("/terminals/settings")
async def get_terminal_settings(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _call(profile, lambda: {"enabled": terminals_enabled(),
                                         "supported": _host_module().supported()}, "settings", check=False)


@router.post("/terminals/settings")
async def post_terminal_settings(request: Request, response: Response,
                                 profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, TERMINALS_MAX_BODY_BYTES)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="enabled must be true or false", headers=NO_STORE)
    enabled = body["enabled"]

    def op() -> Dict[str, Any]:
        _save_enabled(enabled)
        if not enabled:
            # Off means off: every open shell ends now.
            _host_module().host().close_all()
        return {"enabled": enabled}

    return await _call(profile, op, "settings", check=False)


@router.websocket("/terminals/socket")
async def terminal_socket(ws: WebSocket) -> None:
    authorized = await grok_live._ws_authorized(ws)
    if not authorized:
        try:
            _terminals_auth_failure_limiter.acquire("")
        except core.TokenError:
            await ws.close(code=CLOSE_UNAUTHORIZED)
            return
    await ws.accept()

    async def refuse(code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code=code, reason=grok_live._close_reason(reason))

    if not authorized:
        await refuse(CLOSE_UNAUTHORIZED, "Dashboard sign-in required")
        return
    profile = (ws.query_params.get("profile") or "").strip() or None
    try:
        profile = await core._conduit_route_profile(ws, profile)
    except HTTPException as exc:
        await refuse(CLOSE_FAILED if exc.status_code >= 500 else CLOSE_REFUSED, str(exc.detail))
        return
    owner = core._limiter_key(profile)
    module = _host_module()
    refused = await asyncio.get_running_loop().run_in_executor(_terminals_executor, lambda: _why_not(profile))
    if refused is not None:
        await refuse(CLOSE_REFUSED, refused[1])
        return
    try:
        terminal = module.host().get(owner, ws.query_params.get("id"))
    except module.TerminalError as exc:
        await refuse(CLOSE_GONE, str(exc))
        return

    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[Any]" = asyncio.Queue()

    def push(item: Any) -> None:
        if queue.qsize() >= TERMINALS_SOCKET_BACKLOG:
            queue.put_nowait(("slow", None))
            return
        queue.put_nowait(item)

    def viewer(chunk: Optional[bytes], end: int) -> None:
        with contextlib.suppress(RuntimeError):  # the loop closed
            loop.call_soon_threadsafe(push, ("data", (chunk, end)))

    key, missed, start, end = terminal.watch(viewer, ws.query_params.get("since"))
    try:
        await ws.send_json({"type": "hello", "terminal": terminal.row(), "start": start, "end": end})
        if missed:
            await ws.send_json({"type": "output", "b64": base64.b64encode(missed).decode("ascii"), "end": end})
        if terminal.exit_code is not None:
            await ws.send_json({"type": "exit", "code": terminal.exit_code, "end": end})
            await refuse(1000, "Ended")
            return

        async def send() -> None:
            while True:
                kind, value = await queue.get()
                if kind == "slow":
                    await refuse(CLOSE_SLOW, "Fell behind; reconnect")
                    return
                chunk, chunk_end = value
                if chunk is None:
                    await ws.send_json({"type": "exit", "code": terminal.exit_code, "end": chunk_end})
                    await refuse(1000, "Ended")
                    return
                await ws.send_json({"type": "output", "b64": base64.b64encode(chunk).decode("ascii"),
                                    "end": chunk_end})

        async def receive() -> None:
            while True:
                text = await ws.receive_text()
                if len(text) > TERMINALS_MAX_FRAME_CHARS:
                    continue
                try:
                    frame = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "input" and isinstance(frame.get("data"), str):
                    data = frame["data"].encode("utf-8", errors="replace")
                    try:
                        await loop.run_in_executor(_terminal_input_executor, terminal.write, data)
                    except module.TerminalError:
                        if terminal.exit_code is not None:
                            return
                        # A shell that isn't reading drops what was typed; the socket stays.
                elif frame.get("type") == "resize":
                    terminal.resize(frame.get("cols"), frame.get("rows"))

        tasks = [asyncio.ensure_future(send()), asyncio.ensure_future(receive())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Terminal socket failed: %s", type(exc).__name__)
        await refuse(CLOSE_FAILED, "The terminal relay failed on the host")
    finally:
        terminal.unwatch(key)
