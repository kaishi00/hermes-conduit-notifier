"""Conduit's rich cards: maps, places, stats, charts, forecasts, comparisons,
steps, media, contacts and statuses Conduit draws natively, checked against
the one schema both sides read (``cards/card-schema.json``).

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
  artwork; a contact keeps only a phone and an email that look like one;
  a live name is kept on the first card of a set that has it.

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
    live = set()
    for raw in value:
        if len(cards) == MAX_CARDS:
            break
        card = normalize_card(raw)
        if card is None:
            continue
        if "live" in card:
            if card["live"] in live:
                del card["live"]
            else:
                live.add(card["live"])
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


# Each type's own field; a card keeps it and the shared fields, and nothing else.
_TYPE_FIELDS = {"map": ("places", "route"), "place": ("places",), "places": ("places",), "stats": ("stats",),
                "chart": ("chart",), "weather": ("weather",), "comparison": ("options",), "steps": ("steps", "needs"),
                "media": ("media",), "contact": ("people",), "status": ("status",)}
PLACE_TYPES = ("map", "place", "places")
# The information cards (plugin 0.46+), media and contacts, and statuses
# (0.47+): a Conduit draws each group only when its exchange names it.
INFO_TYPES = ("stats", "chart", "weather", "comparison", "steps")
MEDIA_TYPES = ("media", "contact")
LIVE_TYPES = ("status",)
FEATURE_TYPES = {"info-cards": INFO_TYPES, "media-cards": MEDIA_TYPES, "live-cards": LIVE_TYPES}
# A phone number's characters, and an address with nothing that could end a
# link or a header early.
_PHONE = re.compile(r"^[0-9+\-(). ]+$")
_EMAIL = re.compile(r"^[^@\s<>()\[\]\"'`,;:\\]+@[^@\s<>()\[\]\"'`,;:\\]+\.[^@\s<>()\[\]\"'`,;:\\.]+$")


def _apply_rules(card: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any] | None:
    kind = card["type"]
    if kind not in _TYPE_FIELDS:
        return None
    keep = {"type", "title", "live", *_TYPE_FIELDS[kind]}
    if kind not in PLACE_TYPES:
        keep.add("source")
    card = {key: value for key, value in card.items() if key in keep}
    field = _TYPE_FIELDS[kind][0]
    if field not in card:
        return None
    if kind in PLACE_TYPES:
        return _place_rules(card, raw)
    if kind == "chart":
        chart = _chart_rules(card["chart"])
        if chart is None:
            return None
        card["chart"] = chart
    elif kind == "weather":
        weather = card["weather"]
        if "days" in weather:
            weather["days"] = [day for day in weather["days"] if day["low"] <= day["high"]]
        for key in ("hours", "days"):
            if key in weather and not weather[key]:
                del weather[key]
        if "source" not in card or not any(key in weather for key in ("now", "hours", "days")):
            return None
    elif kind == "comparison":
        seen = False
        for option in card["options"]:
            if option.get("recommended"):
                if seen:
                    del option["recommended"]
                seen = True
    elif kind == "media":
        if "source" not in card:
            return None
        # The phone loads media over https only.
        card["media"] = [item for item in card["media"] if _is_https(item["url"])]
        if not card["media"]:
            return None
        for item in card["media"]:
            if item["kind"] == "image" or not _is_https(item.get("artwork", "https://")):
                item.pop("artwork", None)
            if item["kind"] == "image":
                item.pop("duration", None)
    elif kind == "contact":
        for person in card["people"]:
            if "phone" in person and not _phone_ok(person["phone"]):
                del person["phone"]
            if "email" in person and not _EMAIL.match(person["email"]):
                del person["email"]
            # The phone loads photos over https only.
            if "photo" in person and not _is_https(person["photo"]["url"]):
                del person["photo"]
    return card


def _is_https(url: str) -> bool:
    return url[:8].lower() == "https://"


def _phone_ok(phone: str) -> bool:
    return bool(_PHONE.match(phone)) and 3 <= sum(char.isdigit() for char in phone) <= 20


def _place_rules(card: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    places = card["places"]
    if card["type"] == "place":
        card["places"] = places[:1]
    # The phone loads photos over https only.
    for place in card["places"]:
        if "photo" in place and not _is_https(place["photo"]["url"]):
            del place["photo"]
    route = card.pop("route", None)
    # Only a map carries a route (_TYPE_FIELDS), so this is for maps alone.
    if route is not None:
        given = raw.get("places")
        survived = isinstance(given, list) and len(given) == len(places)
        to, start = route["to"], route.get("from")
        if survived and to < len(places) and (start is None or (start < len(places) and start != to)):
            card["route"] = route
    return card


def _chart_rules(chart: dict[str, Any]) -> dict[str, Any] | None:
    count = len(chart["labels"])
    series = [line for line in chart["series"] if len(line["values"]) == count]
    if chart["kind"] == "pie":
        series = series[:1]
        if series and any(value < 0 for value in series[0]["values"]):
            series = []
    if not series:
        return None
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
        facts.append(f"{round(status['progress'] * 100)}%")
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


_INFO_MARKDOWN = {"stats": _stats_markdown, "chart": _chart_markdown, "weather": _weather_markdown,
                  "comparison": _comparison_markdown, "steps": _steps_markdown, "media": _media_markdown,
                  "contact": _contact_markdown, "status": _status_markdown}


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
