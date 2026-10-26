"""What the user's ratings and reasons change (designs/pulse-learning.md).

Everything Pulse brings the user becomes a card (Conduit's Pulse screen
lists them). What the user does with a card changes what Pulse does, in
code, not as a hint to a model:

- A thumbs up or down, Do it and Done are signals (pulse_store's signals):
  pulse_prefs learns from them which kinds, chats and timings are worth
  bringing up and how loudly, slowly and fading, and the report's router
  and the follow-ups use what it learned.
- Each kind has a level on the loudness ladder: ``push`` (a push),
  ``screen`` (a card in the Pulse screen, no push), ``brief`` (a card held
  for the morning brief, which stamps ``briefed_at``; until the brief is
  built it waits in the Pulse screen), ``off`` (not checked at all). The
  level is what the user set, or "too often" set: a ceiling. Learning moves
  things below it, never above.
- A thumbs down can carry a reason, on a follow-up or a report card alike,
  and it acts at once:
  - ``not_relevant``: a rule, "no updates about this": that chat's items are
    closed before Pulse speaks, the report leaves out what comes from it,
    and a follow-up's topic goes to every check;
  - ``already_knew``: checks skip news the user already saw in that chat,
    and the report's list leaves out what its chats show they know;
  - ``too_late``: checks come sooner (half the wait, at most twice), and
    the report counts that kind as due sooner and is told to bring it up
    earlier;
  - ``too_often``: the kind moves to the morning brief at once;
  - ``wrong_time``: the card comes back at a better time
    (pulse_followup.wrong_time_rest), and one that pushed closes the part
    of the day it came in for its kind (``closed_parts``: pulse_hours holds
    its pushes then). It's no thumb: the timing was wrong, not the card.

Each reason's change is a lesson row; the Pulse screen lists them and
undoing one puts Pulse back the way it was for what comes next (promises a
rule already closed stay closed, checks already brought forward stay
sooner). So is a reset (``reset``): it forgets what the signals taught
about one kind, place or timing, a kind's loudness, or everything, and
undoing it counts them again.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional, Sequence

from . import pulse_hours as hours
from . import pulse_prefs as prefs
from . import pulse_store as store

LEVELS = prefs.LEVELS
DEFAULT_LEVELS = {"followup": "push"}
RATINGS = ("up", "down")
WRONG_TIME = store.WRONG_TIME
REASONS = ("not_relevant", "already_knew", "too_late", "too_often", WRONG_TIME)
SOONER = 0.5
MAX_SOONER = 2
# The level lessons that set a ceiling: the user's own, and "too often".
# Before learning, two net thumbs stepped a kind along the ladder as a lesson
# (cause STEPPED); the store retires those once, its thumbs being signals now.
SET, TOO_OFTEN = "set", "too often"
STEPPED = store.STEPPED_CAUSES
LEVEL, RULE, SOONER_LESSON, SEEN = "level", "rule", "sooner", "seen"
# "Wrong time": a part of the day (pulse_hours.PARTS, its value) closed for a kind's pushes.
PART = "part"
PART_WORDS = {hours.MORNING: "in the morning", hours.MIDDAY: "in the middle of the day",
              hours.EVENING: "in the evening", hours.NIGHT: "at night"}
# A reset: its kind is what it forgets (pulse_prefs.RESET_SCOPES), its
# value which one ("" for everything).
RESET = store.RESET
TOPIC_LIMIT = 200
CARDS = 100

# How much Pulse nudges, the user's own pick (Settings > Pulse, `pulse nudge`):
# reminders a day, how soon checks come round (times the ratings' pace), the
# follow-up token budget when none is set, and how high the report's bar is.
NUDGES = {
    "gentle": {"reminders": 1, "pace": 1.5, "followup_tokens": 60_000},
    "balanced": {"reminders": 3, "pace": 1.0, "followup_tokens": 150_000},
    "eager": {"reminders": 6, "pace": 0.5, "followup_tokens": 300_000},
}
DEFAULT_NUDGE = "balanced"

KIND_NAMES = {"followup": "Follow-ups"}
LEVEL_NAMES = {"push": "sent with a push", "screen": "shown in Pulse without a push",
               "brief": "held for the morning brief", "off": "off"}


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# What's in force --------------------------------------------------------------

def level(pulse_store: store.PulseStore, kind: str) -> str:
    """The kind's level, a ceiling: its newest level lesson still in force
    (the user's, or "too often"), else its default."""
    lessons = [lesson for lesson in pulse_store.lessons(kind, LEVEL)
               if lesson["value"] in LEVELS and lesson["cause"] not in STEPPED]
    return lessons[-1]["value"] if lessons else DEFAULT_LEVELS.get(kind, "push")


def nudge(pulse_store: store.PulseStore) -> str:
    value = pulse_store.get("nudge")
    return value if isinstance(value, str) and value in NUDGES else DEFAULT_NUDGE


def nudging(pulse_store: store.PulseStore) -> dict[str, Any]:
    return dict(NUDGES[nudge(pulse_store)])


def pace(pulse_store: store.PulseStore, kind: str) -> float:
    """How much of the usual wait a check waits: the nudge level's share,
    less after "too late" ratings."""
    return nudging(pulse_store)["pace"] * SOONER ** min(len(pulse_store.lessons(kind, SOONER_LESSON)), MAX_SOONER)


def report_pace(pulse_store: store.PulseStore, kind: str) -> float:
    """How much sooner a report kind counts as due after "too late" on its
    cards (pulse_prefs.band's ``pace``): 1, then half, at most a quarter."""
    return SOONER ** min(len(pulse_store.lessons(kind, SOONER_LESSON)), MAX_SOONER)


def report_lessons(pulse_store: store.PulseStore, what: str) -> list[str]:
    """The report kinds with a ``what`` lesson ("already knew": SEEN, "too
    late": SOONER_LESSON) in force, for the report's list to be told."""
    return sorted({lesson["kind"] for lesson in pulse_store.lessons(what=what) if lesson["kind"] != "followup"})


def skips_seen(pulse_store: store.PulseStore, kind: str) -> bool:
    return bool(pulse_store.lessons(kind, SEEN))


def closed_parts(pulse_store: store.PulseStore, kind: str) -> set[str]:
    """The parts of the day "wrong time" closed for the kind's pushes."""
    return {lesson["value"] for lesson in pulse_store.lessons(kind, PART) if lesson["value"] in hours.PARTS}


def rules(pulse_store: store.PulseStore, kind: str) -> list[dict]:
    return pulse_store.lessons(kind, RULE)


def chat_rules(pulse_store: store.PulseStore) -> list[dict]:
    """Every "not relevant" rule in force: one made on a follow-up and one
    made on a report card both cover their chat for both."""
    return rules(pulse_store, "followup") + rules(pulse_store, NOTICED)


def muted_by(pulse_store: store.PulseStore, kind: str, profile: str, chat_id: str) -> Optional[dict]:
    """The rule that closes this chat's items, if any (``chat_rules``;
    ``kind`` is kept for callers)."""
    return next((rule for rule in chat_rules(pulse_store)
                 if rule["chat_id"] and rule["chat_id"] == chat_id and rule["profile"] == profile), None)


# Cards ------------------------------------------------------------------------

def add_card(pulse_store: store.PulseStore, *, kind: str, item: dict, level_name: str, news: str, why: str,
             chat_title: str, now: float, learned: Optional[dict] = None, place: str = "", band: str = "") -> int:
    """The item's card for this news; the one already there when a crash
    came between making it and closing the item. ``learned``: the biggest
    part learning played in bringing it up (pulse_prefs' Learned.factor),
    for its Why?; ``place`` and ``band``: where it came from and how soon
    it was due as it came up, which its signals carry."""
    existing = pulse_store.card_for(kind, item["id"], news)
    if existing is not None:
        return existing
    return pulse_store.add_card({
        "kind": kind, "item_id": item["id"], "level": level_name, "title": _clip(item["text"], 300),
        "quote": _clip(item["evidence"], 300), "news": news, "why": why, "profile": item["profile"],
        "chat_id": item["chat_id"], "chat_title": _clip(chat_title, 120), "message_id": item["message_id"],
        "created_at": now, "learned": factor_json(learned), "place": place, "band": band})


def factor_json(factor: Any) -> str:
    """A factor (pulse_prefs' Learned.factor) as a card or candidate keeps
    it: JSON, "" for none."""
    if isinstance(factor, str):
        return factor
    return json.dumps(factor, sort_keys=True) if isinstance(factor, dict) and factor else ""


def factor_entry(factor: Any) -> Optional[dict]:
    """A kept factor as the app reads it: {"scope", "value", "effect",
    "useful", "votes"}, or None."""
    try:
        found = json.loads(factor) if isinstance(factor, str) and factor else factor
    except ValueError:
        return None
    if not isinstance(found, dict) or found.get("scope") not in ("all",) + prefs.FEATURES:
        return None
    return {key: found.get(key) for key in ("scope", "value", "effect", "useful", "votes")}


# A report or brief as it reached the Pulse chat, on the Pulse screen too.
TURN_KINDS = ("report", "brief")
TURN_NEWS_LIMIT = 6000


def add_turn_card(pulse_store: store.PulseStore, *, kind: str, chat_id: str, chat_title: str, turn: Any,
                  level_name: str, why: str, now: float, brief: str = "") -> Optional[int]:
    """The card for a delivered report or brief turn (``turn`` is its
    pulse_hermes.TurnRows), once: the one already there for that reply.
    ``brief``: a brief's attempt item id, which its held cards carry too."""
    if kind not in TURN_KINDS or not turn.reply or turn.reply_id is None or empty_turn(kind, turn.reply):
        return None
    existing = pulse_store.card_for_message(kind, turn.session_id, turn.reply_id)
    if existing is not None:
        return existing
    news = turn.reply.strip()  # its lines and lists kept, unlike a follow-up's one-line news
    if len(news) > TURN_NEWS_LIMIT:
        news = news[:TURN_NEWS_LIMIT - 1] + "…"
    return pulse_store.add_card({
        "kind": kind, "item_id": None, "level": level_name, "news": news, "why": why,
        "chat_id": turn.session_id or chat_id, "chat_title": _clip(chat_title, 120), "message_id": turn.reply_id,
        "brief": brief, "created_at": now})


# A report item's line starts with its number ("1. [kind] ..."); a report
# that says it has nothing (pulse_report.REPORT_TEXT's words) and has no item
# brought nothing up.
_REPORT_ITEM = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?\d+[.)]", re.MULTILINE)
_REPORT_NOTHING = re.compile(r"nothing worth bringing up", re.IGNORECASE)


