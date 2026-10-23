"""Hermes puts something on the screen of the user's voice call: the
``conduit_show`` tool.

During a spoken conversation in Conduit (Local Live, Classic voice, or a
live call that hands work to Hermes), a table, a chart, steps or links are
better seen than heard. The tool keeps a card (present_store) that the phone
picks up on its next exchange, which it runs as soon as it sees the tool in
the turn and again when the turn ends. Conduit opens the card over the call;
Hermes says in a sentence that it's there. A card nobody picks up within ten
minutes is dropped: it belongs to the conversation it was shown in.

Maps, places, stats, charts, forecasts, comparisons, steps, media, contacts
and statuses go as rich cards (``cards``, checked against ``card_schema``): Conduit draws them
natively, in a typed chat as well as on a call, and fills in what Apple Maps
knows on the phone. The call's normalized
cards come back in the tool's answer, so they stay in the chat's history and
Conduit draws them again whenever the chat is opened. Without ``markdown``,
the cards' own Markdown is shown where they can't be drawn. A card with a
``live`` name replaces the conversation's earlier card of that name, so
Hermes keeps a status up to date by showing it again. A contact card can
carry a text message Hermes wrote for the user to send; Conduit opens
Messages with it filled in and the user taps Send, so nothing is ever sent
for them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import card_schema, client, reply_sources
from .present_store import (CHAT_CARDS_FEATURE, MAX_CARD_MARKDOWN_CHARS, MAX_CARD_TITLE_CHARS, PresentError,
                            PresentStore, clip_markdown)

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_show"
# A Conduit that offers Send text for a contact card's message says this in
# its exchange (plugin 0.67+).
TEXT_DRAFTS_FEATURE = "text-drafts"
TOOLSET = "conduit"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Show something on the user's phone screen in Conduit. On a voice call, use it for anything better seen "
        "than heard: tables, charts, forecasts, recipes and other step-by-step instructions, lists, comparisons, "
        "code, images and links, and whenever the user asks to see or show something. Prefer cards, which Conduit "
        "draws natively, on a call or in a typed chat: a map with pins and a route, one place in detail, several "
        "places to swipe through, big numbers (stats), a line, bar or pie chart, a weather forecast, a comparison "
        "of two to four options, steps the user can tick off (with timers), photos, videos or audio to play, people "
        "to call, message or email, or the status of something in progress (a delivery, a flight, a match, a job). "
        "When the user asks you to text someone, show a contact card for them with their phone and the message "
        "you wrote: Conduit offers Send text, which opens Messages with it filled in, and the user taps Send there. "
        "Say it's ready to send, never that it was sent. "
        "To keep a card up to date, give it a live name and show the whole card again with the same name as "
        "things change: it replaces the earlier one. For places, Conduit adds what Apple "
        "Maps knows (address, phone, website, travel time), so give names, addresses, coordinates or conduit_maps "
        "place ids, and only ratings, hours, prices and photos you actually found, each with its source; never draw "
        "a map of your own as an HTML page. Name the "
        "source of numbers, forecasts, media and statuses you got from a tool or site; a forecast and media need "
        "one, and media must be direct https links to files you actually found. For anything else, "
        "write Markdown: tables for data, numbered lists for steps, ![description](url) for images (direct image "
        "URLs you actually found) and [title](url) for links. Then say in a sentence that it's on their screen and "
        "give the gist; don't read it out, and don't repeat it in your reply. In a typed chat, use it only for "
        "cards and write the rest in your reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": MAX_CARD_TITLE_CHARS,
                      "description": f"A short heading, a few words (up to {MAX_CARD_TITLE_CHARS} characters)."},
            "markdown": {"type": "string", "maxLength": MAX_CARD_MARKDOWN_CHARS,
                         "description": f"What to show, as Markdown (up to {MAX_CARD_MARKDOWN_CHARS} characters). "
                                        "Leave it out when cards say it all."},
            "cards": card_schema.tool_parameter("Cards for Conduit to draw, shown above the Markdown."),
        },
        "required": ["title"],
    },
}

INVALID_CARDS_NOTE = ("None of the cards could be shown: each needs a type and that type's field (places with "
                      "names for map, place and places, people for contact; stats, chart, weather, options, steps, "
                      "media or status for the others), "
                      "and a forecast and media need their source.")
UNSUPPORTED_CARDS_NOTE = ("This version of Conduit can't draw these cards. On a call, show it as Markdown instead; "
                          "in a typed chat, write it in your reply.")
LEFT_OUT_NOTE = ("This version of Conduit can't draw {kinds} cards, so they weren't shown: give what they held in "
                 "your reply.")
NO_TEXT_DRAFTS_NOTE = ("This version of Conduit can't fill in a text message, so the message was left off the card: "
                       "give the user the text in your reply.")
SHOWN_NOTE = ("On the user's screen. Say in a sentence that it's there and give the gist; don't read it out or "
              "repeat it in your reply.")
IN_CHAT_NOTE = ("The cards are shown in the chat; Markdown isn't, so put anything else in your reply. Give the "
                "gist in a sentence or two; don't repeat the cards' details.")


def register(ctx: Any, *, is_child: Callable[[Any], bool] = lambda _: False) -> None:
    """Registers the tool, if this Hermes supports plugin tools."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child)

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🖼️")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def _store() -> PresentStore:
    return PresentStore(get_hermes_home())


