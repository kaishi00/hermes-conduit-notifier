"""Conduit's rich cards: maps, places, stats, charts, forecasts, comparisons,
steps, media, contacts and statuses, and the board kinds (tables, timelines,
checklists, lists, progress and callouts) Conduit draws natively, checked
against the one schema both sides read (``cards/card-schema.json``). A set of
cards from one conduit_show call is a board: it can be laid out as tabs and
carry follow-ups (``normalize_layout``, ``normalize_next``).

Hermes fills in a card's facts (a place's name, where it is, a rating it
found and where from; numbers, a forecast, the steps of a recipe); Conduit
owns the look, and the phone adds what Apple Maps knows itself at draw time
(address, category, phone, website, travel time, Look Around). So the schema
has no fields for those, and a rating, hours, price or photo without a named
source is left out rather than shown as fact. The other cards say where
their information is from when Hermes names a source (a forecast and media
must), and otherwise read as Hermes' own; a contact says when the person is
in the user's own contacts. A card with a ``live`` name replaces the earlier
card of that name in the conversation, so a status can be kept up to date.

Normalizing follows the schema and is the same in Conduit's Swift
(``RichCardSchema``); ``cards/card-vectors.json`` holds the examples both
test suites run, so the two can't drift:
- a table's cells and column names are made text first, so each keeps its
  place (``_prepared``);
- unknown keys are dropped;
- text has control characters turned into spaces, is trimmed, and is cut to
  ``maxLength`` code points (a URL that's too long, or holds a space, quote,
  bracket or parenthesis, is invalid instead);
- an invalid optional value is dropped, an invalid list item is dropped
  (then the list is cut to ``maxItems``), and an invalid required value
  makes its object invalid;
- enums match without regard to case and come out as written in the schema;
- then the schema's ``rules``: a card keeps only its type's fields and
  needs its type's own; a route only on a map whose places all survived,
  naming places in the list, from one to another; a place card keeps its
  first place; a place's or person's photo needs an https url; a chart keeps the series that fit its labels (one, never
  negative, for a pie); a forecast needs a source and something to show,
  and drops a day whose low is above its high; a comparison recommends one
  option at most; media needs a source, and an image has no duration or
  artwork; a contact keeps only a phone and an email that look like one,
  and a person's message only with their phone;
  a table keeps the rows with one cell per column; a checklist keeps one
  item per text; a list item's photo needs an https url; a progress goal is
  above 0;
  a live name is kept on the first card of a set that has it.

With a ``notes`` list, normalizing also says what it dropped or cut and why,
so conduit_show can hand the model exact errors to fix.

The tool (agent process) and the dashboard (dashboard process, voice call
turns) both use this module: the dashboard loads it by path, so it imports
nothing from the plugin package.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_PATH = Path(__file__).resolve().parent / "cards" / "card-schema.json"

_INVALID = object()
# No spaces, quotes, brackets or parentheses: a URL also lands inside a
# Markdown link, where any of them could end it early.
_URI = re.compile(r"^https?://[^\s()<>\[\]{}\"'`\\]+$", re.IGNORECASE)


def _load() -> dict[str, Any]:
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


DEFINITION: dict[str, Any] = _load()
CARD_SCHEMA: dict[str, Any] = DEFINITION["card"]
MAX_CARDS: int = DEFINITION["max_cards"]
VERSION: int = DEFINITION["version"]


def tool_parameter(description: str) -> dict[str, Any]:
    """The ``cards`` parameter of a tool: a short list of cards. A diff card
    takes its changes as ``inputs.diff`` (a patch, a patch file or two
    texts), which normalizing reads into the card's diff."""
    properties = {**CARD_SCHEMA["properties"], "diff": DEFINITION["inputs"]["diff"]}
    return {"type": "array", "maxItems": MAX_CARDS, "description": description,
            "items": {**CARD_SCHEMA, "properties": properties}}


def normalize_cards(value: Any, notes: list[str] | None = None) -> list[dict[str, Any]]:
    """The valid cards in ``value``, normalized; invalid ones are dropped.

    With ``notes``, each thing dropped or cut is added to it as a line the
    model can act on, like ``cards[1].table.rows[3]: 4 cells for 3 columns,
    dropped``. Normalizing is the same with or without it."""
    if not isinstance(value, list):
        if notes is not None and value is not None:
            notes.append("cards: not a list, nothing shown")
        return []
    cards = []
    live = set()
    for index, raw in enumerate(value):
        if len(cards) == MAX_CARDS:
            if notes is not None:
                notes.append(f"cards: only {MAX_CARDS} can be shown at once, the rest were left out")
            break
        card = normalize_card(raw, notes, f"cards[{index}]")
        if card is None:
            continue
        if "live" in card:
            if card["live"] in live:
                del card["live"]
                if notes is not None:
                    notes.append(f"cards[{index}].live: another card has that name, left out")
            else:
                live.add(card["live"])
        cards.append(card)
    return cards


def normalize_card(raw: Any, notes: list[str] | None = None, path: str = "card") -> dict[str, Any] | None:
    raw = _prepared(raw, notes, path)
    card = _normalize(CARD_SCHEMA, raw, path, notes)
    if card is _INVALID:
        _note(notes, f"{path}: dropped")
        return None
    result = _apply_rules(card, raw, notes, path)
    if result is None:
        _note(notes, f"{path}: dropped")
    return result


def _note(notes: list[str] | None, text: str) -> None:
    if notes is not None:
        notes.append(text)


