"""Pulse's suggestions (proactivity.md 7.6, items 6 and 7; designs/pulse-look-first.md).

A Pulse turn never changes the phone. An add_event or add_reminder it asks
for is checked as conduit_calendar's write checks it (phone_tool.
proposed_add), not written, and kept here as a suggestion; the model is told
it suggested it. Once the turn's message is in, each suggestion becomes a
card with Add and Not now. Add approves that one suggestion, once, and the
plugin asks the phone for the change from the stored fields, with no model
in between and never in the "add without asking" tier. The card follows
the change on the phone by its id, and Undo asks the phone to delete what
it added.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import phone_tool
from . import pulse_report as report
from . import pulse_store as store
from .phone_store import FILE_NAME as PHONE_FILE, LOST_NOTE, PhoneError, PhoneStore, new_change_id

logger = logging.getLogger(__name__)

# A suggestion's card kind.
PROPOSAL = "proposal"
KINDS = ("add_event", "add_reminder")
# Suggestions a turn may make, and a day's (UTC days, as for look-ups).
MAX_PER_TURN = 2
DAILY = 5
# A suggestion lasts until its card is dismissed, its time has passed, or the
# phone's own wait for a change (phone_store.CHANGE_TTL_S).
KEEP_S = 7 * 86400.0
# Kept this long, while its card is.
PRUNE_S = 30 * 86400.0
# Add's change and Undo's delete get their ids before the phone store is
# asked for them. One the phone store still doesn't have after this was
# never written (the process stopped in between): it can be tapped again,
# and asking again uses the same id, so the phone gets one change.
STALE_S = 300.0
WAITING, APPROVED, DISMISSED, EXPIRED = "waiting", "approved", "dismissed", "expired"

PROPOSED = "Suggested, not added: the user gets an Add button for it. Say you suggested it; don't say it's done."
TURN_CAP = "That's the most suggestions a Pulse turn makes: mention anything else worth adding in your message."
DAILY_CAP = "Pulse has made today's suggestions: mention it in your message, and the user can add it."
PAST = "That time has passed, so there's nothing to suggest."
# The change's reason on the phone's card, and Undo's.
REASON = "Pulse suggested it, and you tapped Add."
UNDO_REASON = "You tapped Undo on Pulse's suggestion."
# Why? on a suggestion's card, by its turn's kind.
WHY = {"report": "Pulse suggested this in its report.", "followup": "Pulse suggested this in a follow-up.",
       "brief": "Pulse suggested this in your morning brief."}


class ProposalError(Exception):
    """Why a suggestion can't be added, dismissed or undone: "not_found",
    "already_added", "dismissed", "expired", "not_added", or the phone's
    own code (PhoneError), with its words in ``note``."""

    def __init__(self, code: str, note: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.note = note


def day_from(now: float) -> float:
    """The start of ``now``'s UTC day."""
    return now - now % 86400.0


def propose(pulse_store: store.PulseStore, attempt: dict, args: Any, *, profile: str, now: float) -> str:
    """Keeps an add a Pulse turn asked for as a suggestion, checked as the
    calendar tool's write checks it, and says what the hook answers the
    model: a change that fails the checks gets the tool's own error, so the
    model can fix it. Kept from a copy of ``args``, so nothing that changes
    them after this check changes what's kept."""
    args = copy.deepcopy(args) if isinstance(args, dict) else {}
    turn, day = pulse_store.proposal_counts(attempt["attempt_id"], day_from(now))
    if turn >= MAX_PER_TURN:
        return TURN_CAP
    if day >= DAILY:
        return DAILY_CAP
    try:
        checked = phone_tool.proposed_add(args)
    except PhoneError as error:
        return phone_tool.refusal(error)
    expires = expires_at(checked, now)
    if expires <= now:
        return PAST
    added = pulse_store.add_proposal({
        "attempt_id": attempt["attempt_id"], "kind": checked["kind"], "fields": checked["fields"],
        "device": checked["device_id"], "profile": profile, "line": line(checked), "created_at": now,
        "expires_at": expires}, per_turn=MAX_PER_TURN, per_day=DAILY, day_from=day_from(now))
    if added is None:  # another call on this turn got there first
        return TURN_CAP if pulse_store.proposal_counts(attempt["attempt_id"], day_from(now))[0] >= MAX_PER_TURN \
            else DAILY_CAP
    return PROPOSED


