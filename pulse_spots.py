"""Walking past (designs/pulse-walking-past.md).

With Where you are on (pulse_places) and walking past on (`pulse spots
on|off`, PUT /pulse's "spots", on by default), Pulse asks the phone to watch
a few kinds of shop, never coordinates:

- The daily look ahead may hold a pick for a kind of shop instead of a saved
  place ("moment": {"spot": "a florist"}): flowers before an anniversary,
  a card, a pharmacy. Its card waits off the Pulse screen as one held for a
  saved place does (pulse_places), for "spot:" and the search words.
- An item memory kept for a place that matches no saved place ("next time
  I'm at Costco") is watched by its words.

The phone hears the watches in the sealed reply to the batches it already
sends (``watches``, location_store.ingest's ``reply``): an arrival, a
departure, a visit or opening the app is enough, with the app closed. It
searches Apple Maps for each near where it is, watches the nearest few
(saved places come first in iOS's 20) until ``until``, and tells the host
when the user comes within one's circle, in the next batch
("spot_entries").

Each watch carries its intent (INTENTS) and circle (``radius``): a size for
walking and one for driving, the phone using whichever fits how the user
moves now. What it optimises for is a useful chance to act, not the shop's
edge:

- an opportunity (a look ahead pick, by default) is broad, 750 m walking
  and 3 km driving: worth a detour, so any entry counts;
- an errand (an item kept for a place, by default) is narrow, 100 m: the
  fix taken on entry has to be inside the circle;
- an arrival is narrow and confirmed: the fix has to be inside it with its
  whole accuracy, because being there is what matters.

Even then a small circle says the user is at or right by the shop, never
that they went in, and what Pulse says doesn't claim more. The look ahead
may give a pick another intent and its own circle ("moment": {"spot": ...,
"intent": "errand", "radius_m": 300}), within that intent's bounds
(BOUNDS).

Each entry that counts becomes a moment (pulse_places) at that shop: it brings up the
pick held for its kind, or makes due what was said for it, as getting to a
saved place does, with the same checks (the night, a quiet day, busy). And
each is kept 30 days with how late iOS told the app (``lateness``): how far
inside the circle the phone's fix was, over its speed. `pulse spots` lists
them.

Off, the phone watches nothing (the reply lists none) and what was kept goes
with the moments.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional, Sequence

from . import pulse_followup as followup
from . import pulse_places as places
from . import pulse_store as store

logger = logging.getLogger(__name__)

SETTING = "spots"
# A moment's kind and its place id's prefix, for a shop walked into.
KIND = "spot"
PREFIX = "spot:"
# The search words for a kind of shop: short, so they read in a sentence and
# search as words.
QUERY_CHARS = 40
QUERY_WORDS = 5
# At most this many watches go to the phone at once (it watches the nearest
# few of each, within what iOS's 20 leaves after saved places).
MAX_WATCHES = 6
# An item's watch lasts a day at a time: each batch's reply renews it.
ITEM_WATCH_S = 86400.0
# How long a shop walked into is kept, with its timing.
KEEP_S = 30 * 86400.0
# How long ago the phone last said how many it could watch, for `pulse spots`.
PHONE = "spots_phone"
# Below this, a fix's speed says little: walking pace is taken instead.
MIN_SPEED = 0.3
WALK_MPS = 1.4
# Each intent's circle, in metres, walking and driving: an opportunity's
# broad (worth a detour), an errand's and an arrival's about as tight as iOS
# reliably tells an app it entered one.
INTENTS = {"opportunity": {"walk": 750.0, "drive": 3000.0},
           "errand": {"walk": 100.0, "drive": 100.0},
           "arrival": {"walk": 100.0, "drive": 100.0}}
# How far a pick's own circle may go for each intent ("radius_m").
BOUNDS = {"opportunity": (500.0, 5000.0), "errand": (50.0, 300.0), "arrival": (50.0, 300.0)}
PICK_INTENT = "opportunity"
ITEM_INTENT = "errand"
_ARTICLE = re.compile(r"^(?:a|an|the|some)\s+", re.IGNORECASE)
_WATCH_ID = re.compile(r"[ci][0-9]{1,12}")


def on(pulse_store: Any) -> bool:
    """Whether walking past is on (it is unless the user turned it off)."""
    return pulse_store.get(SETTING) is not False


def reads(pulse_store: Any, now: float, home: Optional[Path] = None) -> bool:
    """Whether the phone may watch shops for this Pulse: Pulse, Where you
    are and walking past on, and the phone sharing in the background."""
    return bool(pulse_store.get("enabled")) and on(pulse_store) and places.reads(pulse_store, now, home)


def words(value: Any) -> str:
    """The model's words for a kind of shop ("a florist"), tidied: one
    line, at most QUERY_CHARS characters and QUERY_WORDS words; "" when
    they aren't."""
    text = " ".join(str(value or "").split()) if isinstance(value, str) else ""
    if not text or len(text) > QUERY_CHARS or len(text.split()) > QUERY_WORDS:
        return ""
    return text if any(char.isalpha() for char in text) else ""