def empty_turn(kind: str, reply: Optional[str]) -> bool:
    """A report that brought nothing up: no card, it would only crowd the
    ones that did."""
    return kind == "report" and bool(_REPORT_NOTHING.search(reply or "")) and not _REPORT_ITEM.search(reply or "")


def drop_empty_reports(pulse_store: store.PulseStore) -> int:
    """Takes away the cards of reports that brought nothing up (made before
    those stopped getting one)."""
    return pulse_store.drop_cards([card["id"] for card in pulse_store.turn_cards("report")
                                   if empty_turn("report", card["news"])])


# One item of a report, as its own card (the report's lines, split).
NOTICED = store.NOTICED
NOTICED_NEWS_LIMIT = 1200
# "1. [date] text #12", also bold or bulleted; the number, the kind, the rest.
_REPORT_LINE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(\d{1,2})[.)](?:\*\*)?\s*\[([A-Za-z-]+)\]\s*(.+?)\s*$", re.MULTILINE)
_LINE_REF = re.compile(r"\s*#(\d{1,9})\W*$")
_ANY_REF = re.compile(r"#\d")


def report_lines(reply: Optional[str]) -> list[tuple[str, str, Optional[int]]]:
    """A report's items in order, as (kind, text, the remembered item its
    "#n" names); a number used twice is its first line."""
    lines, numbers = [], set()
    for number, kind, text in _REPORT_LINE.findall(reply or ""):
        if number in numbers:
            continue
        numbers.add(number)
        # A "#n" counts only as the line's one: "PR #12 needs #4" links nothing.
        ref = _LINE_REF.search(text) if len(_ANY_REF.findall(text)) == 1 else None
        lines.append((kind.lower(), (text[:ref.start()] if ref else text).strip(), int(ref.group(1)) if ref else None))
    return [line for line in lines if line[1]]