def line(checked: dict) -> str:
    """What a suggestion's card says, written from its fields alone, never
    the model's words: "Add to Personal: Dinner at Nopa, Sat 25 Oct, 19:00
    to 21:00", "Add to Errands: Renew passport, due Fri 31 Oct"."""
    fields = checked["fields"]
    event = checked["kind"] == "add_event"
    where = checked.get("calendar") or ("your calendar" if event else "your reminders")
    title = fields.get("title") or ("Event" if event else "Reminder")
    # Times as the phone's calendar shows them, whatever offset they came in.
    zone = _zone(checked.get("time_zone"), None)
    if event:
        when = _span(_parsed(fields.get("start"), zone), _parsed(fields.get("end"), zone))
    else:
        when = _when(_parsed(fields.get("due"), zone))
        when = f"due {when}" if when else ""
    return f"Add to {where}: {title}" + (f", {when}" if when else "")


def _parsed(value: Any, zone: Any = None) -> Optional[date | datetime]:
    """A field's date, or its time (in ``zone`` when that's known). The
    fields are the phone store's, so a time has its offset ("Z" is
    "+00:00" by then)."""
    if not isinstance(value, str):
        return None
    try:
        moment = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    except ValueError:
        return None
    if zone is not None and isinstance(moment, datetime) and moment.tzinfo is not None:
        return moment.astimezone(zone)
    return moment


def _when(moment: Optional[date | datetime]) -> str:
    if isinstance(moment, datetime):
        return moment.strftime("%a %d %b, %H:%M")
    return moment.strftime("%a %d %b") if moment is not None else ""


def _span(begins: Optional[date | datetime], ends: Optional[date | datetime]) -> str:
    if begins is None:
        return ""
    if ends is None or ends == begins:
        return _when(begins)
    if isinstance(begins, datetime) and isinstance(ends, datetime) and ends.date() == begins.date():
        return f"{_when(begins)} to {ends:%H:%M}"
    return f"{_when(begins)} to {_when(ends)}"


def expires_at(checked: dict, now: float) -> float:
    """When a suggestion runs out: when its event starts or its reminder is
    due (a day's, at the end of that day in the phone's time zone), or after
    KEEP_S, whichever comes first."""
    fields = checked["fields"]
    moment = _parsed(fields.get("start") if checked["kind"] == "add_event" else fields.get("due"))
    zone = _zone(checked.get("time_zone"))
    if isinstance(moment, datetime):
        at = (moment if moment.tzinfo else moment.replace(tzinfo=zone)).timestamp()
    elif isinstance(moment, date):
        at = datetime.combine(moment + timedelta(days=1), clock(), zone).timestamp()
    else:
        return now + KEEP_S
    return min(now + KEEP_S, at)


def _zone(name: Any, unknown: Any = timezone.utc) -> Any:
    """The phone's time zone, or ``unknown`` when it names none this host
    knows (phone_tool._zone, which is always UTC then)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name) if isinstance(name, str) and name else unknown
    except Exception:  # noqa: BLE001 — a zone this host doesn't know
        return unknown


def profile_name(home: Path) -> str:
    """The Hermes profile a home belongs to: "default" for the root, else its folder's name."""
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


def phone_home(home: Path, profile: str) -> Optional[Path]:
    """The Hermes home whose phone store a suggestion goes to: Pulse's own,
    or a profile beside it (pulse_report.profile_homes)."""
    home = Path(home)
    if profile in ("", profile_name(home)):
        return home
    return next((path for name, path in report.profile_homes(home) if name == profile), None)


# Cards ------------------------------------------------------------------------

def settle(pulse_store: store.PulseStore, home: Path, now: float) -> None:
    """Each suggestion whose turn's message is in becomes its card; those of
    turns that ended without one, and waiting ones past their time, expire;
    old ones go once their card has."""
    ended = []
    for proposal in pulse_store.proposals(state=WAITING, uncarded=True):
        attempt = pulse_store.attempt(proposal["attempt_id"])
        if attempt is None or attempt["state"] in (store.REFUSED, store.NOT_ADMITTED, store.FAILED):
            ended.append(proposal["attempt_id"])
        elif attempt["state"] == store.DELIVERED and proposal["expires_at"] > now:
            pulse_store.add_proposal_card(proposal["id"], card_row(pulse_store, proposal, attempt, now))
    pulse_store.expire_proposals(now, attempts=sorted(set(ended)))
    pulse_store.prune_proposals(now - PRUNE_S)


def card_row(pulse_store: store.PulseStore, proposal: dict, attempt: dict, now: float) -> dict[str, Any]:
    """A suggestion's card: on the screen, never pushed (its turn's message
    was), saying what its turn looked up first, as that turn's cards do."""
    from . import pulse_learning as learning

    return {"kind": PROPOSAL, "level": "screen", "title": proposal["line"], "why": WHY.get(attempt["kind"], ""),
            "chat_id": attempt["session_key"], "chat_title": pulse_store.get("home_title") or "Pulse",
            "created_at": now, "looked": learning.looked_json(store.looked_labels(attempt.get("looked"))),
            "proposal": proposal["id"]}


