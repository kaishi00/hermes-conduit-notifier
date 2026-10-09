"""Hermes lifecycle observer for Conduit push notifications."""

from __future__ import annotations

import functools
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from . import call_tool, calls, clarify_loop
from .client import enqueue
from .events import approval_decision, clarification_text, event_id, is_silent_response, push_event


logger = logging.getLogger(__name__)
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
    # A call held over a restart goes out when its hold runs out (#449).
    calls.resume(_profile)
    # Hermes asks for calls itself (#449): the tool and its skill. An answered
    # approval stops its alert call; a Hermes without the hook leaves that to
    # the turn's end.
    call_tool.register(ctx, is_child=_is_child,
                       skill_path=Path(__file__).resolve().parent / "skills" / call_tool.SKILL_NAME / "SKILL.md")
    try:
        ctx.register_hook("post_approval_response", _post_approval_response)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: post_approval_response hook unavailable", exc_info=True)
    # The voice hint is an extra: a Hermes that refuses the hook must not
    # take the notifications down with it.
    try:
        ctx.register_hook("pre_llm_call", _pre_llm_call)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: pre_llm_call hook unavailable; voice replies get no persona hint", exc_info=True)
    # Wrap clarify execution so a backgrounded device gets an answerable card
    # (plugin-minted id, answered through the relay). Older gateways without
    # middleware support simply keep the original clarify path.
    if hasattr(ctx, "register_middleware"):
        ctx.register_middleware(
            "tool_execution",
            functools.partial(clarify_loop.middleware, is_child_session=_is_child),
        )
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
    if _is_child(session_id):
        return
    ready = None if is_silent_response(response) else push_event(
        "response.ready",
        identifier=event_id("response", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
        body=_text(response),
    )
    # A job the user asked to be called about (#449) ends here: the call
    # request takes the place of the ready notification.
    if calls.turn_ended(session_id, "done", profile=_profile, fallback=ready):
        return
    if ready is not None:
        enqueue(ready)


def _on_session_end(**kwargs: Any) -> None:
    # Fires at the end of every turn, whatever happened (post_llm_call only
    # after a turn that finished): the one place a watched job's failure or
    # stop shows up. A finished turn's watch was normally consumed by
    # post_llm_call already, so this finds nothing then.
    session_id = _text(kwargs.get("session_id"))
    if _is_child(session_id):
        return
    if kwargs.get("completed"):
        calls.turn_ended(session_id, "done", profile=_profile, fallback=None)
        return
    if kwargs.get("interrupted"):
        calls.turn_ended(session_id, "stopped", profile=_profile, fallback=None)
        return
    failed = push_event(
        "turn.failed",
        identifier=event_id("failure", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
    )
    if calls.turn_ended(session_id, "failed", profile=_profile, fallback=failed):
        return
    # With alert calls on, a failed turn calls instead.
    if calls.failed_turn(session_id, profile=_profile, fallback=failed, turn_id=kwargs.get("turn_id")):
        return
    enqueue(failed)


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
    # With alert calls on, an approval left unanswered for a minute calls the
    # user too (#449), beside the answerable notification.
    calls.alert(session_id, "approval", description, profile=_profile)


def _post_approval_response(**kwargs: Any) -> None:
    calls.cancel_alerts(_text(kwargs.get("session_key")), "approval")


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


# Hermes' voice-live delegation note starts with this; used when the running
# Hermes has no tools.voice_live to import it from.
_VOICE_LIVE_NOTE_PREFIX = "[Note: this message is a delegation from a live spoken conversation"
_voice_live_prefix: str | None = None

PERSONA_VOICE_NOTE = (
    "[Spoken reply: keep your usual personality through word choice and tone, but everything you "
    "write will be read aloud, so write only words meant to be spoken. No stage directions or "
    "narrated actions (for example *sets down the gavel*), no sound effects, no emoji, no markdown.]"
)


def _voice_note_prefix() -> str:
    global _voice_live_prefix
    if _voice_live_prefix is None:
        try:
            from tools.voice_live import VOICE_LIVE_TURN_NOTE

            prefix = VOICE_LIVE_TURN_NOTE if isinstance(VOICE_LIVE_TURN_NOTE, str) and VOICE_LIVE_TURN_NOTE else ""
        except Exception:  # noqa: BLE001 — an older Hermes, or none (tests)
            prefix = ""
        _voice_live_prefix = prefix or _VOICE_LIVE_NOTE_PREFIX
    return _voice_live_prefix


def _text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [part["text"] for part in content
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)]
    return []


def _pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    """Keep a voice-live delegation's reply speakable without flattening the persona.

    The TUI gateway prepends Hermes' voice-live note to the model input of a
    turn delegated from a live spoken conversation; the ``user_message`` kwarg
    is the clean text, so the note is read from the turn's own user row in
    ``conversation_history``.
    """
    try:
        history = kwargs.get("conversation_history")
        if not isinstance(history, list):
            return None
        for message in reversed(history):
            if isinstance(message, dict) and message.get("role") == "user":
                prefix = _voice_note_prefix()
                if any(text.lstrip().startswith(prefix) for text in _text_parts(message.get("content"))):
                    return {"context": PERSONA_VOICE_NOTE}
                return None
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        return None
    return None


def _is_child(session_id: Any) -> bool:
    value = _text(session_id)
    with _children_lock:
        return bool(value and value in _child_sessions)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
