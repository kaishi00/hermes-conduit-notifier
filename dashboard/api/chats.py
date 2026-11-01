"""Chat takeover, where in Conduit a message was sent, and predictions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import contextlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core, watch_jobs

logger = core.logger
router = core.router


# --- Chat takeover -----------------------------------------------------------
#
# Hermes lets one live surface own a chat at a time (hermes_cli.active_sessions,
# the runtime/active_sessions.json registry). Hermes Desktop claims a chat on
# its first turn and keeps the claim until the chat is closed there, so the
# same chat refuses Conduit's next send with SESSION_NOT_OWNED. This route is
# the "Take over" Conduit offers on that refusal (#304): it drops the other
# surface's claim, under the registry's own lock, so the next send claims the
# chat for Conduit. A turn the other surface is running is never cut off: its
# turn marker (tui_gateway.turn_marker) answers "busy" and Conduit asks again.
#
# Only another process's claim is dropped. A claim held by this dashboard
# process (Conduit on another device, or the web chat) is left alone: Hermes
# already hands those over itself once their client is gone. "This process"
# is its pid: Hermes serves the dashboard as one process (its chat runtimes
# and turn leases live in that process's memory), as the voice routes assume.
#
# The owner writes its turn marker without the registry lock, so a turn it
# starts in the instant between the marker check and the write can still lose
# its claim. That turn keeps running in the owner (nothing is interrupted);
# it is the same exposure as the known limit that the owner isn't told.
#
# Hermes' gateway also holds a chat through its durable ledger in the
# profile's state.db: another bot's message (message_agent) runs in the bot's
# Bot Chat that way, and every other surface's send is refused until its reply
# ends, with no claim to drop. The route answers "busy" (surface "bot", or
# "gateway" for other queued work) without touching the registry, and Conduit
# sends once the ledger lets go.
#
# Only Desktop and TUI turns write a turn marker. A live claim from any other
# surface (a CLI or kanban worker, a gateway, or a blank one) has no marker
# to read, so it could look idle mid-turn: it is never dropped and answers
# "busy" (#97).
#
# A timed-out request is abandoned: the worker thread can't be cancelled, but
# it checks the abandon flag once it holds the lock and again right before it
# writes, so a 504 almost never drops a claim after the fact (only a write
# already under way when the timeout fires still lands). Conduit retries a
# 504, and asking again reports the real state.

TAKEOVER_MAX_BODY_BYTES = 4096
TAKEOVER_MAX_IDS = 4
TAKEOVER_MAX_ID_CHARS = 200
TAKEOVER_LIMIT = 60
TAKEOVER_WINDOW_S = 60.0
TAKEOVER_TIMEOUT_S = 20.0
_takeover_limiter = core._MintLimiter(TAKEOVER_LIMIT, TAKEOVER_WINDOW_S,
                                 message="Too many takeover requests; try again shortly")
_takeover_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-takeover")


def _takeover_ids(body: Any) -> list:
    raw = body.get("session_ids") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw or len(raw) > TAKEOVER_MAX_IDS:
        raise core.TokenError(400, f"session_ids must list 1 to {TAKEOVER_MAX_IDS} chat ids")
    ids = []
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > TAKEOVER_MAX_ID_CHARS:
            raise core.TokenError(400, f"session_ids must be non-empty strings of at most {TAKEOVER_MAX_ID_CHARS} characters")
        if value.strip() not in ids:
            ids.append(value.strip())
    return ids


def _takeover() -> Any:
    return core._plugin_package_module("chat_takeover")


def _takeover_call(call: Callable[[Any], Any]) -> Any:
    module = _takeover()
    try:
        return call(module)
    except module.TakeoverError as exc:
        raise core.TokenError(exc.status, str(exc)) from None


def _takeover_modules() -> Tuple[Any, Any]:
    return _takeover_call(lambda module: module.modules())


def _takeover_home() -> Any:
    return _takeover_call(lambda module: module.home())


def _owner_pid(entry: Dict[str, Any]) -> int:
    return _takeover()._owner_pid(entry)


def take_over_session(session_ids: list, *, registry: Any, turn_marker: Any, home: Any,
                      own_pid: Optional[int] = None,
                      abandoned: Optional[threading.Event] = None) -> Dict[str, Any]:
    """chat_takeover.take_over_session, its refusals as TokenError."""
    return _takeover_call(lambda module: module.take_over_session(
        session_ids, registry=registry, turn_marker=turn_marker, home=home, own_pid=own_pid, abandoned=abandoned))


def _take_over_scoped(session_ids: list, abandoned: Optional[threading.Event] = None) -> Dict[str, Any]:
    registry, turn_marker = _takeover_modules()
    return take_over_session(session_ids, registry=registry, turn_marker=turn_marker, home=_takeover_home(),
                             abandoned=abandoned)


@router.post("/sessions/takeover")
async def post_session_takeover(request: Request, response: Response,
                                profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        body = await core._read_json_body(request, TAKEOVER_MAX_BODY_BYTES)
        ids = _takeover_ids(body)
        _ensure_claims_sweeper()
        # One budget for the whole dashboard, like the voice routes: the
        # profile isn't resolved yet, so a made-up name can't mint a window.
        _takeover_limiter.acquire("")
        abandoned = threading.Event()
        try:
            result = await asyncio.wait_for(
                core._run_scoped(profile, lambda: _take_over_scoped(ids, abandoned), _takeover_executor),
                timeout=TAKEOVER_TIMEOUT_S)
        except asyncio.TimeoutError:
            abandoned.set()
            raise
    except asyncio.TimeoutError:
        logger.warning("Chat takeover timed out after %ss", TAKEOVER_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Chat takeover timed out", headers=no_store)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise core._unexpected("takeover", exc, feature="Chat takeover")
    return {"ok": True, **result}


# The reverse direction (hermes-conduit#1123): a chat Conduit opened here keeps
# its claim while Conduit stays connected, so Desktop's own process is refused
# it long after Conduit's turn ended. A sweep lets go of Conduit's claims on
# chats that sit idle; conduit_claims.py explains when. One sweeper per
# process: a reloaded copy of this module takes over from the old one.
_CLAIMS_SWEEPER = "_conduit_idle_claims_sweeper"


def _claims_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _sweep_idle_claims(token: object) -> None:
    while sys.__dict__.get(_CLAIMS_SWEEPER) is token:
        try:
            claims = core._plugin_package_module("conduit_claims")
            time.sleep(claims.SWEEP_S)
            if sys.__dict__.get(_CLAIMS_SWEEPER) is token:
                claims.sweep(watch_jobs._tui_session_api(), _claims_home)
        except Exception:  # noqa: BLE001 — keep sweeping
            logger.debug("Conduit: idle claim sweep failed", exc_info=True)
            time.sleep(60.0)


def _start_claims_sweeper() -> None:
    token = object()
    sys.__dict__[_CLAIMS_SWEEPER] = token
    try:
        threading.Thread(target=_sweep_idle_claims, args=(token,), name="conduit-idle-claims", daemon=True).start()
    except RuntimeError:
        sys.__dict__.pop(_CLAIMS_SWEEPER, None)  # the next takeover tries again
        logger.warning("Conduit: no thread to let go of idle chat claims")


def _ensure_claims_sweeper() -> None:
    """Starts the sweeper unless one runs: a takeover means Conduit chats here."""
    if sys.__dict__.get(_CLAIMS_SWEEPER) is None:
        _start_claims_sweeper()


if "hermes_cli.web_server" in sys.modules:
    _start_claims_sweeper()


# --- Where in Conduit a message was sent (hermes-conduit#888) ----------------
#
# Conduit names the surface (iPhone, iPad, Watch, CarPlay, Siri…) and how the
# message was entered just before it sends it, with the message text's hash;
# that message's turn gets one line saying so (turn_surface.py). It's kept on Hermes' own record of
# the chat, so it needs the gateway in this process, like the Desktop views;
# without it the answer says nothing was kept and the turn has no line.

TURN_SURFACE_MAX_BODY_BYTES = 1024
_turn_surface_module: Any = None
_turn_surface_lock = threading.Lock()


_voice_archive_module: Any = None
_voice_archive_lock = threading.Lock()


def _voice_archive() -> Any:
    """voice_archive.py in the plugin's folder, loaded once."""
    global _voice_archive_module
    with _voice_archive_lock:
        if _voice_archive_module is None:
            path = os.path.join(core._plugin_root(), "voice_archive.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_voice_archive", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("This plugin install is missing voice_archive.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _voice_archive_module = module
        return _voice_archive_module


def _turn_surface() -> Any:
    """turn_surface.py in the plugin's folder, loaded once."""
    global _turn_surface_module
    with _turn_surface_lock:
        if _turn_surface_module is None:
            path = os.path.join(core._plugin_root(), "turn_surface.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_turn_surface", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("This plugin install is missing turn_surface.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _turn_surface_module = module
        return _turn_surface_module


def record_turn_surface(body: Any) -> bool:
    """Keeps the surface on the chat's live record; False when there's none
    here. Raises ValueError for a body that isn't one."""
    module = _turn_surface()
    report = module.parse(body)
    if core._authority_session(report.session_id) is not None:
        # The gateway's listener: the authority runs the chat (turn_surface.py).
        module.record_for_authority(report)
        return True
    server = watch_jobs._tui_session_api()
    session = server._sessions.get(report.session_id) if server is not None else None
    if not isinstance(session, dict):
        return False
    module.record(session, report)
    return True


@router.post("/turn-surface")
async def post_turn_surface(request: Request, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    body = await core._read_json_body(request, TURN_SURFACE_MAX_BODY_BYTES)
    try:
        recorded = record_turn_surface(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc), headers=no_store)
    except Exception as exc:
        raise core._unexpected("turn-surface", exc, feature="Turn surface")
    return {"ok": True, "recorded": recorded}


# --- Predictions (predict.py) -------------------------------------------------
#
# Conduit's opt-in Predictive features ask about a chat open on this host:
# suggested replies, guesses for an unsent draft, and whether the agent wants
# a turn while the user looks at the chat. Each is answered by a fork of the
# live chat, as Hermes' /btw is (same prompt cache, every tool refused,
# nothing saved), but only to the device that asked: prompt.btw's answer goes
# to every client, and Hermes Desktop shows it as a note. The fork runs on the
# chat's own model, or on `auxiliary.conduit_predict` ({provider, model}) in
# the chat's profile config when Conduit asks for the fast one.

PREDICT_LIMIT = 40
PREDICT_WINDOW_S = 60.0
# A 2000-character draft of emoji is 8 KB of UTF-8 on its own.
PREDICT_MAX_BODY_BYTES = 16384
# Forks alive at once on this host, answered or not: one the route stopped
# waiting for keeps its place until it ends.
PREDICT_MAX_RUNNING = 4
PREDICT_FORK_ITERATIONS = 3
PREDICT_TASK = "conduit_predict"
_predict_limiter = core._MintLimiter(PREDICT_LIMIT, PREDICT_WINDOW_S,
                                message="Too many predictions; try again shortly")
_predict_module: Any = None
_predict_lock = threading.Lock()
# Chats with a fork running, by runtime session id (one dict for the whole
# process, so an id names one chat whatever the profile), with when it
# started: one at a time each, so a fast typist can't queue forks behind a
# slow model.
_predict_running: Dict[str, float] = {}
# Predictions answered today, by local date: shown in Conduit's settings.
_predict_counts: Dict[str, int] = {}


def _predict() -> Any:
    """predict.py in the plugin's folder, loaded once."""
    global _predict_module
    with _predict_lock:
        if _predict_module is None:
            path = os.path.join(core._plugin_root(), "predict.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_predict", path)
            if spec is None or spec.loader is None or not os.path.isfile(path):
                raise core.TokenError(501, "This plugin install is missing predict.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _predict_module = module
        return _predict_module


def _predict_profile_scope(session: dict) -> Any:
    """Hermes' scope for the chat's own profile (config, secrets), as its
    side agents use; none on a Hermes without it."""
    server = sys.modules.get("tui_gateway.server")
    scope = getattr(server, "_session_profile_runtime_scope", None)
    return scope(session) if callable(scope) else contextlib.nullcontext()


def _predict_fast_model() -> Dict[str, Any]:
    """``auxiliary.conduit_predict`` from the active profile's config, or {}."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception:  # noqa: BLE001 — no fast model, the chat's own model answers
        return {}
    auxiliary = config.get("auxiliary") if isinstance(config, dict) else None
    task = auxiliary.get(PREDICT_TASK) if isinstance(auxiliary, dict) else None
    if not isinstance(task, dict) or not str(task.get("model") or "").strip():
        return {}
    return task


def _predict_fork_answer(agent: Any, history: List[Dict[str, Any]], question: str,
                         task_cfg: Dict[str, Any]) -> str:
    """The question answered by a no-tools fork of the live chat. A Hermes
    without the fork's parts answers through its plain /btw (the chat's own
    model)."""
    try:
        from agent.background_review import (
            _digest_history, _record_review_usage_to_parent, _snapshot_review_usage, build_cache_parity_fork)
        from agent.side_question import _FORK_PROMPT, trim_snapshot_for_fork
        from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist
    except ImportError:
        try:
            from agent.side_question import answer_side_question
        except ImportError:
            raise core.TokenError(501, "This Hermes can't answer side questions; update Hermes")
        return answer_side_question(question, history, parent_agent=agent) or ""
    fork, _runtime, routed = build_cache_parity_fork(agent, task_cfg or None,
                                                     max_iterations=PREDICT_FORK_ITERATIONS,
                                                     write_origin="conduit_predict")
    try:
        set_thread_tool_whitelist(set(), deny_msg_fmt=(
            "Prediction denied tool call: {tool_name}. Tools are disabled here; answer from the conversation."))
        snapshot = trim_snapshot_for_fork(history)
        result = fork.run_conversation(user_message=f"{_FORK_PROMPT}\n\nSide question: {question}",
                                       conversation_history=_digest_history(snapshot) if routed else snapshot)
        text = (result or {}).get("final_response", "") or ""
        if not text and result and result.get("error"):
            raise RuntimeError(str(result["error"]))
        return text.strip()
    finally:
        for step in (clear_thread_tool_whitelist,
                     lambda: _record_review_usage_to_parent(agent, _snapshot_review_usage(fork)),
                     lambda: fork.shutdown_memory_provider(), lambda: fork.close()):
            try:
                step()
            except Exception:  # noqa: BLE001 — teardown never fails the answer
                pass


def _predict_request(body: Any) -> Tuple[Any, Any, dict, Any]:
    """The checked request, with the live chat it asks about."""
    module = _predict()
    try:
        request = module.parse(body)
    except ValueError as exc:
        raise core.TokenError(400, str(exc))
    authority = core._authority_agent(request.session_id)
    if authority is None and core._authority_session(request.session_id) is not None:
        # Open on the gateway's listener, but the gateway let its agent go (idle
        # for its cache's hour): no turn to predict after, as a closed chat.
        raise core.TokenError(404, "That chat's agent isn't loaded on the host")
    if authority is not None:
        # The gateway's listener: the chat's agent is the gateway's, and its
        # profile is the authority's (hermes-conduit#1618).
        return module, request, {"authority": authority[0]}, authority[1]
    server = watch_jobs._tui_session_api()
    session = server._sessions.get(request.session_id) if server is not None else None
    agent = session.get("agent") if isinstance(session, dict) else None
    if agent is None:
        raise core.TokenError(404, "That chat isn't open on the host")
    return module, request, session, agent


def _answer_prediction(module: Any, request: Any, session: dict, agent: Any) -> Dict[str, Any]:
    history = list(getattr(agent, "_session_messages", None) or session.get("history") or [])
    scope = core._authority_scope(session["authority"]) if "authority" in session else _predict_profile_scope(session)
    with scope:
        task_cfg = _predict_fast_model() if request.model == "fast" else {}
        text = _predict_fork_answer(agent, history, module.question(request), task_cfg)
    today = time.strftime("%Y-%m-%d")
    with _predict_lock:
        for day in [day for day in _predict_counts if day != today]:
            del _predict_counts[day]
        _predict_counts[today] = _predict_counts.get(today, 0) + 1
    return module.answer(request, text)


def start_prediction(module: Any, request: Any, session: dict, agent: Any) -> "concurrent.futures.Future":
    """The fork, on a thread of its own. Its chat's place is freed when it
    ends, whether or not anyone still waits for it, so a fork past its wait
    holds only its own chat and one of PREDICT_MAX_RUNNING, never a shared
    worker."""
    with _predict_lock:
        if request.session_id in _predict_running:
            raise core.TokenError(429, "A prediction for this chat is still running")
        if len(_predict_running) >= PREDICT_MAX_RUNNING:
            raise core.TokenError(503, "The host is busy with other predictions; try again shortly")
        _predict_running[request.session_id] = time.monotonic()
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_running_or_notify_cancel()

    def run() -> None:
        try:
            result = _answer_prediction(module, request, session, agent)
        except BaseException as exc:  # noqa: BLE001 — handed to the waiting route
            outcome: Tuple[bool, Any] = (False, exc)
        else:
            outcome = (True, result)
        finally:
            with _predict_lock:
                _predict_running.pop(request.session_id, None)
        if outcome[0]:
            future.set_result(outcome[1])
        else:
            future.set_exception(outcome[1])

    try:
        threading.Thread(target=run, name="conduit-predict", daemon=True).start()
    except BaseException:
        with _predict_lock:
            _predict_running.pop(request.session_id, None)
        raise
    return future


def prediction_status() -> Dict[str, Any]:
    """The fast model, if the profile names one, and today's count."""
    task = _predict_fast_model()
    with _predict_lock:
        today = _predict_counts.get(time.strftime("%Y-%m-%d"), 0)
    return {"fast_model": str(task.get("model")).strip() if task else None, "today": today}


@router.get("/predict")
async def get_predict(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    try:
        return {"ok": True, **(await core._run_scoped(profile, prediction_status))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        # The profile scope's own refusal (an unknown ?profile=) stands.
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("predict status", exc, feature="Predictions")


@router.post("/predict")
async def post_predict(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    profile = await core._conduit_route_profile_no_store(request, profile)
    # One bucket for the host: the fork runs under the chat's own profile
    # whatever ?profile= names, so a bucket per profile could be sidestepped.
    await core._scoped_acquire(_predict_limiter, profile, key="")
    body = await core._read_json_body(request, PREDICT_MAX_BODY_BYTES)
    try:
        module, asked, session, agent = _predict_request(body)
        future = start_prediction(module, asked, session, agent)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except Exception as exc:
        raise core._unexpected("predict", exc, feature="Predictions")
    try:
        # Not cancelled on a timeout (it's already running): the fork ends on
        # its own, and frees its chat then.
        result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=asked.wait_s + 1.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="The prediction took too long", headers=no_store)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except Exception as exc:
        raise core._unexpected("predict", exc, feature="Predictions")
    return {"ok": True, **result}
