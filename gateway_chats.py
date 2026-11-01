"""The chats the gateway's session authority runs (hermes-conduit#1618).

Hermes runs chats two ways. ``hermes dashboard`` keeps them in its TUI gateway
(``tui_gateway.server``: a dict per chat, answered by ``server.dispatch``);
the gateway's own listener (``hermes webapp``, the listener the dashboard is
mounted on there) runs them on its session authority, whose verbs take shapes
of their own. A gateway process can load the TUI module too (its socket
handler imports it for the verbs the authority doesn't have), but the chats in
it aren't the ones clients see. So a process whose gateway API is up
(``gateway.run_api._ACTIVE_RUNNER``, set only while the listener serves)
reads and drives chats through the authority, and any other through the TUI
gateway, as before.

``session_api()`` is Hermes' session API in the TUI gateway's shapes over the
authority (``dispatch(frame, transport)`` and ``_sessions.get``): what the
plugin's own chats use (Watch jobs, a Watch call's Local Live chat, Pulse's
turns), so their code stays one. Each transport gets a connection of its own,
as a socket does, acting for the host's owner; the chat's events reach the
transport's ``write`` in the TUI gateway's shapes.

Standalone (no imports from the plugin package at load): the dashboard loads
it from its file, the plugin imports it. Nothing here imports Hermes before
its gateway runs: a copy this plugin imported would be a private one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
import sys
import threading
import uuid
import weakref
from contextlib import nullcontext
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _sibling(name: str) -> Any:
    """Another module of the plugin, from the package this one was loaded in."""
    package = __name__.rpartition(".")[0]
    return importlib.import_module(f"{package}.{name}" if package else name)


AUTHORITY_RPC_TIMEOUT_S = 60.0
# Who the plugin's own chats act as: a sign-in acts for the host's owner, as
# Conduit's socket on the Webapp's session token does, so the phone and the
# Webapp open the chats the plugin starts.
AUTHORITY_IDENTITY = {"user_id": "conduit-plugin", "provider": "server-internal", "profile_scope": "host"}


def runner() -> Any:
    """The gateway serving this process's listener, or None (``hermes dashboard``)."""
    return getattr(sys.modules.get("gateway.run_api"), "_ACTIVE_RUNNER", None)


def _authorities(gateway: Any) -> list:
    lister = getattr(sys.modules.get("gateway.session_authorities"), "all_authorities", None)
    try:
        found = lister(gateway) if callable(lister) else [getattr(gateway, "session_authority", None)]
    except Exception:  # noqa: BLE001 — none readable: none
        return []
    return [authority for authority in found if authority is not None]


def authority_session(session_id: Any) -> Optional[tuple]:
    """``(runner, authority, live session)`` for a chat a session authority in
    this process has open, else None."""
    gateway = runner()
    if gateway is None or not isinstance(session_id, str) or not session_id:
        return None
    for authority in _authorities(gateway):
        sessions = getattr(authority, "sessions", None)
        try:
            live = sessions.get(session_id) if callable(getattr(sessions, "get", None)) else None
        except Exception:  # noqa: BLE001
            live = None
        if live is not None:
            return gateway, authority, live
    return None


def authority_scope(authority: Any) -> Any:
    """The scope of the profile that owns ``authority`` (its config and
    secrets), as the authority's own handlers read them; none without one."""
    scope = getattr(sys.modules.get("gateway.session_authorities"), "owner_scope", None)
    if not callable(scope):
        # A Hermes without per-profile scopes reads one profile's settings.
        return nullcontext()
    # A scope that fails raises: reading the gateway's default profile instead
    # would answer with another profile's model and secrets.
    return scope(authority)


def authority_agent(session_id: Any) -> Optional[tuple]:
    """``(authority, agent)`` for a chat the authority has open whose agent the
    gateway holds (running a turn or cached since its last), else None."""
    found = authority_session(session_id)
    if found is None:
        return None
    gateway, authority, live = found
    # A scope that fails raises to the caller: it isn't a missing agent.
    with authority_scope(authority):
        try:
            agent = gateway._resident_agent_for(gateway._session_key_for_source(live.source))
        except Exception:  # noqa: BLE001 — no agent to read
            return None
    return (authority, agent) if agent is not None else None