def _prepared(raw: Any, notes: list[str] | None = None, path: str = "card") -> Any:
    """A table's cells and column names as text, so a number, a blank or a
    null keeps its place in the row: a string stays as it is (an empty one
    is \u2013), a number becomes its digits, anything else is \u2013. A diff
    card's changes (inputs.diff) read into its files and hunks
    (``read_diff``)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        return raw
    if raw["type"].lower() == "diff":
        return _prepared_diff(raw, notes, path)
    if raw["type"].lower() != "table":
        return raw
    table = raw.get("table")
    if not isinstance(table, dict):
        return raw
    table = dict(table)
    if isinstance(table.get("columns"), list):
        table["columns"] = [_cell(cell) for cell in table["columns"]]
    if isinstance(table.get("rows"), list):
        table["rows"] = [[_cell(cell) for cell in row] if isinstance(row, list) else row for row in table["rows"]]
    return {**raw, "table": table}


_BLANK = "\u2013"


def _cell(value: Any) -> str:
    if isinstance(value, str):
        return value if clean_text(value) else _BLANK
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _BLANK
    if isinstance(value, float) and not math.isfinite(value):
        return _BLANK
    if abs(value) >= 1e15:
        return _BLANK
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    return repr(value)


def _normalize(schema: dict[str, Any], value: Any, path: str = "", notes: list[str] | None = None) -> Any:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            _note(notes, f"{path}: not an object")
            return _INVALID
        required = set(schema.get("required", []))
        result: dict[str, Any] = {}
        for key, inner in schema.get("properties", {}).items():
            if key not in value or value[key] is None:
                if key in required:
                    _note(notes, f"{path}.{key}: missing")
                    return _INVALID
                continue
            clean = _normalize(inner, value[key], f"{path}.{key}", notes)
            if clean is _INVALID:
                if key in required:
                    return _INVALID
                _note(notes, f"{path}.{key}: left out")
                continue
            result[key] = clean
        return result
    if kind == "array":
        if not isinstance(value, list):
            _note(notes, f"{path}: not a list")
            return _INVALID
        limit = schema.get("maxItems")
        items = []
        for index, raw in enumerate(value):
            if limit is not None and len(items) == limit:
                _note(notes, f"{path}: at most {limit} items, the rest were left out")
                break
            item = _normalize(schema["items"], raw, f"{path}[{index}]", notes)
            if item is not _INVALID:
                items.append(item)
            else:
                _note(notes, f"{path}[{index}]: left out")
        if len(items) < schema.get("minItems", 0):
            _note(notes, f"{path}: needs at least {schema['minItems']} valid items")
            return _INVALID
        return items
    if kind == "string":
        if not isinstance(value, str):
            _note(notes, f"{path}: not text")
            return _INVALID
        if schema.get("format") == "line":
            # A line of code or text keeps its spaces: never trimmed.
            text = _clean_line(value)
            limit = schema.get("maxLength")
            if not text:
                _note(notes, f"{path}: empty")
                return _INVALID
            if limit is not None and len(text) > limit:
                _note(notes, f"{path}: cut to {limit} characters")
                text = text[:limit]
            return text
        text = clean_text(value)
        if not text:
            _note(notes, f"{path}: empty")
            return _INVALID
        limit = schema.get("maxLength")
        if schema.get("format") == "uri":
            if not _URI.match(text) or (limit is not None and len(text) > limit):
                _note(notes, f"{path}: not a link (http or https, no spaces, quotes or brackets)")
                return _INVALID
            return text
        if limit is not None and len(text) > limit:
            _note(notes, f"{path}: cut to {limit} characters")
            text = text[:limit].rstrip()
        if "enum" in schema:
            for option in schema["enum"]:
                if option.lower() == text.lower():
                    return option
            _note(notes, f"{path}: not one of {', '.join(schema['enum'])}")
            return _INVALID
        return text
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _note(notes, f"{path}: not a number")
            return _INVALID
        if isinstance(value, float) and not math.isfinite(value):
            _note(notes, f"{path}: not a number")
            return _INVALID
        if kind == "integer":
            if isinstance(value, float):
                if not value.is_integer():
                    _note(notes, f"{path}: not a whole number")
                    return _INVALID
                value = int(value)
        if "minimum" in schema and value < schema["minimum"]:
            _note(notes, f"{path}: below {schema['minimum']:g}")
            return _INVALID
        if "maximum" in schema and value > schema["maximum"]:
            _note(notes, f"{path}: above {schema['maximum']:g}")
            return _INVALID
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        _note(notes, f"{path}: not true or false")
        return _INVALID
    return _INVALID


def clean_text(text: str) -> str:
    """Control characters become spaces; trimmed."""
    return _clean_line(text).strip()


def _clean_line(text: str) -> str:
    """Control characters become spaces."""
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


# Each type's own field; a card keeps it and the shared fields, and nothing else.
_TYPE_FIELDS = {"map": ("places", "route"), "place": ("places",), "places": ("places",), "stats": ("stats",),
                "chart": ("chart",), "weather": ("weather",), "comparison": ("options",), "steps": ("steps", "needs"),
                "media": ("media",), "contact": ("people",), "status": ("status",), "table": ("table",),
                "timeline": ("timeline",), "checklist": ("checklist",), "list": ("list", "ranked"),
                "progress": ("progress",), "callout": ("callout",), "diff": ("diff",)}
PLACE_TYPES = ("map", "place", "places")
# The information cards (plugin 0.46+), media and contacts, and statuses
# (0.47+): a Conduit draws each group only when its exchange names it.
INFO_TYPES = ("stats", "chart", "weather", "comparison", "steps")
MEDIA_TYPES = ("media", "contact")
LIVE_TYPES = ("status",)
# Boards (plugin 0.128+): these kinds, more than LEGACY_MAX_CARDS cards in a
# set, a layout and follow-ups.
BOARD_TYPES = ("table", "timeline", "checklist", "list", "progress", "callout")
BOARD_FEATURE = "board-cards"
# Diffs (plugin 0.133+).
DIFF_TYPES = ("diff",)
DIFF_FEATURE = "diff-cards"
FEATURE_TYPES = {"info-cards": INFO_TYPES, "media-cards": MEDIA_TYPES, "live-cards": LIVE_TYPES,
                 BOARD_FEATURE: BOARD_TYPES, DIFF_FEATURE: DIFF_TYPES}
# How many cards a Conduit without boards draws from a set.
LEGACY_MAX_CARDS = 4
# A phone number's characters, and an address with nothing that could end a
# link or a header early, or add a header or recipient to its mailto: link.
_PHONE = re.compile(r"^[0-9+\-(). ]+$")
_EMAIL = re.compile(r"^[^@\s<>()\[\]\"'`,;:\\?#%&=]+@[^@\s<>()\[\]\"'`,;:\\?#%&=]+\.[^@\s<>()\[\]\"'`,;:\\?#%&=.]+$")


def _apply_rules(card: dict[str, Any], raw: dict[str, Any], notes: list[str] | None = None,
                 path: str = "card") -> dict[str, Any] | None:
    kind = card["type"]
    if kind not in _TYPE_FIELDS:
        return None
    keep = {"type", "title", "live", *_TYPE_FIELDS[kind]}
    if kind not in PLACE_TYPES:
        keep.add("source")
    for key in card:
        if key not in keep:
            _note(notes, f"{path}.{key}: not used on a {kind} card, left out")
    card = {key: value for key, value in card.items() if key in keep}
    field = _TYPE_FIELDS[kind][0]
    if field not in card:
        _note(notes, f"{path}.{field}: a {kind} card needs it")
        return None
    if kind in PLACE_TYPES:
        return _place_rules(card, raw, path, notes)
    if kind == "chart":
        chart = _chart_rules(card["chart"], f"{path}.chart", notes)
        if chart is None:
            _note(notes, f"{path}.chart: no series has one value per label (a pie needs one, never negative)")
            return None
        card["chart"] = chart
    elif kind == "weather":
        weather = card["weather"]
        if "days" in weather:
            days = [day for day in weather["days"] if day["low"] <= day["high"]]
            if len(days) < len(weather["days"]):
                _note(notes, f"{path}.weather.days: a day's low is above its high, those were left out")
            weather["days"] = days
        for key in ("hours", "days"):
            if key in weather and not weather[key]:
                del weather[key]
        if "source" not in card:
            _note(notes, f"{path}.source: a weather card needs it")
            return None
        if not any(key in weather for key in ("now", "hours", "days")):
            _note(notes, f"{path}.weather: needs now, hours or days")
            return None
    elif kind == "comparison":
        seen = False
        for option in card["options"]:
            if option.get("recommended"):
                if seen:
                    del option["recommended"]
                    _note(notes, f'{path}.options "{option["name"]}".recommended: one option at most, left out')
                seen = True
    elif kind == "media":
        if "source" not in card:
            _note(notes, f"{path}.source: a media card needs it")
            return None
        # The phone loads media over https only.
        media = [item for item in card["media"] if _is_https(item["url"])]
        if len(media) < len(card["media"]):
            _note(notes, f"{path}.media: an item's url isn't https, those were left out")
        card["media"] = media
        if not card["media"]:
            return None
        for item in card["media"]:
            if item["kind"] == "image" or not _is_https(item.get("artwork", "https://")):
                if item.pop("artwork", None) is not None:
                    _note(notes, f"{path}.media: artwork goes with video or audio over https, left out")
            if item["kind"] == "image" and item.pop("duration", None) is not None:
                _note(notes, f"{path}.media: an image has no duration, left out")
    elif kind == "contact":
        for person in card["people"]:
            # Named by name: people the schema left out are gone by now.
            who = f'{path}.people "{person["name"]}"'
            if "phone" in person and not _phone_ok(person["phone"]):
                del person["phone"]
                _note(notes, f"{who}.phone: not a phone number, left out")
            if "email" in person and not _EMAIL.match(person["email"]):
                del person["email"]
                _note(notes, f"{who}.email: not an email address, left out")
            # A text goes to their number, so there's none without it.
            if "phone" not in person and person.pop("message", None) is not None:
                _note(notes, f"{who}.message: a text needs their phone number, left out")
            # The phone loads photos over https only.
            if "photo" in person and not _is_https(person["photo"]["url"]):
                del person["photo"]
                _note(notes, f"{who}.photo: not https, left out")
    elif kind == "table":
        table = card["table"]
        width = len(table["columns"])
        rows = []
        for index, row in enumerate(table["rows"]):
            if len(row) == width:
                rows.append(row)
            else:
                # Rows the schema left out are gone by now, so the row is
                # named by its first cell rather than a place that moved.
                _note(notes, f"{path}.table.rows: the row starting \"{row[0] if row else ''}\" has {len(row)} cells "
                             f"for {width} columns, left out")
        if not rows:
            return None
        table["rows"] = rows
    elif kind == "checklist":
        seen: set[str] = set()
        items = []
        for item in card["checklist"]:
            key = item["text"].lower()
            if key in seen:
                _note(notes, f"{path}.checklist: \"{item['text']}\" is there twice, kept once")
                continue
            seen.add(key)
            items.append(item)
        card["checklist"] = items
    elif kind == "list":
        for item in card["list"]:
            # The phone loads photos over https only. Named by title: items
            # the schema left out are gone by now.
            if "photo" in item and not _is_https(item["photo"]["url"]):
                del item["photo"]
                _note(notes, f'{path}.list "{item["title"]}".photo: not https, left out')
    elif kind == "progress":
        items = [item for item in card["progress"] if item["goal"] > 0]
        if len(items) < len(card["progress"]):
            _note(notes, f"{path}.progress: a goal must be above 0, those without were left out")
        if not items:
            return None
        card["progress"] = items
    elif kind == "diff":
        _diff_rules(card["diff"], f"{path}.diff", notes)
    return card


def _diff_rules(diff: dict[str, Any], path: str, notes: list[str] | None = None) -> None:
    """A hunk keeps the lines a patch can hold (+, - or a space first) and
    goes when none is left; a binary file has no lines; an id is the host's."""
    if "id" in diff and not DIFF_ID.fullmatch(diff["id"]):
        del diff["id"]
        _note(notes, f"{path}.id: not an id the host gave, left out")
    for file in diff["files"]:
        if file.get("binary") and file.pop("hunks", None) is not None:
            _note(notes, f'{path}.files "{file["path"]}".hunks: a binary file shows no lines, left out')
        hunks = []
        for hunk in file.get("hunks", []):
            lines = [line for line in hunk["lines"] if line[:1] in ("+", "-", " ")]
            if len(lines) < len(hunk["lines"]):
                _note(notes, f'{path}.files "{file["path"]}": a line not starting with +, - or a space, left out')
            if lines:
                hunk["lines"] = lines
                hunks.append(hunk)
        if hunks:
            file["hunks"] = hunks
        else:
            file.pop("hunks", None)


