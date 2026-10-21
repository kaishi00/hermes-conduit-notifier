"""Pulse delivery: tagged attempts and the reconciler (designs/proactivity.md 6.4.1).

A Pulse message is a hidden turn submitted into the home chat. The host
accepting it only means the turn was taken, so every attempt carries a tag
in its text, is written to the store before it's sent, and moves on only by
what Hermes's own transcript shows:

- the chat refused it: ``refused``;
- no tagged user row after a grace, or after a restart of the process that
  sent it (a queued turn lives in that process): ``not_admitted``;
- taken behind a turn in progress: ``queued``; its row written: ``running``;
- a reply after the tagged row: ``delivered``;
- a failed turn, a turn that ended without a reply, or one stuck for hours:
  ``failed``, with what was seen.

A new attempt for an item is only ever made once the last one is
``refused``, ``not_admitted`` or ``failed``: never on a guess.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import pulse_hermes as hermes
from . import pulse_store as store

logger = logging.getLogger(__name__)

# How long a sent turn may go without its user row before it counts as never
# taken. The host writes the row before it answers the submit.
ADMIT_GRACE_S = 120.0
# A turn that ended (on_session_end) without a reply row gets this long for it.
ENDED = "the turn ended; waiting for the reply row"
REPLY_GRACE_S = 60.0
# An attempt that hasn't finished in this long counts as failed, so the next
# one can go.
STUCK_S = 2 * 3600.0
# The transcript is searched from a little before the attempt (clock skew).
SINCE_SLACK_S = 60.0


def new_tag(item_id: str, attempt: int) -> str:
    return f"p{item_id[:8]}-{attempt}"


def derive_state(turn: Optional[hermes.TurnRows], *, state: str, age_s: float, sent_by_earlier_process: bool,
                 prior_detail: str = "", unchanged_s: float = 0.0) -> tuple[str, str]:
    """The attempt's state and a short detail, from Hermes's rows (and the
    state the attempt is in now)."""
    if turn is None:
        if sent_by_earlier_process:
            return store.NOT_ADMITTED, "Hermes has no record of it"
        if state == store.QUEUED:
            if age_s >= STUCK_S:
                return store.FAILED, f"still queued after {int(age_s // 60)} minutes"
            return store.QUEUED, prior_detail
        if age_s >= ADMIT_GRACE_S:
            return store.NOT_ADMITTED, "Hermes has no record of it"
        return state, prior_detail
    if turn.failed:
        return store.FAILED, "Hermes marked the turn failed"
    if turn.reply:
        return store.DELIVERED, turn.reply[:200]
    if turn.closed:
        return store.FAILED, "ended without a reply"
    if prior_detail == ENDED and unchanged_s >= REPLY_GRACE_S:
        return store.FAILED, "ended without a reply"
    if age_s >= STUCK_S:
        return store.FAILED, f"no reply after {int(age_s // 60)} minutes"
    return store.RUNNING, prior_detail if prior_detail == ENDED else ""


def submit(pulse_store: store.PulseStore, send: Callable[[str, str], Any], *, kind: str, chat_id: str,
           text: Callable[[str], str], item_id: str = "", attempt: int = 1) -> dict:
    """One attempt: recorded first, then sent. ``send(chat_id, text)``
    returns a ``pulse_host.Sent``."""
    item_id = item_id or uuid.uuid4().hex
    tag = new_tag(item_id, attempt)
    attempt_id = f"{item_id}:{attempt}"
    body = text(tag)
    pulse_store.add_attempt(attempt_id=attempt_id, item_id=item_id, attempt=attempt, kind=kind, tag=tag,
                            text=body, session_key=chat_id)
    try:
        sent = send(chat_id, body)
    except Exception as error:  # noqa: BLE001 — the host refused it one way or another
        logger.warning("conduit_push: Pulse send raised", exc_info=True)
        pulse_store.update_attempt(attempt_id, state=store.REFUSED, detail=f"sending raised {type(error).__name__}")
    else:
        if not sent.ok:
            pulse_store.update_attempt(attempt_id, state=store.REFUSED, detail=sent.detail)
        elif sent.status == "queued":
            pulse_store.update_attempt(attempt_id, state=store.QUEUED, detail="behind a turn in progress")
    return pulse_store.attempt_by_tag(tag) or {}


def reconcile(pulse_store: store.PulseStore, home: Path, *, process_started_at: float,
              now: Optional[float] = None) -> list[dict]:
    """Moves every in-flight attempt to what Hermes shows; returns the ones
    that changed. Unreadable state leaves an attempt as it was."""
    now = time.time() if now is None else now
    changed = []
    for row in pulse_store.in_flight():
        try:
            turn = hermes.find_turn(home, chat_id=row["session_key"], tag=row["tag"],
                                    since=row["created_at"] - SINCE_SLACK_S)
        except hermes.HermesUnreadable as error:
            logger.debug("conduit_push: Pulse can't read state.db: %s", error)
            continue
        state, detail = derive_state(turn, state=row["state"], age_s=now - row["created_at"],
                                     sent_by_earlier_process=row["created_at"] < process_started_at,
                                     prior_detail=row["detail"], unchanged_s=now - row["updated_at"])
        if state != row["state"] or detail != row["detail"]:
            pulse_store.update_attempt(row["attempt_id"], state=state, detail=detail)
            changed.append({**row, "state": state, "detail": detail})
    return changed


def describe(row: dict[str, Any]) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"]))
    detail = f" ({row['detail']})" if row.get("detail") else ""
    return f"{when}  {row['kind']:<6} {row['tag']:<14} {row['state']}{detail}"
