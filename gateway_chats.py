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
import time
import uuid
import weakref
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _sibling(name: str) -> Any:
    """Another module of the plugin, from the package this one was loaded in."""
    package = __name__.rpartition(".")[0]
    return importlib.import_module(f"{package}.{name}" if package else name)


AUTHORITY_RPC_TIMEOUT_S = 60.0
# How long a view's own record of a chat's waiting approvals stands in for
# reading the chat again (notifier #764). The view hears each prompt asked and
# settled; Hermes prunes a prompt the native queue dropped only when the chat
# is read, so a read this often catches what no event said.
SEEN_APPROVALS_MAX_AGE_S = 300.0
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
        logger.warning("Conduit: couldn't list the gateway's session authorities", exc_info=True)
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
        except Exception:  # noqa: BLE001 — read as not open there, said in the log
            logger.warning("Conduit: couldn't read a chat from the gateway's session authority", exc_info=True)
            live = None
        if live is not None:
            return gateway, authority, live
    return None


def authority_for_home(home: Any) -> Any:
    """The session authority in this process serving the Hermes home ``home``
    (a gateway serving several profiles has one per profile), else None."""
    gateway = runner()
    if gateway is None:
        return None
    try:
        wanted = Path(home).resolve()
    except (OSError, TypeError, ValueError):
        return None
    for authority in _authorities(gateway):
        try:
            if Path(str(authority.profile_id)).resolve() == wanted:
                return authority
        except (OSError, TypeError, ValueError, AttributeError):
            continue
    return None


def authority_session_ids(home: Any) -> Optional[set]:
    """The chats the session authority serving ``home`` has open, or None when
    no authority in this process serves it."""
    authority = authority_for_home(home)
    if authority is None:
        return None
    sessions = getattr(authority, "sessions", None)
    try:
        return {sid for sid in list(sessions.keys()) if isinstance(sid, str)}
    except Exception:  # noqa: BLE001 — unreadable: the caller tries again later
        logger.warning("Conduit: couldn't list the chats a session authority has open", exc_info=True)
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
    # A scope or lookup that fails raises to the caller: it isn't a missing
    # agent, and saying the agent isn't loaded would tell the user the wrong
    # thing.
    with authority_scope(authority):
        try:
            agent = gateway._resident_agent_for(gateway._session_key_for_source(live.source))
        except Exception as exc:  # noqa: BLE001 — Hermes changed how it keeps agents
            logger.warning("Conduit: couldn't read the gateway's agent for a chat", exc_info=True)
            raise AgentLookupError("Conduit couldn't read the chat's agent on this Hermes") from exc
    return (authority, agent) if agent is not None else None


_TURN_CONTROLS = ("session.steer", "session.redirect", "session.interrupt")
# What every submission id the plugin's own chats send starts with: a turn of
# theirs a gateway restart lost is theirs to discard (``_settle_lost``), a
# person's (a different id) never.
OWN_SUBMISSION_PREFIX = "conduit-plugin-"
# A discard refused for these reasons finds the lost turn already gone.
_SETTLED_LOST_REASONS = frozenset({"stale_generation", "not_found", "admission_terminal", "already_resolved"})


def _reason(response: Any) -> Optional[str]:
    """An error answer's refusal reason (``data.reason``, else its message)."""
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return None
    data = error.get("data")
    return (data.get("reason") if isinstance(data, dict) else None) or error.get("message")


def _log_failed_detach(future: Any) -> None:
    # The older view stays open until the link closes.
    if not future.cancelled() and future.exception() is not None:
        logger.warning("Conduit: detaching an older view of a chat failed", exc_info=future.exception())


def _log_failed_close(future: Any) -> None:
    # The authority drops a dead connection's views itself.
    if not future.cancelled() and future.exception() is not None:
        logger.debug("Conduit: closing an authority connection failed", exc_info=future.exception())


def _prompt_text(prompt: dict) -> tuple:
    """An approval prompt's (command, description, when it times out, when it
    was heard), the times monotonic."""
    now = time.monotonic()
    timeout = prompt.get("timeout_s")
    expires = (now + timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
               and timeout > 0 else None)
    return str(prompt.get("command") or ""), str(prompt.get("description") or ""), expires, now