class _AuthorityLink:
    """One transport's connection to the authority, as a socket has one: the
    plugin's requests go in the TUI gateway's shapes and come back in them,
    and the chat's events reach the transport's ``write`` the same way."""

    def __init__(self, chats: "_AuthorityChats", transport: Any) -> None:
        self.chats = chats
        self.transport = transport
        self.connection: Any = None
        self.lock = threading.Lock()
        # session id -> the generation of its turn now (or its last), from
        # snapshots and events: the authority names a turn by it.
        self.generations: Dict[str, int] = {}
        # Open approval prompts, oldest first: prompt id -> (session id, generation).
        self.prompts: Dict[str, tuple] = {}
        # session id -> this connection's subscription to it (session.detach names it).
        self.subscriptions: Dict[str, str] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    # The authority writes the chat's events here.
    def write(self, frame: Any) -> bool:
        if self._closed:
            return False
        try:
            shaped = self._event(frame)
        except Exception:  # noqa: BLE001 — Hermes' writer must never see our failure
            logger.debug("Conduit: an authority event couldn't be read", exc_info=True)
            return True
        if shaped is not None:
            try:
                self.transport.write(shaped)
            except Exception:  # noqa: BLE001
                logger.debug("Conduit: an authority event couldn't be passed on", exc_info=True)
        return True

    def _event(self, frame: Any) -> Optional[dict]:
        params = frame.get("params") if isinstance(frame, dict) and frame.get("method") == "event" else None
        if not isinstance(params, dict):
            return frame if isinstance(frame, dict) else None
        sid = params.get("session_id") if isinstance(params.get("session_id"), str) else ""
        kind = params.get("type")
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        generation = params.get("execution_generation")
        with self.lock:
            if sid and type(generation) is int:
                self.generations[sid] = generation
            if kind == "approval.request" and isinstance(payload.get("prompt_id"), str):
                asked = payload.get("execution_generation", generation)
                self.prompts[payload["prompt_id"]] = (sid, asked if type(asked) is int else self.generations.get(sid, 0))
                # The TUI gateway's name for the prompt's id.
                payload = {**payload, "request_id": payload["prompt_id"]}
            elif kind == "approval.settled":
                # Answered (here or by another client) or timed out: the TUI
                # gateway's word for a prompt that is no longer waiting.
                prompt_id = payload.get("prompt_id")
                self.prompts.pop(prompt_id, None)
                kind, payload = "approval.cancelled", {"request_ids": [prompt_id] if isinstance(prompt_id, str) else []}
            elif kind in ("message.complete", "error"):
                for prompt_id in [key for key, (owner, _) in self.prompts.items() if owner == sid]:
                    del self.prompts[prompt_id]
        return {**frame, "params": {**params, "type": kind, "payload": payload}}

    # The plugin's requests.
    def call(self, frame: dict) -> dict:
        rid = frame.get("id")
        method = frame.get("method")
        params = dict(frame.get("params") or {})
        try:
            shaped = self._request(method, params)
            if isinstance(shaped, dict) and "result" in shaped:
                return {"jsonrpc": "2.0", "id": rid, "result": shaped["result"]}
            response = self.chats.run(self._dispatch({"jsonrpc": "2.0", "id": rid, "method": shaped[0],
                                                      "params": shaped[1]}))
        except Exception as exc:  # noqa: BLE001 — reported as the call's error
            if isinstance(exc, TimeoutError):
                # It may have reached the authority, so a turn may be coming.
                message = f"{method} timed out; it may still have started"
            else:
                self._drop_surface(method, params)
                message = str(exc)[:300] or type(exc).__name__
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": message}}
        if not isinstance(response, dict):
            self._drop_surface(method, params)
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"{method} gave no answer"}}
        if method == "prompt.submit" and (response.get("error") or str(
                (response.get("result") or {}).get("status") or "").lower() == "terminal"):
            # No turn of that text is coming (refused, or settled without one): its
            # surface line would wait for the next one. "queued", "started" and
            # "unknown" each have the turn still to run.
            self._drop_surface(method, params)
        return self._answer(method, params, shaped, response)

    def _drop_surface(self, method: Any, params: Dict[str, Any]) -> None:
        if method == "prompt.submit":
            self.chats.drop_surface(params.get("session_id"), params.get("text"))

    async def _dispatch(self, request: dict) -> Any:
        connection = self.connection
        if connection is None:
            if self._closed:
                raise ConnectionError("This transport's connection to the authority is closed")
            connection = self.chats.connect(self)
            with self.lock:
                stale = self._closed or self.connection is not None
                if not stale:
                    self.connection = connection
            if stale:
                # Closed meanwhile, or another call connected first: this one goes unused.
                await connection.close()
                if self._closed:
                    raise ConnectionError("This transport's connection to the authority is closed")
                connection = self.connection
        return await connection.dispatch(request)

    def _generation(self, sid: str) -> int:
        with self.lock:
            known = self.generations.get(sid)
        if known is None:
            # Its own id: two chats' controls on one connection read their fences at once.
            snapshot = self.chats.run(self._dispatch({"jsonrpc": "2.0", "id": f"conduit-fences-{uuid.uuid4().hex}",
                                                      "method": "session.resume",
                                                      "params": {"session_id": sid, "omit_messages": True}}))
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            self._note_snapshot(result)
            with self.lock:
                known = self.generations.get(sid)
        return known or 0

    def _note_snapshot(self, result: Any) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("session_id"), str):
            return
        sid = result["session_id"]
        with self.lock:
            if type(result.get("execution_generation")) is int:
                self.generations[sid] = result["execution_generation"]
            if isinstance(result.get("subscription_id"), str):
                self.subscriptions[sid] = result["subscription_id"]

    def _request(self, method: Any, params: Dict[str, Any]) -> Any:
        """The request in the authority's shape, or ``{"result": …}`` when
        nothing needs asking."""
        sid = params.get("session_id") if isinstance(params.get("session_id"), str) else ""
        if method == "session.create":
            # Desktop's window shape (cols, source desktop) is the TUI gateway's; the
            # authority names the GUI surface and the effort its own way (HermesClient.createParams).
            params.pop("cols", None)
            params["source"] = "gui"
            if "reasoning_effort" in params:
                params["reasoning"] = params.pop("reasoning_effort")
            if params.pop("fast", None) is True:
                params["service_tier"] = "fast"
            return method, params
        if method == "prompt.submit":
            # One admission per call: the authority keeps a retried submission_id to one turn.
            params["submission_id"] = uuid.uuid4().hex
            self.chats.note_surface(self.transport, sid, params.get("text"))
            return method, params
        if method in ("session.interrupt", "session.steer", "session.redirect"):
            params["execution_generation"] = self._generation(sid)
            return method, params
        if method == "approval.respond":
            wanted = params.pop("request_id", None)
            with self.lock:
                if not isinstance(wanted, str) or not wanted:
                    # The TUI gateway answers the oldest; so does this.
                    wanted = next((key for key, (owner, _) in self.prompts.items() if owner == sid), None)
                prompt = self.prompts.get(wanted) if wanted else None
            if prompt is None:
                return {"result": {"resolved": 0}}
            return method, {"session_id": prompt[0], "execution_generation": prompt[1], "prompt_id": wanted,
                            "choice": params.get("choice")}
        if method == "session.close":
            # The authority keeps the chat; this connection stops viewing it.
            with self.lock:
                subscription = self.subscriptions.pop(sid, None)
            if not subscription:
                return {"result": {}}
            return "session.detach", {"session_id": sid, "subscription_id": subscription}
        return method, params

    def _answer(self, method: Any, params: Dict[str, Any], shaped: Any, response: dict) -> dict:
        error = response.get("error")
        reason = ((error.get("data") or {}).get("reason") or error.get("message")) if isinstance(error, dict) else None
        if method == "approval.respond":
            prompt_id = shaped[1].get("prompt_id")
            with self.lock:
                self.prompts.pop(prompt_id, None)
            if reason == "stale_generation":
                # The turn that asked has ended: nothing was waiting.
                return {**response, "error": None, "result": {"resolved": 0}}
            if not error:
                status = str((response.get("result") or {}).get("status") or "").lower()
                return {**response, "result": {"resolved": 1 if status == "resolved" else 0}}
        if method in ("session.steer", "session.redirect", "session.interrupt") and reason == "stale_generation":
            # The turn it named has ended: nothing is running to steer or stop.
            sid = params.get("session_id")
            with self.lock:
                self.generations.pop(sid, None)
            return {**response, "error": None, "result": {} if method == "session.interrupt" else {"status": "rejected"}}
        if not error and method == "session.create":
            self._note_snapshot(response.get("result"))
        return response

    def close(self) -> None:
        """Ends the connection, which stops its views of every chat (the chats
        stay). Never waits: a transport closes from any thread."""
        self._closed = True
        connection, self.connection = self.connection, None
        loop = getattr(self.chats.runner, "_gateway_loop", None)
        if connection is None or loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(connection.close(), loop)
        except Exception:  # noqa: BLE001 — the authority drops a dead connection's views itself
            logger.debug("Conduit: closing an authority connection failed", exc_info=True)