def add_noticed_cards(pulse_store: store.PulseStore, *, turn: Any, lines: list[tuple[str, str, Optional[int]]],
                      chat_id: str, chat_title: str, shown: set[int], kinds: tuple[str, ...],
                      item_chat_title: Callable[[dict], str], now: float, attempt: str = "",
                      marks: tuple[str, ...] = (), keep: int = 50, links: Sequence[int] = (),
                      found_in: Sequence[tuple[str, str]] = (), whys: Sequence[str] = (),
                      levels: Sequence[str] = (), factors: Sequence[str] = (),
                      attrs: Sequence[tuple[str, str]] = ()) -> list[int]:
    """A delivered report's items (``report_lines``) as cards, one each, all
    at once, marked as ``add_cards`` marks them. An item drawn from a
    remembered thing (its "#n", shown in the report; what replaced it since,
    if anything) points at the chat that thing came from; one the report
    found in a chat (``found_in``: each line's profile and chat, "" for none)
    points at that chat; the rest point at the report in the Pulse chat.
    Done and Do it work on all of them (``action_card``). ``whys``: each
    line's reason (why now, where it came from), the card's Why?; a report
    written without a candidate list has none. ``levels``: how loudly each
    came up (pulse_candidates.levels: push, screen or brief); "screen" for
    one without. ``factors``: the biggest part learning played in each
    (pulse_candidates.factors), for its Why?; ``attrs``: each one's place
    and band as it came up (pulse_candidates.card_attrs), which its signals
    carry.
    ``item_chat_title`` gives a chat's title from its profile and chat_id."""
    rows = []
    for n, (kind, text, ref) in enumerate(lines):
        current = pulse_store.current_item(ref) if ref is not None and ref in shown else None
        item = pulse_store.item(current) if current is not None else None
        profile, found = found_in[n] if n < len(found_in) else ("", "")
        level_name = levels[n] if n < len(levels) and levels[n] in ("push", "screen", "brief") else "screen"
        row: dict[str, Any] = {
            "kind": NOTICED, "level": level_name, "topic": kind if kind in kinds else "other",
            "news": _clip(text, NOTICED_NEWS_LIMIT), "why": whys[n] if n < len(whys) else "", "created_at": now,
            "learned": factor_json(factors[n]) if n < len(factors) else ""}
        if n < len(attrs):
            row["place"], row["band"] = attrs[n]
        if item is not None:
            row.update(item_id=item["id"], title=_clip(item["text"], 300), quote=_clip(item["evidence"], 300),
                       profile=item["profile"], chat_id=item["chat_id"], message_id=item["message_id"],
                       chat_title=_clip(item_chat_title(item), 120))
        elif found:
            row.update(item_id=None, title=_clip(text, 300), profile=profile, chat_id=found,
                       chat_title=_clip(item_chat_title({"profile": profile, "chat_id": found}), 120))
        else:
            row.update(item_id=None, title=_clip(text, 300), chat_id=turn.session_id or chat_id,
                       message_id=turn.reply_id, chat_title=_clip(chat_title, 120))
        rows.append(row)
    return pulse_store.add_cards(rows, attempt=attempt, marks=marks, keep=keep, links=links)