# Follow-ups a tap sends as the user's message: never a command, a file
# reference or a shell line, so a tap can only ever ask.
_NEXT_MENTION = re.compile(r"@[\w/.~]")
NEXT_SCHEMA: dict[str, Any] = DEFINITION["board"]["next"]
LAYOUT_SCHEMA: dict[str, Any] = DEFINITION["board"]["layout"]


def normalize_next(value: Any, notes: list[str] | None = None) -> list[str]:
    """The follow-ups worth offering under a set of cards (the board's
    rules): each item checked as the schema's text, the ones that aren't a
    plain message and repeats dropped, then the first maxItems kept."""
    if not isinstance(value, list):
        if value is not None:
            _note(notes, "next: not a list of messages, left out")
        return []
    limit = NEXT_SCHEMA["maxItems"]
    kept: list[str] = []
    for index, raw in enumerate(value):
        text = _normalize(NEXT_SCHEMA["items"], raw, f"next[{index}]", notes)
        if text is _INVALID:
            continue
        if text[:1] in ("/", "!") or _NEXT_MENTION.search(text) or "`" in text:
            _note(notes, f"next[{index}]: \"{text}\" isn't a plain message, left out")
            continue
        if text.lower() in (other.lower() for other in kept):
            _note(notes, f"next[{index}]: \"{text}\" is there twice, kept once")
            continue
        if len(kept) == limit:
            _note(notes, f"next: at most {limit}, the rest were left out")
            break
        kept.append(text)
    return kept


