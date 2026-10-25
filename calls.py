"""Hermes calls you (#449): turning a watched job's end into a call request.

The turn-end hooks hand every top-level turn end here. A turn of a session
Conduit watches consumes the watch (calls_store) and, within the user's
limits, sends a ``call.requested`` event in place of the push the hook would
have sent. Delivery retries a transport or relay failure with the same event
id, which the relay dedupes; a rejection (an older relay, the relay's daily
ceiling), a relay that took the event but couldn't reach Apple, or a final
failure sends that ordinary push instead, so the user still hears about the
job.

A job that ends while its watch is held (the user's call is still going)
gets its ordinary push; the call waits. A waiter thread calls once the hold
runs out unreleased (the phone went away mid-call), and every turn end
sweeps for such calls too, so a gateway restart doesn't lose one.

Hermes asks for a call itself through the ``conduit_call_user`` tool (a watch
on the asking session, see ``tool_watch``). With alert calls on, an approval
or question left unanswered calls through the same waiter (``alert``), and a
failed turn calls in place of its push (``failed_turn``).

A call the user declined or didn't answer comes back from Conduit (the
dashboard's outcome route), and the next turn of its chat tells Hermes so
(``outcome_note``): nothing rings again.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import client, location_profiles, session_titles
from .calls_store import CallStore
from .location_store import LocationStore
from .events import push_event

logger = logging.getLogger("hermes.plugins.conduit_push")

CALL_TITLE = "Hermes wants to talk"
# Seconds to wait before each delivery attempt.
DELIVERY_DELAYS_S = (0.0, 2.0, 4.0)
# The relay's errors for an event it took but Apple didn't (relay/src/server.mjs).
APNS_FAILURES = frozenset({"apns_rejected", "apns_unreachable"})


def _spawn(work: Callable[[], None]) -> None:
    if _stopped.is_set():
        return
    # Under the profile that started the work (see client.enqueue).
    threading.Thread(target=contextvars.copy_context().run, args=(work,), name="conduit-call", daemon=True).start()


# Homes with a waiter thread running, so a burst of held turn ends starts one,
# and what wakes each early.
_waiting: set[str] = set()
_wakes: dict[str, threading.Event] = {}
_waiting_lock = threading.Lock()
# Set while the plugin is unloaded (stop()): no call request goes out and the
# waiters end. A waiter from before a stop() ends even if start() ran
# meanwhile: it belongs to an older generation.
_stopped = threading.Event()
_generation = 0


def start() -> None:
    _stopped.clear()


def stop() -> None:
    """The plugin is being unloaded: held calls stay in the store (the next
    load's resume() waits on them again) but nothing here sends them."""
    global _generation
    with _waiting_lock:
        _stopped.set()
        _generation += 1
        wakes = list(_wakes.values())
        _waiting.clear()
        _wakes.clear()
    for wake in wakes:
        wake.set()


def _sleep(seconds: float, wake: threading.Event | None = None) -> None:
    """Sleeps ``seconds``, or until ``wake`` is set."""
    if wake is None:
        time.sleep(seconds)
    else:
        wake.wait(seconds)


def _clock() -> float:
    return time.time()


def _store(home: Any) -> CallStore:
    return CallStore(home, clock=lambda: _clock(), place_blocked=lambda settings: _place_blocked(home, settings))


def _place_blocked(home: Any, settings: dict[str, Any]) -> bool:
    """Whether the user is at a saved place they don't want calls at (or
    Conduit can't say where they are and they asked to count that too)."""
    # A profile reading another's shared location is held back by it too.
    store, _ = location_profiles.store_for(home, lambda path: LocationStore(path, clock=lambda: _clock()))
    place = store.latest_place()
    known = place is not None and place["trusted"] and not place["paused"]
    if not known:
        return bool(settings["avoid_unknown_place"])
    return place["place_id"] in settings["avoid_places"]


def turn_ended(session_id: str, outcome: str, *, profile: str, fallback: dict[str, Any] | None) -> bool:
    """A top-level turn of ``session_id`` ended with ``outcome``.

    True when a call request now owns the user's notice: the caller must not
    send ``fallback`` itself (delivery sends it if the call can't go out).
    False when no watch covers the session or the call was held back by the
    user's settings or limits; the caller sends its usual push.
    """
    if not session_id:
        return False
    # A hook must never break the turn: every step below is guarded.
    try:
        home = get_hermes_home()
        store = _store(home)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not open call watches for this turn", exc_info=True)
        return False
    try:
        # Watches consumed here are sent even if this turn's own check fails.
        _send_due(store.fire_due(), profile)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not sweep held calls", exc_info=True)
    try:
        result = store.fire(session_id, outcome)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not check call watches for this turn", exc_info=True)
        return False
    if result is None:
        return False
    if result["status"] == "held":
        # The call waits for the user's call to end; this turn's own push
        # goes out as usual.
        try:
            _wait_for_holds(home, profile)
        except Exception:  # noqa: BLE001 — the next turn end sweeps again
            logger.warning("Conduit could not wait on a held call", exc_info=True)
        return False
    if result["status"] != "call":
        logger.info("Conduit call for a finished job held back: %s", result.get("reason") or result["status"])
        return False
    try:
        event = call_event(result["watch"], outcome, session_id=session_id, profile=profile)
        _spawn(lambda: deliver(event, fallback))
    except Exception:  # noqa: BLE001 — the caller sends its usual push
        logger.warning("Conduit could not send a call request", exc_info=True)
        return False
    return True


def _send_due(results: list[dict[str, Any]], profile: str) -> None:
    """Calls for jobs that ended during a hold that ran out. Their ordinary
    push went out when they ended, so there is nothing to fall back to."""
    for result in results:
        if result["status"] != "call":
            logger.info("Conduit call for a finished job held back: %s", result.get("reason") or result["status"])
            continue
        # One that can't go out never stops the rest: they're consumed too.
        try:
            event = call_event(result["watch"], result["outcome"], session_id=result["session_id"], profile=profile)
            _spawn(lambda event=event: deliver(event, None))
        except Exception:  # noqa: BLE001
            logger.warning("Conduit could not send a held call", exc_info=True)


def resume(profile: str) -> None:
    """At start-up: a hold over an ended job that was waiting when the
    agent stopped gets its waiter back, so it doesn't wait for a turn."""
    try:
        home = get_hermes_home()
        if _store(home).next_due() is not None:
            _wait_for_holds(home, profile)
    except Exception:  # noqa: BLE001 — the next turn end sweeps again
        logger.warning("Conduit could not resume held calls", exc_info=True)


def _wait_for_holds(home: Any, profile: str) -> None:
    """Sleeps until each hold over an ended job runs out, then calls for it
    unless Conduit released or removed the watch meanwhile."""
    key = str(home)
    with _waiting_lock:
        if _stopped.is_set():
            return
        if key in _waiting:
            # It may now be due sooner than the waiter wakes (an alert's
            # minute beside a long hold): the waiter looks again.
            if key in _wakes:
                _wakes[key].set()
            return
        _waiting.add(key)
    try:
        _spawn(_waiter(home, profile, key, _generation))
    except BaseException:
        with _waiting_lock:
            _waiting.discard(key)
        raise


def _waiter(home: Any, profile: str, key: str, generation: int = 0) -> Callable[[], None]:
    def wait() -> None:
        try:
            # Inside the try: a store that can't open must still let go of
            # the key, or no later hold for this home starts a waiter.
            store = _store(home)
            while True:
                # Under the lock: a hold that starts now either shows up here
                # or wakes this waiter, or starts its own.
                with _waiting_lock:
                    if generation != _generation:  # stop() already let go of its key
                        return
                    due_at = store.next_due()
                    if due_at is None:
                        _waiting.discard(key)
                        _wakes.pop(key, None)
                        return
                    wake = _wakes.setdefault(key, threading.Event())
                    wake.clear()
                _sleep(max(0.0, due_at - store.clock()) + 1.0, wake)
                if generation != _generation:
                    return
                _send_due(store.fire_due(), profile)
        except Exception:  # noqa: BLE001 — the next turn end sweeps again
            logger.warning("Conduit stopped waiting on a held call", exc_info=True)
            with _waiting_lock:
                if generation == _generation:
                    _waiting.discard(key)
                    _wakes.pop(key, None)

    return wait


def settings() -> dict[str, Any]:
    return _store(get_hermes_home()).settings()


def tool_watch(session_id: str, reason: str, *, asked: bool) -> dict[str, Any]:
    """The conduit_call_user tool: call when this turn of ``session_id``
    ends. The store's answer; raises ValueError like ``add_tool_watch``."""
    return _store(get_hermes_home()).add_tool_watch(session_id, reason, asked)


def alert(session_id: str, kind: str, reason: str, *, profile: str, turn_session_id: str = "") -> None:
    """Hermes waits on the user's approval or answer: call if it stays
    unanswered (alert calls on). ``turn_session_id``: the id the turn hooks
    know the session by when ``session_id`` is the gateway's key, so the
    turn's end drops the alert and its outcome reaches the next turn. Never
    raises: a hook calls this."""
    ids = list(dict.fromkeys(value for value in (session_id, turn_session_id) if value))
    if not ids:
        return
    try:
        home = get_hermes_home()
        if _store(home).add_alert(ids, kind, reason) is not None:
            _wait_for_holds(home, profile)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not set up an alert call", exc_info=True)


def cancel_alerts(session_id: str, kind: str | None = None) -> None:
    """The user answered: no call for it, and nothing left for a call to
    answer. Never raises."""
    if not session_id:
        return
    try:
        store = _store(get_hermes_home())
        store.cancel_alerts(session_id, kind)
        store.forget_waiting(session_id, kind)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not cancel an alert call", exc_info=True)


def note_waiting(session_id: str, kind: str, *, turn_session_id: str = "", **detail: Any) -> bool:
    """Keeps what Hermes waits on (``CallStore.note_waiting``) for a call
    to read and answer. Never raises: a hook calls this."""
    ids = list(dict.fromkeys(value for value in (session_id, turn_session_id) if value))
    if not ids:
        return False
    try:
        return _store(get_hermes_home()).note_waiting(ids, kind, **detail)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not keep what Hermes waits on for a call", exc_info=True)
        return False


def taken_answers(request_id: str) -> dict[str, str] | None:
    """A question batch answered in full from a call, taken. Never raises."""
    try:
        return _store(get_hermes_home()).take_answers(request_id)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not read a call's answers", exc_info=True)
        return None


def has_call_answers(request_id: str) -> bool:
    """Whether a call has answered every question of ``request_id``,
    without taking them. Never raises."""
    try:
        return _store(get_hermes_home()).answers_ready(request_id)
    except Exception:  # noqa: BLE001
        return False


# How often an approval waiting on the user looks for an answer from a call,
# and for how long at most (Hermes' own approval timeout ends it sooner).
APPROVAL_ANSWER_POLL_S = 1.0
APPROVAL_ANSWER_MAX_S = 2 * 3600.0
# Polls a call's answer may find no matching pending approval before it's dropped.
APPROVAL_MATCH_TRIES = 5


# The chats whose approval a follower watches for a call's answer.
_followed: set[tuple[Any, str]] = set()
_followed_lock = threading.Lock()


def follow_approval(session_key: str, command: str) -> None:
    """An approval waits on the user in ``session_key`` and a call may
    answer it (``note_waiting`` kept it): until it's answered or gone,
    passes a call's answer to Hermes as its own /approve or /deny would.
    Only the request the call read out: the one whose command it is."""
    if not session_key:
        return
    try:
        home = get_hermes_home()
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not follow an approval for a call", exc_info=True)
        return

    if _stopped.is_set():
        return
    with _followed_lock:
        # A replayed hook finds its follower running: one per chat; it reads
        # the newest wait's command from the store.
        if (home, session_key) in _followed:
            return
        _followed.add((home, session_key))

    def follow() -> None:
        try:
            while True:
                follow_answer()
                with _followed_lock:
                    # A new approval in the chat while this one was answered:
                    # its hook found this follower running, so keep watching.
                    try:
                        again = not _stopped.is_set() and _store(home).has_waiting(session_key, "approval")
                    except Exception:  # noqa: BLE001
                        again = False
                    if not again:
                        _followed.discard((home, session_key))
                        return
        except BaseException:
            with _followed_lock:
                _followed.discard((home, session_key))
            raise

    def follow_answer() -> None:
        store = _store(home)
        deadline = time.monotonic() + APPROVAL_ANSWER_MAX_S
        # Its own wait, not _sleep: the plugin's unload ends it at once.
        misses = 0
        claimed = ""
        while time.monotonic() < deadline and not _stopped.wait(APPROVAL_ANSWER_POLL_S):
            try:
                if not store.has_waiting(session_key, "approval"):
                    if claimed:
                        # Answered on its card meanwhile: the call says the
                        # user's answer didn't go.
                        store.settle_approval(claimed, False)
                    return
                # Read and claimed, not taken: it stays until a pending
                # request takes it.
                answer = store.take_approval(session_key, keep=True)
            except Exception:  # noqa: BLE001
                logger.warning("Conduit could not read a call's approval answer", exc_info=True)
                return
            if answer is None:
                continue
            claimed = answer["answer_id"]
            try:
                resolved = _resolve_approval(session_key, command, answer)
            except Exception:  # noqa: BLE001
                logger.warning("Conduit could not pass a call's approval answer to Hermes", exc_info=True)
                resolved = 0
            if not resolved:
                misses += 1
                if misses < APPROVAL_MATCH_TRIES:
                    # Hermes may list the request a moment late: try again.
                    continue
                # Answered on its card meanwhile, or not the request the call
                # read out: the call says it didn't go, and the user answers
                # it there.
                logger.warning("Conduit found no pending approval for a call's answer (%s)", answer["choice"])
            else:
                logger.info("Conduit passed a call's approval answer (%s): %s resolved", answer["choice"], resolved)
            try:
                # The call hears which: it says it went only when it did.
                store.settle_approval(answer["answer_id"], bool(resolved))
            except Exception:  # noqa: BLE001
                logger.warning("Conduit could not settle a call's approval answer", exc_info=True)
            return
        if claimed:
            # Unloaded (or out of time) mid-answer: the call hears it didn't
            # go, rather than waiting on an answer nothing will settle.
            try:
                store.settle_approval(claimed, False)
            except Exception:  # noqa: BLE001
                logger.warning("Conduit could not settle a call's approval answer", exc_info=True)

    # A thread of its own, not _spawn: it waits as long as the approval does.
    try:
        threading.Thread(target=contextvars.copy_context().run, args=(follow,), name="conduit-call-approval",
                         daemon=True).start()
    except RuntimeError:
        # A hook never raises: the user answers on the card instead.
        logger.warning("Conduit could not follow an approval for a call", exc_info=True)
        with _followed_lock:
            _followed.discard((home, session_key))


def _resolve_approval(session_key: str, command: str, answer: dict[str, Any]) -> int:
    """Answers the pending approval whose command is ``command``, by its
    request id: never another one queued in the same session."""
    import hashlib

    from tools.approval import list_gateway_approvals, resolve_gateway_approval

    wanted = answer.get("command_sha") or hashlib.sha256(command.encode("utf-8")).hexdigest()
    pending = list_gateway_approvals(session_key)
    matches = [entry for entry in pending
               if hashlib.sha256(str(entry.get("command") or "").encode("utf-8")).hexdigest() == wanted]
    if len(matches) != 1:
        # None, or two the same: which one the user heard can't be told.
        return 0
    request_id = str(matches[0].get("request_id") or "")
    if not request_id and len(pending) != 1:
        # A Hermes without request ids answers its oldest: only safe alone.
        return 0
    kwargs: dict[str, Any] = {"request_id": request_id} if request_id else {}
    reason = answer.get("reason") or None
    if not _takes_reason(resolve_gateway_approval):
        # A Hermes before /deny reasons.
        return resolve_gateway_approval(session_key, answer["choice"], **kwargs)
    return resolve_gateway_approval(session_key, answer["choice"], reason=reason, **kwargs)


def _takes_reason(function: Any) -> bool:
    import inspect

    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.name == "reason" or parameter.kind is inspect.Parameter.VAR_KEYWORD
               for parameter in parameters)