def _viewing(home: Any, sid: str, subscription: str) -> bool:
    """Whether the authority serving ``home`` still sends ``subscription``
    the chat's events: a view it retired (its backlog overflowed, or the chat
    was closed and opened again) hears nothing more."""
    authority = authority_for_home(home)
    try:
        live = getattr(authority, "sessions", {}).get(sid) if authority is not None else None
        return subscription in getattr(live, "subscribers", {})
    except Exception:  # noqa: BLE001 — unreadable: the caller reads the chat instead
        logger.debug("Conduit: couldn't read a chat's viewers", exc_info=True)
        return False


class AgentLookupError(RuntimeError):
    """The gateway's agent for a chat couldn't be read (not: it has none)."""


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
        # What each open prompt asks: prompt id -> (command, description, when it
        # times out or None, when this link heard of it), the times monotonic.
        self.prompt_texts: Dict[str, tuple] = {}
        # session id -> when this link last read the chat (monotonic): its record of
        # the chat's prompts is whole from then on while its view lasts.
        self.viewed: Dict[str, float] = {}
        # session id -> this connection's subscription to it (session.detach names it).
        self.subscriptions: Dict[str, str] = {}
        # session id -> the plugin's own turns a gateway restart lost there, as the
        # chat's ``pending`` last listed them: admission id -> the generation it ran as.
        # A chat this link has read (a snapshot or a ``session.info``) is a key.
        self.lost: Dict[str, Dict[str, int]] = {}
        # session id -> the profile its requests named: on a gateway serving
        # several profiles the authority finds a sibling profile's chat only
        # by it, so the link's own reads of that chat name it too.
        self.profiles: Dict[str, str] = {}
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
            if sid and kind == "session.info":
                self._note_pending(sid, payload)
            if kind == "approval.request" and isinstance(payload.get("prompt_id"), str):
                asked = payload.get("execution_generation", generation)
                self.prompts[payload["prompt_id"]] = (sid, asked if type(asked) is int else self.generations.get(sid, 0))
                self.prompt_texts[payload["prompt_id"]] = _prompt_text(payload)
                # The TUI gateway's name for the prompt's id.
                payload = {**payload, "request_id": payload["prompt_id"]}
            elif kind == "approval.settled":
                # Answered (here or by another client) or timed out: the TUI
                # gateway's word for a prompt that is no longer waiting.
                prompt_id = payload.get("prompt_id")
                self._forget_prompt(prompt_id)
                kind, payload = "approval.cancelled", {"request_ids": [prompt_id] if isinstance(prompt_id, str) else []}
            elif kind in ("message.complete", "error"):
                for prompt_id in [key for key, (owner, _) in self.prompts.items() if owner == sid]:
                    self._forget_prompt(prompt_id)
        return {**frame, "params": {**params, "type": kind, "payload": payload}}

    # The plugin's requests.
    def call(self, frame: dict, timeout: float = AUTHORITY_RPC_TIMEOUT_S) -> dict:
        rid = frame.get("id")
        method = frame.get("method")
        params = dict(frame.get("params")) if isinstance(frame.get("params"), dict) else {}
        shaped: Any = None
        sid = params.get("session_id") if isinstance(params.get("session_id"), str) else ""
        if sid and isinstance(params.get("profile"), str) and params["profile"]:
            with self.lock:
                self.profiles[sid] = params["profile"]
        # One budget for the whole call: the fence reads and the stale retry
        # come out of the caller's timeout, never on top of it.
        asked_at = time.monotonic()
        deadline = asked_at + timeout

        def left() -> float:
            return max(0.05, deadline - time.monotonic())
        try:
            shaped = self._request(method, dict(params), left)
            if isinstance(shaped, dict) and "result" in shaped:
                return {"jsonrpc": "2.0", "id": rid, "result": shaped["result"]}
            response = self.chats.run(self._dispatch({"jsonrpc": "2.0", "id": rid, "method": shaped[0],
                                                      "params": shaped[1]}), left())
            if method in _TURN_CONTROLS and _reason(response) == "stale_generation":
                # The fence came from events that may lag a newer turn, or a
                # turn ended and another began since the read: read the chat
                # and go once more. Refused again, it's nothing to stop only
                # if the chat has no turn running now.
                with self.lock:
                    self.generations.pop(sid, None)
                shaped = self._request(method, dict(params), left)
                response = self.chats.run(self._dispatch({"jsonrpc": "2.0", "id": rid, "method": shaped[0],
                                                          "params": shaped[1]}), left())
                if _reason(response) == "stale_generation" and self._still_running(sid, left()):
                    # Refused again while a turn runs: the turn changed under the
                    # retry, so nothing was steered or stopped.
                    with self.lock:
                        self.generations.pop(sid, None)
                    return {"jsonrpc": "2.0", "id": rid, "error": {
                        "code": -32000,
                        "message": f"{method} missed: the chat's turn changed, and nothing was sent to the new one"}}
        except Exception as exc:  # noqa: BLE001 — reported as the call's error
            if method == "session.close" and isinstance(shaped, tuple) and shaped[1].get("subscription_id"):
                # Not detached: keep the view's subscription for another try.
                with self.lock:
                    self.subscriptions.setdefault(params.get("session_id"), shaped[1]["subscription_id"])
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
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        if method == "prompt.submit" and (response.get("error") or str(result.get("status") or "").lower() == "terminal"):
            # No turn of that text is coming (refused, or settled without one): its
            # surface line would wait for the next one. "queued", "started" and
            # "unknown" each have the turn still to run.
            self._drop_surface(method, params)
        return self._answer(method, params, shaped, response, asked_at)

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
        # An in-process AuthorityConnection over the authority object, not a
        # socket: it doesn't drop on its own, and lives as long as this link
        # (Hermes' session_controls; a gateway restart restarts this process).
        return await connection.dispatch(request)

    def _scoped(self, sid: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """``params`` naming the profile this link's requests named for the chat."""
        with self.lock:
            profile = self.profiles.get(sid)
        return {**params, "profile": profile} if profile else params

    def _generation(self, sid: str, timeout: float = AUTHORITY_RPC_TIMEOUT_S) -> int:
        with self.lock:
            known = self.generations.get(sid)
        if known is None:
            asked_at = time.monotonic()
            # Its own id: two chats' controls on one connection read their fences at once.
            try:
                snapshot = self.chats.run(self._dispatch({"jsonrpc": "2.0",
                                                          "id": f"conduit-fences-{uuid.uuid4().hex}",
                                                          "method": "session.resume",
                                                          "params": self._scoped(sid, {"session_id": sid, "omit_messages": True})}),
                                         timeout)
            except TimeoutError as exc:
                # Nothing was sent for the control itself: not "may have started".
                raise RuntimeError("Reading the chat's turn timed out; nothing was sent") from exc
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            self._note_snapshot(result, asked_at)
            with self.lock:
                known = self.generations.get(sid)
            if known is None:
                if isinstance(result, dict) and result.get("running") is False:
                    # Nothing runs: any fence reads as nothing to steer or stop.
                    return 0
                # A fence of 0 against a running turn would read its refusal as
                # nothing running (an interrupt "done" while the turn goes on).
                raise RuntimeError("Hermes didn't say which turn the chat is running")
        return known

    def _still_running(self, sid: str, timeout: float = AUTHORITY_RPC_TIMEOUT_S) -> bool:
        """Whether the chat runs a turn now; one that can't be read counts as running,
        so a refusal is never reported as nothing to stop."""
        asked_at = time.monotonic()
        try:
            snapshot = self.chats.run(self._dispatch({"jsonrpc": "2.0",
                                                      "id": f"conduit-running-{uuid.uuid4().hex}",
                                                      "method": "session.resume",
                                                      "params": self._scoped(sid, {"session_id": sid, "omit_messages": True})}),
                                     timeout)
        except Exception:  # noqa: BLE001 — unknown reads as running
            return True
        result = snapshot.get("result") if isinstance(snapshot, dict) else None
        self._note_snapshot(result, asked_at)
        return not (isinstance(result, dict) and result.get("running") is False)

    def _note_pending(self, sid: str, snapshot: Dict[str, Any]) -> None:
        """The chat's lost turns from a snapshot or ``session.info`` that lists its
        ``pending`` admissions (call with the lock held). One without the key says
        nothing about them."""
        pending = snapshot.get("pending")
        if not isinstance(pending, list):
            return
        self.lost[sid] = {row["admission_id"]: row["execution_generation"] for row in pending
                          if isinstance(row, dict) and row.get("status") == "unknown"
                          and isinstance(row.get("admission_id"), str)
                          and type(row.get("execution_generation")) is int
                          and str(row.get("input_id") or "").startswith(OWN_SUBMISSION_PREFIX)}

    def _settle_lost(self, sid: str, left: Any = None) -> None:
        """Discards the plugin's own turns a gateway restart left ``unknown`` in a
        chat before the plugin sends into it again. Hermes holds every later
        message behind one until someone discards it (Desktop's Discard, Conduit's
        card), and settles only automation's by itself; the plugin's chats (Watch
        jobs, a Watch call's Local Live chat, Pulse) have nobody on a screen to ask,
        and their next turn supersedes the lost one. Never resent. A person's lost
        turn (not the plugin's submission id) stays for them to decide in Conduit.
        A chat this link hasn't read is read first."""
        if not sid:
            return
        with self.lock:
            known = sid in self.lost
        if not known:
            asked_at = time.monotonic()
            try:
                snapshot = self.chats.run(self._dispatch({"jsonrpc": "2.0",
                                                          "id": f"conduit-pending-{uuid.uuid4().hex}",
                                                          "method": "session.resume",
                                                          "params": self._scoped(sid, {"session_id": sid, "omit_messages": True})}),
                                         left() if left else AUTHORITY_RPC_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — the send goes on; a refused read is the send's to report
                logger.debug("Conduit: reading a chat's queue before a send failed", exc_info=True)
                return
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            if not isinstance(result, dict) or result.get("session_id") != sid:
                return  # Not read: the next send reads it again.
            self._note_snapshot(result, asked_at)
            with self.lock:
                # Read: a Hermes whose snapshot lists no queue isn't read again.
                self.lost.setdefault(sid, {})
        with self.lock:
            lost = dict(self.lost.get(sid) or {})
        for admission_id, generation in lost.items():
            try:
                answer = self.chats.run(self._dispatch({"jsonrpc": "2.0",
                                                        "id": f"conduit-discard-{uuid.uuid4().hex}",
                                                        "method": "prompt.resolve_unknown",
                                                        "params": self._scoped(sid, {
                                                            "session_id": sid, "admission_id": admission_id,
                                                            "execution_generation": generation})}),
                                       left() if left else AUTHORITY_RPC_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — the send still goes; Hermes keeps it queued behind the lost turn
                logger.debug("Conduit: discarding a chat's lost turn failed", exc_info=True)
                continue
            if isinstance(answer, dict) and answer.get("error"):
                reason = _reason(answer)
                if reason not in _SETTLED_LOST_REASONS:
                    # Still holding the chat: the next send tries again.
                    logger.info("Conduit: Hermes kept a lost turn in %s: %s", sid, reason)
                    continue
            with self.lock:
                (self.lost.get(sid) or {}).pop(admission_id, None)

    def _note_snapshot(self, result: Any, asked_at: float) -> None:
        """Notes a chat's snapshot, read by a request sent at ``asked_at``
        (monotonic) or later."""
        if not isinstance(result, dict) or not isinstance(result.get("session_id"), str):
            return
        sid = result["session_id"]
        listed = {prompt.get("prompt_id") for prompt in result.get("prompts") or () if isinstance(prompt, dict)}
        with self.lock:
            if type(result.get("execution_generation")) is int:
                self.generations[sid] = result["execution_generation"]
            self._note_pending(sid, result)
            for prompt_id in [key for key, (owner, _) in self.prompts.items() if owner == sid and key not in listed]:
                # Settled before the read though this link never heard it (a
                # retired view misses the event). One heard asked after the
                # request went may be newer than the snapshot: its own events settle it.
                heard = self.prompt_texts.get(prompt_id, (None, None, None, 0.0))[3]
                if heard < asked_at:
                    self._forget_prompt(prompt_id)
            for prompt in result.get("prompts") or ():
                # An approval still waiting when the chat was read: answerable by its id,
                # though this link never saw it asked.
                if (isinstance(prompt, dict) and prompt.get("kind") == "approval"
                        and isinstance(prompt.get("prompt_id"), str) and type(prompt.get("execution_generation")) is int):
                    self.prompts.setdefault(prompt["prompt_id"], (sid, prompt["execution_generation"]))
                    self.prompt_texts.setdefault(prompt["prompt_id"], _prompt_text(prompt))
            self.viewed[sid] = time.monotonic()
            replaced = None
            if isinstance(result.get("subscription_id"), str):
                replaced = self.subscriptions.get(sid)
                self.subscriptions[sid] = result["subscription_id"]
        if replaced and replaced != result.get("subscription_id"):
            # A second view of the chat on this connection (a resume for its
            # fences): the older one ends, so a close detaches all there is.
            self._detach_later(sid, replaced)

    def _forget_prompt(self, prompt_id: Any) -> None:
        # Called with the lock held.
        self.prompts.pop(prompt_id, None)
        self.prompt_texts.pop(prompt_id, None)

    def seen_approvals(self, sid: str, viewing: Any) -> Optional[list]:
        """The approvals ``sid`` waits on, oldest first, as this link's view of
        the chat heard them asked and not settled; None when the view can't
        say (never read, read too long ago, or ``viewing(subscription)`` finds
        the authority no longer sends it the chat's events)."""
        now = time.monotonic()
        with self.lock:
            subscription = self.subscriptions.get(sid)
            viewed = self.viewed.get(sid)
            if self._closed or not subscription or viewed is None or now - viewed > SEEN_APPROVALS_MAX_AGE_S:
                return None
            current = self.generations.get(sid)
            rows = []
            for prompt_id, (owner, generation) in self.prompts.items():
                command, description, expires, _heard = self.prompt_texts.get(prompt_id, ("", "", None, 0.0))
                if owner != sid or (current is not None and generation != current):
                    continue
                if expires is not None and now >= expires:
                    continue  # timed out: Hermes stopped waiting on it
                rows.append({"request_id": prompt_id, "command": command, "description": description})
        return rows if viewing(subscription) else None

    def _detach_later(self, sid: str, subscription: str) -> None:
        connection = self.connection
        loop = getattr(self.chats.runner, "_gateway_loop", None)
        if connection is None or loop is None or loop.is_closed():
            return
        request = {"jsonrpc": "2.0", "id": f"conduit-detach-{uuid.uuid4().hex}", "method": "session.detach",
                   "params": self._scoped(sid, {"session_id": sid, "subscription_id": subscription})}
        try:
            future = asyncio.run_coroutine_threadsafe(connection.dispatch(request), loop)
        except Exception:  # noqa: BLE001 — the view ends with the connection
            logger.debug("Conduit: detaching an older view failed", exc_info=True)
            return
        future.add_done_callback(_log_failed_detach)

    def _request(self, method: Any, params: Dict[str, Any], left: Any = None) -> Any:
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
            if "idempotency_key" in params:
                # The authority keeps a retried create to one chat by its request id.
                params.setdefault("request_id", params.pop("idempotency_key"))
            return method, params
        if method == "prompt.submit":
            self._settle_lost(sid, left)
            # One admission per call: the authority keeps a retried submission_id to one turn.
            params["submission_id"] = OWN_SUBMISSION_PREFIX + uuid.uuid4().hex
            self.chats.note_surface(self.transport, sid, params.get("text"))
            return method, params
        if method in ("session.interrupt", "session.steer", "session.redirect"):
            params["execution_generation"] = self._generation(sid, left() if left else AUTHORITY_RPC_TIMEOUT_S)
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
            return method, self._scoped(prompt[0], {"session_id": prompt[0], "execution_generation": prompt[1],
                                                    "prompt_id": wanted, "choice": params.get("choice")})
        if method == "session.close":
            # The authority keeps the chat; this connection stops viewing it.
            with self.lock:
                subscription = self.subscriptions.pop(sid, None)
            if not subscription:
                return {"result": {}}
            return "session.detach", self._scoped(sid, {"session_id": sid, "subscription_id": subscription})
        return method, params

    def _answer(self, method: Any, params: Dict[str, Any], shaped: Any, response: dict, asked_at: float) -> dict:
        error = response.get("error")
        reason = _reason(response)
        if method == "approval.respond":
            prompt_id = shaped[1].get("prompt_id")
            if not error or reason == "stale_generation":
                # Settled (or its turn ended); a refused answer keeps it to try again.
                with self.lock:
                    self._forget_prompt(prompt_id)
            if reason == "stale_generation":
                # The turn that asked has ended: nothing was waiting.
                return {**response, "error": None, "result": {"resolved": 0}}
            if not error:
                answered = response.get("result") if isinstance(response.get("result"), dict) else {}
                status = str(answered.get("status") or "").lower()
                return {**response, "result": {"resolved": 1 if status == "resolved" else 0}}
        if method in ("session.steer", "session.redirect", "session.interrupt") and reason == "stale_generation":
            # The turn it named has ended: nothing is running to steer or stop.
            sid = params.get("session_id")
            with self.lock:
                self.generations.pop(sid, None)
            return {**response, "error": None, "result": {} if method == "session.interrupt" else {"status": "rejected"}}
        if method == "session.close" and error:
            # Not detached: keep the view's subscription for another try.
            subscription = shaped[1].get("subscription_id")
            if subscription:
                with self.lock:
                    self.subscriptions.setdefault(params.get("session_id"), subscription)
        if not error and method in ("session.create", "session.resume"):
            made = response.get("result") if isinstance(response.get("result"), dict) else {}
            if (method == "session.create" and isinstance(made.get("session_id"), str)
                    and isinstance(params.get("profile"), str) and params["profile"]):
                with self.lock:
                    self.profiles[made["session_id"]] = params["profile"]
            self._note_snapshot(made, asked_at)
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
            future = asyncio.run_coroutine_threadsafe(connection.close(), loop)
        except Exception:  # noqa: BLE001 — the authority drops a dead connection's views itself
            logger.debug("Conduit: closing an authority connection failed", exc_info=True)
            return
        future.add_done_callback(_log_failed_close)


class _AuthorityChats:
    """Hermes' session API in the TUI gateway's shapes (``dispatch(frame,
    transport)`` and ``_sessions.get``), over the gateway's session authority:
    what the plugin's own chats (Watch jobs, a Watch call's Local Live chat,
    Pulse's turns) use. The calls block; run them off the gateway's loop."""

    # What work_broker tells this API from the TUI gateway's module by.
    serves_authority = True

    def __init__(self, runner: Any) -> None:
        self.runner = runner
        self._sessions = self
        self.lock = threading.Lock()
        self.links: "weakref.WeakSet[_AuthorityLink]" = weakref.WeakSet()

    def link(self, transport: Any) -> _AuthorityLink:
        with self.lock:
            link = getattr(transport, "_conduit_authority_link", None)
            if link is None or link.closed or link.chats is not self:
                if link is not None and not link.closed:
                    # The gateway's runner was replaced: the old link's view ends.
                    link.close()
                link = _AuthorityLink(self, transport)
                transport._conduit_authority_link = link
                self.links.add(link)
            return link

    # Callers with their own budget pass it to dispatch (the TUI registry's takes none).
    takes_timeout = True

    def dispatch(self, frame: dict, transport: Any, timeout: float = AUTHORITY_RPC_TIMEOUT_S) -> dict:
        return self.link(transport).call(frame, timeout)

    def seen_approvals(self, transport: Any, home: Any, session_id: str) -> Optional[list]:
        """The approvals a chat waits on as ``transport``'s view of it heard
        them, without asking the authority; None when it has no such view,
        and the caller reads the chat."""
        link = getattr(transport, "_conduit_authority_link", None)
        if link is None or link.closed or link.chats is not self:
            return None
        return link.seen_approvals(session_id, lambda subscription: _viewing(home, session_id, subscription))

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
        if loop is None or loop.is_closed():
            coroutine.close()
            raise RuntimeError("The gateway's loop isn't running")
        if running is loop:
            coroutine.close()
            raise RuntimeError("Called on the gateway's own loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            if future.done():
                raise  # the call's own error, not this wait's
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
            if surface not in turn_surface.SURFACES:
                # The work broker's turns are from no screen: a report no
                # turn would take would only crowd out real ones.
                return
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
