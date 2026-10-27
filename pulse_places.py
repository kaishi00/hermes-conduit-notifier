"""Pulse knows where you are (designs/pulse-where-you-are.md).

With Where you are on (Settings > Pulse, `pulse where on`, PUT /pulse's
"where") and background sharing on in Conduit's Location settings, Pulse
reads the location Conduit shares with this profile (location_store):

- Its look-ups (pulse_tools) may read conduit_location (now, places,
  history, distance_to) and ask Maps from "me" or a saved place. The tool
  logs each read in Location's read log, under the Pulse chat, as it logs
  every read.
- An arrival at or departure from a saved place (never a private one) that
  the location store passes on as it takes the phone's batch is a moment.
  Pulse keeps it a day, and on its next tick makes due what waits for it: a
  commitment or promise memory kept for that place ("next time I'm at
  Costco", "when I leave work"). Its follow-up is checked then, with the
  moment, and its reminder goes at once (pulse_followup), unless it's
  night, a quiet day, a part of the day the user said is the wrong time for
  follow-ups, or the user is in an event or a Focus that holds calls: then
  it waits for the next time.

What Pulse remembers is looked up by what's happening (``Memories``): an
item that shares a name (a person, a place) or a rare word with one of
today's timed events comes with the morning brief ("lunch with Dan at 1: he
started the new job on Monday"), and one that shares one with a place joins
the check of a reminder made due there. A moment with nothing waiting for it
says nothing yet: what it would have brought up is kept with it, for `pulse
where`, to judge before it speaks.

A place's words match a saved place by its name ("Costco" matches "Costco
Wholesale") or its kind ("home", "work" or "the office", "the gym",
"school"). Words that match no saved place are kept, and match one saved
later.

Off, Pulse reads nothing of where the user is and keeps no moments; the
place words memory kept on an item stay, like the rest of its memory.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, time as clock, tzinfo
from pathlib import Path
from typing import Any, Callable, Collection, Optional, Sequence

from . import pulse_followup as followup
from . import pulse_hours as hours
from . import pulse_learning as learning
from . import pulse_memory as memory
from . import pulse_quiet as quiet
from . import pulse_report as report
from . import pulse_store as store

logger = logging.getLogger(__name__)

# A moment handled later than this after it happened is too late to act on
# (the location store passes on only those under 15 minutes old).
FRESH_S = 20 * 60.0
# A place's kind, by the words people use for it. Only "the" and "a" are
# left out before matching, so "her place" isn't the user's home.
KIND_WORDS = {
    "home": ("home", "my place", "my house", "house", "my flat", "my apartment"),
    "work": ("work", "office", "my office", "workplace", "my work"),
    "gym": ("gym", "my gym"),
    "school": ("school", "my school"),
}
# Words a place's name match leaves out.
_SKIP = frozenset({"the", "a", "an", "my", "our", "at", "to", "in", "on", "of", "and", "&"})
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "`": "'"})
# What a saved place's kind is called, to look up what Pulse remembers about it.
KIND_KEYS = {"home": ("home", "house", "flat", "apartment"), "work": ("work", "office", "workplace"),
             "gym": ("gym",), "school": ("school",)}
# What a morning brief or a moment brings along at most.
MAX_RELATED = 5
# A word in this share of what Pulse remembers (and in RARE_MIN items at
# least) is too common to relate an item by itself, unless it's a name.
RARE_SHARE = 0.1
RARE_MIN = 5
MAX_EVENTS = 40
_KEY = re.compile(r"[^\W\d_][^\W_]{2,}")
_LETTERS = re.compile(r"[^\W\d_]+")
# Words of three letters or more that name nothing: Hermes is in every
# promise, and dates and meetings in most items and events. Stemmed, so
# their other forms go too ("Mondays"; memory's stem takes one ending, so
# "meetings" is listed).
_COMMON = frozenset(memory.stem(word) for word in (
    "the", "and", "for", "with", "from", "into", "onto", "about", "this", "that", "these", "those", "your", "you",
    "our", "ours", "their", "them", "they", "his", "her", "hers", "its", "are", "was", "were", "will", "would",
    "can", "could", "should", "may", "might", "must", "not", "but", "all", "any", "some", "out", "off", "over",
    "under", "then", "than", "when", "what", "who", "how", "why", "where", "there", "here", "has", "have", "had",
    "get", "got", "via", "per", "new", "next", "last", "mine", "user", "hermes",
    "day", "today", "tomorrow", "tonight", "week", "weekend", "month", "year", "morning", "mornings", "afternoon",
    "evening", "evenings", "night", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
    "december", "meeting", "meetings", "call", "sync", "standup", "appointment", "event"))
# Why a moment made nothing due (pulse where).
SKIPPED_WORDS = {"off": "Where you are was off", "late": "it reached Pulse too late",
                 "followups_off": "follow-ups or Pulse memory were off", "brief": "follow-ups go only in your brief",
                 "quiet": "it was a quiet day", "night": "it was night",
                 "on_its_way": "what waited for it was already on its way",
                 hours.EVENT: "your calendar was busy", hours.FOCUS: "a Focus held calls"}


def on(pulse_store: Any) -> bool:
    """Whether Where you are is on: off unless the user turned it on."""
    return pulse_store.get(store.WHERE_SETTING) is True


def view(home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """What the location store this profile reads holds now (its own, or
    the one another profile shares with it), or None when nothing is
    shared or it can't be read."""
    try:
        from hermes_constants import get_hermes_home

        from . import location_profiles
        from .location_store import LocationStore
        location, owner = location_profiles.store_for(home if home is not None else get_hermes_home(), LocationStore)
        # Never another profile's write lock, as the tool reads it.
        return location.snapshot(upgrade=owner is None) if location.exists() else None
    except Exception:  # noqa: BLE001 — unreadable: nothing shared, as far as Pulse goes
        logger.debug("conduit_push: Pulse couldn't read the location store", exc_info=True)
        return None


