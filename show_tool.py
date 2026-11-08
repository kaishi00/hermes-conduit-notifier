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
import re
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import card_schema, card_templates, client, reply_sources, review_store, turn_surface
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
        "else (an email ready to send, a day's agenda, an idea to act on). When a choice is easier to see than to "
        "describe (layouts, colours, charts, posters, before and after), show a review card: its options as HTML "
        "pages or pictures, one decision as a quick pick or several the user marks Approve, Change or Skip with "
        "notes; their marks come back as their message, and you show the next round with the review's id. Any card can carry up to three buttons "
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
        f"problems, fix those cards and show the whole board again. For a trip plan, a comparison, a dashboard, a tracker or "
        f"options to choose between, load "
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
REVIEW_SHOWN_NOTE = ("The review {id} is round {round}: the user marks it in the chat and their marks come back as "
                     "their message. Don't ask them to reply in words, and don't post your own got-it.")
REVIEW_FOR_NOTE = (" Their marks go to {ref}, which builds the next round; show it with this review's id and for "
                   "{ref}.")
REVIEW_NEXT_NOTE = " Show the next round with this review's id, so it stays the same review."
ROOM_REVIEW_NOTE = ("cards: a review in a group room comes later (everyone votes), so it was left out: describe the "
                    "options in your reply")
WORKSTREAM_REVIEW_NOTE = ("cards: only the Bot Chat shows a review, so it was left out: write the options' pages to "
                          "files and name them in your report; the Bot Chat shows them for you")
SPOKEN_REVIEW_NOTE = ("cards: a review is marked in the chat, not on a call, so it was left out: say you'll put the "
                      "options in the chat, and show them once the user types there")
NO_REVIEWS_NOTE = "cards: reviews can't be kept on this host right now, left out"
REVIEW_ELSEWHERE_NOTE = ("cards[{index}].review: {id} belongs to {ref}, which makes every round of it: show its rounds "
                         "with for {ref}, and send what the user says about it to {ref} with work_send")


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


class ReviewHost:
    """What a review card needs from the rest of the plugin: where reviews
    are kept, and which chats and workstreams are the bot's."""

    def store(self) -> review_store.KeptReviews:
        raise NotImplementedError

    def in_workstream(self, session: str) -> bool:
        return False

    def is_bot_chat(self, session: str) -> bool:
        return False

    def workstream(self, ref: str) -> bool:
        return False


def register(ctx: Any, *, is_child: Callable[[Any], bool] = lambda _: False,
             is_room: Callable[[Any], bool] = lambda _: False,
             room_board: Callable[[Any, dict[str, Any]], bool] = lambda *_: False,
             desktop_board: Callable[[dict[str, Any]], str | None] = lambda _: None,
             keep_diff: Callable[[dict[str, Any]], str | None] = lambda _: None, skill_path: Any = None,
             reviews: ReviewHost | None = None) -> None:
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
                      desktop_board=desktop_board, keep_diff=keep_diff, reviews=reviews)

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
           keep_diff: Callable[[dict[str, Any]], str | None] = lambda _: None,
           reviews: ReviewHost | None = None) -> str:
    try:
        return json.dumps(_handle(args if isinstance(args, dict) else {}, session_id, is_child, is_room, room_board,
                                  desktop_board, keep_diff, reviews), ensure_ascii=False)
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_show failed", exc_info=True)
        return json.dumps({"ok": False, "error": "unavailable",
                           "note": "It couldn't be shown. Tell the user the gist instead."})


def _handle(args: dict[str, Any], session_id: Any, is_child: Callable[[Any], bool],
            is_room: Callable[[Any], bool] = lambda _: False,
            room_board: Callable[[Any, dict[str, Any]], bool] = lambda *_: False,
            desktop_board: Callable[[dict[str, Any]], str | None] = lambda _: None,
            keep_diff: Callable[[dict[str, Any]], str | None] = lambda _: None,
            reviews: ReviewHost | None = None) -> dict[str, Any]:
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
    given = _reviews_kept(given, session, problems, reviews, is_room)
    # Every round kept above, however the card spelled its type.
    kept = [card["review"] for card in given if isinstance(card, dict) and _is_review(card)] \
        if isinstance(given, list) and reviews is not None else []
    cards = card_schema.normalize_cards(given, problems)
    if args.get("cards") not in (None, []) and not cards:
        _drop_unshown(reviews, kept, [])
        return {"ok": False, "error": "invalid_cards", "note": INVALID_CARDS_NOTE, "problems": problems[:20],
                **({"saved": saved} if saved else {})}
    if saved and not cards and not (isinstance(args.get("markdown"), str) and args["markdown"].strip()):
        return _with_problems({"ok": True, "saved": saved, "note": SAVED_NOTE}, problems)
    try:
        answer = _handle_cards(args, session, cards, problems, is_room, room_board, desktop_board, keep_diff)
    except BaseException:
        _drop_unshown(reviews, kept, [])  # a show that failed keeps no round
        raise
    if saved:
        answer["saved"] = saved
    # Hermes Desktop's half draws the card but takes no marks (an open gap).
    shown = [card["review"] for card in answer.get("cards", []) if card.get("type") == "review"] \
        if answer.get("ok") and not answer.get("directive") else []
    # A round no card shows (this Conduit draws no reviews, the turn went to
    # Desktop, the card was dropped) isn't kept as one to mark.
    _drop_unshown(reviews, kept, shown)
    if shown and answer.get("ok"):
        review = shown[0]
        answer["note"] += " " + REVIEW_SHOWN_NOTE.format(id=review["id"], round=review["round"]) + (
            REVIEW_FOR_NOTE.format(ref=review["for"]) if review.get("for") else REVIEW_NEXT_NOTE)
    return answer


