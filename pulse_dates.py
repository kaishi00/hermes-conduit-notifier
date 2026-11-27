"""Dates that come back every year (designs/pulse-dates.md).

A birthday or an anniversary said in a chat is a fact memory keeps with its
month and day (``every_year`` and ``occasion`` on the item, pulse_memory),
and the people picked in Conduit's Settings > Calendar & phone > People send
their birthday (phone_store's contacts). This lists them (``yearly``), works
out when each comes round next (``next_on``: 29 February falls on the 28th
in other years) and which lead time is due today (``coming``):

- a birthday or an anniversary: two weeks ahead (AHEAD, from 14 days out
  until 2, so one Pulse learns of later still gets it), the day before (EVE)
  and on the day (DAY);
- any other yearly date: the day before and on the day.

Each lead time comes up once a year: the look ahead (pulse_ahead) journals
every date it decided at one with its ``ref``, and asks only about the rest.
Code words each one (``line``, ``why``); the look ahead's model can say it
better, and when it can't be asked, code's words go as they are.

A deadline memory kept from the Shortcuts inbox (pulse_memory.read_inbox,
designs/pulse-inbox-deadlines.md) is a date too, once (``deadlines``): it
comes up a week ahead (SOON, from 7 days out until 2) and the day before
(LAST, or on the day when Pulse learns of it then).
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Collection, Optional, Sequence

from . import pulse_store as store

logger = logging.getLogger(__name__)

BIRTHDAY, ANNIVERSARY, OTHER = "birthday", "anniversary", "other"
# The occasions that get two weeks' notice too: time to plan, book or buy.
GIFTS = (BIRTHDAY, ANNIVERSARY)
# A deadline from the Shortcuts inbox: a date that comes once.
DEADLINE = "deadline"
# The lead times, and the days out each is due from and until: a yearly
# date's, then a deadline's.
AHEAD, EVE, DAY = "ahead", "eve", "day"
SOON, LAST = "soon", "last"
STAGES = {AHEAD: (14, 2), EVE: (1, 1), DAY: (0, 0), SOON: (7, 2), LAST: (1, 0)}
STAGE_WORDS = {AHEAD: "two weeks ahead", EVE: "the day before", DAY: "on the day", SOON: "a week ahead",
               LAST: "the day before"}
# How far ahead the look ahead lists dates.
WINDOW_DAYS = 14
# A year before this is a placeholder or a typo, not when it began.
MIN_YEAR = 1900
QUOTE_LIMIT = 160
# "YYYY-MM-DD", "--MM-DD" (a contact's birthday without its year) or "MM-DD" (a fact's).
_DATE = re.compile(r"(?:(\d{4})-|--)?(\d{2})-(\d{2})")


@dataclass(frozen=True)
class Yearly:
    """A date that comes back every year: a picked person's birthday, or a
    fact memory kept."""
    key: str  # "person:<contact id>" or "fact:<item id>"
    occasion: str  # BIRTHDAY, ANNIVERSARY or OTHER
    month: int
    day: int
    year: Optional[int]  # the year it began, when known
    who: str = ""  # a picked person's nickname, else their name
    relation: str = ""  # what they are to the user, as the user typed it ("wife")
    item: Optional[dict] = None  # the fact, for a fact's

    @property
    def what(self) -> str:
        """What it is, for code's words: a person's birthday, or a fact's own text."""
        return f"{self.who}'s birthday" if self.item is None else str(self.item["text"])


@dataclass(frozen=True)
class Deadline:
    """A deadline memory kept from the Shortcuts inbox: the day by which
    something of the user's ends or is due, once."""
    key: str  # "deadline:<item id>"
    on: date
    item: dict  # the deadline
    occasion: str = DEADLINE
    year: Optional[int] = None

    @property
    def what(self) -> str:
        return str(self.item["text"])


@dataclass(frozen=True)
class Coming:
    """A yearly date or a deadline within the look ahead's window."""
    date: Yearly | Deadline
    on: date  # when it next comes round
    days: int  # days from today
    stage: str  # the lead time due today, or ""
    years: Optional[int]  # the age it marks, or how many years, when its year is known

    @property
    def ref(self) -> str:
        """What the journal knows this lead time by, this year; "" with none due."""
        return ref(self.date.key, self.on, self.stage) if self.stage else ""


