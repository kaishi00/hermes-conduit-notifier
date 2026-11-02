"""Test-only routes that drive the Conduit plugin's own chats on the gateway's
listener, as its features do (upstream/test_gateway_listener.py). The plugin
runs them from its Watch calls and Pulse's loop, which a test can't reach
without the relay and a Watch, so these call the same code: the session API
Watch jobs and a Watch call's Local Live chat use, the Local Live chat itself,
and Pulse's send. Each runs off the gateway's loop, as the plugin's do.

Never install this outside the upstream harness: its routes send turns into
any chat with no sign-in check. It isn't shipped (upstream/ stays out of the
plugin's copy)."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import Any, Dict, List

from fastapi import APIRouter

router = APIRouter()
WAIT_S = 60.0


def _conduit(suffix: str, marker: str) -> Any:
    """The Conduit plugin's module whose name ends with ``suffix``, as Hermes loaded it."""
    for name, module in list(sys.modules.items()):
        if name.endswith(suffix) and hasattr(module, marker):
            return module
    raise LookupError(f"no Conduit module {suffix} loaded")


class _Transport:
    """A Watch transport as the plugin's are: it names the Watch, spoken."""

    conduit_client = "conduit"
    conduit_surface = "watch"
    conduit_input = "spoken"

    def __init__(self) -> None:
        self.frames: List[Dict[str, Any]] = []
        self.seen = threading.Condition()
        self._closed = False

    def write(self, obj: Dict[str, Any]) -> bool:
        with self.seen:
            self.frames.append(obj)
            self.seen.notify_all()
        return True

    def event(self, kind: str) -> Dict[str, Any]:
        deadline = time.monotonic() + WAIT_S
        with self.seen:
            while True:
                for frame in self.frames:
                    params = frame.get("params") or {}
                    if frame.get("method") == "event" and params.get("type") == kind:
                        return params
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no {kind}; saw {[(f.get('params') or {}).get('type') for f in self.frames]}")
                self.seen.wait(left)


def _session_api_turn() -> Dict[str, Any]:
    watch_jobs = _conduit(".watch_jobs", "WATCH_JOB_TOOLS")
    server = watch_jobs._hermes_session_api()
    transport = _Transport()
    out: Dict[str, Any] = {"api": type(server).__name__}

    def rpc(rid: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return server.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, transport)

    # Watch jobs' own shapes (watch_jobs._start_session and its approval answer).
    created = rpc("a", "session.create", {"cols": 96, "source": "desktop", "title": "Watch job"})
    out["create"] = created
    sid = (created.get("result") or {}).get("session_id")
    out["submit"] = rpc("b", "prompt.submit", {"session_id": sid, "text": "RUN the job"})
    asked = transport.event("approval.request")
    out["approval"] = asked.get("payload")
    out["viewer"] = server._sessions.get(sid) is not None
    out["respond"] = rpc("c", "approval.respond", {"session_id": sid, "choice": "deny",
                                                   "request_id": asked["payload"].get("request_id")})
    out["settled"] = transport.event("approval.cancelled").get("payload")
    out["complete"] = transport.event("message.complete").get("payload", {}).get("text")
    # The turn has ended: a stale control reads as nothing running, as on the dashboard.
    out["steer_after"] = rpc("d", "session.steer", {"session_id": sid, "text": "late"})
    out["again"] = rpc("e", "approval.respond", {"session_id": sid, "choice": "deny"})
    out["close"] = rpc("f", "session.close", {"session_id": sid})
    out["viewer_after_close"] = server._sessions.get(sid) is not None
    watch_jobs.core._release_authority_link(transport)
    out["session_id"] = sid
    return out


def _watch_call_turn() -> Dict[str, Any]:
    watch_local_live = _conduit(".watch_local_live", "_WatchCallChat")
    watch_jobs = _conduit(".watch_jobs", "WATCH_JOB_TOOLS")
    chat = watch_local_live._WatchCallChat(watch_jobs._hermes_session_api(), None)
    kinds: List[str] = []
    ended = threading.Event()

    def heard(kind: str, payload: Dict[str, Any]) -> None:
        kinds.append(kind)
        if kind in ("message.complete", "error"):
            ended.set()

    chat.listener = heard
    chat.open()
    status = chat.submit("hello from the watch")
    finished = ended.wait(WAIT_S)
    sid = chat.session_id
    chat.close()
    return {"status": status, "finished": finished, "kinds": sorted(set(kinds)), "session_id": sid}


def _pulse_send(chat_id: str) -> Dict[str, Any]:
    pulse_host = _conduit("pulse_host", "_send_on_authority")
    sent = pulse_host.send(chat_id, "PULSE check in", model={"provider": "custom", "model": "fake-model-2"})
    return {"available": pulse_host.available(), "ok": sent.ok, "status": sent.status, "detail": sent.detail}


@router.get("/session-api-turn")
async def session_api_turn() -> Dict[str, Any]:
    return await asyncio.to_thread(_session_api_turn)


@router.get("/watch-call-turn")
async def watch_call_turn() -> Dict[str, Any]:
    return await asyncio.to_thread(_watch_call_turn)


@router.get("/pulse-send")
async def pulse_send(chat_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_pulse_send, chat_id)
