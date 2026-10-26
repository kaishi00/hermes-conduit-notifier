"""Quiet times: when Pulse keeps to itself.

Eric (2026-10-25): weekends, a vacation, "quiet for today" from a
notification. Two settings, read in the user's time zone
(pulse.report_zone): ``quiet_days``, the weekdays Pulse is quiet on (Monday
0 to Sunday 6), and ``quiet_until``, when a quiet spell ends (epoch seconds:
the end of today, or the start of the day a vacation ends on).

While it's quiet Pulse sends nothing on its own: no report, no delivery
check, no morning brief, and a follow-up due now is held for the first brief
after instead (a card on the Pulse screen meanwhile, as one below "push").
A report or brief whose time fell while it was quiet is skipped, not sent
late once the quiet ends (at midnight), and a scheduled report on its way
when the quiet begins doesn't go.
Memory keeps reading chats, a Do it check still runs (the user asked for
it), a card put off with Not now still comes back on the screen, and a
report or delivery check asked for by hand still goes.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, tzinfo
from typing import Any, Optional

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# A quiet spell is set at most this far ahead.
MAX_AHEAD_S = 366 * 86400.0
# Why it's quiet: a spell the user set, or a quiet weekday.
UNTIL, DAY = "until", "day"


def days(pulse_store: Any) -> list[int]:
    """The quiet weekdays, Monday 0 to Sunday 6, sorted."""
    value = pulse_store.get("quiet_days")
    return sorted({day for day in value if isinstance(day, int) and not isinstance(day, bool) and 0 <= day < 7}) \
        if isinstance(value, list) else []


def seconds(value: Any) -> Optional[float]:
    """``value`` as epoch seconds, or None: not a number, or out of range."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        at = float(value)
    except OverflowError:
        return None
    return at if math.isfinite(at) else None


def until(pulse_store: Any) -> Optional[float]:
    """When the quiet spell set ends, if one is."""
    return seconds(pulse_store.get("quiet_until"))


def why(pulse_store: Any, zone: tzinfo, now: float) -> str:
    """Why Pulse is quiet now: UNTIL (a spell the user set), DAY (a quiet
    weekday), or "" when it isn't."""
    end = until(pulse_store)
    if end is not None and now < end:
        return UNTIL
    if datetime.fromtimestamp(now, zone).weekday() in days(pulse_store):
        return DAY
    return ""


def ends_at(pulse_store: Any, zone: tzinfo, now: float) -> Optional[float]:
    """When the quiet ends: the spell's end, then the start of the first
    day that isn't a quiet day. None when it isn't quiet, or when every day
    is."""
    reason = why(pulse_store, zone, now)
    if not reason:
        return None
    quiet_days = set(days(pulse_store))
    at = until(pulse_store) if reason == UNTIL else now
    for _ in range(len(DAYS) + 1):
        if datetime.fromtimestamp(at, zone).weekday() not in quiet_days:
            return at
        at = next_midnight(zone, at)
    return None


def next_midnight(zone: tzinfo, now: float) -> float:
    """The start of the next day, local: when "quiet for today" ends."""
    local = datetime.fromtimestamp(now, zone)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def day_start(zone: tzinfo, text: Any) -> Optional[float]:
    """The start of the day ``text`` names (YYYY-MM-DD), local; None for
    anything else."""
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return datetime(day.year, day.month, day.day, tzinfo=zone).timestamp()