def ref(key: str, on: date, stage: str) -> str:
    """What the journal knows a date's lead time by (candidates' ``ref``):
    the date, the day it comes round and the lead time, so each comes up
    once a year."""
    return f"date:{key}:{on.isoformat()}:{stage}"


def parse(value: Any) -> Optional[tuple[Optional[int], int, int]]:
    """A yearly date as stored, as (year or None, month, day); None when it
    isn't one. A year before MIN_YEAR, or one that makes 29 February no real
    date, counts as none."""
    found = _DATE.fullmatch(value.strip()) if isinstance(value, str) else None
    if found is None:
        return None
    year, month, day = (int(found[1]) if found[1] else None), int(found[2]), int(found[3])
    if not _real(2000, month, day):  # a leap year: 29 February is a real date
        return None
    if year is not None and (year < MIN_YEAR or not _real(year, month, day)):
        year = None
    return year, month, day


def words(value: Any) -> str:
    """A stored yearly date in words, "3 March", with its year when known
    ("3 March, since 1986"); "" when it isn't one."""
    parsed = parse(value)
    if parsed is None:
        return ""
    year, month, day = parsed
    return f"{day} {calendar.month_name[month]}" + (f", since {year}" if year is not None else "")


def _real(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def on_in(year: int, month: int, day: int) -> date:
    """The day it falls on in ``year``: 29 February on the 28th in other years."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_on(month: int, day: int, today: date) -> date:
    """When it next comes round, today included."""
    this = on_in(today.year, month, day)
    return this if this >= today else on_in(today.year + 1, month, day)


def stages(occasion: str) -> tuple[str, ...]:
    if occasion == DEADLINE:
        return SOON, LAST
    return (AHEAD, EVE, DAY) if occasion in GIFTS else (EVE, DAY)


def stage_on(occasion: str, days: int) -> str:
    """The lead time due ``days`` out, or ""."""
    return next((name for name in stages(occasion) if STAGES[name][1] <= days <= STAGES[name][0]), "")


def people(home: Path) -> list[Yearly]:
    """The picked people's birthdays, from the phone that synced last
    sharing them; none when nothing is shared or the store can't be read."""
    try:
        from .phone_store import store_for as phone_store_for
    except Exception:  # noqa: BLE001 — no phone store in this build
        return []
    phone = phone_store_for(home)[0]
    if not phone.exists():
        return []
    try:
        with phone.reader("contacts") as opened:
            if opened is None:
                return []
            db, device = opened
            found = phone.contacts(db, device["id"])
    except Exception:  # noqa: BLE001 — the look ahead goes without them
        logger.debug("conduit_push: Pulse couldn't read the picked people", exc_info=True)
        return []
    out = []
    for person in found:
        parsed = parse(person.get("birthday"))
        who = str(person.get("nickname") or person.get("name") or "").strip()
        if parsed is None or not who or not person.get("id"):
            continue
        year, month, day = parsed
        out.append(Yearly(key=f"person:{person['id']}", occasion=BIRTHDAY, month=month, day=day, year=year, who=who,
                          relation=str(person.get("relation") or "").strip()))
    return out


def facts(pulse_store: store.PulseStore, profile: Optional[str] = None, without: Sequence[str] = ()) -> list[Yearly]:
    """The yearly dates memory kept (open facts with ``every_year``), of
    ``profile`` ("" for this one) or every profile's (None) but the ones
    named in ``without``."""
    out = []
    for item in pulse_store.yearly_facts(profile=profile, without=without):
        parsed = parse(item["every_year"])
        if parsed is None:
            continue
        year, month, day = parsed
        occasion = item["occasion"] if item["occasion"] in (BIRTHDAY, ANNIVERSARY) else OTHER
        out.append(Yearly(key=f"fact:{item['id']}", occasion=occasion, month=month, day=day, year=year, item=item))
    return out


def yearly(pulse_store: store.PulseStore, home: Path) -> list[Yearly]:
    """Every yearly date Pulse knows: the picked people's birthdays, then
    the facts from the profiles Pulse reads (`report_profiles`: this one
    only, or every profile but the ones left out). A birthday fact on the
    same day as a picked person's birthday is that person's, so it counts
    once."""
    picked = people(home)
    birthdays = {(person.month, person.day) for person in picked}
    every = pulse_store.get("report_profiles") == "all"
    kept = facts(pulse_store, None if every else "", pulse_store.excluded_profiles() if every else ())
    return picked + [fact for fact in kept
                     if not (fact.occasion == BIRTHDAY and (fact.month, fact.day) in birthdays)]


def deadlines(pulse_store: store.PulseStore) -> list[Deadline]:
    """The open deadlines memory kept from the Shortcuts inbox, each on its day."""
    out = []
    for item in pulse_store.deadlines():
        try:
            on = date.fromisoformat(str(item["due"])[:10])
        except ValueError:
            continue
        out.append(Deadline(key=f"deadline:{item['id']}", on=on, item=item))
    return out


def coming(dates: Sequence[Yearly | Deadline], today: date, days: int = WINDOW_DAYS) -> list[Coming]:
    """The dates that come round within ``days`` of today (today included),
    soonest first, each with the lead time due today, if any. A deadline
    whose day has passed comes round no more."""
    out = []
    for found in dates:
        on = found.on if isinstance(found, Deadline) else next_on(found.month, found.day, today)
        ahead = (on - today).days
        if ahead > days or ahead < 0:
            continue
        years = on.year - found.year if found.year is not None and on.year > found.year else None
        out.append(Coming(found, on, ahead, stage_on(found.occasion, ahead), years))
    return sorted(out, key=lambda entry: (entry.on, entry.date.key))


def day_words(on: date) -> str:
    """"Saturday 8 November"."""
    return f"{on:%A} {on.day} {on:%B}"


def years_words(entry: Coming) -> str:
    """" (turning 40)" for a birthday, " (10 years)" for the rest, when its
    year is known; else ""."""
    if entry.years is None:
        return ""
    if entry.date.occasion == BIRTHDAY:
        return f" (turning {entry.years})"
    return f" ({entry.years} year{'s' if entry.years != 1 else ''})"


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def line(entry: Coming) -> str:
    """Code's words for a date at its lead time, written to the user: what
    and when, and the years when known. A fact's are the user's own words;
    a deadline's, what memory noted, with its time when it has one."""
    if isinstance(entry.date, Deadline):
        when = {0: "today", 1: f"tomorrow, {day_words(entry.on)}"}.get(entry.days, f"on {day_words(entry.on)}")
        due = str(entry.date.item.get("due") or "")
        at = f" at {due[11:16]}" if "T" in due else ""
        return f"{_clip(entry.date.what, QUOTE_LIMIT)} {when}{at}."
    when = {DAY: "today", EVE: f"tomorrow, {day_words(entry.on)}"}.get(entry.stage, f"on {day_words(entry.on)}")
    if entry.date.item is None:
        verb = "is" if entry.stage in (DAY, EVE) else "is coming up"
        return f"{entry.date.what} {verb} {when}{years_words(entry)}."
    said = _clip(entry.date.item["evidence"], QUOTE_LIMIT)
    return f"Coming up {when}{years_words(entry)}, as you said: “{said}”"


def why(entry: Coming, said_on: str = "") -> str:
    """Code's Why? for a date at its lead time: where Pulse knows it from,
    and its lead times. ``said_on``: when a fact was said, or a deadline
    logged, as words."""
    if isinstance(entry.date, Deadline):
        sender = _clip(entry.date.item.get("sender"), QUOTE_LIMIT)
        where = (f"From {sender}, logged" if sender else "Logged") + " on your phone" \
            + (f" on {said_on}" if said_on else "") + "."
        return f"{where} Pulse brings up deadlines from your Shortcuts inbox a week ahead and the day before."
    if entry.date.item is None:
        who = entry.date.who + (f" ({entry.date.relation})" if entry.date.relation else "")
        where = f"{who} is one of the people you picked in Settings."
    else:
        where = f"You said it{f' on {said_on}' if said_on else ''}."
    if entry.date.occasion in GIFTS:
        return f"{where} Pulse brings up birthdays and anniversaries two weeks ahead, the day before and on the day."
    return f"{where} Pulse brings up dates that come back every year the day before and on the day."


def next_due(found: Yearly | Deadline, today: date, decided: Collection[str] = ()) -> Optional[tuple[date, str]]:
    """When a date next comes up and at which lead time (today, if one is
    due now), past the lead times the look ahead already decided this time
    round (``decided``: their refs); for `pulse dates` and `pulse inbox`.
    None for a deadline with none left."""
    if isinstance(found, Deadline):
        for name in stages(DEADLINE):
            first, last = STAGES[name]
            if today <= found.on - timedelta(days=last) and ref(found.key, found.on, name) not in decided:
                return max(found.on - timedelta(days=first), today), name
        return None
    on = next_on(found.month, found.day, today)
    for name in stages(found.occasion):
        first, last = STAGES[name]
        if today <= on - timedelta(days=last) and ref(found.key, on, name) not in decided:
            return max(on - timedelta(days=first), today), name
    # Every lead time decided, the day's too: next year's first.
    following = next_on(found.month, found.day, on + timedelta(days=1))
    name = stages(found.occasion)[0]
    return following - timedelta(days=STAGES[name][0]), name


def refs_round(found: Yearly | Deadline, today: date) -> list[str]:
    """The refs of a date's lead times this time round, for ``next_due``'s
    ``decided``."""
    on = found.on if isinstance(found, Deadline) else next_on(found.month, found.day, today)
    return [ref(found.key, on, name) for name in stages(found.occasion)]


def local_today(zone: tzinfo, now: float) -> date:
    return datetime.fromtimestamp(now, zone).date()


def _offset_moment(value: Any) -> Optional[datetime]:
    """An ISO moment with a time and an offset ("Z" too); None for a date
    alone, a floating time with no offset, or anything else."""
    text = value.strip() if isinstance(value, str) else ""
    if "T" not in text:
        return None
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def in_zone(value: Any, zone: Optional[tzinfo]) -> Optional[datetime]:
    """An ISO moment that carries an offset, as the same instant in ``zone``.

    The phone keeps an event's start in the zone the event was made in (a
    booking site's or an organizer's), not the phone's, so printing it as it
    is reads hours off from what the calendar shows. None when there is no
    zone, or the value is a date alone or a floating time with no offset:
    those mean the same wall clock everywhere, so they are left as they are.
    """
    moment = _offset_moment(value) if zone is not None else None
    return moment.astimezone(zone) if moment is not None else None


def event_day(event: dict, zone: Optional[tzinfo]) -> str:
    """The day ("2026-11-27") a calendar entry starts on in ``zone``: an early
    hour in Tokyo is the evening before in Paris. An all-day entry is a date,
    the same day wherever it's read (the phone may keep it as midnight with an
    offset); so is a floating time, or one read with no zone: as written."""
    start = event.get("start")
    moved = None if event.get("all_day") else in_zone(start, zone)
    return moved.date().isoformat() if moved is not None else str(start or "").strip()[:10]


def elsewhere(value: Any, moved: Optional[datetime], event_zone: Any) -> str:
    """Where an event was made, when its own clock (``value``, as the phone
    keeps it) differs from ``moved`` (the same instant in the reader's zone),
    so a converted time never leaves its reader guessing: the event's zone
    name, else its offset ("UTC+09:00"). Empty when the clocks agree."""
    original = _offset_moment(value)
    if original is None or moved is None or original.utcoffset() == moved.utcoffset():
        return ""
    name = event_zone.strip() if isinstance(event_zone, str) else ""
    if name:
        return name
    minutes = round(original.utcoffset().total_seconds() / 60)  # an offset with seconds rounds to its minute
    return f"UTC{'-' if minutes < 0 else '+'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
