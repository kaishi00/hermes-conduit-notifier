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

from . import (call_tool, call_watches, calls, chat_on_screen, clarify_loop, client, conduit_first, cron_runs,
               desktop_selected, health_tool, inbox_tool, live_activity, location_tool, maps_tool, phone_tool,
               present_tool, pulse, reply_sources, review_notices, room_memory, settings_tool, show_tool, turn_surface,
               voice_archive)
from .session_titles import bot_name as _bot_name, title as _session_title, titled as _titled
from .client import enqueue
from .events import approval_decision, clarification_text, event_id, is_silent_response, plain_text, push_event


logger = logging.getLogger(__name__)
_child_sessions: set[str] = set()
# Id a reported child moved to on compaction -> the id it was reported by.
_compacted_children: dict[str, str] = {}
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
# Session id -> (a token, the grace timer) for a turn Hermes named no id for:
# its reply can't wait for the end, but its Live Activity's end still does.
_unnamed_ends: dict[str, tuple[object, Any]] = {}
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
    # Pulse (designs/proactivity.md): off unless turned on, and then only
    # the gateway process sends anything.
    pulse.register(ctx)
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(_unload)
    # A group room's turns aren't saved into the memory provider as the
    # owner's own words (room_memory).
    room_memory.install()
    # An open chat's self-improvement updates follow the setting as it is
    # now, not as it was when the chat opened (review_notices).
    review_notices.install()
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_approval_request", _pre_approval_request)
    # Pulse's outcome ledger: every tool call's outcome while Pulse is on.
    # Optional: a Hermes without the hook records nothing.
    try:
        ctx.register_hook("post_tool_call", _post_tool_call)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: post_tool_call hook unavailable; Pulse records no tool outcomes", exc_info=True)
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
    # What the user's Shortcuts automations logged through Conduit's Log to
    # Hermes action (notifications, messages, emails, payments...), read-only.
    inbox_tool.register(ctx, is_child=_is_child)
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
        pulse.turns_marked = True
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: pre_llm_call hook unavailable; voice replies get no persona hint, "
                       "Hermes doesn't hear about declined calls and Pulse doesn't run", exc_info=True)
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


def _pre_tool_call(**kwargs: Any) -> dict[str, str] | None:
    if _is_child(kwargs.get("session_id")):
        return None
    # A Pulse turn gets no tools. Fails closed: Hermes blocks the call when
    # this hook raises, so the check is outside _quietly.
    block = pulse.pre_tool_call(_text(kwargs.get("session_id")), kwargs.get("turn_id"))
    if block is not None:
        return block
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
        profile_name=_bot_name(),
        title=_titled("Input needed", kwargs.get("session_id")),
        title_label="input.needed",
        session_title=_session_title(kwargs.get("session_id")),
        body=clarification_text(kwargs.get("args")),
    ))


_ledger_warned = False


def _post_tool_call(**kwargs: Any) -> None:
    # Every session counts, subagents and cron runs included (3.6).
    try:
        pulse.post_tool_call(**kwargs)
    except Exception:  # noqa: BLE001 — the ledger must never break a tool call
        global _ledger_warned
        # Once per process at warning level, so a broken ledger shows up in the log.
        logger.log(logging.DEBUG if _ledger_warned else logging.WARNING, "conduit_push: Pulse outcome not recorded",
                   exc_info=True)
        _ledger_warned = True


