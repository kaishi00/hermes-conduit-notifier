"""Hermes lifecycle observer for Conduit push notifications."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from . import clarify_loop
from .client import enqueue
from .events import approval_decision, clarification_text, event_id, is_silent_response, push_event


_child_sessions: set[str] = set()
_children_lock = threading.Lock()
_profile = "default"
_clarify_loop_active = False


def register(ctx: Any) -> None:
    global _profile
    _profile = ctx.profile_name
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_approval_request", _pre_approval_request)
    ctx.register_hook("subagent_start", _subagent_start)
    ctx.register_hook("subagent_stop", _subagent_stop)
    # Wrap clarify execution so a backgrounded device gets an answerable card
    # (plugin-minted id, answered through the relay). Older gateways without
    # middleware support simply keep the original clarify path.
    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware("tool_execution", clarify_loop.middleware)
        clarify_loop.set_profile(_profile)
        global _clarify_loop_active
        _clarify_loop_active = True
    from .cli import dispatch, register_cli
    ctx.register_cli_command(
        name="conduit-push",
        help="Pair and test Hermes Conduit notifications",
        setup_fn=register_cli,
        handler_fn=dispatch,
        description="Manage the profile-scoped Conduit push notification pairing.",
    )


def _pre_tool_call(**kwargs: Any) -> None:
    if kwargs.get("tool_name") != "clarify" or _is_child(kwargs.get("session_id")):
        return
    if _clarify_loop_active:
        # The middleware wraps the execution and pushes a richer, answerable
        # input.needed event itself; pushing here too would double-notify.
        return
    enqueue(push_event(
        "input.needed",
        identifier=event_id("input", kwargs.get("turn_id"), kwargs.get("tool_call_id")),
        session_id=_text(kwargs.get("session_id")),
        profile=_profile,
        body=clarification_text(kwargs.get("args")),
    ))


def _post_llm_call(**kwargs: Any) -> None:
    session_id = _text(kwargs.get("session_id"))
    response = kwargs.get("assistant_response")
    if _is_child(session_id) or is_silent_response(response):
        return
    enqueue(push_event(
        "response.ready",
        identifier=event_id("response", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
        body=_text(response),
    ))


def _on_session_end(**kwargs: Any) -> None:
    session_id = _text(kwargs.get("session_id"))
    if kwargs.get("completed") or kwargs.get("interrupted") or _is_child(session_id):
        return
    enqueue(push_event(
        "turn.failed",
        identifier=event_id("failure", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
    ))


def _pre_approval_request(**kwargs: Any) -> None:
    if kwargs.get("surface") == "smart":
        return
    session_id = _text(kwargs.get("session_key"))
    description = _text(kwargs.get("description"))
    # The event id deliberately excludes `id(kwargs)`: CPython recycles ids, so
    # a later, distinct approval with the same session/pattern/command could
    # collide and be silently deduped by the relay for 24h. `turn_id` (forwarded
    # by the approval hook) is stable across replays of one turn and distinct
    # across turns. When the hook carries no turn id, fall back to a unique id
    # per raise: two identical commands approved in sequence must both notify,
    # and the plugin's delivery queue never retries, so a stable id has no
    # at-least-once role to play there.
    turn_id = kwargs.get("turn_id") or uuid.uuid4().hex
    enqueue(push_event(
        "approval.needed",
        identifier=event_id("approval", session_id, turn_id, kwargs.get("pattern_key"), kwargs.get("command")),
        session_id=session_id,
        profile=_profile,
        body=description or "Hermes is waiting for your approval.",
        # Attach the structured card so Conduit can render an answerable
        # approval from the push payload while backgrounded. Requires both a
        # session_key (to route the choice back via approval.respond) and a
        # description (the card's display text); without either, the
        # sanitizer would reject it anyway, so skip the dead build.
        # The raw command is omitted (see approval_decision) to avoid
        # echoing secrets through APNs.
        decision=(
            approval_decision(session_key=session_id, description=description)
            if session_id and description
            else None
        ),
    ))


def _subagent_start(**kwargs: Any) -> None:
    child = _text(kwargs.get("child_session_id"))
    if child:
        with _children_lock:
            _child_sessions.add(child)


def _subagent_stop(**kwargs: Any) -> None:
    child = _text(kwargs.get("child_session_id"))
    if child:
        with _children_lock:
            _child_sessions.discard(child)
    parent = _text(kwargs.get("parent_session_id"))
    status = _text(kwargs.get("child_status")) or "finished"
    enqueue(push_event(
        "background_task.finished",
        identifier=event_id("subagent", kwargs.get("parent_turn_id"), child, status),
        session_id=parent,
        profile=_profile,
        body=_text(kwargs.get("child_summary")) or f"A delegated task {status}.",
    ))


def _is_child(session_id: Any) -> bool:
    value = _text(session_id)
    with _children_lock:
        return bool(value and value in _child_sessions)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