def query(text: Any) -> str:
    """What the phone searches Apple Maps for: the words without a leading
    "a", "an" or "the"; "" when nothing else is left ("a the")."""
    searched = _ARTICLE.sub("", words(text)).strip()
    return searched if places._phrase(searched) else ""


def intent(value: Any, default: str = PICK_INTENT) -> str:
    """One of INTENTS, ``default`` when ``value`` isn't."""
    return value if isinstance(value, str) and value in INTENTS else default


def own_radius(value: Any, wanted: str) -> Optional[float]:
    """A pick's own circle in metres, kept within ``wanted``'s BOUNDS; None
    when it gave none (or not a number)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        return None
    low, high = BOUNDS[wanted]
    return float(min(max(value, low), high))


def radius(wanted: str, own: Any = None) -> dict[str, float]:
    """The circle the phone watches for a watch: {"walk", "drive"} metres,
    the intent's, or the pick's own for both."""
    mine = own_radius(own, wanted)
    return {"walk": mine, "drive": mine} if mine is not None else dict(INTENTS[wanted])


def held_spot(value: Any, saved: Sequence[dict[str, Any]], wanted: Any = None,
              own: Any = None) -> Optional[dict[str, Any]]:
    """The moment a look ahead pick waits for when the model named a kind of
    shop ("spot"), as its card keeps it ({"place_id", "place", "kind",
    "spot", "intent"}: ``wanted``, an opportunity unless it's another of
    INTENTS, and "radius_m" when it gave its own, ``own``); None when the
    words aren't usable or name a saved place (that one is held for the
    place itself)."""
    said = words(value)
    searched = query(said)
    if not searched or places.place_for(said, saved) is not None:
        return None
    kept = intent(wanted)
    held = {"place_id": PREFIX + places._phrase(searched), "place": said, "kind": KIND, "spot": searched,
            "intent": kept}
    mine = own_radius(own, kept)
    return held if mine is None else {**held, "radius_m": mine}


def _until(wait: dict[str, Any]) -> float:
    until = wait.get("until")
    return float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else 0.0


def watches(pulse_store: Any, home: Path, now: float) -> list[dict[str, Any]]:
    """What the phone watches for this Pulse, the soonest window's end
    first, at most MAX_WATCHES: {"id": "c12" (a held card) or "i7" (an
    item), "query", "until" (epoch seconds), "intent", "radius"}. None
    while ``reads`` says no."""
    if not reads(pulse_store, now, home):
        return []
    found: list[dict[str, Any]] = []
    for card in pulse_store.moment_cards():
        wait = places.held(card)
        if (card["done"] or wait.get("kind") != KIND or "reached_at" in wait or not wait.get("spot")
                or _until(wait) <= now):
            continue
        wanted = intent(wait.get("intent"))
        found.append({"id": f"c{card['id']}", "query": str(wait["spot"]), "until": _until(wait),
                      "intent": wanted, "radius": radius(wanted, wait.get("radius_m"))})
    found.sort(key=lambda watch: (watch["until"], watch["id"]))
    saved = places.saved(places.view(home))
    kinds = {word for words_ in places.KIND_KEYS.values() for word in words_}
    for item in places.waiting(pulse_store, "arrive"):
        said = item.get("place_words")
        searched = query(said)
        if not searched or places.place_for(said, saved) is not None or places._phrase(searched) in kinds:
            continue  # a saved place's, or one only a saved place can be ("home")
        end = min(followup.place_wait_end(item), now + ITEM_WATCH_S)
        if end > now:
            found.append({"id": f"i{item['id']}", "query": searched, "until": end, "intent": ITEM_INTENT,
                          "radius": radius(ITEM_INTENT)})
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for watch in found:
        unique.setdefault((places._phrase(watch["query"]), watch["intent"]), watch)
    return list(unique.values())[:MAX_WATCHES]


def reply(homes: Sequence[Path], now: float) -> dict[str, Any]:
    """The sealed reply's body for a batch: every reading profile's watches
    (``watches``), each id prefixed with that profile's place in ``homes``
    ("0.c12"), at most MAX_WATCHES, each with its intent and circle
    ("radius": {"walk", "drive"} metres). {"spots": []} when none."""
    found: list[dict[str, Any]] = []
    for index, home in enumerate(homes):
        try:
            if not (Path(home) / store.DB_NAME).exists():
                continue
            found += [{**watch, "id": f"{index}.{watch['id']}"}
                      for watch in watches(store.open_store(Path(home)), Path(home), now)]
        except Exception:  # noqa: BLE001 — the others' still go
            logger.warning("conduit_push: Pulse couldn't list a profile's shops to watch", exc_info=True)
    found.sort(key=lambda watch: (watch["until"], watch["id"]))
    return {"spots": [{"id": watch["id"], "query": watch["query"],
                       "until": datetime.fromtimestamp(watch["until"], timezone.utc).isoformat(timespec="seconds"),
                       "intent": watch["intent"], "radius": watch["radius"]}
                      for watch in found[:MAX_WATCHES]]}


def lateness(entry: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """How late iOS told the app the user walked into a shop: how far inside
    its circle the phone's fix was (metres, None without a fix), and the
    seconds that took at the fix's speed (walking pace when it's slower
    than MIN_SPEED or unknown), plus how long after iOS's time the fix
    came. An estimate: a fix is only as good as its accuracy."""
    distance, radius = entry.get("distance_m"), entry.get("radius_m")
    if distance is None or radius is None:
        return None, None
    depth = max(0.0, float(radius) - float(distance))
    speed = entry.get("speed_mps")
    pace = float(speed) if isinstance(speed, (int, float)) and speed >= MIN_SPEED else WALK_MPS
    fixed = entry.get("fix_at")
    after = max(0.0, float(fixed) - float(entry["at"])) if isinstance(fixed, (int, float)) else 0.0
    return round(depth, 1), round(depth / pace + after, 1)


def close_enough(entry: dict[str, Any], wanted: str, own: Any = None) -> bool:
    """Whether coming within a shop's circle counts for its watch's intent:
    always for an opportunity; for an errand, the fix taken on entry inside
    the circle (the watch's, ``own`` or the intent's, or the phone's when
    smaller); for an arrival, inside it with its whole accuracy. No fix, no
    errand or arrival."""
    if wanted == "opportunity":
        return True
    distance, watched = entry.get("distance_m"), entry.get("radius_m")
    if distance is None:
        return False
    circle = radius(wanted, own)["walk"]
    circle = min(float(watched), circle) if watched is not None else circle
    if wanted == "errand":
        return float(distance) <= circle
    accuracy = entry.get("accuracy_m")
    return accuracy is not None and float(distance) + float(accuracy) <= circle


def reach(entry: dict[str, Any]) -> str:
    """The narrowest of INTENTS an entry was close enough for."""
    return next(wanted for wanted in ("arrival", "errand", "opportunity") if close_enough(entry, wanted))


def entered(homes: Sequence[Path], device_id: str, entries: Sequence[dict[str, Any]], now: float) -> bool:
    """The shops the phone said the user walked into (location_store's
    parsed "spot_entries"): each kept with its timing by the profile whose
    watch it was ("0.c12": the first of ``homes``), and made a moment there
    while that Pulse may still read where the user is (pulse_places.arrived).
    True when any moment was new, so the caller wakes Pulse."""
    told = False
    for entry in entries:
        index, _, watch = str(entry.get("spot_id") or "").partition(".")
        if not index.isdigit() or int(index) >= len(homes) or not _WATCH_ID.fullmatch(watch):
            continue
        home = Path(homes[int(index)])
        try:
            if not (home / store.DB_NAME).exists():
                continue
            told = _entered(store.open_store(home), home, device_id, watch, entry, now) or told
        except Exception:  # noqa: BLE001 — the other entries still count
            logger.warning("conduit_push: Pulse couldn't take a shop walked into", exc_info=True)
    return told


def _entered(pulse_store: Any, home: Path, device_id: str, watch: str, entry: dict[str, Any], now: float) -> bool:
    if not reads(pulse_store, now, home):
        return False
    searched, wanted, own = _watched(pulse_store, watch)
    if not searched:
        return False  # done, reached or gone since: nothing waits for it
    depth, late = lateness(entry)
    confirmed = close_enough(entry, wanted, own)
    moment_id = f"{device_id}/{entry['id']}"
    moment = {"id": moment_id, "place_id": PREFIX + places._phrase(searched), "place": entry["name"], "kind": KIND,
              "on": "arrive", "at": entry["at"], "keep": True, "reach": reach(entry)}
    pulse_store.add_spot_entry({
        "id": moment_id, "device": device_id, "watch": watch, "query": searched, "name": entry["name"],
        "at": entry["at"], "received_at": now, "distance_m": entry.get("distance_m"),
        "accuracy_m": entry.get("accuracy_m"), "speed_mps": entry.get("speed_mps"), "radius_m": entry.get("radius_m"),
        "depth_m": depth, "late_s": late, "intent": wanted, "confirmed": int(confirmed),
        "moment": moment_id if confirmed else ""})
    pulse_store.prune_spot_entries(now - KEEP_S)
    return confirmed and places.arrived(pulse_store, [moment], now)


def _watched(pulse_store: Any, watch: str) -> tuple[str, str, Any]:
    """The search words, intent and own circle (None for the intent's) of a
    watch still waiting: a held card's, or an item's still waiting for its
    place; "" for words when it isn't."""
    number = int(watch[1:])
    if watch[0] == "c":
        card = next((card for card in pulse_store.moment_cards() if card["id"] == number), None)
        wait = places.held(card) if card is not None else {}
        if card is None or card["done"] or wait.get("kind") != KIND or "reached_at" in wait:
            return "", PICK_INTENT, None
        return str(wait.get("spot") or ""), intent(wait.get("intent")), wait.get("radius_m")
    item = next((item for item in places.waiting(pulse_store, "arrive") if item["id"] == number), None)
    return (query(item.get("place_words")) if item is not None else ""), ITEM_INTENT, None


def phone_said(pulse_store: Any, room: Any, now: float) -> None:
    """Keeps how many shops the phone said it could watch, for `pulse spots`."""
    if isinstance(room, int) and not isinstance(room, bool) and 0 <= room <= 20:
        pulse_store.set(PHONE, {"room": room, "at": now})


def describe(entry: dict[str, Any], zone: tzinfo) -> str:
    """A shop walked into as `pulse spots` lists it."""
    when = datetime.fromtimestamp(float(entry["at"]), zone).strftime("%a %d %b %H:%M")
    timing = ("no fix to tell how late" if entry.get("depth_m") is None else
              f"{entry['depth_m']:.0f} m inside, about {entry['late_s']:.0f} s late" +
              (f" (fix ±{entry['accuracy_m']:.0f} m)" if entry.get("accuracy_m") is not None else ""))
    counted = "" if entry.get("confirmed", 1) else f", not close enough for an {entry.get('intent')}"
    return f"- {when}: {entry['name']} ({entry['query']}, {entry.get('intent') or PICK_INTENT}), {timing}{counted}"

