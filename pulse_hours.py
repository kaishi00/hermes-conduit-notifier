"""When Pulse may push (designs/pulse-right-time.md).

Eric (2026-10-26): "The same reminder might be incredibly useful at 9 AM and
infuriating at 11 PM." Reports and the morning brief go at the times the user
set, but a follow-up's news comes whenever its check lands. This decides when
a follow-up's push may go (pulse_followup.next_send), and how loudly a
report's picks may come up (pulse_candidates.route):

- The night (the ``night`` setting: from and to, "HH:MM" in the user's time
  zone; 22:00 to 07:00 by default, [] for none): nothing pushes in it. What
  would is held for the morning brief, unless it can't wait: news its check
  marks urgent, or something due before it would come.
- The parts of the day (``part``): morning (the night's end to 12:00),
  midday (12:00 to 17:00), evening (17:00 to the night) and night. With no
  night set, they're named as by the default one.
- When a follow-up's news is useful (its check's ``when``, WHEN): any time,
  office hours (weekdays, 09:00 to 17:00), the morning or the evening.
- "Wrong time" on a card that pushed closes the part of the day it came in
  for its kind (pulse_learning.closed_parts): that kind pushes no more then.
- A push that may not go now waits for the first time it may (``timing``):
  held for the morning brief when that's the night's end and the brief comes
  soon after, else until then, and checked once more before it goes.
- Busy (``busy``): an event on the phone's calendar now (not all-day, free,
  cancelled or declined), the phone driving or on CarPlay in the last
  DRIVING_FRESH_S, or a Focus that holds Hermes's calls. A push waits until
  that's over, and comes up on the Pulse screen without one past BUSY_MAX_S.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Collection, Optional

logger = logging.getLogger(__name__)

SETTING = "night"
DEFAULT_NIGHT = ("22:00", "07:00")
DEFAULT_BOUNDS = ((22, 0), (7, 0))
MORNING, MIDDAY, EVENING, NIGHT = "morning", "midday", "evening", "night"
PARTS = (MORNING, MIDDAY, EVENING, NIGHT)
# Minutes after midnight.
NOON, EVENING_START = 12 * 60, 17 * 60
OFFICE = (9 * 60, 17 * 60)  # on weekdays
ANY, OFFICE_HOURS = "any", "office_hours"
WHEN = (ANY, OFFICE_HOURS, MORNING, EVENING)
# How far ahead a push looks for a time it may go; past that, it rides the brief.
AHEAD_DAYS = 8
# A push that may go no sooner than the night's end rides the brief instead
# when the brief comes within this of then (07:00 both, by default).
BRIEF_NEAR_S = 2 * 3600.0
# What ``timing`` decides.
NOW, HOLD, BRIEF = "now", "hold", "brief"
# Busy: what holds a push, how long at most, and how fresh "driving" must be.
EVENT, DRIVING, FOCUS = "event", "driving", "focus"
BUSY_MAX_S = 3 * 3600.0
DRIVING_FRESH_S = 15 * 60.0

Bounds = tuple[tuple[int, int], tuple[int, int]]


def clock(text: Any) -> Optional[tuple[int, int]]:
    """"HH:MM" (or "H:MM") as (hour, minute), or None."""
    found = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", text.strip()) if isinstance(text, str) else None
    if found is None:
        return None
    hour, minute = int(found[1]), int(found[2])
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


def night(pulse_store: Any) -> Optional[Bounds]:
    """The night the user set, as ((hour, minute) it starts, (hour, minute)
    it ends); None when there's none. Unset or unreadable, it's
    DEFAULT_NIGHT; [] is none."""
    value = pulse_store.get(SETTING)
    if value == []:
        return None
    start, end = (clock(value[0]), clock(value[1])) if isinstance(value, list) and len(value) == 2 else (None, None)
    if start is None or end is None:
        return DEFAULT_BOUNDS
    return (start, end) if start != end else None


def bounds(pulse_store: Any) -> Bounds:
    """The night that names the parts of the day: the user's, else the default."""
    return night(pulse_store) or DEFAULT_BOUNDS


def night_text(pulse_store: Any) -> list[str]:
    """The night as the settings show it: ["22:00", "07:00"], or [] for none."""
    found = night(pulse_store)
    return [f"{hour:02d}:{minute:02d}" for hour, minute in found] if found else []