def _drop_unshown(reviews: ReviewHost | None, kept: list[dict[str, Any]], shown: list[dict[str, Any]]) -> None:
    showing = {(review["id"], review["round"]) for review in shown}
    for review in kept:
        if (review["id"], review["round"]) in showing:
            continue
        try:
            reviews.store().drop_round(review["id"], review["round"])
        except (review_store.ReviewError, OSError):
            logger.debug("conduit_show: couldn't drop an unshown review round", exc_info=True)


def open_reviews_note(reviews: list[dict[str, Any]]) -> str | None:
    """The turn's line on the chat's open reviews (review_store.open_reviews):
    the id to show each one's next round under, and the workstream it stays
    with."""
    if not reviews:
        return None
    lines = []
    for meta in reviews[:3]:
        marked = bool(meta["rounds"][-1].get("marks"))
        state = "marked, waiting on its next round" if marked else "waiting on the user's marks"
        line = f'- {meta["id"]} "{meta["title"] or "untitled"}", round {len(meta["rounds"])}: {state}'
        if meta["for"]:
            line += f'; it belongs to {meta["for"]}: what the user says about it goes to {meta["for"]} (work_send)'
        lines.append(line)
    return ("[Conduit: reviews open in this chat (conduit_show review cards). Show a next round with its id; a "
            "new review only for new options:\n" + "\n".join(lines) + "]")


_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def _short_id(value: Any) -> str:
    """A decision's or option's id as the tool gave it, letters, digits, _ and
    - only."""
    return _ID_CHARS.sub("", card_schema.clean_text(value))[:40] if isinstance(value, str) else ""


def _text(value: Any, limit: int) -> str:
    return card_schema.clean_text(value).strip()[:limit].rstrip() if isinstance(value, str) else ""


def _is_review(card: dict[str, Any]) -> bool:
    return isinstance(card.get("type"), str) and card_schema.clean_text(card["type"]).strip().lower() == "review"


def _reviews_kept(given: Any, session: str, problems: list[str], reviews: ReviewHost | None,
                  is_room: Callable[[Any], bool]) -> Any:
    """The cards with each review's options kept on the host, round by
    round, and the review as the card carries it: labels, ids and pictures'
    links, never a page (review_store)."""
    if not isinstance(given, list):
        return given
    cards = []
    for index, raw in enumerate(given):
        if isinstance(raw, dict) and _is_review(raw):
            raw = _kept_review(raw, index, session, problems, reviews, is_room)
            if raw is None:
                continue
        cards.append(raw)
    return cards


def _kept_review(raw: dict[str, Any], index: int, session: str, problems: list[str], reviews: ReviewHost | None,
                 is_room: Callable[[Any], bool]) -> dict[str, Any] | None:
    path = f"cards[{index}]"
    if session and is_room(session):
        problems.append(ROOM_REVIEW_NOTE)
        return None
    if reviews is None:
        problems.append(NO_REVIEWS_NOTE)
        return None
    if session and reviews.in_workstream(session):
        problems.append(WORKSTREAM_REVIEW_NOTE)
        return None
    if turn_surface.turn_entry(session) == "spoken":
        problems.append(SPOKEN_REVIEW_NOTE)
        return None
    given = raw.get("review")
    if not isinstance(given, dict) or not isinstance(given.get("decisions"), list):
        problems.append(f"{path}.review: a review card needs its decisions, each with its options")
        return None
    store = reviews.store()
    wanted = _text(given.get("id"), 40)
    existing = None
    if wanted:
        try:
            existing = store.get(wanted)
        except review_store.ReviewError:
            existing = None
        if existing is None:
            problems.append(f"{path}.review.id: no review {wanted} is kept here (they go after a month); leave id "
                            "out to start a new one")
            return None
    for_ref = _text(given.get("for"), 8)
    if for_ref and not card_schema.WORK_REF.fullmatch(for_ref):
        problems.append(f"{path}.review.for: a workstream ref like W3, left out")
        for_ref = ""
    elif for_ref and not reviews.is_bot_chat(session):
        problems.append(f"{path}.review.for: only a Bot Chat shows a review for a workstream, left out")
        for_ref = ""
    elif for_ref and not reviews.workstream(for_ref):
        problems.append(f"{path}.review.for: {for_ref} isn't one of this bot's workstreams; show the review for "
                        "the one that made the options")
        return None
    if existing is not None and existing["for"] and not reviews.is_bot_chat(session):
        # Its marks go to its workstream from the Bot Chat alone.
        problems.append(f"{path}.review: {wanted} belongs to {existing['for']}, so only the Bot Chat shows its rounds")
        return None
    if existing is not None:
        bound = existing["for"]
        if bound and for_ref and for_ref != bound:
            problems.append(REVIEW_ELSEWHERE_NOTE.format(index=index, id=wanted, ref=bound))
            return None
        if bound:
            for_ref = bound
        elif for_ref:
            problems.append(f"{path}.review.for: {wanted} started outside a workstream, so its rounds stay in this "
                            "chat; left out")
            for_ref = ""
    title = _text(raw.get("title"), card_schema.CARD_SCHEMA["properties"]["title"]["maxLength"])
    decisions, pages = _review_decisions(given["decisions"], f"{path}.review", problems)
    if not decisions:
        problems.append(f"{path}.review: no decision has two options that could be shown")
        return None
    started = ""
    try:
        review_id = wanted or store.start(title, session, for_ref)
        started = "" if wanted else review_id
        number = store.add_round(review_id, title, decisions, pages, session)
        started = ""
        settled = store.settled(review_id, {decision["id"] for decision in decisions}) if existing else []
    except (review_store.ReviewError, OSError) as error:
        if started:
            # A new review whose first round didn't save leaves nothing behind.
            try:
                store.drop_empty(started)
            except (review_store.ReviewError, OSError):
                pass
        problems.append(f"{path}.review: {error}" if isinstance(error, review_store.ReviewError) else NO_REVIEWS_NOTE)
        return None
    review = {"id": review_id, "round": number, **({"for": for_ref} if for_ref else {}), "decisions": decisions,
              **({"settled": settled} if settled else {})}
    return {**{key: value for key, value in raw.items() if key != "review"}, "review": review}


