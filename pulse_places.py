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
import re
from datetime import tzinfo
from pathlib import Path
from typing import Any, Optional, Sequence

from . import pulse_followup as followup
from . import pulse_hours as hours
from . import pulse_learning as learning
from . import pulse_quiet as quiet
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
# Why a moment made nothing due (pulse where).
SKIPPED_WORDS = {"off": "Where you are was off", "late": "it reached Pulse too late",
                 "followups_off": "follow-ups were off", "brief": "follow-ups go only in your brief",
                 "quiet": "it was a quiet day", "night": "it was night",
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


def matches(words: Any, place: dict[str, Any]) -> bool:
    """Whether the place words said for an item name ``place`` ({"name",
    "kind"}): its name, its kind's words ("the office" for work), or a name
    whose words all appear in the other ("Costco" and "Costco Wholesale").
    A kind's words name only a place of that kind, or one named just that:
    "home" isn't Home Depot."""
    said = _phrase(words)
    if not said:
        return False
    if said == _phrase(place.get("name")) or said in KIND_WORDS.get(str(place.get("kind") or ""), ()):
        return True
    if any(said in kind_words for kind_words in KIND_WORDS.values()):
        return False
    mine, theirs = _tokens(words), _tokens(place.get("name"))
    return bool(mine) and bool(theirs) and (mine <= theirs or theirs <= mine)


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


# Moments ---------------------------------------------------------------------------

def arrived(pulse_store: Any, moments: Any, now: float) -> bool:
    """The location store took arrivals or departures (``moments``, its
    ingest's): kept for Pulse's next tick while Pulse and Where you are are
    on. True when any was new, so the caller wakes Pulse."""
    if not pulse_store.get("enabled") or not on(pulse_store):
        return False
    usable = [moment for moment in moments if isinstance(moment, dict) and moment.get("on") in ("arrive", "leave")]
    return bool(usable and pulse_store.add_moments(usable, now))


def take_moments(pulse_store: Any, home: Path, zone: tzinfo, now: float) -> list[int]:
    """Handles the moments not handled yet, oldest first: each makes due
    the items waiting for its place, unless it came too late or Pulse may
    not speak up now. The ids of the items it made due; when there are any,
    memory's next run checks them first (``memory_requested``)."""
    pulse_store.prune_moments(now - store.MOMENTS_KEEP_S)
    due: list[int] = []
    for moment in pulse_store.new_moments():
        outcome = handle(pulse_store, home, zone, moment, now)
        if pulse_store.moment_handled(moment["id"], outcome, now):
            due.extend(outcome.get("due", []))
    if due:
        pulse_store.set("memory_requested", True)
    return due


def handle(pulse_store: Any, home: Path, zone: tzinfo, moment: dict, now: float) -> dict[str, Any]:
    """What a moment does: {"due": [item ids]}, or {"skipped": why, "items":
    [the ids it would have made due]} (SKIPPED_WORDS)."""
    if not on(pulse_store):
        return {"skipped": "off"}
    if now - float(moment["at"]) > FRESH_S:
        return {"skipped": "late"}
    place = {"name": moment["place"], "kind": moment["kind"]}
    items = [item for item in waiting(pulse_store, moment["event"]) if matches(item["place_words"], place)]
    if not items:
        return {"due": []}
    ids = [item["id"] for item in items]
    level = learning.level(pulse_store, followup.FOLLOWUP)
    if not pulse_store.get("followups") or not pulse_store.get("memory") or level == "off":
        return {"skipped": "followups_off", "items": ids}
    if level == "brief":
        # The user keeps follow-ups for the brief, which comes after they've left.
        return {"skipped": "brief", "items": ids}
    why = not_now(pulse_store, home, zone, now)
    if why:
        return {"skipped": why, "items": ids}
    made = [item["id"] for item in items if followup.place_reached(pulse_store, item, moment, now)]
    return {"due": made}


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
    named = lambda ids: "; ".join(texts.get(item_id, f"#{item_id}") for item_id in ids)
    if moment.get("handled_at") is None:
        what = "not handled yet"
    elif outcome.get("due"):
        what = f"made due: {named(outcome['due'])}"
    elif outcome.get("skipped"):
        skipped = str(outcome["skipped"])
        reason = SKIPPED_WORDS.get(skipped) or (f"you said {skipped.partition(':')[2]} was the wrong time"
                                                if skipped.startswith("part:") else skipped)
        what = f"nothing, as {reason}" + (f" (waiting: {named(outcome['items'])})" if outcome.get("items") else "")
    else:
        what = "nothing waited for it"
    return f"- {when} {verb} {moment['place']}: {what}"