def entry(proposal: dict, home: Path, now: float) -> dict[str, Any]:
    """A suggestion as its card shows it: what it adds and where it stands
    ("waiting", "adding", "added", "changed" on the phone, "failed" with the
    phone's reason, "not_added", "expired", "removing" or "removed" after
    Undo), with the change's id once it's asked for, which the app that
    tapped Add applies without the phone's own card."""
    state, note = _state(proposal, home, now)
    answer: dict[str, Any] = {"id": proposal["id"], "kind": proposal["kind"], "line": proposal["line"],
                              "state": state, "expires_at": proposal["expires_at"]}
    if note:
        answer["note"] = note
    for key in ("change_id", "undo_id"):
        if proposal[key]:
            answer[key] = proposal[key]
    return answer


_OUTCOMES = {"pending": "adding", "applied": "added", "edited": "changed", "dismissed": "not_added",
             "failed": "failed", "expired": "expired", "lost": "failed"}


def _state(proposal: dict, home: Path, now: float) -> tuple[str, str]:
    if proposal["state"] == WAITING:
        return ("expired" if proposal["expires_at"] <= now else WAITING), ""
    if proposal["state"] != APPROVED:
        return ("not_added" if proposal["state"] == DISMISSED else EXPIRED), ""
    phone = _phone(proposal["profile"], home)
    outcome = phone.change_outcome(proposal["change_id"]) if phone is not None else None
    if outcome is None:
        if proposal["asked_at"] is not None:
            return "failed", "The phone no longer has this change."
        # Approved, and its change is on its way to the phone store; one never
        # written (STALE_S) can be tapped again while the suggestion lasts.
        if (proposal["decided_at"] or 0) >= now - STALE_S:
            return "adding", ""
        return ("expired" if proposal["expires_at"] <= now else WAITING), ""
    if proposal["undo_id"] and outcome["state"] in ("applied", "edited"):
        undone = phone.change_outcome(proposal["undo_id"])
        if undone is None and (proposal["undo_at"] or 0) >= now - STALE_S:
            return "removing", ""  # on its way to the phone store
        if undone is not None and undone["state"] in ("pending", "applied"):
            return ("removing" if undone["state"] == "pending" else "removed"), ""
    note = LOST_NOTE if outcome["state"] == "lost" else outcome["note"] or ""
    return _OUTCOMES.get(outcome["state"], "adding"), note if outcome["state"] in ("edited", "failed", "lost") else ""


def _phone(profile: str, home: Path) -> Optional[PhoneStore]:
    path = phone_home(home, profile)
    return PhoneStore(path) if path is not None and (path / PHONE_FILE).exists() else None


# One tap -------------------------------------------------------------------------

def add(pulse_store: store.PulseStore, proposal_id: Any, home: Path, now: float) -> dict[str, Any]:
    """Add on a suggestion's card: approves it, once (a second tap, a retry
    or another device gets "already_added"), then asks the phone for the
    change from the stored fields, always asking there. A change the phone
    store refuses puts the suggestion back, with the reason. Its entry."""
    proposal = _open(pulse_store, proposal_id, home, now)
    # The change's id goes with the approval, before the phone store is asked:
    # asking again for one never written uses that id, and the phone store
    # keeps one change per id.
    change_id = proposal["change_id"] or new_change_id()
    if not pulse_store.swap_proposal(proposal["id"], {"state": proposal["state"], "decided_at": proposal["decided_at"]},
                                     state=APPROVED, change_id=change_id, decided_at=now):
        raise ProposalError(_taken(pulse_store, proposal["id"]))
    path = phone_home(home, proposal["profile"])
    try:
        if path is None:
            raise PhoneError("nothing_shared", "The Hermes profile this suggestion was for is gone")
        change = PhoneStore(path).add_change(proposal["kind"], proposal["fields"], None, reason=REASON,
                                             session_title=pulse_store.get("home_title") or "Pulse",
                                             device=proposal["device"], ask=True, change_id=change_id)
    except PhoneError as error:
        pulse_store.swap_proposal(proposal["id"], {"state": APPROVED, "decided_at": now}, state=WAITING,
                                  change_id="", decided_at=None)
        raise ProposalError(error.code, str(error)) from None
    pulse_store.swap_proposal(proposal["id"], {"change_id": change_id}, asked_at=now)
    phone_tool.nudge(change, proposal["profile"] or "default")
    return entry(pulse_store.proposal(proposal["id"]), home, now)