def left_out_card(pulse_store: store.PulseStore, found: dict, *, kinds: tuple[str, ...], now: float,
                  item_chat_title: Callable[[dict], str]) -> dict[str, Any]:
    """The card for something a report left out that the user brought up
    (pulse_control.bring_up): a report card shown without a push, as
    ``add_noticed_cards`` makes one from ``found``, its journaled
    candidate, keeping its reason, the part learning played and how it came
    up. One from no chat points at none, so Do it starts a new chat."""
    kind = found["kind"]
    row: dict[str, Any] = {
        "kind": NOTICED, "level": "screen", "topic": kind if kind in kinds else "other",
        "news": _clip(found["text"], NOTICED_NEWS_LIMIT), "why": found["why"] or "", "created_at": now,
        "learned": found["factor"] or "", "place": found["place"] or "", "band": found["band"] or ""}
    current = pulse_store.current_item(found["item_id"]) if found["item_id"] is not None else None
    item = pulse_store.item(current) if current is not None else None
    if item is not None:
        row.update(item_id=item["id"], title=_clip(item["text"], 300), quote=_clip(item["evidence"], 300),
                   profile=item["profile"], chat_id=item["chat_id"], message_id=item["message_id"],
                   chat_title=_clip(item_chat_title(item), 120))
    elif found["chat_id"]:
        row.update(item_id=None, title=_clip(found["text"], 300), profile=found["profile"], chat_id=found["chat_id"],
                   chat_title=_clip(item_chat_title({"profile": found["profile"], "chat_id": found["chat_id"]}), 120))
    else:
        row.update(item_id=None, title=_clip(found["text"], 300))
    return row