def normalize_layout(value: Any, notes: list[str] | None = None) -> str | None:
    """tabs, or None for the default stack."""
    layout = _normalize(LAYOUT_SCHEMA, value, "layout", notes) if value is not None else _INVALID
    return "tabs" if layout == "tabs" else None


def _is_https(url: str) -> bool:
    return url[:8].lower() == "https://"


def _phone_ok(phone: str) -> bool:
    return bool(_PHONE.match(phone)) and 3 <= sum(char.isdigit() for char in phone) <= 20


def _place_rules(card: dict[str, Any], raw: dict[str, Any], path: str = "cards[0]",
                 notes: list[str] | None = None) -> dict[str, Any]:
    places = card["places"]
    if card["type"] == "place":
        if len(places) > 1:
            _note(notes, f"{path}.places: a place card shows one, the rest were left out")
        card["places"] = places[:1]
    # The phone loads photos over https only.
    for place in card["places"]:
        if "photo" in place and not _is_https(place["photo"]["url"]):
            del place["photo"]
            _note(notes, f'{path}.places "{place["name"]}".photo: not https, left out')
    route = card.pop("route", None)
    # Only a map carries a route (_TYPE_FIELDS), so this is for maps alone.
    if route is not None:
        given = raw.get("places")
        survived = isinstance(given, list) and len(given) == len(places)
        to, start = route["to"], route.get("from")
        if survived and to < len(places) and (start is None or (start < len(places) and start != to)):
            card["route"] = route
        else:
            _note(notes, f"{path}.route: its places aren't all there, left out")
    return card


def _chart_rules(chart: dict[str, Any], path: str = "cards[0].chart",
                 notes: list[str] | None = None) -> dict[str, Any] | None:
    count = len(chart["labels"])
    series = [line for line in chart["series"] if len(line["values"]) == count]
    if chart["kind"] == "pie":
        series = series[:1]
        if series and any(value < 0 for value in series[0]["values"]):
            series = []
    if not series:
        return None
    if len(series) < len(chart["series"]):
        dropped = len(chart["series"]) - len(series)
        _note(notes, f"{path}.series: {dropped} without one value per label (a pie shows one) left out")
    chart["series"] = series
    return chart


# --- Diffs: a patch, a patch file or two texts, read into files and hunks -------

# The most a diff may hold, whatever it came in: a patch file's bytes or a
# patch's (or a text's) characters.
MAX_DIFF_INPUT = 2_000_000
# The longest before or after compared on the host, as the schema says.
MAX_COMPARED_TEXT = 20_000
# What a diff keeps: the files the schema lists, then the lines over all of
# them; past either, the card says how many more and the model hears why.
MAX_DIFF_LINES = 20_000
MAX_DIFF_LINE_CHARS = 1000
# A diff short enough for the card to carry whole (the chat's history, and
# so the model's context, holds it); a longer one is kept on the host and
# the card lists its files (``answer_diff``).
INLINE_DIFF_LINES = 150
INLINE_DIFF_FILES = 20
ANSWER_DIFF_FILES = 100
DIFF_ID = re.compile(r"cd-[0-9a-f]{16}")
_HUNK_HEAD = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+ ?(.*)$")
_DIFF_INPUTS = ("patch", "patch_file", "before", "after", "name")


def _prepared_diff(raw: dict[str, Any], notes: list[str] | None, path: str) -> dict[str, Any]:
    """A diff card's input read into the files and hunks the card keeps; a
    card already read (one an answer holds) stays as it is."""
    given = raw.get("diff")
    if not isinstance(given, dict) or ("files" in given and not any(key in given for key in _DIFF_INPUTS)):
        return raw
    try:
        files = read_diff(given)
    except DiffError as error:
        _note(notes, f"{path}.diff{error.where}: {error}")
        return {**raw, "diff": None}
    diff, problems = _kept(files)
    for problem in problems:
        _note(notes, f"{path}.diff: {problem}")
    return {**raw, "diff": diff}


class DiffError(ValueError):
    """Why a diff card's input can't be read; ``where`` names its field."""

    def __init__(self, message: str, where: str = "") -> None:
        super().__init__(message)
        self.where = where


def read_diff(given: dict[str, Any]) -> list[dict[str, Any]]:
    """The files a diff card's input changes, each with every hunk and its
    lines (``parse_patch``): from ``patch``, else ``patch_file``, else
    ``before`` and ``after``."""
    if isinstance(given.get("patch"), str) and given["patch"].strip():
        text = given["patch"]
        if len(text) > MAX_DIFF_INPUT:
            raise DiffError("over 2 MB; show a narrower diff (one folder or file)", ".patch")
        files = parse_patch(text, given.get("name"))
        if not files:
            raise DiffError("not a unified diff (no ---/+++ file lines or @@ hunks)", ".patch")
        return files
    if isinstance(given.get("patch_file"), str) and given["patch_file"].strip():
        files = parse_patch(read_patch_file(given["patch_file"]), given.get("name"))
        if not files:
            raise DiffError("not a unified diff (no ---/+++ file lines or @@ hunks), nothing shown", ".patch_file")
        return files
    before, after = given.get("before"), given.get("after")
    if before is not None or after is not None:
        for key, text in (("before", before), ("after", after)):
            if text is not None and not isinstance(text, str):
                raise DiffError("before and after are text", f".{key}")
        before, after = before or "", after or ""
        # difflib grows faster than the texts, so two long ones go as a
        # patch the shell made.
        if len(before) > MAX_COMPARED_TEXT or len(after) > MAX_COMPARED_TEXT:
            raise DiffError(f"over {MAX_COMPARED_TEXT:,} characters; diff them with the shell (diff -u) "
                            "and pass patch or patch_file")
        if before == after:
            raise DiffError("before and after are the same, nothing to show")
        name = clean_text(given["name"]) if isinstance(given.get("name"), str) else ""
        name = name[:200] or "Text"
        lines = difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{name}", f"b/{name}",
                                     lineterm="", n=3)
        files = parse_patch("\n".join(lines))
        if not files:
            raise DiffError("before and after have the same lines (only line endings differ), nothing to show")
        if not before:
            files[0]["status"] = "added"
        elif not after:
            files[0]["status"] = "deleted"
        return files
    raise DiffError("give patch, patch_file, or before and after")