def parse_night(value: Any) -> Optional[list[str]]:
    """A night as the settings take it: two "HH:MM" (from, to), or [] for
    none; the same time twice is none. None for anything else."""
    if value == []:
        return []
    if not isinstance(value, list) or len(value) != 2:
        return None
    start, end = clock(value[0]), clock(value[1])
    if start is None or end is None:
        return None
    return [] if start == end else [f"{hour:02d}:{minute:02d}" for hour, minute in (start, end)]


def _minute(zone: tzinfo, at: float) -> tuple[int, int]:
    """The local minute of the day ``at``, and its weekday (Monday 0)."""
    local = datetime.fromtimestamp(at, zone)
    return local.hour * 60 + local.minute, local.weekday()


def _within(start: int, end: int, minute: int) -> bool:
    """Whether ``minute`` is in [start, end), across midnight when end <= start."""
    return start <= minute < end if start < end else minute >= start or minute < end


def part(names: Bounds, zone: tzinfo, at: float) -> str:
    """The part of the day ``at`` is in, by the night ``names``: NIGHT, else
    MIDDAY (12:00 to 17:00), MORNING (the night's end to 12:00), and
    EVENING for the rest (17:00 to the night)."""
    minute, _ = _minute(zone, at)
    (start_hour, start_minute), (end_hour, end_minute) = names
    start, end = start_hour * 60 + start_minute, end_hour * 60 + end_minute
    if _within(start, end, minute):
        return NIGHT
    if NOON <= minute < EVENING_START:
        return MIDDAY
    if end < NOON and end <= minute < NOON:
        return MORNING
    return EVENING


def part_of(pulse_store: Any, zone: tzinfo, at: float) -> str:
    return part(bounds(pulse_store), zone, at)


def is_night(pulse_store: Any, zone: tzinfo, at: float) -> bool:
    """Whether ``at`` is in the night the user set (none set: never)."""
    found = night(pulse_store)
    return found is not None and part(found, zone, at) == NIGHT


def night_starts(pulse_store: Any, zone: tzinfo, at: float) -> Optional[float]:
    """When the night the user set next starts from ``at`` on: ``at`` when
    it's night then; None when there's none."""
    found = night(pulse_store)
    if found is None:
        return None
    if part(found, zone, at) == NIGHT:
        return at
    return next((edge for edge in _edges(found, zone, at) if part(found, zone, edge) == NIGHT), None)