def card_entry(card: dict, done: Optional[dict] = None) -> dict[str, Any]:
    """``done``: its done mark, as ``done_marks`` gives it. ``learned``: the
    biggest part learning played in bringing it up (``factor_entry``), or
    None."""
    entry = {key: card[key] for key in ("id", "kind", "item_id", "level", "title", "quote", "news", "why", "profile",
                                        "chat_id", "chat_title", "message_id", "topic", "created_at", "seen_at",
                                        "briefed_at", "brief", "dismissed_at", "rest_until", "rating", "reason",
                                        "rated_at")}
    return {**entry, "done_at": done["done_at"] if done else None, "done_by": done["done_by"] if done else "",
            "learned": factor_entry(card.get("learned"))}


# Done -------------------------------------------------------------------------

# Who closed an item the user marked done on the Pulse screen; Undo only puts
# back an item this still holds closed.
DONE_BY = "user: done on the Pulse screen"
# Pulse marked it, having read the chat get it done after Do it (pulse_followup).
ACT_DONE_BY = "pulse: done in the chat after Do it"
# Pulse marked it, having read it get done while the card waited for the brief.
HELD_DONE_BY = "pulse: done before the morning brief"
RESTED_DONE_BY = "pulse: done before it came back from Not now"


def action_card(pulse_store: store.PulseStore, card_id: Any) -> dict:
    """The card ``card_id`` names, if Done and Do it take it (a follow-up or
    a report item, whether or not something remembered is behind it), or
    Invalid("card")."""
    card = pulse_store.card(card_id) if isinstance(card_id, int) and not isinstance(card_id, bool) else None
    if card is None or card["kind"] not in ("followup", NOTICED):
        raise Invalid("card")
    return card


def done(pulse_store: store.PulseStore, card_id: Any, is_done: Any, *, now: float) -> bool:
    """Marks a card done, or takes the mark off (Undo); whether it changed.
    Done closes the promise behind it as done and stops its checks, without
    a rating: Pulse was right to bring it up, the user just hadn't said. A
    report item Pulse found on its own has nothing behind it: Done marks the
    card, which the next report reads (pulse_candidates.recent_section).
    Undo puts the promise and its checks back as they were."""
    card = action_card(pulse_store, card_id)
    if not isinstance(is_done, bool):
        raise Invalid("done")
    if is_done:
        return pulse_store.mark_done(card["id"], card["item_id"], closed_by=DONE_BY, now=now)
    return pulse_store.unmark_done(card["id"], card["item_id"],
                                   closed_by=(DONE_BY, ACT_DONE_BY, HELD_DONE_BY, RESTED_DONE_BY), now=now)


