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
Hermes keeps a status up to date by showing it again.

A set of cards is a board: tables, timelines, checklists, lists, progress and
callouts sit beside the rest, ``layout`` "tabs" shows one card at a time, and
``next`` offers the follow-ups a tap sends. The answer says exactly what was
dropped and why (``problems``), so Hermes can fix it and show it again, and
always carries the cards' Markdown, which Conduit shows wherever a card can't
be drawn. Outside Conduit the answer hands Hermes that Markdown for its reply. A contact card can
carry a text message Hermes wrote for the user to send; Conduit opens
Messages with it filled in and the user taps Send, so nothing is ever sent
for them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import card_schema, card_templates, client, reply_sources, turn_surface
from .present_store import (CHAT_CARDS_FEATURE, MAX_CARD_MARKDOWN_CHARS, MAX_CARD_TITLE_CHARS, PresentError,
                            PresentStore, clip_markdown)

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_show"
# The board recipes (skills/drawing-boards).
SKILL_NAME = "drawing-boards"
# A Conduit that offers Send text for a contact card's message says this in
# its exchange (plugin 0.67+).
TEXT_DRAFTS_FEATURE = "text-drafts"
# Conduit draws a room's boards under its agents' messages (room_boards).
ROOM_BOARDS_FEATURE = "room-boards"
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
        "to call, message or email, the status of something in progress (a delivery, a flight, a match, a job), "
        "a table to sort, a day-by-day plan (timeline), a checklist whose ticks are kept, a ranked list, progress "
        "toward goals, a tip or warning (callout), or a diff of changes to code, a config or any text (give a patch, "
        "a patch_file for a long one, or before and after; put the pull request or commit in source), or a composed "
        "card you build from text, metrics, badges, icons, images, progress bars, fields and buttons for anything "
        "else (an email ready to send, a day's agenda, an idea to act on). Any card can carry up to three buttons "
        "(actions): open a link, copy, send a message as the user, or tap, which reaches you at once as the user's "
        "message with the card's field values: do it with your tools, then show the card again under its live name "
        "with the result. A tap is the user's OK for exactly what the card showed, so put what will happen on the "
        "card and give a send, buy or delete button a confirm. Save a composed card you'll show again with "
        "save_templates and show it by name (type template). Up to 12 "
        "cards make a board: put a short title on each, use "
        "layout tabs for one card at a time, and give next: the follow-ups the user would likely send, which a tap "
        "sends. "
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
        "cards and write the rest in your reply: Markdown alone shows nothing there. If the answer lists "
        f"problems, fix those cards and show the whole board again. For a trip plan, a comparison, a dashboard or a tracker, load "
        f"skill_view(\"conduit_push:{SKILL_NAME}\") first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": MAX_CARD_TITLE_CHARS,
                      "description": f"A short heading, a few words (up to {MAX_CARD_TITLE_CHARS} characters)."},
            "markdown": {"type": "string", "maxLength": MAX_CARD_MARKDOWN_CHARS,
                         "description": f"What to show, as Markdown (up to {MAX_CARD_MARKDOWN_CHARS} characters). "
                                        "Leave it out when cards say it all."},
            "cards": card_schema.tool_parameter("Cards for Conduit to draw, shown above the Markdown.",
                                                {"template": card_templates.TEMPLATE_INPUT}),
            "from_work": {"type": "string",
                          "description": "In a Bot Chat: a workstream's ref (\"W3\") whose update says it showed "
                                         "cards in its own chat. Shows those cards here as it drew them; leave "
                                         "out cards, layout and next."},
            "save_templates": card_templates.SAVE_SCHEMA,
            "layout": card_schema.LAYOUT_SCHEMA,
            "next": card_schema.NEXT_SCHEMA,
        },
        "required": ["title"],
    },
}