def homes(home: Path) -> list[Path]:
    """Whose Pulse hears the arrivals and departures this home's location
    store takes: its own, and that of each other profile on this host that
    reads it ("Share with this host's other profiles",
    location_profiles.store_for), as their look-ups do."""
    from . import location_profiles
    from .location_store import LocationStore

    own = LocationStore(home).path.resolve()
    found = [Path(home)]
    for _, path in location_profiles.profile_homes(Path(home)):
        try:
            location, owner = location_profiles.store_for(path, LocationStore)
            if owner is not None and location.path.resolve() == own:
                found.append(path)
        except Exception:  # noqa: BLE001 — that profile hears nothing this time
            logger.debug("conduit_push: Pulse couldn't tell whether a profile reads this location", exc_info=True)
    return found


def background(found: Optional[dict[str, Any]], now: float) -> bool:
    """Whether ``found`` (``view``) has background sharing on and not paused."""
    from .location_store import sharing_paused

    if not found:
        return False
    sharing = found.get("sharing") or {}
    return bool(sharing.get("background")) and not sharing_paused(sharing, now)


def reads(pulse_store: Any, now: float, home: Optional[Path] = None) -> bool:
    """Whether Pulse's look-ups may read where the user is: Where you are on
    and the phone sharing in the background."""
    return on(pulse_store) and background(view(home), now)


# Words to saved places -------------------------------------------------------------

def _phrase(text: Any) -> str:
    """Lower case, curly quotes made straight, "the" and "a" left out."""
    words = _WORD.findall(str(text or "").translate(_QUOTES).casefold())
    return " ".join(word for word in words if word not in ("the", "a", "an"))


def _tokens(text: Any) -> frozenset:
    """A name's words, possessives without their 's, leaving out _SKIP."""
    words = (re.sub(r"'s?$", "", word) for word in _WORD.findall(str(text or "").translate(_QUOTES).casefold()))
    return frozenset(word for word in words if word and word not in _SKIP)


def matcher(place: dict[str, Any]) -> Callable[[Any], bool]:
    """Whether the place words said for an item name ``place`` ({"name",
    "kind"}): its name, its kind's words ("the office" for work), or a name
    whose words all appear in the other ("Costco" and "Costco Wholesale").
    A kind's words name only a place of that kind, or one named just that:
    "home" isn't Home Depot. The place's own words are read once, for a
    moment's many items."""
    name, theirs = _phrase(place.get("name")), _tokens(place.get("name"))
    own = KIND_WORDS.get(str(place.get("kind") or ""), ())

    def match(words: Any) -> bool:
        said = _phrase(words)
        if not said:
            return False
        if said == name or said in own:
            return True
        if any(said in kind_words for kind_words in KIND_WORDS.values()):
            return False
        mine = _tokens(words)
        return bool(mine) and bool(theirs) and (mine <= theirs or theirs <= mine)

    return match