# Not now ----------------------------------------------------------------------

def dismiss(pulse_store: store.PulseStore, card_id: Any, dismissed: Any, *, now: float,
            rest_until: Optional[float] = None) -> bool:
    """Swipes a card away ("Not now"), or puts it back; whether it changed.
    Nothing is learned: the kind keeps its loudness and no rating counts.
    With ``rest_until`` (pulse_followup.rest_time: later today, tomorrow or
    next week) the card comes back as new at that time, its chat read first
    (pulse_followup.return_rested, check_rested), and the remembered thing
    behind it rests until then; without one it stays away, and the thing
    rests a week (pulse_report.DISMISS_REST_S). A held card swiped away
    stays out of the morning brief while it's away."""
    card = pulse_store.card(card_id) if isinstance(card_id, int) and not isinstance(card_id, bool) else None
    if card is None:
        raise Invalid("card")
    if not isinstance(dismissed, bool):
        raise Invalid("dismissed")
    return pulse_store.dismiss_card(card["id"], dismissed, now, rest_until if dismissed else None)


# Rating -----------------------------------------------------------------------

class Invalid(ValueError):
    """A rating Pulse doesn't take: ``field`` names what's wrong."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _lesson(pulse_store: store.PulseStore, kind: str, what: str, *, value: str = "", card: Optional[dict] = None,
            cause: str, now: float, unless: tuple = (), limit: int = 0,
            chat: Optional[tuple[str, str]] = None) -> Optional[dict]:
    """The lesson added, or None when ``limit`` like it are in force already
    (or, for a level, when the kind is at that level already). ``chat``: a
    rule's (profile, chat id), when it isn't the card's own."""
    profile, chat_id = chat if chat is not None else ((card["profile"], card["chat_id"]) if card else ("", ""))
    row = {"kind": kind, "what": what, "value": value, "profile": profile if card or chat else "",
           "chat_id": chat_id if what == RULE else "", "card_id": card["id"] if card else None,
           "cause": cause, "created_at": now}
    changes_from = DEFAULT_LEVELS.get(kind, "push") if what == LEVEL else None
    lesson_id = pulse_store.add_lesson(row, unless=unless, limit=limit, changes_from=changes_from)
    if lesson_id is None:
        return None
    row["id"] = lesson_id
    return row


def set_level(pulse_store: store.PulseStore, kind: str, level_name: str, now: float, *,
              cause: str = SET, card: Optional[dict] = None) -> Optional[dict]:
    """Sets the kind's level, a ceiling (the user's, or "too often" with
    ``card``); the lesson, or None when it's there already. Only the
    follow-ups' level is set by the user; "too often" sets a report kind's."""
    if not isinstance(kind, str) or (kind not in DEFAULT_LEVELS and (cause == SET or not kind)):
        raise Invalid("kind")
    if not isinstance(level_name, str) or level_name not in LEVELS:
        raise Invalid("level")
    if level(pulse_store, kind) == level_name:
        return None
    return _lesson(pulse_store, kind, LEVEL, value=level_name, card=card, cause=cause, now=now)


def lesson_kind(card: dict) -> str:
    """The kind a card's reasons teach about: a follow-up's, or a report
    item's own kind (its topic)."""
    return (card["topic"] or "other") if card["kind"] == NOTICED else card["kind"]


def source_chat(pulse_store: store.PulseStore, card: dict) -> Optional[tuple[str, str]]:
    """The chat a card's thing came from, as (profile, chat id): a
    follow-up's, a report item's candidate's (none when it came from the
    calendar, reminders or notes), or once the journal no longer has it, the
    chat of the remembered thing behind it or the one it was found in (the
    card points there, with no message of Pulse's turn). None when there's
    no chat."""
    if card["kind"] != NOTICED:
        return (card["profile"], card["chat_id"]) if card["chat_id"] else None
    found = pulse_store.card_candidate(card["id"])
    if found is not None:
        return (found["profile"], found["chat_id"]) if found["chat_id"] else None
    in_chat = card["item_id"] is not None or card["message_id"] is None
    return (card["profile"], card["chat_id"]) if in_chat and card["chat_id"] else None