def read_patch_file(path: Any) -> str:
    """A patch file on this host, up to 2 MB, as text."""
    if not isinstance(path, str):
        raise DiffError("not a path", ".patch_file")
    path = os.path.expanduser(clean_text(path))
    if not os.path.isabs(path):
        raise DiffError("give the file's absolute path", ".patch_file")
    try:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        if os.path.getsize(path) > MAX_DIFF_INPUT:
            raise DiffError("over 2 MB; show a narrower diff (one folder or file)", ".patch_file")
        with open(path, "rb") as handle:
            data = handle.read(MAX_DIFF_INPUT + 1)
    except DiffError:
        raise
    except FileNotFoundError:
        raise DiffError("no such file on the Hermes host (if your terminal runs in a container or on another "
                        "machine, pass the diff as patch instead)", ".patch_file") from None
    except OSError:
        raise DiffError("can't be read on the Hermes host", ".patch_file") from None
    if b"\0" in data[:8000]:
        raise DiffError("not a unified diff (no ---/+++ file lines or @@ hunks), nothing shown", ".patch_file")
    return data.decode("utf-8", errors="replace")


def parse_patch(text: str, name: Any = None) -> list[dict[str, Any]]:
    """A unified diff's files, in order: each with its path (the new one),
    old_path for a rename, status, binary, the lines added and removed, and
    its hunks (old_start, new_start, context and lines, each starting with
    +, - or a space; tabs as four spaces). git diff, git show, gh pr diff
    and diff -u all read; anything between files (a commit's message, index
    lines) is skipped, and a patch of bare @@ hunks is one file, named
    ``name``."""
    lines = text.splitlines()
    files: list[dict[str, Any]] = []
    file: dict[str, Any] | None = None
    index = 0

    def start(path: str, old: str | None = None) -> dict[str, Any]:
        entry = {"path": path, "old": old or path, "status": "modified", "added": 0, "removed": 0, "hunks": [],
                 "headers": False}
        files.append(entry)
        return entry

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            old, new = _git_paths(line[len("diff --git "):])
            file = start(new, old)
        elif line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old, new = _header_path(line[4:]), _header_path(lines[index + 1][4:])
            if file is None or file["hunks"] or file["headers"]:
                file = start(new or old or "file", old or new)
            if old is None:
                file["status"] = "added"
            elif new is None:
                file["status"] = "deleted"
            else:
                file["path"], file["old"] = new, old
            file["headers"] = True
            index += 1
        elif file is not None and not file["hunks"] and line.startswith("new file mode"):
            file["status"] = "added"
        elif file is not None and not file["hunks"] and line.startswith("deleted file mode"):
            file["status"] = "deleted"
        elif file is not None and not file["hunks"] and line.startswith("rename from "):
            file["old"] = _unquoted(line[len("rename from "):])
        elif file is not None and not file["hunks"] and line.startswith("rename to "):
            file["path"] = _unquoted(line[len("rename to "):])
        elif file is not None and (line.startswith("Binary files ") or line == "GIT binary patch"):
            file["binary"] = True
        elif _HUNK_HEAD.match(line):
            if file is None:
                file = start(clean_text(name)[:300] if isinstance(name, str) and clean_text(name) else "Changes")
                file["headers"] = True
            index = _read_hunk(lines, index, file)
            continue
        index += 1
    result = []
    for entry in files:
        if not entry["hunks"] and not entry.get("binary") and entry["old"] == entry["path"] \
                and entry["status"] == "modified" and not entry["headers"]:
            # A diff --git line with nothing under it (a mode change): no
            # changes to show.
            continue
        result.append(_file(entry))
    return result


def _read_hunk(lines: list[str], index: int, file: dict[str, Any]) -> int:
    """Reads the hunk whose @@ line is at ``index`` into ``file``; answers
    the index after it. Its lines are counted by the @@ line's lengths, so a
    removed line that reads like a file header stays in the hunk."""
    head = _HUNK_HEAD.match(lines[index])
    old_start, old_count = int(head.group(1)), int(head.group(2) or "1")
    new_start, new_count = int(head.group(3)), int(head.group(4) or "1")
    hunk = {"old_start": old_start, "new_start": new_start, "lines": []}
    context = clean_text(head.group(5))
    if context:
        hunk["context"] = context[:200]
    index += 1
    while index < len(lines) and (old_count > 0 or new_count > 0):
        line = lines[index]
        kind = line[:1]
        if kind == "\\":
            index += 1
            continue
        if kind == "-":
            old_count -= 1
            file["removed"] += 1
        elif kind == "+":
            new_count -= 1
            file["added"] += 1
        elif kind == " " or line == "":
            # A context line some tools wrote with its space trimmed.
            old_count -= 1
            new_count -= 1
            kind = " "
        else:
            break
        hunk["lines"].append(kind + _line_text(line[1:]))
        index += 1
    while index < len(lines) and lines[index].startswith("\\"):
        index += 1
    if hunk["lines"]:
        file["hunks"].append(hunk)
    return index


def _line_text(text: str) -> str:
    text = text.expandtabs(4)
    if len(text) > MAX_DIFF_LINE_CHARS:
        text = text[:MAX_DIFF_LINE_CHARS - 1] + "\u2026"
    return text


def _file(entry: dict[str, Any]) -> dict[str, Any]:
    file: dict[str, Any] = {"path": entry["path"][:300], "status": entry["status"]}
    if entry["old"] != entry["path"] and entry["status"] == "modified":
        file["status"] = "renamed"
        file["old_path"] = entry["old"][:300]
    if entry.get("binary"):
        file["binary"] = True
    file["added"], file["removed"] = entry["added"], entry["removed"]
    if entry["hunks"] and not entry.get("binary"):
        file["hunks"] = entry["hunks"]
    return file


def _git_paths(rest: str) -> tuple[str, str]:
    """The a/ and b/ paths of a ``diff --git`` line, quoted or not."""
    if rest.startswith('"'):
        parts = re.findall(r'"((?:\\.|[^"\\])*)"|(\S+)', rest)
        names = [_unquoted(f'"{quoted}"') if quoted else plain for quoted, plain in parts]
        if len(names) >= 2:
            return _strip_side(names[0], "a/"), _strip_side(names[1], "b/")
    # Unquoted: the two halves are the same path unless it was renamed;
    # split where " b/" starts the second.
    cut = rest.rfind(" b/")
    if cut > 0:
        return _strip_side(rest[:cut], "a/"), _strip_side(rest[cut + 1:], "b/")
    half = rest.split(" ", 1)
    return _strip_side(half[0], "a/"), _strip_side(half[-1], "b/")


