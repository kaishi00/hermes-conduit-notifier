"""Conduit's rich cards: maps and places Conduit draws natively, checked
against the one schema both sides read (``cards/card-schema.json``).

Hermes fills in a card's facts (a place's name, where it is, a rating it
found and where from); Conduit owns the look, and the phone adds what Apple
Maps knows itself at draw time (address, category, phone, website, travel
time, Look Around). So the schema has no fields for those, and a rating,
hours, price or photo without a named source is left out rather than shown
as fact.

Normalizing follows the schema and is the same in Conduit's Swift
(``RichCardSchema``); ``cards/card-vectors.json`` holds the examples both
test suites run, so the two can't drift:
- unknown keys are dropped;
- text has control characters turned into spaces, is trimmed, and is cut to
  ``maxLength`` code points (a URL that's too long, or holds a space, quote,
  bracket or parenthesis, is invalid instead);
- an invalid optional value is dropped, an invalid list item is dropped
  (then the list is cut to ``maxItems``), and an invalid required value
  makes its object invalid;
- enums match without regard to case and come out as written in the schema;
- then the schema's ``rules``: a route only on a map whose places all
  survived, naming places in the list, from one to another; a place card
  keeps its first place.

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


def normalize_cards(value: Any) -> list[dict[str, Any]]:
    """The valid cards in ``value``, normalized; invalid ones are dropped."""
    if not isinstance(value, list):
        return []
    cards = []
    for raw in value:
        if len(cards) == MAX_CARDS:
            break
        card = normalize_card(raw)
        if card is not None:
            cards.append(card)
    return cards


def normalize_card(raw: Any) -> dict[str, Any] | None:
    card = _normalize(CARD_SCHEMA, raw)
    if card is _INVALID:
        return None
    return _apply_rules(card, raw)


def _normalize(schema: dict[str, Any], value: Any) -> Any:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return _INVALID
        required = set(schema.get("required", []))
        result: dict[str, Any] = {}
        for key, inner in schema.get("properties", {}).items():
            if key not in value or value[key] is None:
                if key in required:
                    return _INVALID
                continue
            clean = _normalize(inner, value[key])
            if clean is _INVALID:
                if key in required:
                    return _INVALID
                continue
            result[key] = clean
        return result
    if kind == "array":
        if not isinstance(value, list):
            return _INVALID
        limit = schema.get("maxItems")
        items = []
        for raw in value:
            if limit is not None and len(items) == limit:
                break
            item = _normalize(schema["items"], raw)
            if item is not _INVALID:
                items.append(item)
        if len(items) < schema.get("minItems", 0):
            return _INVALID
        return items
    if kind == "string":
        if not isinstance(value, str):
            return _INVALID
        text = clean_text(value)
        if not text:
            return _INVALID
        limit = schema.get("maxLength")
        if schema.get("format") == "uri":
            if not _URI.match(text) or (limit is not None and len(text) > limit):
                return _INVALID
            return text
        if limit is not None:
            text = text[:limit].rstrip()
        if "enum" in schema:
            for option in schema["enum"]:
                if option.lower() == text.lower():
                    return option
            return _INVALID
        return text
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _INVALID
        if isinstance(value, float) and not math.isfinite(value):
            return _INVALID
        if kind == "integer":
            if isinstance(value, float):
                if not value.is_integer():
                    return _INVALID
                value = int(value)
        if "minimum" in schema and value < schema["minimum"]:
            return _INVALID
        if "maximum" in schema and value > schema["maximum"]:
            return _INVALID
        return value
    if kind == "boolean":
        return value if isinstance(value, bool) else _INVALID
    return _INVALID


def clean_text(text: str) -> str:
    """Control characters become spaces; trimmed."""
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text).strip()


def _apply_rules(card: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    places = card["places"]
    if card["type"] == "place":
        card["places"] = places[:1]
    route = card.pop("route", None)
    if route is not None and card["type"] == "map":
        given = raw.get("places")
        survived = isinstance(given, list) and len(given) == len(places)
        to, start = route["to"], route.get("from")
        if survived and to < len(places) and (start is None or (start < len(places) and start != to)):
            card["route"] = route
    return card


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
