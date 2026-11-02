"""Bot workstreams' broker: starts, steers and stops a bot's work sessions
through Hermes' session API, in the process that hosts Conduit's chats
(designs/bot-mode-coordinator, C.1 and B).

Two rules come from how that API binds its client:

- ``server.dispatch(request, transport)`` binds the transport it's given for
  the call, and Hermes streams a session's events to whichever transport last
  bound it. A session the broker starts or reopens is bound to the broker's own
  transport, so none of its events reach the app's socket.
- A tool runs on a thread that already has the app's socket bound, and a
  session someone has open (the user in Conduit) is bound to theirs. Calling
  the method table from either would move the session's stream. So every call
  runs on a thread of its own: through ``dispatch`` with the broker's
  transport, or, for a session another client holds, straight through the
  method table with nothing bound, as Pulse sends its turns (pulse_host.py).

Only a process that already runs the session host has ``tui_gateway.server``
loaded. A plugin host process (``plugins.isolation: host``) doesn't, and the
broker says so instead of starting a second, private host.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

RPC_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 45.0
# Hermes answers a submit Desktop's chat owns with this code (SESSION_NOT_OWNED).
NOT_OWNED = 4090

UNAVAILABLE_HOST = ("Bot workstreams need the Hermes dashboard to run this bot's plugins in its own process. This "
                    "Hermes runs plugins in a separate plugin host (plugins.isolation: host), where there is no "
                    "session API to start work from, so they're off here.")
UNAVAILABLE_NO_API = ("Bot workstreams run in the Hermes dashboard, next to the chats Conduit opens. This Hermes "
                      "process has no session API, so they're off here.")


class BrokerError(Exception):
    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def not_owned(self) -> bool:
        return self.code == NOT_OWNED or "SESSION_NOT_OWNED" in str(self)

    @property
    def unsupported(self) -> bool:
        message = str(self).lower()
        return self.code == -32601 or "method not found" in message or "unknown method" in message


@dataclass(frozen=True)
class Started:
    runtime_id: str
    session_id: str  # the stored id the registry keeps
    profile: str


def server() -> Any:
    """Hermes' session API module when this process hosts the chats, else None."""
    module = sys.modules.get("tui_gateway.server")
    if module is None or not callable(getattr(module, "dispatch", None)):
        return None
    sessions = getattr(module, "_sessions", None)
    methods = getattr(module, "_methods", None)
    if sessions is None or not callable(getattr(sessions, "get", None)) or not isinstance(methods, dict):
        return None
    return module


def unavailable_reason() -> str:
    """Why the broker can't run here, or "" when it can."""
    if server() is not None:
        return ""
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return UNAVAILABLE_HOST
    return UNAVAILABLE_NO_API


def profile_of(home: Path) -> str:
    """The profile a Hermes home is: profiles/<name>, else "default"."""
    home = Path(home)
    return home.name if home.parent.name == "profiles" and home.name else "default"


class _Transport:
    """The broker's end of the sessions it runs. Hermes writes their events
    here as it would to a socket; responses to the broker's own calls are
    picked out, the rest is dropped (the registry follows a session through
    the plugin's hooks, which see it whoever holds it). ``_closed`` is what
    Hermes' session reaper reads."""

    # Turns the broker sends are Conduit's (reply_sources.py), from no screen.
    conduit_client = "conduit"
    conduit_surface = "work"

    def __init__(self) -> None:
        self._closed = False
        self._lock = threading.Lock()
        self._waiters: dict[str, tuple[threading.Event, list]] = {}

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False
        if isinstance(obj, dict) and "method" not in obj and isinstance(obj.get("id"), str):
            with self._lock:
                waiter = self._waiters.get(obj["id"])
            if waiter is not None:
                waiter[1].append(obj)
                waiter[0].set()
        return True

    def close(self) -> None:
        self._closed = True

    def expect(self, rid: str) -> tuple[threading.Event, list]:
        waiter = (threading.Event(), [])
        with self._lock:
            self._waiters[rid] = waiter
        return waiter

    def forget(self, rid: str) -> None:
        with self._lock:
            self._waiters.pop(rid, None)