def rate(pulse_store: store.PulseStore, card_id: Any, rating: Any, reason: Any = "", *,
         now: float, zone: Any = None, rest_until: Optional[float] = None) -> list[dict]:
    """Records a rating (a signal pulse_prefs learns from, slowly) and acts
    on its reason at once; the lessons it made. Rating a card again replaces
    its signal; lessons an earlier rating made stay until undone. "Wrong
    time" swipes the card away until ``rest_until``
    (pulse_followup.wrong_time_rest), and closes the part of the day it
    pushed in, in ``zone``, for its kind."""
    card = pulse_store.card(card_id) if isinstance(card_id, int) and not isinstance(card_id, bool) else None
    if card is None:
        raise Invalid("card")
    if rating not in RATINGS:
        raise Invalid("rating")
    reason = reason or ""
    if reason not in ("",) + REASONS or (reason and rating != "down"):
        raise Invalid("reason")
    if card["kind"] not in DEFAULT_LEVELS and card["kind"] != NOTICED:  # checked before the rating is kept
        raise Invalid("card")
    while not pulse_store.rate_card(card, rating=rating, reason=reason, now=now):
        card = pulse_store.card(card["id"])  # rated meanwhile: count from what it says now
        if card is None:
            raise Invalid("card")
    if reason == WRONG_TIME and rest_until is not None:
        pulse_store.dismiss_card(card["id"], True, now, rest_until)
    if card["rating"] == rating and card["reason"] == reason:
        return []
    kind = lesson_kind(card)
    made: list[dict] = []
    if reason == "not_relevant":
        chat = source_chat(pulse_store, card)
        if chat is not None:  # a rule needs a chat: one from the calendar or notes counts as the thumb only
            made.append(_lesson(pulse_store, card["kind"], RULE, value=_clip(card["title"] or card["news"], TOPIC_LIMIT),
                                card=card, cause="not relevant", now=now, unless=("profile", "chat_id", "value"),
                                limit=1, chat=chat))
    elif reason == "already_knew":
        made.append(_lesson(pulse_store, kind, SEEN, card=card, cause="already knew", now=now, limit=1))
    elif reason == "too_late":
        sooner = _lesson(pulse_store, kind, SOONER_LESSON, card=card, cause="too late", now=now, limit=MAX_SOONER)
        made.append(sooner)
        if sooner and kind == "followup":  # checks already booked come sooner too
            pulse_store.sooner_followups(SOONER, now)
    elif reason == "too_often" and LEVELS.index(level(pulse_store, kind)) < LEVELS.index("brief"):
        made.append(set_level(pulse_store, kind, "brief", now, cause=TOO_OFTEN, card=card))
    elif reason == WRONG_TIME and card["level"] == "push" and zone is not None:
        # Only a push came at a time: a card on the screen or in the brief waited for the user.
        made.append(_lesson(pulse_store, kind, PART, value=hours.part_of(pulse_store, zone, float(card["created_at"])),
                            card=card, cause="wrong time", now=now, unless=("value",), limit=1))
    return made


def undo(pulse_store: store.PulseStore, lesson_id: Any, now: float) -> bool:
    if not isinstance(lesson_id, int) or isinstance(lesson_id, bool):
        raise Invalid("lesson")
    return pulse_store.undo_lesson(lesson_id, now)


RESET_VALUE_LIMIT = 300
# The places a reset can name, as pulse_prefs.place writes them.
_RESET_PLACE = re.compile(r"chat:[^:]*:.+|area:.+|source:.+", re.S)