def _post_llm_call(**kwargs: Any) -> None:
    session_id = _text(kwargs.get("session_id"))
    response = kwargs.get("assistant_response")
    if _is_child(session_id):
        return
    # The Lock Screen's end waits for on_session_end too: this hook also runs
    # for a failed turn, and the first end the phone gets is the one it keeps.
    started = _take_turn_start(kwargs.get("turn_id"))
    # A cron run says nothing when the user muted its job or it answered
    # [SILENT] by Hermes' cron rule ("[SILENT] No changes" too). A Pulse
    # probe, or a report that found nothing, is Pulse working on its own:
    # nothing to tell the phone. A follow-up, a brief or a report's items are
    # news for the user, so they go like any reply.
    pulse_turn = pulse.is_pulse_turn(session_id, kwargs.get("turn_id"))
    quiet = (is_silent_response(response) or cron_runs.quiet(session_id, kwargs.get("task_id"), response)
             or (pulse_turn and not pulse.pushes(session_id, kwargs.get("turn_id"), response)))
    chat_title = "" if quiet or pulse_turn else _session_title(session_id)
    # A Pulse push is titled by what it brings, not by Pulse's hidden chat.
    copy = (pulse.push_copy(session_id, kwargs.get("turn_id"), response) if pulse_turn and not quiet
            else {"title": chat_title, "title_label": "response.ready", "body": response})
    ready = None if quiet else push_event(
        "response.ready",
        identifier=event_id("response", kwargs.get("turn_id"), session_id),
        session_id=session_id,
        profile=_profile,
        profile_name=_bot_name(),
        title=copy["title"],
        title_label=copy["title_label"],
        session_title=chat_title,
        body=plain_text(copy["body"]),
        count=copy.get("count"),
    )
    # A quick reply in a chat Hermes Desktop has selected: the user most
    # likely watched it arrive there (#87); so did one in a chat Conduit has
    # on screen, on any device (chat_on_screen.py). A reply to a message sent
    # from a source the user turned off (Desktop, a messaging app…) arrives
    # there (#115).
    send = (ready is not None and not _desktop_watched(session_id, started) and not _conduit_watched(session_id, started)
            and _source_notifies(kwargs.get("platform")))
    # Hermes also runs this hook for a turn that failed with text (its error
    # explanation): on_session_end, right after, says which it was, so the
    # reply waits for it and a failed turn sends only its failure notice.
    turn_id = kwargs.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        _finish_ready(session_id, ready, send)
        if not pulse_turn:
            _hold_unnamed_end(session_id)
        return
    key = (session_id, turn_id)
    # A Hermes that never reports the turn's end still gets its reply out,
    # and its Live Activity ended (a Pulse turn never started one).
    timer = _later(READY_GRACE_S, lambda: _turn_end_missed(session_id, turn_id, live=not pulse_turn))
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


def _turn_end_missed(session_id: str, turn_id: str, live: bool = True) -> None:
    if _settle_ready(session_id, turn_id, "done") and live:
        _quietly(live_activity.reporter.turn_ended, session_id, "done")


def _hold_unnamed_end(session_id: str) -> None:
    """A Hermes that names no turn and never reports its end still gets
    the Live Activity ended, after the grace; on_session_end or the chat's
    next turn takes the timer's place."""
    token = object()

    def missed() -> None:
        with _pending_lock:
            if _unnamed_ends.get(session_id, (None, None))[0] is not token:
                return
            del _unnamed_ends[session_id]
        _quietly(live_activity.reporter.turn_ended, session_id, "done")

    # Registered before its timer starts, so a drop in between isn't lost.
    with _pending_lock:
        earlier = _unnamed_ends.pop(session_id, None)
        _unnamed_ends[session_id] = (token, None)
        overflow = []
        while len(_unnamed_ends) > MAX_TURN_STARTS:
            oldest = next(iter(_unnamed_ends))
            overflow.append((oldest, _unnamed_ends.pop(oldest)))
    if earlier is not None:
        _cancel(earlier[1])
    timer = _later(READY_GRACE_S, missed)
    with _pending_lock:
        if _unnamed_ends.get(session_id, (None, None))[0] is token:
            _unnamed_ends[session_id] = (token, timer)
            timer = None
    _cancel(timer)  # dropped meanwhile
    # Far more chats than ever end at once: the oldest end now.
    for old_session, (_, old_timer) in overflow:
        _cancel(old_timer)
        _quietly(live_activity.reporter.turn_ended, old_session, "done")