INVALID_CARDS_NOTE = ("None of the cards could be shown: each needs a type and that type's field (places with "
                      "names for map, place and places, people for contact; stats, chart, weather, options, steps, "
                      "media, status, table (columns and rows), timeline, checklist, list, progress, callout or diff "
                      "(a patch, a patch_file, or before and after) for the others), "
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
TYPED_CHAT_NOTE = ("This message was typed in the chat, which shows cards but not Markdown, so nothing was shown: "
                   "write it in your reply instead.")
CHAT_NO_CARDS_NOTE = ("This version of Conduit draws no cards in a chat. Put the markdown below in your reply "
                      "instead, as it is or tidied up.")
NOT_CONDUIT_NOTE = ("This conversation isn't open in Conduit, so cards can't be drawn. Put the markdown below in "
                    "your reply instead, as it is or tidied up.")
PROBLEMS_NOTE = ("Some of it was left out; problems says what and why. Fix those parts and show the whole board "
                 "again if they matter, or give them in your reply.")
ROOM_NOTE = ("A group room shows messages, not cards. Put the markdown below in your reply instead, as it is or "
             "tidied up.")
OLD_BOARD_NOTE = ("This version of Conduit draws at most {count} cards, one under another, with no follow-ups, so "
                  "the rest weren't shown: give them in your reply.")
OLD_BOARD_LAYOUT_NOTE = ("layout: this version of Conduit draws cards one under another, with no tabs, left out")
OLD_BOARD_NEXT_NOTE = ("next: this version of Conduit draws no follow-ups, left out: offer them in your reply if "
                       "they matter")
ROOM_CONTACTS_NOTE = ("cards: a contact card holds the user's own details and drafts, so it isn't shared into a "
                      "room: leave it out of your reply too")
ROOM_SHOWN_NOTE = ("Shown as cards under your next message in the room, for the people reading it in Conduit. "
                   "The room's other agents read only your message, so give them what they need from it in a "
                   "sentence or two; don't repeat the cards.")
ROOM_MARKDOWN_NOTE = "A group room shows messages, not a screen, so nothing was shown: write it in your reply."
ROOM_NOTHING_NOTE = "A group room shows messages, and nothing on this board can go to one."
ROOM_PROBLEMS_NOTE = ("Some of it was left out; problems says what and why. Give the rest in your reply if it "
                      "matters, never the contact.")
DESKTOP_SHOWN_NOTE = ("Hermes Desktop draws these cards where your reply holds this line, on a line of its own: "
                      "{line} Write it once, where the board goes, and give the gist in a sentence or two; don't "
                      "repeat the cards.")
TEXT_LAYOUT_NOTE = "layout: these messages show no tabs, left out"
DIFF_NOT_KEPT_NOTE = ("cards: a diff was too long to send whole and couldn't be kept on the host, so its card lists "
                      "the files without their lines: give the changes that matter in your reply")
TEXT_NEXT_NOTE = "next: these messages show no follow-ups, left out: offer them in your reply if they matter"
OLD_ACTIONS_NOTE = ("cards: this version of Conduit draws no buttons or fields, so they were left out: offer what they "
                    "did in your reply")
ROOM_ACTIONS_NOTE = ("cards: a tap in a room would speak for the user to everyone in it, so fields and send and tap "
                     "buttons were left out; open and copy buttons stay")
SAVED_NOTE = "Saved. Show a template with a card of type template, its name and the values for its slots."


BOARD_RULE = ("[Conduit draws boards: when an answer reads better seen than read (the weather or a forecast, a "
              "plan by day, a comparison, numbers or a trend, a ranking, a list to tick off, changes to code or a "
              "config as a diff), show it with "
              "conduit_show as cards (through tool_search and tool_call when it's deferred), with next for the "
              "obvious follow-ups, and keep your reply to the gist. Never draw data as an HTML ::preview page.]")
TEMPLATES_RULE = "[Your saved card templates (conduit_show type template): {names}.]"


DESKTOP_BOARD_RULE = BOARD_RULE.replace("[Conduit draws boards", "[Hermes Desktop draws boards")


def board_rule(turn_source: str | None, desktop_half: Callable[[], bool] = lambda: False) -> str | None:
    """The drawing rule for a turn sent from a Conduit that draws boards, or
    from Hermes Desktop with the plugin's Desktop half loaded; nothing
    elsewhere, where boards would only be Markdown."""
    if turn_source == "desktop" and available():
        try:
            return _with_templates(DESKTOP_BOARD_RULE) if _desktop_app() and desktop_half() else None
        except Exception:  # noqa: BLE001
            return None
    if turn_source != "conduit" or not available():
        return None
    try:
        if not _store().has_feature_everywhere(card_schema.BOARD_FEATURE):
            return None
    except Exception:  # noqa: BLE001
        return None
    return _with_templates(BOARD_RULE)


def _with_templates(rule: str) -> str:
    """The drawing rule, and the names of the templates saved here, so the
    model knows what it can fill."""
    try:
        names = card_templates.TemplateStore(get_hermes_home()).names()
    except Exception:  # noqa: BLE001 — the rule goes without them
        names = []
    return f"{rule}\n{TEMPLATES_RULE.format(names=', '.join(names))}" if names else rule


def register(ctx: Any, *, is_child: Callable[[Any], bool] = lambda _: False,
             is_room: Callable[[Any], bool] = lambda _: False,
             room_board: Callable[[Any, dict[str, Any]], bool] = lambda *_: False,
             desktop_board: Callable[[dict[str, Any]], str | None] = lambda _: None,
             keep_diff: Callable[[dict[str, Any]], str | None] = lambda _: None, skill_path: Any = None) -> None:
    """Registers the tool and its recipes, if this Hermes supports plugin
    tools and skills."""
    if skill_path is not None and hasattr(ctx, "register_skill"):
        try:
            ctx.register_skill(SKILL_NAME, skill_path)
        except Exception:  # noqa: BLE001
            logger.warning("conduit_push: could not register the %s skill", SKILL_NAME, exc_info=True)
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child, is_room=is_room, room_board=room_board,
                      desktop_board=desktop_board, keep_diff=keep_diff)

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🖼️")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def _as_text(error: str, note: str, cards: list[dict[str, Any]], problems: list[str]) -> dict[str, Any]:
    """The board as Markdown for a reply, where no cards can be drawn."""
    return _with_problems({"ok": False, "error": error, "note": note, "markdown": _fallback(cards)}, problems)