def failed_turn(session_id: str, *, profile: str, fallback: dict[str, Any] | None, turn_id: Any = None) -> bool:
    """A turn no watch covers failed: with alert calls on, the call takes the
    place of ``fallback`` (True, like ``turn_ended``). ``turn_id`` keeps a
    replayed hook to the same call."""
    if not session_id:
        return False
    try:
        seed = "" if turn_id is None else str(turn_id)
        result = _store(get_hermes_home()).alert_now(session_id, "failed", "", seed=seed)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not check alert calls for a failed turn", exc_info=True)
        return False
    if result is None:
        return False
    if result["status"] != "call":
        logger.info("Conduit call for a failed turn held back: %s", result.get("reason") or result["status"])
        return False
    try:
        event = call_event(result["watch"], "failed", session_id=session_id, profile=profile)
        _spawn(lambda: deliver(event, fallback))
    except Exception:  # noqa: BLE001 — the caller sends its usual push
        logger.warning("Conduit could not send a call request", exc_info=True)
        return False
    return True


def outcome_note(session_id: str) -> str | None:
    """What this turn of ``session_id`` should know about calls the user
    declined or didn't answer since its last turn, once. Never raises: a
    hook calls this."""
    if not session_id:
        return None
    try:
        store = _store(get_hermes_home())
        taken = store.take_outcomes(session_id)
        now = store.clock()
        return "\n".join(_outcome_line(entry, now) for entry in taken) or None
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not read call outcomes for this turn", exc_info=True)
        return None