def _drop_unnamed_end(session_id: str) -> None:
    with _pending_lock:
        pending = _unnamed_ends.pop(session_id, None)
    if pending is not None:
        _cancel(pending[1])


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
    dropped with their timers, held calls stay stored for the next load, the
    group room memory guard and the self-improvement setting's live read
    come off, and the delivery and Live Activity
    threads end (waiting about a second for a send in progress). Nothing
    more goes to the relay from this load."""
    with _pending_lock:
        held = list(_pending_ready.values())
        _pending_ready.clear()
        unnamed = list(_unnamed_ends.values())
        _unnamed_ends.clear()
    for _ready, _send, timer in held:
        _cancel(timer)
    for _token, timer in unnamed:
        _cancel(timer)
    calls.stop()
    pulse.stop()
    room_memory.uninstall()
    review_notices.uninstall()
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


def _conduit_watched(session_id: str, started: float | None) -> bool:
    try:
        return chat_on_screen.attended(started) and chat_on_screen.on_screen(get_hermes_home(), session_id)
    except Exception:  # noqa: BLE001 — can't tell, so the push goes out
        logger.debug("conduit_push: Conduit's on-screen chats unreadable", exc_info=True)
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
    _drop_unnamed_end(session_id)
    pulse_turn = pulse.is_pulse_turn(session_id, kwargs.get("turn_id"))
    _quietly(pulse.turn_ended, session_id, kwargs.get("turn_id"))
    # A turn Hermes doesn't call failed but that didn't complete (it hit the
    # step limit) still answered: its reply goes out, not a failure. A Hermes
    # that doesn't say counts it failed, as before.
    answered = (not kwargs.get("interrupted") and kwargs.get("failed") is not True
                and (kwargs.get("completed") or kwargs.get("failed") is False))
    if not pulse_turn:
        # A Pulse turn never started a Live Activity, so there is none to end.
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
    if pulse_turn:
        # Pulse records its own failures (``pulse status``).
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
        profile_name=_bot_name(),
        title=_titled("Turn failed", session_id),
        title_label="turn.failed",
        session_title=_session_title(session_id),
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
    _quietly(live_activity.reporter.waiting,
             _text(kwargs.get("session_id")) or session_id, "approval")
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
        profile_name=_bot_name(),
        title=_titled("Approval needed", session_id),
        title_label="approval.needed",
        session_title=_session_title(session_id),
        # No description: the phone writes the generic line in its language.
        body=description,
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
    calls.alert(session_id, "approval", description, profile=_profile,
                turn_session_id=_text(kwargs.get("session_id")))
    # The command and description, for a call answered with no chat open (the
    # Watch) to read out; its spoken answer reaches Hermes from here.
    command = kwargs.get("command") if isinstance(kwargs.get("command"), str) else ""
    if session_id and description and calls.note_waiting(
            session_id, "approval", turn_session_id=_text(kwargs.get("session_id")),
            description=description, command=command):
        calls.follow_approval(session_id, command)


def _post_approval_response(**kwargs: Any) -> None:
    session_key = _text(kwargs.get("session_key"))
    calls.cancel_alerts(session_key or _text(kwargs.get("session_id")), "approval")
    _quietly(live_activity.reporter.thinking, _text(kwargs.get("session_id")) or session_key)


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


def _pulse_note(session_id: str, turn_id: Any, user_message: Any) -> str | None:
    try:
        return pulse.pre_llm_call(session_id, turn_id, user_message)
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        logger.warning("conduit_push: Pulse couldn't check this turn", exc_info=True)
        return None


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
        # Hermes reports the id the child ends under, which a compaction may
        # have moved from the one subagent_start reported.
        with _children_lock:
            reported = _compacted_children.get(child, child)
            unknown = reported not in _child_sessions and bool(_child_sessions)
            known = set(_child_sessions)
        if unknown:
            found = _continues_a_child(child, known)
            if isinstance(found, str):
                reported = found
        with _children_lock:
            _child_sessions.discard(reported)
            for learned in [key for key, root in _compacted_children.items() if root == reported]:
                del _compacted_children[learned]
    parent = _text(kwargs.get("parent_session_id"))
    # A subagent that ends inside the turn that started it (delegate_task
    # waits for it) is part of that turn's reply: only one that outlives it,
    # a background task, notifies. Nor does a nested one, or a muted cron job's.
    # The parent is asked about by id: this hook can run in the child's own
    # run, where Hermes' delegation flag describes the child.
    if (_inside_parent_turn(parent, kwargs.get("parent_turn_id")) or _is_child_session(parent)
            or cron_runs.quiet(parent, None)):
        return
    status = _text(kwargs.get("child_status")) or "finished"
    # Hermes reports "completed" (else "failed", "timeout", "error"). Only a
    # finish gets the phone's own title and generic line, in its language; any
    # other ending keeps the host's words, which say how it ended.
    finished = status in ("finished", "completed")
    summary = plain_text(kwargs.get("child_summary"))
    enqueue(push_event(
        "background_task.finished",
        identifier=event_id("subagent", kwargs.get("parent_turn_id"), child, status),
        session_id=parent,
        profile=_profile,
        profile_name=_bot_name(),
        title=_titled("Task finished", parent),
        title_label="background_task.finished" if finished else "",
        session_title=_session_title(parent),
        body=summary or ("" if finished else f"A delegated task {status}."),
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
    pulse_note = None if child else _pulse_note(session_id, kwargs.get("turn_id"), kwargs.get("user_message"))
    if pulse_note:
        # Pulse's own turn: no Lock Screen progress, and only its note.
        _note_turn_start(kwargs.get("turn_id"))
        return {"context": pulse_note}
    if not child:
        _note_turn_start(kwargs.get("turn_id"))
        _start_parent_turn(session_id, kwargs.get("turn_id"))
        _drop_unnamed_end(session_id)
        _quietly(live_activity.reporter.turn_started, session_id)
        # A voice chat archived when its call ended is in use again (#1124).
        try:
            voice_archive.bring_back(get_hermes_home(), session_id)
        except Exception:  # noqa: BLE001 — a hook must never break the turn
            logger.debug("conduit_push: voice chat un-archive check failed", exc_info=True)
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


# Ids checked against compaction lineage and found not to be a child. A
# session's lineage never changes, so the answer can be kept.
_not_child_lineage: set[str] = set()
_LINEAGE_HOPS = 32


# Hermes' "running inside a delegated child" check (agent.delegation_context,
# Hermes 0.22+), looked up once: False when this Hermes has none.
_delegated_child_check: Any = None
_delegated_check_failed = False


def _inside_delegated_child() -> bool:
    """Whether the code running now is a delegated child's run. Hermes binds
    the flag in the child's own thread, on a copy of the parent's context, so
    the parent never sees it set."""
    global _delegated_child_check, _delegated_check_failed
    if _delegated_child_check is None:
        try:
            from agent.delegation_context import is_delegated_child_context

            _delegated_child_check = is_delegated_child_context
        except ImportError:
            logger.info("conduit_push: this Hermes has no delegation flag; subagents are found by session id")
            _delegated_child_check = False  # an older Hermes: ids and lineage answer
        except Exception:  # noqa: BLE001 — a broken module must not break every hook
            logger.warning("conduit_push: Hermes' delegation context failed to load", exc_info=True)
            _delegated_child_check = False
    if _delegated_child_check is False:
        return False
    try:
        return bool(_delegated_child_check())
    except Exception:  # noqa: BLE001
        if not _delegated_check_failed:
            _delegated_check_failed = True
            logger.warning("conduit_push: Hermes' delegation check failed; using session ids", exc_info=True)
        return False


def _is_child(session_id: Any) -> bool:
    """Whether the session running now is a delegated child: Hermes'
    delegation flag, else :func:`_is_child_session`."""
    return _inside_delegated_child() or _is_child_session(session_id)


def _is_child_session(session_id: Any) -> bool:
    """Whether this id is a delegated child, whichever session is running: an
    id ``subagent_start`` reported, or one that continues it. With
    ``compression.in_place: false`` a compaction gives the agent a new session
    id without firing ``subagent_start`` again; Hermes records the old id as
    the new session's parent and ends the old one with ``compression``, so
    following those links back finds the child it was."""
    value = _text(session_id)
    if not value:
        return False
    with _children_lock:
        if value in _child_sessions or value in _compacted_children:
            return True
        if not _child_sessions or value in _not_child_lineage:
            return False  # no child running: nothing to read the store for
        known = _child_sessions | set(_compacted_children)
    continues = _continues_a_child(value, known)
    if continues:
        with _children_lock:
            root = _compacted_children.get(continues, continues)
            if root in _child_sessions:
                _compacted_children[value] = root
        return True
    if continues is None:
        return False  # unread, so not kept: the next call reads again
    with _children_lock:
        if len(_not_child_lineage) > 4096:
            _not_child_lineage.clear()
        _not_child_lineage.add(value)
    return False


def _continues_a_child(session_id: str, children: set[str]) -> str | bool | None:
    """The child ``session_id`` descends from, through compactions (or
    delegations under it), read from Hermes' session store; False when none,
    None when that can't be settled yet."""
    import sqlite3

    try:
        path = Path(get_hermes_home()) / "state.db"
        if not path.exists():
            return None
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        _lineage_unread("couldn't open the session store to check a subagent's lineage")
        return None
    try:
        current, settled = session_id, True
        for _ in range(_LINEAGE_HOPS):
            row = db.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            if row is None and current == session_id:
                return None  # not stored yet, or another profile's store: ask again later
            parent = row[0] if row else None
            if not parent:
                # A root: no child above. Kept only if every link passed was
                # settled; an open one may be a compaction still being written.
                return False if settled else None
            ended = db.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
            reason = ended[0] if ended else None
            if reason == "branched":
                return False  # a branch is a new conversation, not the child going on
            if parent in children:
                return parent  # a child's compaction, or a subagent under it
            settled = settled and bool(reason)
            current = parent
        return None  # longer than we follow: not an answer to keep
    except sqlite3.Error:
        _lineage_unread("couldn't read a subagent's lineage from the session store")
        return None
    finally:
        db.close()


_lineage_unread_count = 0


def _lineage_unread(message: str) -> None:
    """Warn on the first failure and every 100th after: while a child runs
    every hook may ask again, and an operator should see the gate degraded."""
    global _lineage_unread_count
    _lineage_unread_count += 1
    if _lineage_unread_count % 100 == 1:
        logger.warning("conduit_push: %s (%d times so far); a compacted subagent may not be recognised",
                       message, _lineage_unread_count, exc_info=True)
    else:
        logger.debug("conduit_push: %s", message, exc_info=True)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