def due_end(pulse_store: Any, zone: tzinfo, due_at: float, *, timed: bool) -> float:
    """When something due at ``due_at`` stops being of use: then, when a
    time was given (``timed``); for a day alone, when that day's night
    starts, or at its midnight when that comes first."""
    if timed:
        return due_at
    start = datetime.fromtimestamp(due_at, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight = (start + timedelta(days=1)).timestamp()
    noon = start.replace(hour=NOON // 60).timestamp()
    found = night(pulse_store)
    if found is not None and part(found, zone, noon) == NIGHT:
        # A night over noon started that morning: the day's own night is the next one.
        noon = next((edge for edge in _edges(found, zone, noon) if part(found, zone, edge) != NIGHT), midnight)
    starts = night_starts(pulse_store, zone, noon)
    return min(midnight, starts) if starts is not None else midnight


def useful(when: str, names: Bounds, zone: tzinfo, at: float) -> bool:
    """Whether news useful at ``when`` (WHEN) is useful ``at``."""
    if when == OFFICE_HOURS:
        minute, weekday = _minute(zone, at)
        return weekday < 5 and OFFICE[0] <= minute < OFFICE[1]
    if when in (MORNING, EVENING):
        return part(names, zone, at) == when
    return True


def _edges(names: Bounds, zone: tzinfo, now: float) -> list[float]:
    """The times after ``now``, within AHEAD_DAYS, where the part of the day
    or office hours can change, soonest first."""
    clocks = {(0, 0), (OFFICE[0] // 60, 0), (NOON // 60, 0), (EVENING_START // 60, 0), *names}
    midnight = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    edges = set()
    for day in range(AHEAD_DAYS + 1):
        # On a spring-forward day a time in the gap resolves an hour off; once a year, harmless.
        base = midnight + timedelta(days=day)
        for hour, minute in clocks:
            at = base.replace(hour=hour, minute=minute).timestamp()
            if at > now:
                edges.add(at)
    return sorted(edges)


def next_allowed(names: Bounds, zone: tzinfo, now: float, *, when: str = ANY, closed: Collection[str] = (),
                 night_on: bool = True) -> Optional[float]:
    """The first time from ``now`` on that a push may go: not in the night
    (while ``night_on``), not in a part of the day ``closed``, and when it's
    ``useful``. None when there's none within AHEAD_DAYS."""
    def allowed(at: float) -> bool:
        name = part(names, zone, at)
        return not (night_on and name == NIGHT) and name not in closed and useful(when, names, zone, at)

    if allowed(now):
        return now
    return next((at for at in _edges(names, zone, now) if allowed(at)), None)


@dataclass(frozen=True)
class Timing:
    """When a push may go: NOW, HOLD (until ``until``, then checked again)
    or BRIEF (held for the morning brief). ``held_by``: what keeps it from
    going now (NIGHT, "part:" and a part of the day closed for its kind,
    "when:" and when it's useful), or for NOW with ``cant_wait``, what it
    goes in spite of."""
    action: str
    until: Optional[float] = None
    held_by: str = ""
    cant_wait: bool = False


def timing(pulse_store: Any, zone: tzinfo, now: float, *, brief_at: float, closed: Collection[str] = (),
           when: str = ANY, due_end: Optional[float] = None, urgent: bool = False) -> Timing:
    """When a push may go (``Timing``). ``brief_at``: the next morning
    brief; ``closed``: the parts of the day closed for its kind;
    ``due_end``: when it stops being of use, if it's due. Something urgent,
    or due before it would come, goes now."""
    found = night(pulse_store)
    names = found or bounds(pulse_store)
    when = when if when in WHEN else ANY
    target = next_allowed(names, zone, now, when=when, closed=closed, night_on=found is not None)
    if target == now:
        return Timing(NOW)
    name = part(names, zone, now)
    held_by = NIGHT if found is not None and name == NIGHT else f"part:{name}" if name in closed else f"when:{when}"
    # Nothing may go before a night ends: the brief brings it, if it comes soon after.
    if target is None or (found is not None and part(names, zone, target - 60) == NIGHT
                          and brief_at <= target + BRIEF_NEAR_S):
        action, comes = BRIEF, brief_at
    else:
        action, comes = HOLD, target
    if urgent or (due_end is not None and now < due_end <= comes):
        return Timing(NOW, held_by=held_by, cant_wait=True)
    return Timing(action, until=target if action == HOLD else None, held_by=held_by)


def push_cap(pulse_store: Any, zone: tzinfo, now: float, *, brief_at: float, closed: Collection[str] = (),
             due_end: Optional[float] = None) -> str:
    """The loudest a report's pick may come up now, its time being the
    user's pick: "push" when a push may go now (or it can't wait), "brief"
    at night (held for the morning brief), else "screen" (a part of the day
    closed for its kind)."""
    found = timing(pulse_store, zone, now, brief_at=brief_at, closed=closed, due_end=due_end)
    if found.action == NOW:
        return "push"
    return "brief" if part_of(pulse_store, zone, now) == NIGHT else "screen"


def busy(home: Optional[Path], now: float) -> Optional[tuple[str, Optional[float]]]:
    """What the phone says keeps the user busy now, and until when if
    that's known: (EVENT, when the events on now end), (DRIVING, None) or
    (FOCUS, None). None when nothing does, or nothing is shared."""
    if home is None:
        return None
    try:
        from .phone_store import PhoneStore
    except Exception:  # noqa: BLE001 — no phone store in this build
        return None
    phone = PhoneStore(home)
    if not phone.exists():
        return None
    try:
        with phone.reader("events") as opened:
            if opened is not None:
                db, device = opened
                ends = phone.event_ends(db, device["id"], int(now))
                if ends is not None:
                    return EVENT, float(ends)
        with phone.reader("phone") as opened:
            if opened is not None:
                db, device = opened
                state = phone.phone_status(db, device["id"])
                status, focus = state["status"], state["focus"]
                status_at = state["status_at"]
                if isinstance(status, dict) and isinstance(status_at, (int, float)) \
                        and not isinstance(status_at, bool) and 0 <= now - status_at <= DRIVING_FRESH_S and (
                        status.get("carplay") is True or (status.get("activity") == "automotive"
                                                          and status.get("activity_confidence") != "low")):
                    return DRIVING, None
                # The phone sends its Focus as it turns on and as it ends (the
                # filter runs with its defaults then), so the latest is current
                # however old it is: a work or sleep Focus lasts for hours. An
                # end that never came (the phone died or couldn't reach Hermes)
                # holds each push BUSY_MAX_S at most, then it shows without one.
                if isinstance(focus, dict) and focus.get("active") is True and focus.get("hold_calls") is True:
                    return FOCUS, None
    except Exception:  # noqa: BLE001 — unreadable: not busy, as before
        logger.debug("conduit_push: Pulse couldn't read the phone for busy", exc_info=True)
    return None
