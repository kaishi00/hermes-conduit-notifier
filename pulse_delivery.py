"""Pulse delivery: tagged attempts and the reconciler (designs/proactivity.md 6.4.1).

A Pulse message is a turn injected into the home chat. ``inject_message``
saying True only means the gateway scheduled it, so every attempt carries a
tag in its text, is written to the store before the call, and moves on only
by what Hermes's own rows show:

- no admission and no transcript row after a grace, or after a restart of
  the process that sent it: ``not_admitted``;
- an admission still queued: ``queued``; started or unknown: ``running``;
- a completed admission whose turn wrote a reply: ``delivered``;
- anything else that ended: ``failed``, with what Hermes said.

A new attempt for an item is only ever made once the last one is
``refused``, ``not_admitted`` or ``failed``: never on a guess.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import pulse_hermes as hermes
from . import pulse_store as store

logger = logging.getLogger(__name__)

# How long an attempt may go without any trace in Hermes before it counts as
# never admitted. The gateway commits an admission within moments of the call.
ADMIT_GRACE_S = 120.0
# A completed admission whose reply row isn't visible yet is checked once more.
AWAITING_REPLY = "completed; waiting for the reply row"


@dataclass(frozen=True)
class Seen:
    """What Hermes shows for one attempt."""
    admission: Optional[hermes.Admission]
    turn: Optional[hermes.TurnRows]


def new_tag(item_id: str, attempt: int) -> str:
    return f"p{item_id[:8]}-{attempt}"


def derive_state(seen: Seen, *, age_s: float, sent_by_earlier_process: bool,
                 prior_detail: str = "") -> tuple[str, str]:
    """The attempt's state and a short detail, from Hermes's rows alone."""
    admission, turn = seen.admission, seen.turn
    if admission is None:
        if turn is not None:
            # A host that ran the turn without an admission row.
            return _from_turn(turn, ended=turn.closed)
        if sent_by_earlier_process or age_s >= ADMIT_GRACE_S:
            return store.NOT_ADMITTED, "Hermes has no record of it"
        return store.SUBMITTED, ""
    if admission.status == "queued":
        return store.QUEUED, ""
    if admission.status in ("started", "unknown"):
        return store.RUNNING, "" if admission.status == "started" else "Hermes lost track of it"
    outcome = admission.outcome or "unknown"
    if outcome != "completed":
        return store.FAILED, f"Hermes ended it: {outcome}"
    if turn is not None and (turn.reply or turn.failed):
        return _from_turn(turn, ended=True)
    if prior_detail == AWAITING_REPLY:
        return store.FAILED, "completed without a reply"
    return store.RUNNING, AWAITING_REPLY


def _from_turn(turn: hermes.TurnRows, *, ended: bool) -> tuple[str, str]:
    if turn.failed:
        return store.FAILED, "Hermes marked the turn failed"
    if turn.reply:
        return store.DELIVERED, turn.reply[:200]
    if ended:
        return store.FAILED, "ended without a reply"
    return store.RUNNING, ""


def submit(pulse_store: store.PulseStore, inject: Callable[..., bool], *, kind: str, session_key: str,
           text: Callable[[str], str], item_id: str = "", attempt: int = 1) -> dict:
    """One attempt: recorded first, then handed to the gateway."""
    item_id = item_id or uuid.uuid4().hex
    tag = new_tag(item_id, attempt)
    attempt_id = f"{item_id}:{attempt}"
    body = text(tag)
    pulse_store.add_attempt(attempt_id=attempt_id, item_id=item_id, attempt=attempt, kind=kind, tag=tag,
                            text=body, session_key=session_key)
    try:
        accepted = bool(inject(body, session_key=session_key))
    except Exception as error:  # noqa: BLE001 — the gateway refused it one way or another
        logger.warning("conduit_push: Pulse injection raised", exc_info=True)
        accepted, why = False, f"inject_message raised {type(error).__name__}"
    else:
        why = "inject_message returned False"
    if not accepted:
        pulse_store.update_attempt(attempt_id, state=store.REFUSED, detail=why)
    return pulse_store.attempt_by_tag(tag) or {}


def reconcile(pulse_store: store.PulseStore, home: Path, *, plugin_id: str, process_started_at: float,
              now: Optional[float] = None) -> list[dict]:
    """Moves every in-flight attempt to what Hermes shows; returns the ones
    that changed. Unreadable state leaves an attempt as it was."""
    now = time.time() if now is None else now
    changed = []
    for row in pulse_store.in_flight():
        try:
            seen = look(home, plugin_id=plugin_id, session_key=row["session_key"], tag=row["tag"])
        except hermes.HermesUnreadable as error:
            logger.debug("conduit_push: Pulse can't read state.db: %s", error)
            continue
        state, detail = derive_state(seen, age_s=now - row["created_at"],
                                     sent_by_earlier_process=row["created_at"] < process_started_at,
                                     prior_detail=row["detail"])
        if state != row["state"] or detail != row["detail"]:
            pulse_store.update_attempt(row["attempt_id"], state=state, detail=detail)
            changed.append({**row, "state": state, "detail": detail})
    return changed


def look(home: Path, *, plugin_id: str, session_key: str, tag: str) -> Seen:
    admission = hermes.find_admission(home, plugin_id=plugin_id, session_key=session_key, tag=tag)
    turn = hermes.find_turn(home, session_key=session_key, tag=tag,
                            request_id=admission.request_id if admission else "")
    return Seen(admission, turn)


def describe(row: dict[str, Any]) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"]))
    detail = f" ({row['detail']})" if row.get("detail") else ""
    return f"{when}  {row['kind']:<6} {row['tag']:<14} {row['state']}{detail}"
