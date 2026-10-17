"""Hermes puts something on the screen of the user's voice call: the
``conduit_show`` tool.

During a spoken conversation in Conduit (Local Live, Classic voice, or a
live call that hands work to Hermes), a table, a chart, steps or links are
better seen than heard. The tool keeps a card (present_store) that the phone
picks up on its next exchange, which it runs as soon as it sees the tool in
the turn and again when the turn ends. Conduit opens the card over the call;
Hermes says in a sentence that it's there. A card nobody picks up within ten
minutes is dropped: it belongs to the conversation it was shown in.

Maps and places go as rich cards (``cards``, checked against
``card_schema``): Conduit draws them natively, in a typed chat as well as on
a call, and fills in what Apple Maps knows on the phone. The call's normalized
cards come back in the tool's answer, so they stay in the chat's history and
Conduit draws them again whenever the chat is opened. Without ``markdown``,
the cards' own Markdown is shown where they can't be drawn.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import card_schema, client, reply_sources
from .present_store import MAX_CARD_MARKDOWN_CHARS, MAX_CARD_TITLE_CHARS, PresentError, PresentStore

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_show"
TOOLSET = "conduit"

CHAT_CARDS_FEATURE = "chat-cards"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Show something on the user's phone screen in Conduit. On a voice call, use it for anything better seen "
        "than heard: tables, charts, forecasts, recipes and other step-by-step instructions, lists, comparisons, "
        "code, images and links, and whenever the user asks to see or show something. Write Markdown: tables for "
        "data, numbered lists for steps, ```mermaid blocks for charts, ![description](url) for images (direct image "
        "URLs you actually found) and [title](url) for links. Then say in a sentence that it's on their screen and "
        "give the gist; don't read it out, and don't repeat it in your reply. For places, on a call or in a typed "
        "chat, use cards instead: a map with pins and a route, one place in detail, or several to swipe through. "
        "Conduit draws them and adds what Apple Maps knows (address, phone, website, travel time), so give names, "
        "addresses, coordinates or conduit_maps place ids, and only ratings, hours, prices and photos you actually "
        "found, each with its source. In a typed chat, use it only for cards and write the rest in your reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": MAX_CARD_TITLE_CHARS,
                      "description": f"A short heading, a few words (up to {MAX_CARD_TITLE_CHARS} characters)."},
            "markdown": {"type": "string", "maxLength": MAX_CARD_MARKDOWN_CHARS,
                         "description": f"What to show, as Markdown (up to {MAX_CARD_MARKDOWN_CHARS} characters). "
                                        "Leave it out when cards say it all."},
            "cards": card_schema.tool_parameter("Maps and places for Conduit to draw, shown above the Markdown."),
        },
        "required": ["title"],
    },
}

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
        return {"ok": False, "error": "invalid_cards",
                "note": "None of the cards could be shown: each needs a type (map, place or places) and places "
                        "with names."}
    markdown = args.get("markdown")
    if cards and (not isinstance(markdown, str) or not markdown.strip()):
        markdown = _fallback(cards)
    if cards and _turn_source() not in (None, "conduit"):
        # Desktop, a messaging app or the terminal draws no cards.
        return {"ok": False, "error": "not_conduit",
                "note": "This conversation isn't open in Conduit, so cards can't be drawn. Write it in your "
                        "reply instead."}
    store = _store()
    try:
        card = store.show(args.get("title"), markdown, session_id=session, cards=cards)
    except PresentError as error:
        if error.code == "no_device" and cards and store.has_feature(CHAT_CARDS_FEATURE):
            # No call screen to put it on, but the chat draws it from this
            # answer.
            return {"ok": True, "cards": cards, "note": IN_CHAT_NOTE}
        note = str(error)
        if error.code in ("no_device", "rate_limited"):
            note += " Tell the user the gist instead."
        return {"ok": False, "error": error.code, "note": note}
    answer: dict[str, Any] = {"ok": True, "id": card["id"]}
    if cards:
        # The chat's history keeps the cards from here.
        answer["cards"] = cards
    answer["note"] = SHOWN_NOTE
    return answer


def _fallback(cards: list[dict[str, Any]]) -> str:
    """The cards' Markdown, cut at a line to fit a card."""
    text = card_schema.fallback_markdown(cards)
    if len(text) <= MAX_CARD_MARKDOWN_CHARS:
        return text
    cut = text[:MAX_CARD_MARKDOWN_CHARS]
    return cut[:cut.rfind("\n")] if "\n" in cut else cut


def _turn_source() -> str | None:
    try:
        return reply_sources.turn_source(None, reply_sources.current_transport())
    except Exception:  # noqa: BLE001 — a lookup that failed draws no cards
        return "unknown"