def _with_problems(answer: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """An answer with what normalization dropped, and what to do about it."""
    if problems:
        # A room's reply never carries the contact it was spared.
        tail = ROOM_PROBLEMS_NOTE if ROOM_CONTACTS_NOTE in problems else PROBLEMS_NOTE
        answer["note"] = f"{answer['note']} {tail}"
        answer["problems"] = problems[:20]
    return answer


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


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False,
           is_room: Callable[[Any], bool] = lambda _: False,
           room_board: Callable[[Any, dict[str, Any]], bool] = lambda *_: False,
           desktop_board: Callable[[dict[str, Any]], str | None] = lambda _: None,
           keep_diff: Callable[[dict[str, Any]], str | None] = lambda _: None) -> str:
    try:
        return json.dumps(_handle(args if isinstance(args, dict) else {}, session_id, is_child, is_room, room_board,
                                  desktop_board, keep_diff), ensure_ascii=False)
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_show failed", exc_info=True)
        return json.dumps({"ok": False, "error": "unavailable",
                           "note": "It couldn't be shown. Tell the user the gist instead."})


def _handle(args: dict[str, Any], session_id: Any, is_child: Callable[[Any], bool],
            is_room: Callable[[Any], bool] = lambda _: False,
            room_board: Callable[[Any, dict[str, Any]], bool] = lambda *_: False,
            desktop_board: Callable[[dict[str, Any]], str | None] = lambda _: None,
            keep_diff: Callable[[dict[str, Any]], str | None] = lambda _: None) -> dict[str, Any]:
    session = session_id.strip() if isinstance(session_id, str) else ""
    # The user's screen belongs to the conversation they're in, not to a
    # subagent's work.
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the main conversation can show things on the user's screen. Report back instead."}
    if args.get("from_work") is not None:
        # A workstream's cards, shown in the Bot Chat, where the user is.
        from . import work_tool

        board, why = work_tool.shown_board(session, args["from_work"])
        if board is None:
            return {"ok": False, "error": "no_cards", "note": why}
        args = {**{key: value for key, value in args.items() if key not in ("from_work", "save_templates")},
                "cards": board["cards"], "layout": board.get("layout"), "next": board.get("next")}
        if not (isinstance(args.get("title"), str) and args["title"].strip()):
            args["title"] = board.get("title") or "From your work"
    problems: list[str] = []
    saved: list[str] = []
    templates = card_templates.TemplateStore(get_hermes_home())
    if args.get("save_templates") is not None:
        saved = templates.save(args["save_templates"], problems)
    given = _filled_templates(args.get("cards"), templates, problems)
    cards = card_schema.normalize_cards(given, problems)
    if args.get("cards") not in (None, []) and not cards:
        return {"ok": False, "error": "invalid_cards", "note": INVALID_CARDS_NOTE, "problems": problems[:20],
                **({"saved": saved} if saved else {})}
    if saved and not cards and not (isinstance(args.get("markdown"), str) and args["markdown"].strip()):
        return _with_problems({"ok": True, "saved": saved, "note": SAVED_NOTE}, problems)
    answer = _handle_cards(args, session, cards, problems, is_room, room_board, desktop_board, keep_diff)
    if saved:
        answer["saved"] = saved
    return answer


