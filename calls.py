"""Hermes calls you (#449): turning a watched job's end into a call request.

The turn-end hooks hand every top-level turn end here. A turn of a session
Conduit watches consumes the watch (calls_store) and, within the user's
limits, sends a ``call.requested`` event in place of the push the hook would
have sent. Delivery retries a transport failure with the same event id, which
the relay dedupes; a rejection (an older relay, the relay's daily ceiling) or
a final failure sends that ordinary push instead, so the user still hears
about the job.

A job that ends while its watch is held (the user's call is still going)
gets its ordinary push; the call waits. A waiter thread calls once the hold
runs out unreleased (the phone went away mid-call), and every turn end
sweeps for such calls too, so a gateway restart doesn't lose one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import client
from .calls_store import CallStore
from .events import push_event

logger = logging.getLogger("hermes.plugins.conduit_push")

CALL_TITLE = "Hermes wants to talk"
# Seconds to wait before each delivery attempt.
DELIVERY_DELAYS_S = (0.0, 2.0, 4.0)


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="conduit-call", daemon=True).start()


# Homes with a waiter thread running, so a burst of held turn ends starts one.
_waiting: set[str] = set()
_waiting_lock = threading.Lock()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.time()


def _store(home: Any) -> CallStore:
    return CallStore(home, clock=lambda: _clock())


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
        if key in _waiting:
            return
        _waiting.add(key)
    try:
        _spawn(_waiter(home, profile, key))
    except BaseException:
        with _waiting_lock:
            _waiting.discard(key)
        raise


def _waiter(home: Any, profile: str, key: str) -> Callable[[], None]:
    def wait() -> None:
        store = _store(home)
        try:
            while True:
                due_at = store.next_due()
                if due_at is None:
                    # Checked again under the lock: a hold that starts now
                    # either shows up here or starts its own waiter.
                    with _waiting_lock:
                        if store.next_due() is None:
                            _waiting.discard(key)
                            return
                    continue
                _sleep(max(0.0, due_at - store.clock()) + 1.0)
                _send_due(store.fire_due(), profile)
        except Exception:  # noqa: BLE001 — the next turn end sweeps again
            logger.warning("Conduit stopped waiting on a held call", exc_info=True)
            with _waiting_lock:
                _waiting.discard(key)

    return wait


def call_event(watch: dict[str, Any], outcome: str, *, session_id: str, profile: str) -> dict[str, Any]:
    title = str(watch.get("title") or "")
    subject = f"“{title}”" if title else "Your job"
    body = {
        "done": f"{subject} finished.",
        "failed": f"{subject} failed.",
        "stopped": f"{subject} stopped before finishing.",
    }[outcome]
    return push_event(
        "call.requested",
        # From the watch: a resend of this call is the same event to the relay.
        identifier=f"call:{watch['id']}",
        session_id=session_id,
        profile=profile,
        title=CALL_TITLE,
        body=body,
        call={"id": watch["id"], "kind": outcome, "title": title, "session_ids": watch["session_ids"]},
    )


def deliver(event: dict[str, Any], fallback: dict[str, Any] | None) -> None:
    for delay in DELIVERY_DELAYS_S:
        if delay:
            _sleep(delay)
        try:
            client.send_now(event)
            return
        except RuntimeError as error:
            # The relay answered and refused (or the profile lost its
            # pairing): sending the same thing again won't change that.
            logger.warning("Conduit call request refused: %s", error)
            break
        except Exception as error:  # noqa: BLE001 — transport: try again
            logger.warning("Conduit call request delivery failed: %s", error)
    if fallback is not None:
        client.enqueue(fallback)