def _header_path(text: str) -> str | None:
    """The path of a ---/+++ line: None for /dev/null; a timestamp after a
    tab (diff -u) and git's a/ or b/ taken off."""
    text = text.split("\t", 1)[0].rstrip()
    text = _unquoted(text)
    if text == "/dev/null":
        return None
    for side in ("a/", "b/"):
        if text.startswith(side):
            return text[len(side):]
    return text


def _strip_side(path: str, side: str) -> str:
    path = _unquoted(path.strip())
    return path[len(side):] if path.startswith(side) else path


def _unquoted(text: str) -> str:
    """A path git quoted (spaces kept, \\" and \\\\ read)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = re.sub(r'\\(.)', r"\1", text[1:-1])
    return text


def _kept(files: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """The diff a card keeps: the first files the schema lists, and their
    lines up to MAX_DIFF_LINES, with totals over every file; and what was
    left out, for the model."""
    limit = CARD_SCHEMA["properties"]["diff"]["properties"]["files"]["maxItems"]
    problems = []
    added = sum(file["added"] for file in files)
    removed = sum(file["removed"] for file in files)
    listed = files[:limit]
    budget = MAX_DIFF_LINES
    cut = False
    for file in listed:
        kept = []
        for hunk in file.get("hunks", []):
            if budget <= 0:
                file["cut"] = cut = True
                break
            if len(hunk["lines"]) > budget:
                hunk["lines"] = hunk["lines"][:budget]
                file["cut"] = cut = True
            budget -= len(hunk["lines"])
            kept.append(hunk)
        if "hunks" in file:
            if kept:
                file["hunks"] = kept
            else:
                del file["hunks"]
    diff: dict[str, Any] = {"files": listed, "added": added, "removed": removed}
    if len(files) > limit:
        diff["more_files"] = len(files) - limit
        problems.append(f"{len(files)} files, the first {limit} are listed")
    if cut:
        problems.append(f"only the first {MAX_DIFF_LINES} lines are shown")
    long_lines = sum(1 for file in listed for hunk in file.get("hunks", []) for line in hunk["lines"]
                     if line.endswith("\u2026") and len(line) == MAX_DIFF_LINE_CHARS + 1)
    if long_lines:
        problems.append(f"{long_lines} lines over {MAX_DIFF_LINE_CHARS} characters were cut")
    return diff, problems


def diff_lines(diff: dict[str, Any]) -> int:
    """How many lines a diff's hunks hold."""
    return sum(len(hunk["lines"]) for file in diff["files"] for hunk in file.get("hunks", []))


def carries_whole(diff: dict[str, Any]) -> bool:
    """Whether a card can carry this diff whole."""
    return diff_lines(diff) <= INLINE_DIFF_LINES and len(diff["files"]) <= INLINE_DIFF_FILES


def answer_diff(diff: dict[str, Any], diff_id: str | None) -> dict[str, Any]:
    """What a card carries of a diff too long to carry whole: its files
    (the first ANSWER_DIFF_FILES) without their hunks, the totals, and the
    id the host keeps the whole diff under (none when it couldn't)."""
    files = [{key: value for key, value in file.items() if key != "hunks"}
             for file in diff["files"][:ANSWER_DIFF_FILES]]
    answer: dict[str, Any] = {"files": files, "added": diff["added"], "removed": diff["removed"]}
    more = diff.get("more_files", 0) + max(0, len(diff["files"]) - ANSWER_DIFF_FILES)
    if more:
        answer["more_files"] = more
    if diff_id:
        answer["id"] = diff_id
    return answer


# --- Markdown, for a screen or client that doesn't draw cards -----------------

_MODE_FLAGS = {"driving": "d", "walking": "w", "transit": "r"}


def fallback_markdown(cards: list[dict[str, Any]]) -> str:
    """The cards as Markdown: what a phone that doesn't draw them, or another
    client reading the chat, shows instead."""
    parts = [_card_markdown(card) for card in cards]
    return "\n\n".join(part for part in parts if part)


def _card_markdown(card: dict[str, Any]) -> str:
    lines = []
    if card.get("title"):
        lines.append(f"### {_escape(card['title'])}")
    kind = card.get("type")
    if kind in _INFO_MARKDOWN:
        lines.extend(_INFO_MARKDOWN[kind](card))
        if card.get("source"):
            lines.append(f"_From {_sourced_name(card['source'])}_")
        return "\n\n".join(line for line in lines if line)
    for place in card["places"]:
        lines.append(_place_markdown(place))
    route = card.get("route")
    places = card["places"]
    # Normalized cards always name places in the list; a card from anywhere
    # else just loses its directions link.
    if route and not all(0 <= index < len(places) for index in (route.get("to", -1), route.get("from", 0))):
        route = None
    if route:
        to = places[route["to"]]
        mode = route.get("mode", "driving")
        query = [("daddr", _maps_query(to))]
        if "from" in route:
            query.insert(0, ("saddr", _maps_query(places[route["from"]])))
        label = f"Directions to {_escape(to['name'])}"
        if mode in _MODE_FLAGS:
            # Apple Maps links have no cycling directions, so a cycling
            # route's link names no mode rather than the wrong one.
            query.append(("dirflg", _MODE_FLAGS[mode]))
            label += f" ({mode})"
        link = "https://maps.apple.com/?" + "&".join(f"{key}={quote(value, safe=',')}" for key, value in query)
        lines.append(f"[{label}]({link})")
    return "\n\n".join(lines)


def _place_markdown(place: dict[str, Any]) -> str:
    head = f"**{_escape(place['name'])}**"
    if place.get("address"):
        head += f" · {_escape(place['address'])}"
    lines = [head]
    facts = []
    rating = place.get("rating")
    if rating:
        text = f"★ {rating['value']:g}"
        if "count" in rating:
            text += f" ({rating['count']:,})"
        facts.append(f"{text} · {_sourced(rating)}")
    for key in ("hours", "price"):
        fact = place.get(key)
        if fact:
            facts.append(f"{_escape(fact['text'])} · {_sourced(fact)}")
    if facts:
        lines.append(" — ".join(facts))
    if place.get("note"):
        lines.append(f"_{_escape(place['note'])}_")
    lines.append(f"[Open in Apple Maps](https://maps.apple.com/?{_maps_params(place)})")
    return "  \n".join(lines)


def _stats_markdown(card: dict[str, Any]) -> list[str]:
    rows = []
    for stat in card["stats"]:
        row = f"- **{_escape(stat['label'])}:** {_escape(stat['value'])}"
        if stat.get("change"):
            row += f" ({_escape(stat['change'])})"
        if stat.get("detail"):
            row += f", {_escape(stat['detail'])}"
        rows.append(row)
    return ["\n".join(rows)]