def _filled_templates(given: Any, templates: card_templates.TemplateStore, problems: list[str]) -> Any:
    """The cards with each of type template filled into its composed card."""
    if not isinstance(given, list):
        return given
    cards = []
    for index, raw in enumerate(given):
        if isinstance(raw, dict) and isinstance(raw.get("type"), str) and \
                card_schema.clean_text(raw["type"]).lower() == "template":
            raw = templates.fill(raw, problems, f"cards[{index}]")
            if raw is None:
                continue
        cards.append(raw)
    return cards


def _handle_cards(args: dict[str, Any], session: str, cards: list[dict[str, Any]], problems: list[str],
                  is_room: Callable[[Any], bool], room_board: Callable[[Any, dict[str, Any]], bool],
                  desktop_board: Callable[[dict[str, Any]], str | None],
                  keep_diff: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    cards = _diffs_answered(cards, keep_diff, problems)
    layout = card_schema.normalize_layout(args.get("layout"), problems)
    follow_ups = card_schema.normalize_next(args.get("next"), problems)
    if not cards and session and is_room(session):
        # A room has no call screen: Markdown alone would wait for one.
        return {"ok": False, "error": "group_room", "note": ROOM_MARKDOWN_NOTE}
    if cards and session and is_room(session):
        # A room this host runs keeps the board for Conduit to draw under
        # the agent's message; elsewhere, or for an older Conduit, it goes in
        # the reply as text (Eric, 2026-11-03). Other people's agents may
        # read a room, so a contact's numbers and a text drafted for the user
        # stay out of it, and a tap there would speak for the user to the
        # whole room, so it has no follow-ups.
        if follow_ups:
            problems.insert(0, TEXT_NEXT_NOTE)
        if any(card["type"] == "contact" for card in cards):
            problems.insert(0, ROOM_CONTACTS_NOTE)
            cards = [card for card in cards if card["type"] != "contact"]
            if not cards:
                return _with_problems({"ok": False, "error": "group_room", "note": ROOM_NOTHING_NOTE}, problems)
        if any(card_schema.has_actions(card) for card in cards):
            # A tap or a message from a card in a room would speak for the
            # user to everyone in it, as a follow-up would.
            kept = [card_schema.without_actions(card, keep=("open", "copy")) for card in cards]
            if kept != cards:
                problems.insert(0, ROOM_ACTIONS_NOTE)
            cards = [card for card in kept if card is not None]
            if not cards:
                return _with_problems({"ok": False, "error": "group_room", "note": ROOM_NOTHING_NOTE}, problems)
        store = _store()
        if any(card["type"] in card_schema.DIFF_TYPES for card in cards) and \
                store.has_feature_everywhere(ROOM_BOARDS_FEATURE) and \
                not store.has_feature_everywhere(card_schema.DIFF_FEATURE):
            # A Conduit that draws a room's boards but not diffs would show
            # nothing for one; as text, its Markdown goes in the reply.
            problems.insert(0, LEFT_OUT_NOTE.format(kinds="diff"))
            cards = [card for card in cards if card["type"] not in card_schema.DIFF_TYPES]
            if not cards:
                return {"ok": False, "error": "unsupported_cards", "note": problems.pop(0), "problems": problems[:20]}
        if any(card["type"] in card_schema.COMPOSED_TYPES for card in cards) and \
                store.has_feature_everywhere(ROOM_BOARDS_FEATURE) and \
                not store.has_feature_everywhere(card_schema.COMPOSED_FEATURE):
            # The same for a Conduit that draws a room's boards but not cards built of blocks.
            problems.insert(0, LEFT_OUT_NOTE.format(kinds="composed"))
            cards = [card for card in cards if card["type"] not in card_schema.COMPOSED_TYPES]
            if not cards:
                return {"ok": False, "error": "unsupported_cards", "note": problems.pop(0), "problems": problems[:20]}
        board = {"cards": cards, "markdown": _fallback(cards)}
        if layout and len(cards) > 1:
            board["layout"] = layout
        if store.has_feature_everywhere(ROOM_BOARDS_FEATURE) and room_board(session, board):
            if layout and len(cards) < 2:
                problems.insert(0, "layout: it lays out two or more cards, and fewer were shown, left out")
            return _with_problems({"ok": True, **board, "note": ROOM_SHOWN_NOTE}, problems)
        if layout:
            problems.insert(0, TEXT_LAYOUT_NOTE)
        return _as_text("group_room", ROOM_NOTE, cards, problems)
    source = _turn_source() if cards else None
    if source == "desktop" and _desktop_app():
        # Hermes Desktop (not the web dashboard, which turn_source counts as
        # "desktop" too) draws a board through the plugin's Desktop half,
        # where the reply holds the board's line (Eric, 2026-11-04); without
        # the half the board goes in the reply as Markdown.
        board = {"cards": cards, "card_markdown": [_fallback([card]) for card in cards],
                 "markdown": _fallback(cards)}
        if layout and len(cards) > 1:
            board["layout"] = layout
        if follow_ups:
            board["next"] = follow_ups
        board_id = desktop_board(board)
        if board_id:
            line = f'::conduit-board{{id="{board_id}"}}'
            return _board({"ok": True, "directive": line}, cards, layout, follow_ups, problems,
                          DESKTOP_SHOWN_NOTE.format(line=line))
    if cards and source not in (None, "conduit"):
        # Desktop without its half, a messaging app or the terminal draws no
        # cards; the board's Markdown reads well in all of them.
        if follow_ups:
            problems.insert(0, TEXT_NEXT_NOTE)
        if layout:
            problems.insert(0, TEXT_LAYOUT_NOTE)
        return _as_text("not_conduit", NOT_CONDUIT_NOTE, cards, problems)
    store = _store()
    old_board = not store.has_feature_everywhere(card_schema.BOARD_FEATURE)
    dropped_extras = [note for note, given in ((OLD_BOARD_LAYOUT_NOTE, layout), (OLD_BOARD_NEXT_NOTE, follow_ups))
                      if old_board and given]
    if old_board:
        layout, follow_ups = None, []
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
            return _with_problems({"ok": False, "error": "unsupported_cards", "note": UNSUPPORTED_CARDS_NOTE},
                                  problems)
    if any(card_schema.has_actions(card) for card in cards) and \
            not store.has_feature_everywhere(card_schema.COMPOSED_FEATURE):
        # An older Conduit would draw the card without its buttons, and
        # Hermes would wait for a tap that can't come.
        problems.insert(0, OLD_ACTIONS_NOTE)
        cards = [card_schema.without_actions(card) or card for card in cards]
    if old_board and len(cards) > card_schema.LEGACY_MAX_CARDS:
        # A Conduit from before boards keeps the first four it can draw;
        # Hermes hears to say the rest.
        # First, so the cap on problems never cuts what Hermes must say.
        problems.insert(0, OLD_BOARD_NOTE.format(count=card_schema.LEGACY_MAX_CARDS))
        cards = cards[:card_schema.LEGACY_MAX_CARDS]
    elif dropped_extras:
        problems[:0] = dropped_extras
    no_texts = _has_messages(cards) and not store.has_feature_everywhere(TEXT_DRAFTS_FEATURE)
    if no_texts:
        # An older Conduit would show the person with no Send text, and
        # Hermes would think the message was waiting on the user's phone.
        cards = _without_messages(cards)
    fallback = bool(cards) and not given_markdown
    if fallback:
        markdown = _fallback(cards)
    if turn_surface.turn_entry(session) == "typed":
        # A typed message's chat is open, not a call's screen: the card would
        # wait unclaimed for a screen that isn't there, and Hermes would tell
        # the user to look at it (hermes-conduit#1808). The chat draws the
        # cards from this answer; Markdown goes in the reply.
        if cards and store.has_feature(CHAT_CARDS_FEATURE):
            return _board({"ok": True}, cards, layout, follow_ups, problems,
                          _with_left_out(IN_CHAT_NOTE, left_out, no_texts))
        if cards:
            return _as_text("typed_chat", CHAT_NO_CARDS_NOTE, cards, problems)
        return _with_problems({"ok": False, "error": "typed_chat",
                               "note": _with_left_out(TYPED_CHAT_NOTE, left_out, no_texts)}, problems)
    try:
        card = store.show(args.get("title"), markdown, session_id=session, cards=cards,
                          markdown_is_fallback=fallback, layout=layout)
    except PresentError as error:
        if error.code == "no_device" and cards and store.has_feature(CHAT_CARDS_FEATURE):
            # No call screen to put it on, but the chat draws it from this
            # answer.
            return _board({"ok": True}, cards, layout, follow_ups, problems,
                          _with_left_out(IN_CHAT_NOTE, left_out, no_texts))
        note = str(error)
        if error.code in ("no_device", "rate_limited"):
            note += " Tell the user the gist instead."
        return _with_problems({"ok": False, "error": error.code, "note": _with_left_out(note, left_out, no_texts)},
                              problems)
    return _board({"ok": True, "id": card["id"]}, cards, layout, follow_ups, problems,
                  _with_left_out(SHOWN_NOTE, left_out, no_texts))


def _board(answer: dict[str, Any], cards: list[dict[str, Any]], layout: str | None, follow_ups: list[str],
           problems: list[str], note: str) -> dict[str, Any]:
    """The answer the chat's history keeps: the cards, how they're laid out,
    the follow-ups, and the cards' Markdown, which Conduit shows in a card's
    place whenever it can't draw it."""
    if layout and len(cards) < 2:
        problems = ["layout: it lays out two or more cards, and fewer were shown, left out", *problems]
    if follow_ups and not cards:
        problems = ["next: follow-ups go under cards, and none were shown, left out", *problems]
    if cards:
        answer["cards"] = cards
        if layout and len(cards) > 1:
            answer["layout"] = layout
        if follow_ups:
            answer["next"] = follow_ups
        answer["markdown"] = _fallback(cards)
    if problems:
        answer["problems"] = problems[:20]
        note = f"{note} {PROBLEMS_NOTE}"
    answer["note"] = note
    return answer


def _diffs_answered(cards: list[dict[str, Any]], keep_diff: Callable[[dict[str, Any]], str | None],
                    problems: list[str]) -> list[dict[str, Any]]:
    """The cards as the answer carries them: a diff too long to carry whole
    is kept on the host, and its card lists the files and names the id
    Conduit reads the rest by, so the chat's history (and the model's
    context) never holds a long diff."""
    answered = []
    for card in cards:
        # One the host already keeps (a workstream's card shown again with
        # from_work) carries its id as it is: its hunks were left out.
        kept = card["type"] == "diff" and isinstance(card["diff"].get("id"), str) and card["diff"]["id"] \
            and not any(file.get("hunks") for file in card["diff"]["files"])
        if card["type"] == "diff" and not kept and not card_schema.carries_whole(card["diff"]):
            try:
                diff_id = keep_diff(card["diff"])
            except Exception:  # noqa: BLE001 — the card lists the files alone
                diff_id = None
            if not diff_id:
                problems.append(DIFF_NOT_KEPT_NOTE)
            card = {**card, "diff": card_schema.answer_diff(card["diff"], diff_id)}
        answered.append(card)
    return answered


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


def _desktop_app() -> bool:
    """Whether this turn came from Hermes Desktop itself, where the half draws."""
    try:
        return reply_sources.is_desktop_app(reply_sources.current_transport())
    except Exception:  # noqa: BLE001 — unknown: the board goes as Markdown
        return False


def _turn_source() -> str | None:
    try:
        return reply_sources.turn_source(reply_sources.current_platform(), reply_sources.current_transport())
    except Exception:  # noqa: BLE001 — a lookup that failed draws no cards
        return "unknown"