class _AuthorityChats:
    """Hermes' session API in the TUI gateway's shapes (``dispatch(frame,
    transport)`` and ``_sessions.get``), over the gateway's session authority:
    what the plugin's own chats (Watch jobs, a Watch call's Local Live chat,
    Pulse's turns) use. The calls block; run them off the gateway's loop."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner
        self._sessions = self
        self.lock = threading.Lock()
        self.links: "weakref.WeakSet[_AuthorityLink]" = weakref.WeakSet()

    def link(self, transport: Any) -> _AuthorityLink:
        with self.lock:
            link = getattr(transport, "_conduit_authority_link", None)
            if link is None or link.closed or link.chats is not self:
                link = _AuthorityLink(self, transport)
                transport._conduit_authority_link = link
                self.links.add(link)
            return link

    def dispatch(self, frame: dict, transport: Any) -> dict:
        return self.link(transport).call(frame)

    def get(self, session_id: Any, default: Any = None) -> Any:
        """As the TUI gateway's chat record, only which transport views the
        chat; known only for a chat one of the plugin's own transports opened."""
        with self.lock:
            links = list(self.links)
        for link in links:
            if not link.closed and session_id in link.subscriptions:
                return {"transport": link.transport}
        return default

    def connect(self, link: _AuthorityLink) -> Any:
        """A connection for ``link`` (run on the gateway's loop)."""
        from gateway.session_controls import AuthorityConnection

        authority = getattr(self.runner, "session_authority", None)
        if authority is None:
            raise RuntimeError("This gateway has no session authority")
        identity = {**AUTHORITY_IDENTITY, "instance_id": authority.instance_id, "profile_id": authority.profile_id}
        return AuthorityConnection(authority, link, identity)

    def run(self, coroutine: Any, timeout: float = AUTHORITY_RPC_TIMEOUT_S) -> Any:
        loop = getattr(self.runner, "_gateway_loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or loop.is_closed() or running is loop:
            coroutine.close()
            raise RuntimeError("The gateway's loop can't take this call" if running is not loop
                               else "Called on the gateway's own loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            # Nobody waits for it now; a call already in the authority's hands
            # may still have taken effect, which the caller's error says.
            future.cancel()
            raise TimeoutError(f"no answer within {timeout:g} s") from None

    def release(self, transport: Any) -> None:
        release(transport)

    @staticmethod
    def drop_surface(session_id: Any, text: Any) -> None:
        """Forgets the surface noted for a turn the authority didn't take."""
        if not isinstance(session_id, str) or not session_id or not isinstance(text, str):
            return
        try:
            _sibling("turn_surface").discard_for_authority(session_id, text)
        except Exception:  # noqa: BLE001 — it expires on its own
            logger.debug("Conduit: a refused turn's surface couldn't be dropped", exc_info=True)

    @staticmethod
    def note_surface(transport: Any, session_id: str, text: Any) -> None:
        """A turn the plugin's Watch transports submit names its surface, as
        Conduit's own reports do (turn_surface.py): the authority runs the
        turn without the transport."""
        surface = getattr(transport, "conduit_surface", None)
        if not session_id or not isinstance(text, str) or not surface:
            return
        try:
            turn_surface = _sibling("turn_surface")
            turn_surface.record_for_authority(turn_surface.Report(
                session_id, turn_surface.text_sha256(text), surface, getattr(transport, "conduit_input", None)))
        except Exception:  # noqa: BLE001 — no line for that turn
            logger.debug("Conduit: a Watch turn's surface couldn't be kept", exc_info=True)


_authority_chats: Optional[_AuthorityChats] = None
_chats_lock = threading.Lock()


def session_api() -> Optional[_AuthorityChats]:
    """The session API over this process's gateway authority, or None."""
    global _authority_chats
    gateway = runner()
    if gateway is None or getattr(gateway, "session_authority", None) is None:
        return None
    with _chats_lock:
        chats = _authority_chats
        if chats is None or chats.runner is not gateway:
            chats = _authority_chats = _AuthorityChats(gateway)
    return chats


def release(transport: Any) -> None:
    """Ends ``transport``'s connection to the authority, if it has one."""
    link = getattr(transport, "_conduit_authority_link", None)
    if callable(getattr(link, "close", None)):
        link.close()