def _plain(text: str) -> bool:
    """No control characters (the C0 set, DEL and the C1 set, which the CLI
    and the app would otherwise echo back), and nothing UTF-8 can't carry
    (an unpaired surrogate from JSON)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(ch) < 32 or 0x7f <= ord(ch) <= 0x9f for ch in text)


def reset(pulse_store: store.PulseStore, scope: Any, value: Any, now: float) -> dict:
    """Forgets what learning holds about one thing (pulse_prefs.forget): a
    kind, a place or a timing (``scope`` "kind", "place" or "timely", with
    the attribute's value as pulse_prefs.preferences gives it), a kind's
    loudness ("loud", with the kind), or everything ("everything", no
    value). The lesson; undoing it counts the signals again."""
    if not isinstance(scope, str) or scope not in prefs.RESET_SCOPES:
        raise Invalid("scope")
    value = value if isinstance(value, str) else None
    if value is None or (scope == prefs.EVERYTHING) != (value == "") or len(value) > RESET_VALUE_LIMIT \
            or not _plain(value) or (scope == "timely" and value not in ("yes", "no")) \
            or (scope == "place" and not _RESET_PLACE.fullmatch(value)):
        raise Invalid("value")
    row = _lesson(pulse_store, scope, RESET, value=value, cause="reset", now=now)
    assert row is not None  # no limit: always added
    return row


# Listing ----------------------------------------------------------------------

def describe(lesson: dict) -> str:
    kind = KIND_NAMES.get(lesson["kind"], lesson["kind"])
    what = lesson["what"]
    if what == LEVEL:
        text = f"{kind}: {LEVEL_NAMES.get(lesson['value'], lesson['value'])}"
    elif what == RULE:
        text = f"{kind}: no updates about \"{lesson['value']}\""
    elif what == SOONER_LESSON:
        text = f"{kind}: check sooner"
    elif what == SEEN:
        text = f"{kind}: skip what you already saw in the chat"
    elif what == PART:
        text = f"{kind}: no pushes {PART_WORDS.get(lesson['value'], lesson['value'])}"
    elif what == RESET:
        return "Forgot everything learned before this" if lesson["kind"] == prefs.EVERYTHING \
            else f"Forgot what was learned about {preference_words(lesson['kind'], lesson['value'], lesson.get('chat_title'))}"
    else:
        text = f"{kind}: {what}"
    return text + (f" ({lesson['cause']})" if lesson["cause"] and lesson["cause"] != "set" else "")


def preference_words(scope: str, value: str, chat_title: Optional[str] = None) -> str:
    """What a preference or a reset is about, in words for `pulse learned`;
    ``chat_title``: a chat place's title, when it's known."""
    if scope == prefs.LOUD:
        return f"how loudly {KIND_NAMES.get(value, value)} come up"
    if scope == "kind":
        return f"{KIND_NAMES.get(value, value)} items"
    if scope == "timely":
        return prefs.TIMELY_WORDS.get(value, value)
    if scope == "place":
        return (f"things from {chat_title or 'a chat'}" if value.startswith("chat:")
                else f"things about {value[5:]}" if value.startswith("area:")
                else f"things from {value[7:]}" if value.startswith("source:") else value)
    return "what Pulse brings up" if scope == "all" else value


def lesson_entry(lesson: dict) -> dict[str, Any]:
    return {"id": lesson["id"], "kind": lesson["kind"], "what": lesson["what"], "value": lesson["value"],
            "chat_id": lesson["chat_id"], "profile": lesson["profile"], "card_id": lesson["card_id"],
            "cause": lesson["cause"], "created_at": lesson["created_at"], "text": describe(lesson)}


def learned(pulse_store: store.PulseStore) -> list[dict]:
    """The lessons in force, newest first. A level lesson an even newer one
    replaced is left out (undoing that newer one brings it back), and so is
    a two-thumb step from before learning, which ``level`` ignores too."""
    current = {}
    for lesson in pulse_store.lessons(what=LEVEL):
        if lesson["cause"] not in STEPPED:
            current[lesson["kind"]] = lesson["id"]
    return [lesson_entry(lesson) for lesson in reversed(pulse_store.lessons())
            if lesson["what"] != LEVEL or current.get(lesson["kind"]) == lesson["id"]]


def levels(pulse_store: store.PulseStore) -> dict[str, str]:
    return {kind: level(pulse_store, kind) for kind in DEFAULT_LEVELS}