def _review_decisions(given: list[Any], path: str, problems: list[str]) -> tuple[list[dict[str, Any]],
                                                                                  dict[str, str]]:
    """The decisions as the card shows them, and the pages their options
    open, by decision and option id."""
    decisions: list[dict[str, Any]] = []
    pages: dict[str, str] = {}
    seen: set[str] = set()
    if len(given) > 6:
        problems.append(f"{path}.decisions: six at most, the rest were left out")
    for d_index, raw in enumerate(given[:6]):
        where = f"{path}.decisions[{d_index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: not a decision, left out")
            continue
        title = _text(raw.get("title"), 80)
        if not title:
            problems.append(f"{where}.title: a decision needs one, left out")
            continue
        decision_id = _short_id(raw.get("id")) or f"d{d_index + 1}"
        if decision_id in seen:
            problems.append(f"{where}.id: another decision has it, left out")
            continue
        options: list[dict[str, Any]] = []
        option_pages: dict[str, str] = {}
        taken: set[str] = set()
        recommended = False
        raw_options = raw.get("options") if isinstance(raw.get("options"), list) else []
        if len(raw_options) > 6:
            problems.append(f"{where}.options: six at most, the rest were left out")
        for o_index, option_raw in enumerate(raw_options[:6]):
            named = f"{where}.options[{o_index}]"
            if not isinstance(option_raw, dict):
                problems.append(f"{named}: not an option, left out")
                continue
            label = _text(option_raw.get("label"), 60)
            if not label:
                problems.append(f"{named}.label: an option needs one, left out")
                continue
            option_id = _short_id(option_raw.get("id")) or next(
                letter for letter in "ABCDEFGHIJKL" if letter not in taken)
            if option_id in taken:
                problems.append(f"{named}.id: another option has it, left out")
                continue
            try:
                kind, content = review_store.read_page(option_raw)
            except review_store.ReviewError as error:
                problems.append(f"{named}: {error}, left out")
                continue
            option: dict[str, Any] = {"id": option_id, "label": label, "kind": kind}
            text = _text(option_raw.get("text"), 200)
            if text:
                option["text"] = text
            if option_raw.get("recommended") is True and not recommended:
                option["recommended"] = True
                recommended = True
            if kind == "image":
                option["url"] = content
            else:
                option_pages[f"{decision_id}/{option_id}"] = content
            taken.add(option_id)
            options.append(option)
        if len(options) < 2:
            problems.append(f"{where}: a decision needs two options, left out")
            continue
        decision: dict[str, Any] = {"id": decision_id, "title": title}
        text = _text(raw.get("text"), 300)
        if text:
            decision["text"] = text
        if raw.get("layout") in ("side", "switch", "before_after"):
            decision["layout"] = raw["layout"]
        decision["options"] = options
        seen.add(decision_id)
        decisions.append(decision)
        pages.update(option_pages)
    return decisions, pages


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
        diff = card.get("diff") if isinstance(card, dict) and card.get("type") == "diff" else None
        files = diff.get("files") if isinstance(diff, dict) else None
        kept = isinstance(files, list) and isinstance(diff.get("id"), str) and diff["id"] \
            and not any(isinstance(file, dict) and file.get("hunks") for file in files)
        if isinstance(files, list) and not kept and not card_schema.carries_whole(diff):
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