def _chart_markdown(card: dict[str, Any]) -> list[str]:
    chart = card["chart"]
    unit = f" ({_escape(chart['unit'])})" if chart.get("unit") else ""
    names = [_escape(line.get("name", "Value")) + unit for line in chart["series"]]
    rows = ["| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for index, label in enumerate(chart["labels"]):
        values = " | ".join(_number(line["values"][index]) for line in chart["series"])
        rows.append(f"| {_escape(label)} | {values} |")
    return ["\n".join(rows)]


_CONDITIONS = {"clear": "Clear", "partly_cloudy": "Partly cloudy", "cloudy": "Cloudy", "fog": "Fog",
               "drizzle": "Drizzle", "rain": "Rain", "heavy_rain": "Heavy rain", "thunderstorm": "Thunderstorms",
               "snow": "Snow", "sleet": "Sleet", "wind": "Windy"}


def _weather_markdown(card: dict[str, Any]) -> list[str]:
    weather = card["weather"]
    degree = "°" + weather["unit"]
    lines = []
    now = weather.get("now")
    if now:
        line = f"**{_number(now['temp'])}{degree}**, {_condition_name(now['condition'])}"
        if weather.get("place"):
            line = f"{_escape(weather['place'])}: {line}"
        if now.get("text"):
            line += f". {_escape(now['text'])}"
        lines.append(line)
    elif weather.get("place"):
        lines.append(f"**{_escape(weather['place'])}**")
    if weather.get("hours"):
        lines.append(" · ".join(f"{_escape(hour['time'])} {_number(hour['temp'])}{degree}{_condition(hour)}{_precip(hour)}"
                                for hour in weather["hours"]))
    if weather.get("days"):
        rows = []
        for day in weather["days"]:
            row = f"- **{_escape(day['day'])}:** {_number(day['low'])}–{_number(day['high'])}{degree}"
            if day.get("condition"):
                row += f", {_condition_name(day['condition'])}"
            rows.append(row + _precip(day))
        lines.append("\n".join(rows))
    return lines


def _condition(entry: dict[str, Any]) -> str:
    condition = entry.get("condition")
    return f" {_condition_name(condition).lower()}" if condition else ""


def _condition_name(condition: str) -> str:
    return _CONDITIONS.get(condition) or _escape(condition)


def _precip(entry: dict[str, Any]) -> str:
    return f" ({entry['precip']}% chance of rain)" if entry.get("precip") else ""


def _comparison_markdown(card: dict[str, Any]) -> list[str]:
    parts = []
    for option in card["options"]:
        head = f"**{_escape(option['name'])}**"
        if option.get("recommended"):
            head += " (recommended)"
        lines = [head]
        if option.get("summary"):
            lines.append(_escape(option["summary"]))
        lines.extend(f"{_escape(fact['label'])}: {_escape(fact['value'])}" for fact in option.get("facts", []))
        lines.extend(f"+ {_escape(pro)}" for pro in option.get("pros", []))
        lines.extend(f"− {_escape(con)}" for con in option.get("cons", []))
        parts.append("  \n".join(lines))
    return parts


def _steps_markdown(card: dict[str, Any]) -> list[str]:
    parts = []
    if card.get("needs"):
        parts.append("\n".join(f"- {_escape(need)}" for need in card["needs"]))
    rows = []
    for number, step in enumerate(card["steps"], start=1):
        row = f"{number}. {_escape(step['text'])}"
        if step.get("timer"):
            row += f" ({_duration(step['timer'])})"
        rows.append(row)
    parts.append("\n".join(rows))
    return parts


def _duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{hours} h" if hours else "", f"{minutes} min" if minutes else "", f"{seconds} s" if seconds else ""]
    return " ".join(part for part in parts if part)


def _number(value: float) -> str:
    """Plain digits, never an exponent: up to four decimals, trailing zeros dropped."""
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    if text in ("0", "-0") and value:
        # Too small for four decimals: as many as it takes.
        text = f"{value:.12f}".rstrip("0")
    return "0" if text in ("-0", "") else text


def _media_markdown(card: dict[str, Any]) -> list[str]:
    rows = []
    for item in card["media"]:
        name = _escape(item.get("title") or _MEDIA_KINDS[item["kind"]])
        row = f"- [{name}]({item['url']})"
        if item.get("by"):
            row += f" · {_escape(item['by'])}"
        if item.get("duration"):
            row += f" ({_duration(item['duration'])})"
        if item.get("caption"):
            row += f": {_escape(item['caption'])}"
        rows.append(row)
    return ["\n".join(rows)]


_MEDIA_KINDS = {"image": "Photo", "video": "Video", "audio": "Audio"}


def _contact_markdown(card: dict[str, Any]) -> list[str]:
    parts = []
    for person in card["people"]:
        head = f"**{_escape(person['name'])}**"
        if person.get("detail"):
            head += f" · {_escape(person['detail'])}"
        lines = [head]
        lines.extend(_escape(person[key]) for key in ("phone", "email", "address") if person.get(key))
        if person.get("url"):
            lines.append(f"[Website]({person['url']})")
        if person.get("note"):
            lines.append(f"_{_escape(person['note'])}_")
        if person.get("message"):
            lines.append(f"Text to send: {_escape(person['message'])}")
        parts.append("  \n".join(lines))
    return parts


_PHASES = {"waiting": "Not started", "active": "", "done": "Done", "problem": "Problem"}


def _status_markdown(card: dict[str, Any]) -> list[str]:
    status = card["status"]
    head = f"**{_escape(status['state'])}**"
    phase = _PHASES.get(status.get("phase", "active"), "")
    if phase and phase.lower() != status["state"].lower():
        head += f" ({phase})"
    facts = []
    if "progress" in status:
        # Done is complete, as Conduit draws it, whatever was last reported.
        facts.append(f"{100 if status.get('phase') == 'done' else round(status['progress'] * 100)}%")
    if status.get("eta"):
        facts.append(_escape(status["eta"]))
    if facts:
        head += " · " + " · ".join(facts)
    lines = [head]
    if status.get("detail"):
        lines.append(_escape(status["detail"]))
    if status.get("milestones"):
        lines.append(" → ".join(("✓ " if stage.get("done") else "") + _escape(stage["label"])
                                for stage in status["milestones"]))
    return ["  \n".join(lines)]


def _table_markdown(card: dict[str, Any]) -> list[str]:
    table = card["table"]
    rows = ["| " + " | ".join(_escape(name) for name in table["columns"]) + " |",
            "|" + "---|" * len(table["columns"])]
    rows.extend("| " + " | ".join(_escape(cell) for cell in row) + " |" for row in table["rows"])
    return ["\n".join(rows)]


def _timeline_markdown(card: dict[str, Any]) -> list[str]:
    parts = []
    for day in card["timeline"]:
        head = f"**{_escape(day['label'])}**"
        if day.get("title"):
            head += f" · {_escape(day['title'])}"
        rows = [head]
        for stop in day["stops"]:
            row = "- "
            if stop.get("time"):
                row += f"{_escape(stop['time'])}: "
            row += _escape(stop["text"])
            if stop.get("place"):
                row += f" ([{_escape(stop['place'])}](https://maps.apple.com/?q={quote(stop['place'], safe=',')}))"
            if stop.get("note"):
                row += f". _{_escape(stop['note'])}_"
            rows.append(row)
        parts.append("\n".join(rows))
    return parts


def _checklist_markdown(card: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    rows: list[str] = []
    group = None
    for item in card["checklist"]:
        if item.get("group") != group:
            if rows:
                parts.append("\n".join(rows))
            group = item.get("group")
            rows = [f"**{_escape(group)}**"] if group else []
        rows.append(f"- [ ] {_escape(item['text'])}")
    if rows:
        parts.append("\n".join(rows))
    return parts


def _list_markdown(card: dict[str, Any]) -> list[str]:
    rows = []
    for number, item in enumerate(card["list"], start=1):
        name = _escape(item["title"])
        if item.get("url"):
            name = f"[{name}]({item['url']})"
        row = f"{number}. **{name}**" if card.get("ranked") else f"- **{name}**"
        if item.get("value"):
            row += f" · {_escape(item['value'])}"
        if item.get("detail"):
            row += f": {_escape(item['detail'])}"
        rows.append(row)
    return ["\n".join(rows)]


def _progress_markdown(card: dict[str, Any]) -> list[str]:
    rows = []
    for item in card["progress"]:
        unit = f" {_escape(item['unit'])}" if item.get("unit") else ""
        share = round(min(item["value"] / item["goal"], 1) * 100)
        rows.append(f"- **{_escape(item['label'])}:** {_number(item['value'])} of {_number(item['goal'])}{unit} "
                    f"({share}%)")
    return ["\n".join(rows)]


_TONES = {"tip": "Tip", "note": "Note", "warning": "Warning"}


def _callout_markdown(card: dict[str, Any]) -> list[str]:
    callout = card["callout"]
    return [f"> **{_TONES[callout.get('tone', 'note')]}:** {_escape(callout['text'])}"]


# How many files and lines a diff's Markdown shows, so a reply stays a reply.
MARKDOWN_DIFF_FILES = 20
MARKDOWN_DIFF_LINES = 40
_DIFF_STATUS = {"added": " (new)", "deleted": " (deleted)", "renamed": "", "modified": ""}


def _diff_markdown(card: dict[str, Any]) -> list[str]:
    diff = card["diff"]
    files = diff["files"]
    count = len(files) + diff.get("more_files", 0)
    parts = [f"**+{diff['added']:,} \u2212{diff['removed']:,}** in {count:,} file{'s' if count != 1 else ''}"]
    rows = []
    for file in files[:MARKDOWN_DIFF_FILES]:
        name = _escape(file["path"])
        if file["status"] == "renamed" and file.get("old_path"):
            name = f"{_escape(file['old_path'])} \u2192 {name}"
        row = f"- {name}{_DIFF_STATUS[file['status']]}"
        if file.get("binary"):
            row += " · binary"
        elif file["added"] or file["removed"]:
            row += f" · +{file['added']:,} \u2212{file['removed']:,}"
        elif file["status"] == "renamed":
            row += " · renamed"
        rows.append(row)
    more = count - min(len(files), MARKDOWN_DIFF_FILES)
    if more:
        rows.append(f"- and {more:,} more file{'s' if more != 1 else ''}")
    parts.append("\n".join(rows))
    body: list[str] = []
    left = MARKDOWN_DIFF_LINES
    total = diff_lines(diff)
    for file in files:
        if left <= 0 or not file.get("hunks"):
            continue
        old = file.get("old_path", file["path"])
        body.append("--- /dev/null" if file["status"] == "added" else f"--- a/{old}")
        body.append("+++ /dev/null" if file["status"] == "deleted" else f"+++ b/{file['path']}")
        for hunk in file["hunks"]:
            if left <= 0:
                break
            shown = hunk["lines"][:left]
            olds = sum(1 for line in shown if line[0] != "+")
            news = sum(1 for line in shown if line[0] != "-")
            head = f"@@ -{hunk['old_start']},{olds} +{hunk['new_start']},{news} @@"
            body.append(f"{head} {hunk['context']}" if hunk.get("context") else head)
            body.extend(shown)
            left -= len(shown)
    if body:
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", "\n".join(body))), default=0))
        parts.append(f"{fence}diff\n" + "\n".join(body) + f"\n{fence}")
        hidden = total - (MARKDOWN_DIFF_LINES - left)
        if hidden > 0:
            parts.append(f"_\u2026 {hidden:,} more line{'s' if hidden != 1 else ''}_")
    return parts


_INFO_MARKDOWN = {"diff": _diff_markdown, "stats": _stats_markdown, "chart": _chart_markdown, "weather": _weather_markdown,
                  "comparison": _comparison_markdown, "steps": _steps_markdown, "media": _media_markdown,
                  "contact": _contact_markdown, "status": _status_markdown, "table": _table_markdown,
                  "timeline": _timeline_markdown, "checklist": _checklist_markdown, "list": _list_markdown,
                  "progress": _progress_markdown, "callout": _callout_markdown}


def _sourced_name(source: dict[str, Any]) -> str:
    name = _escape(source["name"])
    return f"[{name}]({source['url']})" if source.get("url") else name


def _sourced(fact: dict[str, Any]) -> str:
    source = _escape(fact["source"])
    return f"[{source}]({fact['url']})" if fact.get("url") else source


def _maps_query(place: dict[str, Any]) -> str:
    if "lat" in place and "lon" in place:
        return f"{place['lat']:.6f},{place['lon']:.6f}"
    return ", ".join(part for part in (place["name"], place.get("address")) if part)


def _maps_params(place: dict[str, Any]) -> str:
    params = [("q", place["name"])]
    if "lat" in place and "lon" in place:
        params.append(("ll", f"{place['lat']:.6f},{place['lon']:.6f}"))
    elif place.get("address"):
        params.append(("address", place["address"]))
    return "&".join(f"{key}={quote(value, safe=',')}" for key, value in params)


def _escape(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>|])", r"\\\1", text)
