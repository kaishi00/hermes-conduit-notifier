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

import json
import math
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
    """The ``cards`` parameter of a tool: a short list of cards."""
    return {"type": "array", "maxItems": MAX_CARDS, "description": description, "items": CARD_SCHEMA}


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
    raw = _prepared(raw)
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


def _prepared(raw: Any) -> Any:
    """A table's cells and column names as text, so a number, a blank or a
    null keeps its place in the row: a string stays as it is (an empty one
    is \u2013), a number becomes its digits, anything else is \u2013."""
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or raw["type"].lower() != "table":
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
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text).strip()


# Each type's own field; a card keeps it and the shared fields, and nothing else.
_TYPE_FIELDS = {"map": ("places", "route"), "place": ("places",), "places": ("places",), "stats": ("stats",),
                "chart": ("chart",), "weather": ("weather",), "comparison": ("options",), "steps": ("steps", "needs"),
                "media": ("media",), "contact": ("people",), "status": ("status",), "table": ("table",),
                "timeline": ("timeline",), "checklist": ("checklist",), "list": ("list", "ranked"),
                "progress": ("progress",), "callout": ("callout",)}
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
FEATURE_TYPES = {"info-cards": INFO_TYPES, "media-cards": MEDIA_TYPES, "live-cards": LIVE_TYPES,
                 BOARD_FEATURE: BOARD_TYPES}
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
    return card


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


_INFO_MARKDOWN = {"stats": _stats_markdown, "chart": _chart_markdown, "weather": _weather_markdown,
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
