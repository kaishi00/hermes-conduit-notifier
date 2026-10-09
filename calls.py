"""Hermes calls you (#449): turning a watched job's end into a call request.

The turn-end hooks hand every top-level turn end here. A turn of a session
Conduit watches consumes the watch (calls_store) and, within the user's
limits, sends a ``call.requested`` event in place of the push the hook would
have sent. Delivery retries a transport failure with the same event id, which
the relay dedupes; a rejection (an older relay, the relay's daily ceiling) or
a final failure sends that ordinary push instead, so the user still hears
about the job.
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


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def turn_ended(session_id: str, outcome: str, *, profile: str, fallback: dict[str, Any] | None) -> bool:
    """A top-level turn of ``session_id`` ended with ``outcome``.

    True when a call request now owns the user's notice: the caller must not
    send ``fallback`` itself (delivery sends it if the call can't go out).
    False when no watch covers the session or the call was held back by the
    user's settings or limits; the caller sends its usual push.
    """
    if not session_id:
        return False
    try:
        result = CallStore(get_hermes_home()).fire(session_id, outcome)
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        logger.warning("Conduit could not check call watches for this turn", exc_info=True)
        return False
    if result is None:
        return False
    if result["status"] != "call":
        logger.info("Conduit call for a finished job held back: %s", result.get("reason") or result["status"])
        return False
    event = call_event(result["watch"], outcome, session_id=session_id, profile=profile)
    _spawn(lambda: deliver(event, fallback))
    return True


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
