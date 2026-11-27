"""Control from Hermes: Conduit for desktop runs a bot's actions on the user's
computer (the ``conduit_user_computer`` tool, pc_control_tool.py).

The tool queues each action in the profile's home (pc_control_store.py);
Conduit on the computer asks ``GET /pc/next`` for work, holding the request
open until something arrives, runs it, and answers on ``POST /pc/result``.
Plain HTTP, since a profile served by its own plugin host refuses WebSockets.
``POST /pc/stop`` is Stop, on the computer or the phone, and
``DELETE /pc/computer`` turns control off there. Conduit polls only the
profiles whose bots the user allowed on that computer.

A chat's first action asks the user on the computer and, as a confirm prompt
(present_store.ask_pc), on their phone. The computer reads the phone's copy on
``GET /pc/ask`` while its dialog is up and answers it on ``POST /pc/ask``; the
first answer counts. ``GET /pc/state`` tells the phone which chat is
controlling which computer, for its line with Stop.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core, notifications

logger = core.logger
router = core.router

PC_WAIT_MAX_S = 25.0
PC_POLL_S = 0.1
# One poll per allowed bot is always open, and each returns as soon as there
# is work: a bot's capture, click, capture runs a few a second.
PC_LIMIT = 600
PC_WINDOW_S = 60.0
# A screenshot of a 4K screen is under 2 MB; base64 adds a third.
PC_RESULT_MAX_BYTES = 12 * 1024 * 1024
PC_SMALL_MAX_BYTES = 4096
# /pc/next says Stop was pressed when it was pressed this soon before the poll,
# so a Stop between two polls isn't missed.
PC_STOP_SEEN_S = 3.0

_pc_limiter = core._MintLimiter(PC_LIMIT, PC_WINDOW_S, message="Too many computer control requests; try again shortly")
_pc_store_module: Any = None
_pc_store_lock = threading.Lock()


def _pc_store_class() -> Any:
    """pc_control_store.py in the plugin's folder, loaded once."""
    global _pc_store_module
    with _pc_store_lock:
        if _pc_store_module is None:
            path = os.path.join(core._plugin_root(), "pc_control_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_pc_control_store", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing pc_control_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _pc_store_module = module
        return _pc_store_module


def _store() -> Any:
    return _pc_store_class().PcControlStore(core._calls_home())


async def _pc_route(profile: Optional[str], response: Response, fn: Callable[[], Dict[str, Any]],
                    route: str, acquired: bool = False) -> Dict[str, Any]:
    """``acquired``: the route already took its limiter slot, before reading its body."""
    response.headers["Cache-Control"] = "no-store"
    try:
        run = fn if acquired else core._limited(_pc_limiter, profile, fn)
        return {"ok": True, **(await core._run_scoped(profile, run))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        error = getattr(_pc_store_module, "PcControlError", None)
        if error is not None and isinstance(exc, error):
            raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
        raise core._unexpected(route, exc, feature="Control from Hermes")


@router.get("/pc/next")
async def get_pc_next(response: Response, computer: str, name: str = "", platform: str = "",
                      wait: float = PC_WAIT_MAX_S, profile: Optional[str] = None) -> Dict[str, Any]:
    """The actions waiting for this computer, waiting up to ``wait`` seconds
    for one. Each call also says the computer is online, under ``name``."""
    def first() -> Dict[str, Any]:
        store = _store()
        store.note_computer(computer, name, platform)
        return {"store": store, "requests": store.claim(computer)}

    started = time.time()
    found = await _pc_route(profile, response, first, "next")
    store, requests = found.pop("store"), found["requests"]
    remaining = max(0.0, min(PC_WAIT_MAX_S, float(wait or 0)))

    def stopped() -> bool:
        # Stop pressed elsewhere (the phone) while this poll was open, or just before it.
        return store.stopped_at(computer) >= started - PC_STOP_SEEN_S

    # The store's home was resolved inside the profile's scope; checking its
    # folder needs no scope, so the wait holds no worker thread.
    ticks = 0
    while not requests and remaining > 0:
        await asyncio.sleep(PC_POLL_S)
        remaining -= PC_POLL_S
        ticks += 1
        if store.has_waiting(computer):
            requests = await asyncio.get_running_loop().run_in_executor(None, store.claim, computer)
        elif ticks % 10 == 0 and stopped():
            break
    return {"ok": True, "requests": requests, "stopped": stopped()}


@router.post("/pc/result")
async def post_pc_result(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The scope and a limiter slot come before the body: a refused caller costs no 12 MB read.
    await core._scoped_acquire(_pc_limiter, profile)
    body = await core._read_json_body(request, PC_RESULT_MAX_BYTES)
    if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
        raise HTTPException(status_code=400, detail="Send {computer, id, result}")

    def answer() -> Dict[str, Any]:
        return {"taken": _store().answer(body.get("id"), body["result"],
                                         computer=_pc_store_class().clean_id(body.get("computer"), "computer"))}

    return await _pc_route(profile, response, answer, "result", acquired=True)


@router.post("/pc/stop")
async def post_pc_stop(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    await core._scoped_acquire(_pc_limiter, profile)
    body = await core._read_json_body(request, PC_SMALL_MAX_BYTES)
    computer = body.get("computer") if isinstance(body, dict) else None
    return await _pc_route(profile, response, lambda: {"stopped": _store().stop(computer)}, "stop", acquired=True)


@router.delete("/pc/computer")
async def delete_pc_computer(response: Response, computer: str, profile: Optional[str] = None) -> Dict[str, Any]:
    def forget() -> Dict[str, Any]:
        _store().forget_computer(computer)
        return {}

    return await _pc_route(profile, response, forget, "computer")


def _present() -> Any:
    return notifications._present_store().PresentStore(core._calls_home())


def _ask_status(status: str) -> str:
    return {"confirmed": "allowed", "declined": "denied"}.get(status, status if status in ("waiting", "gone")
                                                             else "closed")


@router.get("/pc/ask")
async def get_pc_ask(response: Response, computer: str, id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    """The phone's copy of a computer's ask: ``waiting``, ``allowed``,
    ``denied``, ``closed`` (it ran out or was taken back) or ``gone``."""
    def read() -> Dict[str, Any]:
        clean = _pc_store_class().clean_id
        computer_id, prompt_id = clean(computer, "computer"), clean(id, "ask id")
        found = _present().pc_ask(prompt_id)
        if found is None or found["pc"]["computer"] != computer_id:
            return {"status": "gone"}
        return {"status": _ask_status(found["status"])}

    return await _pc_route(profile, response, read, "ask")


@router.post("/pc/ask")
async def post_pc_ask(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """The computer's own answer to its ask: the first answer counts, so the
    answer that stands comes back (the phone may have answered first)."""
    await core._scoped_acquire(_pc_limiter, profile)
    body = await core._read_json_body(request, PC_SMALL_MAX_BYTES)
    if not isinstance(body, dict) or body.get("answer") not in ("allowed", "denied"):
        raise HTTPException(status_code=400, detail="Send {computer, id, answer: allowed or denied}")

    def settle() -> Dict[str, Any]:
        clean = _pc_store_class().clean_id
        computer_id, prompt_id = clean(body.get("computer"), "computer"), clean(body.get("id"), "ask id")
        status = _present().settle_pc(prompt_id, body["answer"] == "allowed", computer=computer_id)
        return {"status": _ask_status(status)}

    return await _pc_route(profile, response, settle, "ask", acquired=True)


@router.get("/pc/state")
async def get_pc_state(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """Which chat is controlling which computer now, for the phone's line
    with Stop (``POST /pc/stop``): each computer's id and name, the chat's
    session and title, and when it last acted."""
    return await _pc_route(profile, response, lambda: {"controlling": _store().controlling()}, "state")