def matches(words: Any, place: dict[str, Any]) -> bool:
    """Whether ``words`` name ``place`` (``matcher``)."""
    return matcher(place)(words)


def saved(found: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The saved places in ``found`` (``view``) Pulse may name: never a private one."""
    places = (found or {}).get("places") or {}
    return [place for place in places.values() if isinstance(place, dict) and not place.get("private")]


def place_for(words: Any, places: Sequence[dict[str, Any]]) -> Optional[str]:
    """The name of the first saved place the words match, or None."""
    return next((str(place.get("name") or "") for place in places if matches(words, place)), None)


def waiting(pulse_store: Any, event: str = "") -> list[dict]:
    """Open items that wait for a place (pulse_followup.waits_for_place),
    for ``event`` ("arrive" or "leave") when given."""
    return [item for kind in followup.PLACE_KINDS for item in pulse_store.open_items(kind)
            if followup.waits_for_place(item) and (not event or (item.get("place_on") or "arrive") == event)]


# What Pulse remembers about what's happening -----------------------------------------

def key_words(text: Any) -> frozenset:
    """The words of three letters or more in ``text`` that name something,
    as memory stems them (pulse_memory.stem), so "Dan's" meets "Dan"."""
    words = (memory.stem(word) for word in _KEY.findall(str(text or "").translate(_QUOTES).casefold()))
    return frozenset(word for word in words if word not in _COMMON)


def title_names(title: Any) -> frozenset:
    """The names in an event's title: its capitalized words after the
    first ("Lunch with Dan", "Dinner at Mum's"), when a word in lower case
    shows it isn't written in Title Case ("Design Review" names nothing)."""
    words = _LETTERS.findall(str(title or "").translate(_QUOTES))
    if not any(word[0].islower() for word in words):
        return frozenset()
    return key_words(" ".join(word for word in words[1:] if word[0].isupper()))


def event_keys(event: dict) -> tuple[frozenset, frozenset]:
    """What a calendar event is about: the words of its title and location,
    and of these the names (``Memories.related``): the people in it, when
    the phone shares them, and the names in its title."""
    people = " ".join(str(person.get("name") or "") for person in event.get("attendees") or []
                      if isinstance(person, dict))
    names = key_words(people) | title_names(event.get("title"))
    return key_words(f"{event.get('title') or ''} {event.get('location') or ''}") | names, names


_KIND_KEYS = key_words(" ".join(word for words in KIND_KEYS.values() for word in words))


def place_keys(name: Any, kind: Any) -> tuple[frozenset, frozenset]:
    """What a saved place is called: its name's words and its kind's, and
    of these the names (``Memories.related``): its name's words but a
    kind's ("Mum's": mum; "Home": none)."""
    names = key_words(name) - _KIND_KEYS
    return key_words(" ".join(KIND_KEYS.get(str(kind or ""), ()))) | names, names


class Memories:
    """What Pulse remembers, each item's words read once (``key_words``), to
    look up what relates to an event or a place."""

    def __init__(self, items: Sequence[dict]) -> None:
        self.items = [(item, key_words(f"{item['text']} {item['evidence']}")) for item in items]
        self.counts = Counter(word for _, words in self.items for word in words)
        self.rare = max(RARE_MIN, int(len(self.items) * RARE_SHARE))

    def related(self, keys: Collection[str], names: Collection[str] = (), *, exclude: Collection[int] = (),
                limit: int = MAX_RELATED) -> list[dict]:
        """The items sharing a name with ``keys`` (``names``: a person, a
        place), or a word rare among what Pulse remembers, so a word many
        items share ("lunch", "review") relates nothing by itself, but Dan
        does however often he comes up. The rarest words shared first (as
        a search engine weighs them), then the most recently mentioned."""
        keys, names = frozenset(keys), frozenset(names)
        found = []
        for item, words in self.items:
            shared = words & keys
            if item["id"] not in exclude and (shared & names or any(self.counts[word] < self.rare for word in shared)):
                weight = sum(math.log(len(self.items) / self.counts[word]) for word in shared)
                found.append((weight, item))
        found.sort(key=lambda pair: (-pair[0], -pair[1]["mentioned_at"], -pair[1]["id"]))
        return [item for _, item in found[:limit]]


def memories(pulse_store: Any, now: float) -> Memories:
    """What a brief or a moment may bring along: the open items, facts and
    interests among them, but those the report leaves out for now: rated
    "no", put away lately (pulse_report.remembered_items), or past their
    date."""
    rated = report.rated_not_useful(pulse_store)
    put_away = pulse_store.resting_items(now, report.DISMISS_REST_S)
    return Memories([item for item in pulse_store.open_items(limit=memory.MAX_OPEN, stale_before=now - memory.EXPIRE_S)
                     if item["mentioned_at"] > max(rated.get(item["id"], -1.0), put_away.get(item["id"], -1.0))])


def today_events(home: Path, zone: tzinfo, now: float) -> list[dict]:
    """The timed events on the phone's calendar from now to the end of the
    day, soonest first: not all-day, cancelled or declined ones. None when
    Conduit shares no calendar or it can't be read."""
    try:
        from .phone_store import PhoneStore

        phone = PhoneStore(home)
        if not phone.exists():
            return []
        end = datetime.combine(datetime.fromtimestamp(now, zone).date(), clock.max, tzinfo=zone).timestamp()
        with phone.reader("events") as opened:
            if opened is None:
                return []
            db, device = opened
            found, _ = phone.events(db, device["id"], int(now), int(end), limit=MAX_EVENTS)
    except Exception:  # noqa: BLE001 — the brief goes without them
        logger.debug("conduit_push: Pulse couldn't read today's events", exc_info=True)
        return []
    return [event for event in found if not event.get("all_day") and event.get("status") != "canceled"
            and event.get("my_status") != "declined"]


RELATED_TITLE = "## Related to today's events (what Pulse remembers that shares a name or a rare word with one)"


def today_related(pulse_store: Any, home: Path, zone: tzinfo, now: float) -> str:
    """The brief's "Related to today's events": for each timed event left
    today, soonest first, what Pulse remembers that relates to it
    (``Memories.related``), each item once, at most MAX_RELATED in all; ""
    when nothing does."""
    events = today_events(home, zone, now)
    if not events:
        return ""
    found, lines, taken = memories(pulse_store, now), [], set()
    for event in events:
        for item in found.related(*event_keys(event), exclude=taken, limit=MAX_RELATED - len(lines)):
            taken.add(item["id"])
            said = datetime.fromtimestamp(float(item["valid_from"]), zone)
            lines.append(f"- {_event_when(event, zone)} {_clip(event.get('title'), 80) or 'Untitled'}: "
                         f"#{item['id']} [{item['kind']}] {_clip(item['text'], 200)} "
                         f"({memory.said_where(item)} {said.day} {said:%b}: \"{_clip(item['evidence'], 120)}\")")
        if len(lines) >= MAX_RELATED:
            break
    return RELATED_TITLE + "\n" + "\n".join(lines) if lines else ""


def _event_when(event: dict, zone: tzinfo) -> str:
    """An event's start in ``zone`` ("13:00"), or "" when it can't be read."""
    text = str(event.get("start") or "").strip()
    try:
        start = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return ""
    return (start.astimezone(zone) if start.tzinfo else start).strftime("%H:%M")


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# Moments ---------------------------------------------------------------------------

def arrived(pulse_store: Any, moments: Any, now: float) -> bool:
    """The location store took arrivals or departures (``moments``, its
    ingest's): kept for Pulse's next tick while Pulse and Where you are are
    on. True when any was new, so the caller wakes Pulse."""
    if not pulse_store.get("enabled") or not on(pulse_store):
        return False
    usable = [moment for moment in moments if isinstance(moment, dict) and moment.get("on") in ("arrive", "leave")]
    if not usable:
        return False
    # Kept a day, even while the tick that also prunes them isn't running.
    pulse_store.prune_moments(now - store.MOMENTS_KEEP_S)
    return bool(pulse_store.add_moments(usable, now))


def take_moments(pulse_store: Any, home: Path, zone: tzinfo, now: float) -> list[int]:
    """Handles the moments not handled yet, oldest first: each makes due
    the items waiting for its place, unless it came too late or Pulse may
    not speak up now. The ids of the items it made due; when there are any,
    memory's next run checks them first (``memory_requested``)."""
    pulse_store.prune_moments(now - store.MOMENTS_KEEP_S)
    due: list[int] = []
    found: Optional[Memories] = None
    for moment in pulse_store.new_moments():
        if found is None and pulse_store.get("memory"):
            found = memories(pulse_store, now)  # read once for this tick's moments
        outcome = handle(pulse_store, home, zone, moment, now, found)
        if pulse_store.moment_handled(moment["id"], outcome, now):
            due.extend(outcome.get("due", []))
    if due:
        pulse_store.set("memory_requested", True)
    return due


def handle(pulse_store: Any, home: Path, zone: tzinfo, moment: dict, now: float,
           found: Optional[Memories] = None) -> dict[str, Any]:
    """What a moment does: {"due": [item ids]}, or {"skipped": why, "items":
    [the ids it would have made due]} (SKIPPED_WORDS); with "related", what
    Pulse remembers about the place (``found``, ``Memories.related``),
    which joins the checks of the items it made due. With none made due,
    nothing says it yet: it's kept for `pulse where`, to judge first."""
    if not on(pulse_store):
        return {"skipped": "off"}
    if now - float(moment["at"]) > FRESH_S:
        return {"skipped": "late"}
    match = matcher({"name": moment["place"], "kind": moment["kind"]})
    items = [item for item in waiting(pulse_store, moment["event"]) if match(item["place_words"])]
    ids = [item["id"] for item in items]
    # What waits for a place, here or elsewhere, has its own moment.
    related = [item["id"] for item in found.related(*place_keys(moment["place"], moment["kind"]),
                                                    exclude={item["id"] for item in waiting(pulse_store)})] \
        if found is not None else []
    extra = {"related": related} if related else {}
    if not items:
        return {"due": [], **extra}
    level = learning.level(pulse_store, followup.FOLLOWUP)
    if not pulse_store.get("followups") or not pulse_store.get("memory") or level == "off":
        return {"skipped": "followups_off", "items": ids, **extra}
    if level == "brief":
        # The user keeps follow-ups for the brief, which comes after they've left.
        return {"skipped": "brief", "items": ids, **extra}
    why = not_now(pulse_store, home, zone, now)
    if why:
        return {"skipped": why, "items": ids, **extra}
    made = [item["id"] for item in items if followup.place_reached(pulse_store, item, moment, now, related=related)]
    if not made:
        return {"skipped": "on_its_way", "items": ids, **extra}
    return {"due": made, **extra}


def not_now(pulse_store: Any, home: Path, zone: tzinfo, now: float) -> str:
    """Why a moment's reminders may not go now ("" when they may): a quiet
    day, the night, a part of the day "Wrong time" closed for follow-ups,
    or an event or a Focus that holds calls. Driving doesn't count: getting
    somewhere ends a drive, and leaving starts one."""
    if quiet.why(pulse_store, zone, now):
        return "quiet"
    if hours.is_night(pulse_store, zone, now):
        return "night"
    name = hours.part_of(pulse_store, zone, now)
    if name in learning.closed_parts(pulse_store, followup.FOLLOWUP):
        return f"part:{name}"
    found = hours.busy(home, now)
    if found is not None and found[0] != hours.DRIVING:
        return found[0]
    return ""


def describe(moment: dict, zone: tzinfo, texts: dict[int, str]) -> str:
    """A moment as `pulse where` lists it. ``texts``: items' text by id."""
    from datetime import datetime

    when = datetime.fromtimestamp(float(moment["at"]), zone).strftime("%a %d %b %H:%M")
    verb = "Arrived at" if moment["event"] == "arrive" else "Left"
    try:
        outcome = json.loads(moment.get("outcome") or "{}")
    except ValueError:
        outcome = {}
    outcome = outcome if isinstance(outcome, dict) else {}

    def named(ids: Sequence[int]) -> str:
        return "; ".join(texts.get(item_id, f"#{item_id}") for item_id in ids)

    related = [item_id for item_id in outcome.get("related") or [] if isinstance(item_id, int)]
    if moment.get("handled_at") is None:
        what = "not handled yet"
    elif outcome.get("due"):
        what = f"made due: {named(outcome['due'])}" + (f" (its check also read: {named(related)})" if related else "")
        related = []
    elif outcome.get("skipped"):
        skipped = str(outcome["skipped"])
        reason = SKIPPED_WORDS.get(skipped) or (f"you said {skipped.partition(':')[2]} was the wrong time"
                                                if skipped.startswith("part:") else skipped)
        what = f"nothing, as {reason}" + (f" (waiting: {named(outcome['items'])})" if outcome.get("items") else "")
    else:
        what = "nothing waited for it"
    # Not said yet: what it would have brought up, to judge before it speaks.
    what += f"; it would have brought up: {named(related)}" if related else ""
    return f"- {when} {verb} {moment['place']}: {what}"