_transport = _Transport()


def _on_own_thread(work: Callable[[], Any], timeout: float = CALL_TIMEOUT_S) -> Any:
    """Runs ``work`` on a new thread with no transport bound, and returns
    what it returned (or raises what it raised)."""
    out: list = []
    failed: list = []

    def run() -> None:
        try:
            transport_module = sys.modules.get("tui_gateway.transport")
            if transport_module is not None and hasattr(transport_module, "bind_transport"):
                transport_module.bind_transport(None)
            out.append(work())
        except BaseException as error:  # noqa: BLE001 — handed back to the caller
            failed.append(error)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,), name="conduit-work-broker",
                              daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise BrokerError("Hermes didn't answer in time")
    if failed:
        raise failed[0]
    return out[0] if out else None


def _answer(response: Any, method: str) -> dict:
    if not isinstance(response, dict):
        raise BrokerError(f"{method} gave no answer")
    error = response.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        data = error.get("data") if isinstance(error, dict) else None
        reason = data.get("reason") if isinstance(data, dict) else ""
        raise BrokerError(f"{message or method + ' failed'}{f' ({reason})' if reason else ''}"[:300], code=code)
    result = response.get("result")
    return result if isinstance(result, dict) else {}


class Broker:
    """One bot's broker: its profile home decides which profile every
    session is made and reopened on, never the model."""

    def __init__(self, home: Path, api: Any = None) -> None:
        self.home = Path(home)
        self.profile = profile_of(self.home)
        self._api = api

    @property
    def api(self) -> Any:
        api = self._api if self._api is not None else server()
        if api is None:
            raise BrokerError(unavailable_reason() or UNAVAILABLE_NO_API)
        return api

    # Calls

    def _dispatch(self, method: str, params: dict, timeout: float = RPC_TIMEOUT_S) -> dict:
        api = self.api

        def call() -> Any:
            rid = f"conduit-work-{uuid.uuid4().hex[:12]}"
            done, box = _transport.expect(rid)
            try:
                response = api.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                                        _transport)
                if response is None:
                    # A slow method answers from Hermes' worker pool.
                    if not done.wait(timeout):
                        raise BrokerError(f"{method} timed out")
                    response = box[0]
                return response
            finally:
                _transport.forget(rid)

        return _answer(_on_own_thread(call, timeout + 5), method)

    def _unbound(self, method: str, params: dict) -> dict:
        methods = self.api._methods
        if method not in methods:
            raise BrokerError(f"unknown method {method}", code=-32601)
        return _answer(_on_own_thread(lambda: methods[method](f"conduit-work-{uuid.uuid4().hex[:12]}", params)),
                       method)

    def _profile_params(self) -> dict:
        return {"profile": self.profile} if self.profile != "default" else {}

    # Sessions

    def live(self, session_id: str) -> Optional[tuple[str, dict]]:
        """The live session (runtime id, record) for a stored id on this
        bot's profile, else None."""
        if not session_id:
            return None
        sessions = getattr(self.api, "_sessions", {})
        home = str(self.home)
        for runtime_id, record in list(sessions.items()):
            if not isinstance(record, dict) or record.get("_finalized"):
                continue
            if str(record.get("session_key") or "") != session_id:
                continue
            record_home = record.get("profile_home")
            if record_home and Path(str(record_home)).resolve() != self.home.resolve():
                continue
            if not record_home and self.profile != "default" and home:
                continue
            return runtime_id, record
        return None

    def live_session_ids(self) -> set[str]:
        sessions = getattr(self.api, "_sessions", {})
        return {str(record.get("session_key") or "") for record in list(sessions.values())
                if isinstance(record, dict) and not record.get("_finalized")}

    def start(self, *, title: str, prompt: str, idempotency_key: str, cwd: str = "",
              created: Optional[Callable[[str], None]] = None, yolo: Optional[bool] = None) -> Started:
        """A new hidden session on this bot's profile, with ``prompt`` as its
        first turn. ``created`` hears its stored id before the prompt goes in (or
        right after, when Hermes names it only then). ``yolo``: the session's
        own approval bypass, set before its first turn (None leaves it).
        Anything that fails once the session exists ends it again (its turn
        interrupted, the session closed), so no session runs outside the
        registry."""
        params: dict[str, Any] = {"cols": 96, "source": "desktop", "title": title, "hidden": True,
                                  "idempotency_key": idempotency_key, **self._profile_params()}
        if cwd:
            params["cwd"] = cwd
        made = self._dispatch("session.create", params)
        runtime_id = made.get("session_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise BrokerError("session.create gave no session")
        try:
            landed = made.get("profile")
            if isinstance(landed, str) and landed and landed.lower() != self.profile.lower():
                raise BrokerError(f"Hermes put the session on {landed[:64]} instead of {self.profile}")
            stored = made.get("stored_session_id")
            stored = stored if isinstance(stored, str) and stored else self._stored_id(runtime_id)
            told = False
            if stored and created is not None:
                created(stored)
                told = True
            if yolo is not None:
                self._set_yolo(runtime_id, yolo, self._dispatch)
            self._dispatch("prompt.submit", {"session_id": runtime_id, "text": prompt})
            stored = stored or self._stored_id(runtime_id)
            if not stored:
                raise BrokerError("Hermes gave the session no stored id")
            if not told and created is not None:
                created(stored)  # known only now: a failure still ends the session
        except BaseException:
            self._end(runtime_id)
            raise
        return Started(runtime_id, stored, self.profile)

    def _stored_id(self, runtime_id: str) -> str:
        record = getattr(self.api, "_sessions", {}).get(runtime_id)
        return str(record.get("session_key") or "") if isinstance(record, dict) else ""

    def _end(self, runtime_id: str) -> None:
        """Best effort: a session the broker made but couldn't keep."""
        for method in ("session.interrupt", "session.close"):
            try:
                self._dispatch(method, {"session_id": runtime_id}, timeout=10)
            except Exception:  # noqa: BLE001 — the start's own failure is what gets reported
                pass

    def _open(self, session_id: str) -> tuple[str, str, bool]:
        """(runtime id, stored id at the lineage tip, whether another client
        holds it) for a session on this profile, reopening it if needed."""
        found = self.live(session_id)
        if found is not None:
            runtime_id, record = found
            return runtime_id, session_id, record.get("transport") not in (None, _transport)
        resumed = self._dispatch("session.resume", {"session_id": session_id, "source": "desktop",
                                                    **self._profile_params()})
        runtime_id = resumed.get("session_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise BrokerError("couldn't reopen the session")
        landed = (resumed.get("info") or {}).get("profile_name") if isinstance(resumed.get("info"), dict) else None
        if isinstance(landed, str) and landed and landed.lower() != self.profile.lower():
            self._end(runtime_id)
            raise BrokerError(f"the session reopened on {landed[:64]}, not {self.profile}")
        tip = resumed.get("stored_session_id")
        return runtime_id, tip if isinstance(tip, str) and tip else session_id, False

    def send(self, session_id: str, text: str, how: str = "auto",
             yolo: Optional[bool] = None) -> tuple[str, str]:
        """Words into a session: ``auto`` (running: redirect, idle: a new
        turn), ``add`` (steered into the running turn) or ``after`` (queued
        behind it). Returns (what Hermes did, the stored id at the tip):
        "started", "redirected", "steered" or "queued". ``yolo``: the
        session's own approval bypass, set before the words go in."""
        runtime_id, tip, held = self._open(session_id)
        call = self._unbound if held else self._dispatch
        if yolo is not None:
            self._set_yolo(runtime_id, yolo, call)
        found = self.live(tip) or self.live(session_id)
        running = bool(found and found[1].get("running"))
        if how == "after" or (running and how not in ("auto", "add")):
            status = call("prompt.submit", {"session_id": runtime_id, "text": text, "queued": True}).get("status")
            return ("queued" if status != "streaming" else "started"), tip
        if running and how == "add":
            try:
                status = call("session.steer", {"session_id": runtime_id, "text": text}).get("status")
                if str(status or "").lower() != "rejected":
                    return "steered", tip
            except BrokerError as error:
                if not error.unsupported:
                    raise
            # Couldn't add it to the turn: it goes after it, never redirecting it.
            status = call("prompt.submit", {"session_id": runtime_id, "text": text, "queued": True}).get("status")
            return ("queued" if status != "streaming" else "started"), tip
        if running:
            try:
                status = str(call("session.redirect", {"session_id": runtime_id, "text": text}).get("status") or "")
            except BrokerError as error:
                if not error.unsupported:
                    raise
                status = ""
            if status in ("redirected", "queued"):
                return status, tip
        status = call("prompt.submit", {"session_id": runtime_id, "text": text, "queued": True}).get("status")
        return ("started" if status == "streaming" else "queued"), tip

    def _set_yolo(self, runtime_id: str, on: bool, call: Callable[[str, dict], dict]) -> None:
        """The session's own approval bypass (Hermes' per-session /yolo, kept
        on its row so a resume keeps it), never the process-wide one: the
        call always names the live session, and Hermes refuses a stale one
        instead of widening it. Turning it on can fail quietly (Hermes keeps
        asking, the safe side); turning it off can't."""
        if not runtime_id:
            raise BrokerError("no session to set approvals on")
        try:
            call("config.set", {"session_id": runtime_id, "key": "yolo", "value": "on" if on else "off",
                                "scope": "session", **self._profile_params()})
        except BrokerError as error:
            # A Hermes without config.set never turned it on, so there's nothing to turn off.
            if not on and not error.unsupported:
                raise
            logger.warning("conduit_push: couldn't turn off approvals in a workstream; Hermes still asks",
                           exc_info=True)

    def stop(self, session_id: str) -> bool:
        """Interrupts the session's turn; False when it isn't running."""
        found = self.live(session_id)
        if found is None or not found[1].get("running"):
            return False
        runtime_id, record = found
        held = record.get("transport") not in (None, _transport)
        (self._unbound if held else self._dispatch)("session.interrupt", {"session_id": runtime_id})
        return True

    def approvals(self, session_id: str) -> list[dict]:
        """The approvals a live session waits on (``approval.pending``), by
        its stored id; none when it isn't live. Read through the method
        table, so the client showing the chat keeps its stream."""
        found = self.live(session_id)
        if found is None:
            return []
        rows = self._unbound("approval.pending", {"session_id": found[0]}).get("approvals")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def answer_approval(self, session_id: str, request_id: str, choice: str) -> bool:
        """Answers one approval by its request id (``approval.respond``):
        never another one queued in the same session. False when it's no
        longer waiting."""
        found = self.live(session_id)
        if found is None or not request_id:
            return False
        answer = self._unbound("approval.respond", {"session_id": found[0], "choice": choice,
                                                    "request_id": request_id})
        return bool(answer.get("resolved"))

    def wake(self, chat_id: str, text: str) -> str:
        """A hidden, queued turn into this bot's Bot Chat, the way Pulse
        sends its turns: reopened through the method table with nothing
        bound, so the app keeps its stream. Hermes' status back."""
        methods = self.api._methods
        found = self.live(chat_id)
        if found is not None:
            runtime_id = found[0]
        else:
            resumed = self._unbound("session.resume", {"session_id": chat_id, "source": "desktop",
                                                       **self._profile_params()})
            runtime_id = str(resumed.get("session_id") or "")
            if not runtime_id:
                raise BrokerError("couldn't reopen the Bot Chat")
        if "prompt.submit" not in methods:
            raise BrokerError("unknown method prompt.submit", code=-32601)
        return str(self._unbound("prompt.submit", {"session_id": runtime_id, "text": text, "display_kind": "hidden",
                                                   "queued": True}).get("status") or "")