def dismiss(pulse_store: store.PulseStore, proposal_id: Any, home: Path, now: float) -> dict[str, Any]:
    """Not now on a suggestion's card: it won't be added, and its card goes."""
    proposal = _open(pulse_store, proposal_id, home, now)
    if not pulse_store.swap_proposal(proposal["id"], {"state": proposal["state"], "decided_at": proposal["decided_at"]},
                                     state=DISMISSED, decided_at=now):
        raise ProposalError(_taken(pulse_store, proposal["id"]))
    pulse_store.dismiss_card(proposal["card_id"], True, now)
    return entry(pulse_store.proposal(proposal["id"]), home, now)


def undo(pulse_store: store.PulseStore, proposal_id: Any, home: Path, now: float) -> dict[str, Any]:
    """Undo on an added suggestion's card: asks the phone to delete what it
    added, never in the auto tier: the app that tapped Undo applies it
    without the phone's own card, as for Add, and another device asks first.
    One Undo at a time: a second tap, a retry or another device gets
    "already_undone" while the delete is on its way or done."""
    proposal = pulse_store.proposal(proposal_id)
    if proposal is None:
        raise ProposalError("not_found")
    phone = _phone(proposal["profile"], home) if proposal["state"] == APPROVED and proposal["change_id"] else None
    outcome = phone.change_outcome(proposal["change_id"]) if phone is not None else None
    if outcome is None or outcome["state"] not in ("applied", "edited") or not outcome["item_id"]:
        raise ProposalError("not_added")
    # The delete's id is taken before the phone store is asked, as Add's is.
    undo_id = proposal["undo_id"]
    if undo_id:
        undone = phone.change_outcome(undo_id)
        if (undone is None and (proposal["undo_at"] or 0) >= now - STALE_S) \
                or (undone is not None and undone["state"] in ("pending", "applied")):
            raise ProposalError("already_undone")
        if undone is not None:  # that delete didn't happen: a new one
            undo_id = ""
    undo_id = undo_id or new_change_id()
    was = {"undo_id": proposal["undo_id"], "undo_at": proposal["undo_at"]}
    if not pulse_store.swap_proposal(proposal["id"], was, undo_id=undo_id, undo_at=now):
        raise ProposalError("already_undone")
    event = proposal["kind"] == "add_event"
    try:
        change = phone.add_change("delete_event" if event else "delete_reminder", None,
                                  {"event_id" if event else "reminder_id": outcome["item_id"]}, reason=UNDO_REASON,
                                  session_title=pulse_store.get("home_title") or "Pulse", device=proposal["device"],
                                  ask=True, change_id=undo_id)
    except PhoneError as error:
        pulse_store.swap_proposal(proposal["id"], {"undo_id": undo_id, "undo_at": now}, **was)  # tap again
        raise ProposalError(error.code, str(error)) from None
    phone_tool.nudge(change, proposal["profile"] or "default")
    return entry(pulse_store.proposal(proposal["id"]), home, now)


def _open(pulse_store: store.PulseStore, proposal_id: Any, home: Path, now: float) -> dict:
    """The suggestion Add or Not now names, while its card stands and it
    still waits (or its approval's change was never written)."""
    proposal = _usable(pulse_store, proposal_id, now)
    shown = _state(proposal, home, now)[0]
    if shown != WAITING:
        raise ProposalError(EXPIRED if shown == "expired" else _taken(pulse_store, proposal["id"]))
    return proposal


def _usable(pulse_store: store.PulseStore, proposal_id: Any, now: float) -> dict:
    """The suggestion a card's button names, while its card stands."""
    proposal = pulse_store.proposal(proposal_id)
    if proposal is None or proposal["card_id"] is None:
        raise ProposalError("not_found")
    card = pulse_store.card(proposal["card_id"])
    if card is None:
        raise ProposalError("not_found")
    if proposal["state"] == WAITING and proposal["expires_at"] <= now:
        pulse_store.expire_proposals(now)
        raise ProposalError(EXPIRED)
    if card["dismissed_at"] is not None and card["rest_until"] is None:
        raise ProposalError(DISMISSED)
    return proposal


def _taken(pulse_store: store.PulseStore, proposal_id: int) -> str:
    """Why a suggestion that moved meanwhile can't move now."""
    proposal = pulse_store.proposal(proposal_id)
    state = proposal["state"] if proposal else ""
    return {APPROVED: "already_added", DISMISSED: DISMISSED, EXPIRED: EXPIRED}.get(state, "not_found")