def _outcome_line(entry: dict[str, Any], now: float) -> str:
    ago = _ago(now - entry["at"])
    if entry["outcome"] == "declined":
        what = f"the user declined the phone call you placed {ago}"
    else:
        what = f"the phone call you placed {ago} went unanswered"
    if entry["reason"]:
        what += f' (you were calling to say: "{_quoted(entry["reason"])}")'
    elif entry["title"]:
        what += f' (about "{_quoted(entry["title"])}")'
    elif entry["kind"] == "approval":
        what += " (about an approval you were waiting for)"
    elif entry["kind"] == "question":
        what += " (about a question you asked them)"
    return f"[Conduit: {what}. They haven't heard what you called about; if it still matters, tell them.]"


# What a reason or title can't carry into the note: it can't close the quote
# or the bracket around it.
_QUOTED = str.maketrans({'"': "'", "[": "(", "]": ")"})


def _quoted(text: str) -> str:
    return "".join(char for char in text.translate(_QUOTED) if char.isprintable())


def _ago(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{minutes} minutes ago"
    return f"about {int(minutes / 60 + 0.5)} hours ago"


def call_event(watch: dict[str, Any], outcome: str, *, session_id: str, profile: str) -> dict[str, Any]:
    title = str(watch.get("title") or "")
    reason = str(watch.get("reason") or "")
    subject = f"“{title}”" if title else "Your job"
    if outcome in ("approval", "question"):
        lead = "Hermes needs your OK" if outcome == "approval" else "Hermes has a question for you"
        body = f"{lead}: {reason}" if reason else f"{lead}."
    elif watch.get("origin") == "alert":
        body = "Hermes ran into a problem with your request."
    else:
        # What Hermes said it calls about, else how the job ended.
        body = reason or {
            "done": f"{subject} finished.",
            "failed": f"{subject} failed.",
            "stopped": f"{subject} stopped before finishing.",
        }[outcome]
    call = {"id": watch["id"], "kind": outcome, "title": title, "session_ids": watch["session_ids"]}
    if reason:
        call["reason"] = reason
    if watch.get("origin") == "alert":
        call["origin"] = "alert"
    return push_event(
        "call.requested",
        # From the watch: a resend of this call is the same event to the relay.
        identifier=f"call:{watch['id']}",
        session_id=session_id,
        profile=profile,
        profile_name=session_titles.bot_name(),
        title=CALL_TITLE,
        title_label="call.requested",
        body=body,
        call=call,
    )


def deliver(event: dict[str, Any], fallback: dict[str, Any] | None) -> None:
    relay_failed = False
    for delay in DELIVERY_DELAYS_S:
        if delay:
            _sleep(delay)
        if _stopped.is_set():
            logger.info("Conduit call request dropped: the plugin was unloaded")
            return
        try:
            answer = client.send_now(event)
        except RuntimeError as error:
            if getattr(error, "status", 0) >= 500 and getattr(error, "detail", "") not in APNS_FAILURES:
                # The relay (or a proxy in front of it) failed to handle it:
                # worth another try.
                logger.warning("Conduit call request delivery failed: %s", error)
                relay_failed = True
                continue
            # The relay refused it, the profile lost its pairing, or the
            # relay took it and Apple didn't: sending the same event again
            # won't change that (a retry only answers "duplicate").
            logger.warning("Conduit call request refused: %s", error)
            break
        except Exception as error:  # noqa: BLE001 — transport: try again
            logger.warning("Conduit call request delivery failed: %s", error)
            continue
        # A relay that failed after taking the event answers the retry as a
        # duplicate without sending it again.
        if not (relay_failed and isinstance(answer, dict) and answer.get("duplicate")):
            return
        logger.warning("Conduit call request was taken but never sent")
        break
    if fallback is not None and not client.enqueue(fallback):
        logger.warning("Conduit could not queue the usual push for a call that didn't go out")