def available() -> bool:
    """Shown to the model only while a Conduit that shows cards on a call has
    exchanged with this profile recently, or a Conduit that draws cards in
    chats has exchanged with it at all."""
    try:
        if client.load_state() is None:
            return False
        store = _store()
        return store.can_show() or store.has_feature(CHAT_CARDS_FEATURE)
    except Exception:  # noqa: BLE001
        return False


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False) -> str:
    try:
        return json.dumps(_handle(args if isinstance(args, dict) else {}, session_id, is_child), ensure_ascii=False)
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_show failed", exc_info=True)
        return json.dumps({"ok": False, "error": "unavailable",
                           "note": "It couldn't be shown. Tell the user the gist instead."})


def _handle(args: dict[str, Any], session_id: Any, is_child: Callable[[Any], bool]) -> dict[str, Any]:
    session = session_id.strip() if isinstance(session_id, str) else ""
    # The user's screen belongs to the conversation they're in, not to a
    # subagent's work.
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the main conversation can show things on the user's screen. Report back instead."}
    given = args.get("cards")
    cards = card_schema.normalize_cards(given)
    if given not in (None, []) and not cards:
        return {"ok": False, "error": "invalid_cards", "note": INVALID_CARDS_NOTE}
    if cards and _turn_source() not in (None, "conduit"):
        # Desktop, a messaging app or the terminal draws no cards.
        return {"ok": False, "error": "not_conduit",
                "note": "This conversation isn't open in Conduit, so cards can't be drawn. Write it in your "
                        "reply instead."}
    store = _store()
    markdown = args.get("markdown")
    given_markdown = isinstance(markdown, str) and bool(markdown.strip())
    left_out: list[str] = []
    hidden = {kind for feature, kinds in card_schema.FEATURE_TYPES.items()
              if any(card["type"] in kinds for card in cards) and not store.has_feature_everywhere(feature)
              for kind in kinds}
    if hidden:
        # An older Conduit would draw nothing for them, and the chat keeps
        # them for whichever of the user's Conduits opens it; Hermes hears
        # which ones so it can say what they held.
        left_out = list(dict.fromkeys(card["type"] for card in cards if card["type"] in hidden))
        cards = [card for card in cards if card["type"] not in hidden]
        if not cards and not given_markdown:
            return {"ok": False, "error": "unsupported_cards", "note": UNSUPPORTED_CARDS_NOTE}
    no_texts = _has_messages(cards) and not store.has_feature_everywhere(TEXT_DRAFTS_FEATURE)
    if no_texts:
        # An older Conduit would show the person with no Send text, and
        # Hermes would think the message was waiting on the user's phone.
        cards = _without_messages(cards)
    fallback = bool(cards) and not given_markdown
    if fallback:
        markdown = _fallback(cards)
    try:
        card = store.show(args.get("title"), markdown, session_id=session, cards=cards,
                          markdown_is_fallback=fallback)
    except PresentError as error:
        if error.code == "no_device" and cards and store.has_feature(CHAT_CARDS_FEATURE):
            # No call screen to put it on, but the chat draws it from this
            # answer.
            return {"ok": True, "cards": cards, "note": _with_left_out(IN_CHAT_NOTE, left_out, no_texts)}
        note = str(error)
        if error.code in ("no_device", "rate_limited"):
            note += " Tell the user the gist instead."
        return {"ok": False, "error": error.code, "note": _with_left_out(note, left_out, no_texts)}
    answer: dict[str, Any] = {"ok": True, "id": card["id"]}
    if cards:
        # The chat's history keeps the cards from here.
        answer["cards"] = cards
    answer["note"] = _with_left_out(SHOWN_NOTE, left_out, no_texts)
    return answer


def _with_left_out(note: str, left_out: list[str], no_texts: bool = False) -> str:
    if left_out:
        note = f"{note} {LEFT_OUT_NOTE.format(kinds=', '.join(left_out))}"
    return f"{note} {NO_TEXT_DRAFTS_NOTE}" if no_texts else note


def _has_messages(cards: list[dict[str, Any]]) -> bool:
    return any("message" in person for card in cards if card["type"] == "contact" for person in card["people"])


def _without_messages(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**card, "people": [{key: value for key, value in person.items() if key != "message"}
                                for person in card["people"]]} if card["type"] == "contact" else card
            for card in cards]


def _fallback(cards: list[dict[str, Any]]) -> str:
    """The cards' Markdown, cut at a line to fit a card."""
    return clip_markdown(card_schema.fallback_markdown(cards))


def _turn_source() -> str | None:
    try:
        return reply_sources.turn_source(reply_sources.current_platform(), reply_sources.current_transport())
    except Exception:  # noqa: BLE001 — a lookup that failed draws no cards
        return "unknown"
