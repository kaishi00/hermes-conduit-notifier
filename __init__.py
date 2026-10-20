"""Hermes lifecycle observer for Conduit push notifications."""

from __future__ import annotations

import contextvars
import functools
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from . import (call_tool, call_watches, calls, clarify_loop, client, conduit_first, cron_runs, desktop_selected,
               health_tool, live_activity, location_tool, maps_tool, phone_tool, present_tool, reply_sources,
               settings_tool, show_tool, turn_surface)
from .session_titles import title as _session_title, titled as _titled
from .client import enqueue
from .events import approval_decision, clarification_text, event_id, is_silent_response, plain_text, push_event


logger = logging.getLogger(__name__)
_child_sessions: set[str] = set()
_children_lock = threading.Lock()
_profile = "default"
_clarify_loop_active = False
# turn_id -> when its pre_llm_call ran (#87, time.monotonic()), oldest first.
_turn_started: dict[str, float] = {}
_turn_started_lock = threading.Lock()
MAX_TURN_STARTS = 256
# (session id, turn_id) -> (its "response ready" event or None, whether it
# goes out, the grace timer), held from post_llm_call until on_session_end
# says the turn finished.
_pending_ready: dict[tuple[str, str], tuple[Any, bool, Any]] = {}
_pending_lock = threading.Lock()
# on_session_end follows post_llm_call in the same finalizer, after only the
# memory sync; the grace is for a Hermes that never reports the end.
READY_GRACE_S = 120.0
# Parent session -> (its running turn's id, when it started by
# time.monotonic()): a subagent that finishes inside it is part of its reply.
_parent_turns: dict[str, tuple[str, float]] = {}
_parent_turns_lock = threading.Lock()
PARENT_TURN_MAX_S = 6 * 3600.0


def register(ctx: Any) -> None:
    global _profile
    _profile = ctx.profile_name
    # Background work belongs to this load: unloading the plugin (disable,
    # reload) stops it. A Hermes without on_unload keeps it until exit.
    client.start()
    calls.start()
    live_activity.reporter.start()
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(_unload)
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
    # Scheduled checks that call when something happens: the plugin keeps
    # the jobs and each one's incident, so the model only describes them.
    call_watches.register(ctx, is_child=_is_child)
    # Hermes configures Conduit: the settings Conduit shares, changed only
    # through requests Conduit checks and the user approves.
    settings_tool.register(ctx, is_child=_is_child)
    # Hermes asks the user to OK an action on their phone (tap, or voice in
    # a call) before it does it.
    present_tool.register(ctx, is_child=_is_child, profile=lambda: _profile)
    # Hermes puts a table, steps or links on the screen of the user's voice
    # call instead of reading them out.
    show_tool.register(ctx, is_child=_is_child)
    # Health data Conduit shares from Apple Health, read-only, with its skill.
    health_tool.register(ctx, is_child=_is_child,
                         skill_path=Path(__file__).resolve().parent / "skills" / health_tool.SKILL_NAME / "SKILL.md")
    # Where the user is, as Conduit shares it, with place rules and its skill.
    location_tool.register(ctx, is_child=_is_child, profile=lambda: _profile,
                           skill_path=Path(__file__).resolve().parent / "skills" / location_tool.SKILL_NAME / "SKILL.md")
    # Apple Maps lookups the user's iPhone answers (search, directions,
    # travel times), on top of location sharing.
    maps_tool.register(ctx, is_child=_is_child, profile=lambda: _profile)
    # Calendar, Reminders, Focus and phone status Conduit shares, with
    # changes the phone applies (the user approves them there), and the skill.
    phone_tool.register(ctx, is_child=_is_child, profile=lambda: _profile,
                        skill_path=Path(__file__).resolve().parent / "skills" / phone_tool.SKILL_NAME / "SKILL.md")
    try:
        ctx.register_hook("post_approval_response", _post_approval_response)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: post_approval_response hook unavailable", exc_info=True)
    # The voice hint and the note on calls the user declined or missed are
    # extras: a Hermes that refuses the hook must not take the
    # notifications down with it.
    try:
        ctx.register_hook("pre_llm_call", _pre_llm_call)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: pre_llm_call hook unavailable; voice replies get no persona hint and "
                       "Hermes doesn't hear about declined calls", exc_info=True)
    # Lock Screen progress (#605): each model request and its token use.
    # Optional like the hooks above: an older Hermes without them still
    # reports tools, approvals and the end of the turn.
    for name, hook in (("pre_api_request", _pre_api_request), ("post_api_request", _post_api_request)):
        try:
            ctx.register_hook(name, hook)
        except Exception:  # noqa: BLE001
            logger.warning("conduit_push: %s hook unavailable; the Lock Screen shows less detail", name, exc_info=True)
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
    if _is_child(kwargs.get("session_id")):
        return
    _quietly(live_activity.reporter.tool_started, _text(kwargs.get("session_id")), kwargs.get("tool_name"))
    if kwargs.get("tool_name") != "clarify":
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
        title=_titled("Input needed", kwargs.get("session_id")),
        body=clarification_text(kwargs.get("args")),
    ))


def _post_llm_call(**kwargs: Any) -> None:
    session_id = _text(kwargs.get("session_id"))
    response = kwargs.get("assistant_response")
    if _is_child(session_id):
        return
    # The Lock Screen's end waits for on_session_end too: this hook also runs
    # for a failed turn, and the first end the phone gets is the one it keeps.
    started = _take_turn_start(kwargs.get("turn_id"))
    # A cron run says nothing when the user muted its job or it answered
    # [SILENT] by Hermes' cron rule ("[SILENT] No changes" too).
    quiet = is_silent_response(response) or cron_runs.quiet(session_id, kwargs.get("task_id"), response)
    ready = None if quiet else push_event(
        "response.ready",
        identifier=event_id("response", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
        title=_session_title(session_id),
        body=plain_text(response),
    )
    # A quick reply in a chat Hermes Desktop has selected: the user most
    # likely watched it arrive there (#87). A reply to a message sent from a
    # source the user turned off (Desktop, a messaging app…) arrives there (#115).
    send = ready is not None and not _desktop_watched(session_id, started) and _source_notifies(kwargs.get("platform"))
    # Hermes also runs this hook for a turn that failed with text (its error
    # explanation): on_session_end, right after, says which it was, so the
    # reply waits for it and a failed turn sends only its failure notice.
    turn_id = kwargs.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        _finish_ready(session_id, ready, send)
        return
    key = (session_id, turn_id)
    # A Hermes that never reports the turn's end still gets its reply out,
    # and its Live Activity ended.
    timer = _later(READY_GRACE_S, lambda: _turn_end_missed(session_id, turn_id))
    with _pending_lock:
        earlier = _pending_ready.pop(key, None)
        _pending_ready[key] = (ready, send, timer)
        overflow = []
        while len(_pending_ready) > MAX_TURN_STARTS:
            oldest = next(iter(_pending_ready))
            overflow.append((oldest[0], _pending_ready.pop(oldest)))
    if earlier is not None:
        _cancel(earlier[2])  # the same turn reported twice: the last reply wins
    # Far more turns than ever end at once: the oldest go out now.
    for old_session, (old_ready, old_send, old_timer) in overflow:
        _cancel(old_timer)
        _finish_ready(old_session, old_ready, old_send)


def _turn_end_missed(session_id: str, turn_id: str) -> None:
    if _settle_ready(session_id, turn_id, "done"):
        _quietly(live_activity.reporter.turn_ended, session_id, "done")


def _settle_ready(session_id: str, turn_id: Any, outcome: str) -> bool:
    """Sends (``done``) or drops the reply held for this turn; False when
    none was held (already settled, or never held)."""
    with _pending_lock:
        if isinstance(turn_id, str) and turn_id:
            found = [_pending_ready.pop((session_id, turn_id), None)]
        else:
            # An end without its turn's id: a chat's turns run one at a
            # time, so whatever it holds is that turn's.
            found = [_pending_ready.pop(key) for key in [key for key in _pending_ready if key[0] == session_id]]
    found = [pending for pending in found if pending is not None]
    for ready, send, timer in found:
        _cancel(timer)
        if outcome == "done":
            _finish_ready(session_id, ready, send)
    return bool(found)


def _finish_ready(session_id: str, ready: dict[str, Any] | None, send: bool) -> None:
    # A job the user asked to be called about (#449) ends here: the call
    # request takes the place of the ready notification.
    if calls.turn_ended(session_id, "done", profile=_profile, fallback=ready):
        return
    if ready is not None and send:
        enqueue(ready)


def _unload() -> None:
    """Hermes unloads the plugin: replies held for a turn's end are
    dropped with their timers, held calls stay stored for the next load, and
    the delivery and Live Activity threads end (waiting about a second for
    a send in progress). Nothing more goes to the relay from this load."""
    with _pending_lock:
        held = list(_pending_ready.values())
        _pending_ready.clear()
    for _ready, _send, timer in held:
        _cancel(timer)
    calls.stop()
    deadline = time.monotonic() + 1.0
    live_activity.reporter.stop(timeout=max(0.0, deadline - time.monotonic()))
    client.stop(timeout=max(0.0, deadline - time.monotonic()))


def _later(delay: float, work: Any) -> Any:
    # Under the profile that set the timer (see client.enqueue).
    timer = threading.Timer(delay, contextvars.copy_context().run, args=(work,))
    timer.daemon = True
    timer.start()
    return timer


def _cancel(timer: Any) -> None:
    cancel = getattr(timer, "cancel", None)
    if callable(cancel):
        cancel()


def _take_turn_start(turn_id: Any) -> float | None:
    """When this turn's pre_llm_call ran, if noted; the entry goes either way."""
    if not isinstance(turn_id, str) or not turn_id:  # Hermes passes a str
        return None
    with _turn_started_lock:
        return _turn_started.pop(turn_id, None)


def _desktop_watched(session_id: str, started: float | None) -> bool:
    try:
        return desktop_selected.quick(started) and desktop_selected.selected(get_hermes_home(), session_id)
    except Exception:  # noqa: BLE001 — can't tell, so the push goes out
        logger.debug("conduit_push: Desktop selection unreadable", exc_info=True)
        return False


def _source_notifies(platform: Any) -> bool:
    try:
        source = reply_sources.turn_source(platform, reply_sources.current_transport())
        return source is None or reply_sources.ReplySourcesStore(get_hermes_home()).notifies(source)
    except Exception:  # noqa: BLE001 — can't tell, so the push goes out
        logger.debug("conduit_push: reply source unreadable", exc_info=True)
        return True


def _on_session_end(**kwargs: Any) -> None:
    # Fires at the end of every turn, whatever happened (post_llm_call after
    # any turn with text, a failed one too): the one place a watched job's
    # failure or stop shows up, and where a held reply goes out.
    session_id = _text(kwargs.get("session_id"))
    if _is_child(session_id):
        return
    _end_parent_turn(session_id)
    # A turn Hermes doesn't call failed but that didn't complete (it hit the
    # step limit) still answered: its reply goes out, not a failure. A Hermes
    # that doesn't say counts it failed, as before.
    answered = (not kwargs.get("interrupted") and kwargs.get("failed") is not True
                and (kwargs.get("completed") or kwargs.get("failed") is False))
    _quietly(live_activity.reporter.turn_ended, session_id,
             "done" if answered else "stopped" if kwargs.get("interrupted") else "failed")
    if answered:
        if not _settle_ready(session_id, kwargs.get("turn_id"), "done"):
            calls.turn_ended(session_id, "done", profile=_profile, fallback=None)
        return
    _settle_ready(session_id, kwargs.get("turn_id"), "dropped")
    if kwargs.get("interrupted"):
        calls.turn_ended(session_id, "stopped", profile=_profile, fallback=None)
        return
    if cron_runs.quiet(session_id, kwargs.get("task_id")):
        # A muted cron job's failed run: no push and no automatic alert call.
        # A call the user asked for on this job (a watch) still rings.
        calls.turn_ended(session_id, "failed", profile=_profile, fallback=None)
        return
    failed = push_event(
        "turn.failed",
        identifier=event_id("failure", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
        title=_titled("Turn failed", session_id),
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
    _quietly(live_activity.reporter.waiting, session_id, "approval")
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
        title=_titled("Approval needed", session_id),
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
    _quietly(live_activity.reporter.thinking, _text(kwargs.get("session_key")))


def _pre_api_request(**kwargs: Any) -> None:
    session_id = _text(kwargs.get("session_id"))
    if not _is_child(session_id):
        _quietly(live_activity.reporter.thinking, session_id)


def _post_api_request(**kwargs: Any) -> None:
    session_id = _text(kwargs.get("session_id"))
    if _is_child(session_id):
        return
    usage = kwargs.get("usage")
    if isinstance(usage, dict):
        _quietly(live_activity.reporter.context_used, session_id, usage.get("prompt_tokens"),
                 kwargs.get("context_length"))


def _quietly(function: Any, *args: Any) -> None:
    """Lock Screen progress is an extra: it must never break a hook."""
    try:
        function(*args)
    except Exception:  # noqa: BLE001
        logger.debug("conduit_push: Live Activity update failed", exc_info=True)


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
    # A subagent that ends inside the turn that started it (delegate_task
    # waits for it) is part of that turn's reply: only one that outlives it,
    # a background task, notifies. Nor does a nested one, or a muted cron job's.
    if (_inside_parent_turn(parent, kwargs.get("parent_turn_id")) or _is_child(parent)
            or cron_runs.quiet(parent, None)):
        return
    status = _text(kwargs.get("child_status")) or "finished"
    enqueue(push_event(
        "background_task.finished",
        identifier=event_id("subagent", kwargs.get("parent_turn_id"), child, status),
        session_id=parent,
        profile=_profile,
        title=_titled("Task finished", parent),
        body=plain_text(kwargs.get("child_summary")) or f"A delegated task {status}.",
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
# Added while a Conduit that shows cards is connected: what isn't spoken
# can still be seen.
SHOW_VOICE_NOTE = (
    "[The user's phone shows a screen during this call: put anything better seen than heard (a table, steps, "
    "a chart, code, images, links, and places as map cards) on it with conduit_show, then say in a sentence "
    "that it's there.]"
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
    """Context for this turn: calls from Hermes the user declined or missed
    since the last turn of this chat (#449), where in Conduit the message
    was sent from, the voice persona hint, and which of the user's data
    Conduit shares here, to use before other routes."""
    session_id = _text(kwargs.get("session_id"))
    child = _is_child(session_id)
    if not child:
        _note_turn_start(kwargs.get("turn_id"))
        _start_parent_turn(session_id, kwargs.get("turn_id"))
        _quietly(live_activity.reporter.turn_started, session_id)
    notes = [
        None if child else calls.outcome_note(session_id),
        None if child else _turn_surface_note(kwargs.get("platform"), kwargs.get("user_message")),
        _voice_persona_note(kwargs.get("conversation_history")),
        # Subagents can't use the shared data (each tool refuses them).
        None if child else conduit_first.note(),
    ]
    context = "\n\n".join(note for note in notes if note)
    return {"context": context} if context else None


def _turn_surface_note(platform: Any, message: Any) -> str | None:
    """Which Conduit surface sent this turn's message (turn_surface.py)."""
    transport = reply_sources.current_transport()
    try:
        from_conduit = reply_sources.turn_source(platform, transport) == "conduit"
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        from_conduit = False
    return turn_surface.note(from_conduit, message, transport)


def _note_turn_start(turn_id: Any) -> None:
    """When each turn started, for the quick-reply check in post_llm_call. A
    turn that never reaches post_llm_call leaves its entry, so the oldest go
    once there are many."""
    if not isinstance(turn_id, str) or not turn_id:
        return
    with _turn_started_lock:
        # A repeated id moves to the end, so eviction takes the oldest turn.
        _turn_started.pop(turn_id, None)
        _turn_started[turn_id] = time.monotonic()
        while len(_turn_started) > MAX_TURN_STARTS:
            _turn_started.pop(next(iter(_turn_started)))


def _start_parent_turn(session_id: str, turn_id: Any) -> None:
    if not session_id:
        return
    with _parent_turns_lock:
        _parent_turns.pop(session_id, None)
        _parent_turns[session_id] = (turn_id if isinstance(turn_id, str) else "", time.monotonic())
        while len(_parent_turns) > MAX_TURN_STARTS:
            _parent_turns.pop(next(iter(_parent_turns)))


def _end_parent_turn(session_id: str) -> None:
    with _parent_turns_lock:
        _parent_turns.pop(session_id, None)


def _inside_parent_turn(session_id: str, turn_id: Any) -> bool:
    """Whether the turn ``turn_id`` of ``session_id`` runs now. Unnamed on
    either side counts as not (the task notifies); one whose end was never
    reported stops counting after a while."""
    if not isinstance(turn_id, str) or not turn_id:
        return False
    with _parent_turns_lock:
        running = _parent_turns.get(session_id)
        if running is not None and time.monotonic() - running[1] >= PARENT_TURN_MAX_S:
            _parent_turns.pop(session_id, None)
            running = None
    return running is not None and turn_id == running[0]


def _voice_persona_note(history: Any) -> str | None:
    """Keep a voice-live delegation's reply speakable without flattening the persona.

    The TUI gateway prepends Hermes' voice-live note to the model input of a
    turn delegated from a live spoken conversation; the ``user_message`` kwarg
    is the clean text, so the note is read from the turn's own user row in
    ``conversation_history``.
    """
    try:
        if not isinstance(history, list):
            return None
        for message in reversed(history):
            if isinstance(message, dict) and message.get("role") == "user":
                prefix = _voice_note_prefix()
                if any(text.lstrip().startswith(prefix) for text in _text_parts(message.get("content"))):
                    return PERSONA_VOICE_NOTE + ("\n" + SHOW_VOICE_NOTE if show_tool.available() else "")
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
