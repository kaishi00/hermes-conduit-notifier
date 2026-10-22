"""What the user's ratings change (designs/pulse-slice-2.md, PR 3).

Everything Pulse brings the user becomes a card (Conduit's Pulse screen
lists them). A thumbs up or down on a card changes what Pulse does, in
code, not as a hint to a model:

- Each kind sits on a loudness ladder: ``push`` (a turn in the home chat
  and a push), ``screen`` (a card in the Pulse screen, no turn, no push),
  ``brief`` (a card held for the morning brief, which stamps ``briefed_at``;
  until the brief is built it waits in the Pulse screen), ``off`` (not
  checked at all). Net
  thumbs down move it a step quieter, net thumbs up a step back louder.
- A thumbs down can carry a reason:
  - ``not_relevant``: a rule, "no updates about this": that chat's items are
    closed before Pulse speaks, and the topic goes to every check;
  - ``already_knew``: checks skip news the user already saw in that chat;
  - ``too_late``: checks come sooner (half the wait, at most twice);
  - ``too_often``: the kind moves to the morning brief at once.

Each change is a lesson row; the Pulse screen lists them and undoing one
puts Pulse back the way it was.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from . import pulse_store as store

LEVELS = ("push", "screen", "brief", "off")
DEFAULT_LEVELS = {"followup": "push"}
RATINGS = ("up", "down")
REASONS = ("not_relevant", "already_knew", "too_late", "too_often")
# Net thumbs, one way, that move a kind a step along the ladder.
STEP_AT = 2
SOONER = 0.5
MAX_SOONER = 2
SCORES = "rating_scores"
LEVEL, RULE, SOONER_LESSON, SEEN = "level", "rule", "sooner", "seen"
TOPIC_LIMIT = 200
CARDS = 100

KIND_NAMES = {"followup": "Follow-ups"}
LEVEL_NAMES = {"push": "sent with a push", "screen": "shown in Pulse without a push",
               "brief": "held for the morning brief", "off": "off"}


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# What's in force --------------------------------------------------------------

def level(pulse_store: store.PulseStore, kind: str) -> str:
    """The kind's loudness: its newest level lesson still in force, else its default."""
    lessons = [lesson for lesson in pulse_store.lessons(kind, LEVEL) if lesson["value"] in LEVELS]
    return lessons[-1]["value"] if lessons else DEFAULT_LEVELS.get(kind, "push")


def pace(pulse_store: store.PulseStore, kind: str) -> float:
    """How much of the usual wait a check waits: 1, or less after "too late"."""
    return SOONER ** min(len(pulse_store.lessons(kind, SOONER_LESSON)), MAX_SOONER)


def skips_seen(pulse_store: store.PulseStore, kind: str) -> bool:
    return bool(pulse_store.lessons(kind, SEEN))


def rules(pulse_store: store.PulseStore, kind: str) -> list[dict]:
    return pulse_store.lessons(kind, RULE)


def muted_by(pulse_store: store.PulseStore, kind: str, profile: str, chat_id: str) -> Optional[dict]:
    """The rule that closes this chat's items for the kind, if any."""
    return next((rule for rule in rules(pulse_store, kind)
                 if rule["chat_id"] and rule["chat_id"] == chat_id and rule["profile"] == profile), None)


# Cards ------------------------------------------------------------------------

def add_card(pulse_store: store.PulseStore, *, kind: str, item: dict, level_name: str, news: str, why: str,
             chat_title: str, now: float) -> int:
    """The item's card for this news; the one already there when a crash
    came between making it and closing the item."""
    existing = pulse_store.card_for(kind, item["id"], news)
    if existing is not None:
        return existing
    return pulse_store.add_card({
        "kind": kind, "item_id": item["id"], "level": level_name, "title": _clip(item["text"], 300),
        "quote": _clip(item["evidence"], 300), "news": news, "why": why, "profile": item["profile"],
        "chat_id": item["chat_id"], "chat_title": _clip(chat_title, 120), "message_id": item["message_id"],
        "created_at": now})


def card_entry(card: dict) -> dict[str, Any]:
    return {key: card[key] for key in ("id", "kind", "item_id", "level", "title", "quote", "news", "why", "profile",
                                       "chat_id", "chat_title", "message_id", "created_at", "seen_at", "briefed_at",
                                       "rating", "reason", "rated_at")}


# Rating -----------------------------------------------------------------------

class Invalid(ValueError):
    """A rating Pulse doesn't take: ``field`` names what's wrong."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _lesson(pulse_store: store.PulseStore, kind: str, what: str, *, value: str = "", card: Optional[dict] = None,
            cause: str, now: float) -> dict:
    row = {"kind": kind, "what": what, "value": value, "profile": card["profile"] if card else "",
           "chat_id": card["chat_id"] if card and what == RULE else "", "card_id": card["id"] if card else None,
           "cause": cause, "created_at": now}
    row["id"] = pulse_store.add_lesson(row)
    return row


def _step(current: str, louder: bool) -> Optional[str]:
    index = LEVELS.index(current) + (-1 if louder else 1)
    return LEVELS[index] if 0 <= index < len(LEVELS) else None


def set_level(pulse_store: store.PulseStore, kind: str, level_name: str, now: float, *,
              cause: str = "set", card: Optional[dict] = None) -> Optional[dict]:
    """Moves the kind to ``level_name``; the lesson, or None when it's there already."""
    if not isinstance(kind, str) or kind not in DEFAULT_LEVELS:
        raise Invalid("kind")
    if not isinstance(level_name, str) or level_name not in LEVELS:
        raise Invalid("level")
    if level(pulse_store, kind) == level_name:
        return None
    _scores_reset(pulse_store, kind)
    return _lesson(pulse_store, kind, LEVEL, value=level_name, card=card, cause=cause, now=now)


def _change_score(pulse_store: store.PulseStore, kind: str, change: Any) -> int:
    """Applies ``change`` (old score -> new) to the kind's score with a swap,
    so the CLI and the dashboard rating at once don't lose a thumb."""
    while True:
        stored = pulse_store.get(SCORES)
        scores = dict(stored) if isinstance(stored, dict) else {}
        current = scores.get(kind) if isinstance(scores.get(kind), int) else 0
        scores[kind] = change(current)
        if pulse_store.swap(SCORES, stored, scores):
            return scores[kind]


def _scores_reset(pulse_store: store.PulseStore, kind: str) -> None:
    _change_score(pulse_store, kind, lambda current: 0)


def rate(pulse_store: store.PulseStore, card_id: Any, rating: Any, reason: Any = "", *,
         now: float) -> list[dict]:
    """Records a rating and acts on it; the lessons it made. Rating a card
    again counts only the change (a down then an up nets nothing); lessons
    an earlier rating made stay until undone."""
    card = pulse_store.card(card_id) if isinstance(card_id, int) and not isinstance(card_id, bool) else None
    if card is None:
        raise Invalid("card")
    if rating not in RATINGS:
        raise Invalid("rating")
    reason = reason or ""
    if reason not in ("",) + REASONS or (reason and rating != "down"):
        raise Invalid("reason")
    if card["kind"] not in DEFAULT_LEVELS:  # checked before the rating is kept
        raise Invalid("card")
    while not pulse_store.rate_card(card, rating=rating, reason=reason, now=now):
        card = pulse_store.card(card["id"])  # rated meanwhile: count from what it says now
        if card is None:
            raise Invalid("card")
    if card["rating"] == rating and card["reason"] == reason:
        return []
    kind = card["kind"]
    delta = (1 if rating == "up" else -1) - {"up": 1, "down": -1}.get(card["rating"], 0)
    made: list[dict] = []
    if reason == "not_relevant" and not muted_by(pulse_store, kind, card["profile"], card["chat_id"]):
        made.append(_lesson(pulse_store, kind, RULE, value=_clip(card["title"], TOPIC_LIMIT), card=card,
                            cause="not relevant", now=now))
    elif reason == "already_knew" and not skips_seen(pulse_store, kind):
        made.append(_lesson(pulse_store, kind, SEEN, card=card, cause="already knew", now=now))
    elif reason == "too_late" and len(pulse_store.lessons(kind, SOONER_LESSON)) < MAX_SOONER:
        made.append(_lesson(pulse_store, kind, SOONER_LESSON, card=card, cause="too late", now=now))
        if kind == "followup":  # checks already booked come sooner too
            pulse_store.sooner_followups(SOONER, now)
    elif reason == "too_often" and LEVELS.index(level(pulse_store, kind)) < LEVELS.index("brief"):
        made.append(set_level(pulse_store, kind, "brief", now, cause="too often", card=card))
        return made
    if not delta:
        return made
    current_level = level(pulse_store, kind)
    step: dict[str, Any] = {}

    def change(score: int) -> int:
        # The step and the score going back to 0 are one swap, so two
        # ratings at once can't both spend the same two thumbs.
        new = max(-STEP_AT, min(STEP_AT, score + delta))
        step["to"] = _step(current_level, louder=new > 0) if abs(new) >= STEP_AT else None
        return 0 if step["to"] else new

    _change_score(pulse_store, kind, change)
    if step.get("to"):
        made.append(_lesson(pulse_store, kind, LEVEL, value=step["to"], card=card,
                            cause=f"{STEP_AT} thumbs {'up' if delta > 0 else 'down'}", now=now))
    return made


def undo(pulse_store: store.PulseStore, lesson_id: Any, now: float) -> bool:
    if not isinstance(lesson_id, int) or isinstance(lesson_id, bool):
        raise Invalid("lesson")
    return pulse_store.undo_lesson(lesson_id, now)


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
    else:
        text = f"{kind}: {what}"
    return text + (f" ({lesson['cause']})" if lesson["cause"] and lesson["cause"] != "set" else "")


def lesson_entry(lesson: dict) -> dict[str, Any]:
    return {"id": lesson["id"], "kind": lesson["kind"], "what": lesson["what"], "value": lesson["value"],
            "chat_id": lesson["chat_id"], "profile": lesson["profile"], "card_id": lesson["card_id"],
            "cause": lesson["cause"], "created_at": lesson["created_at"], "text": describe(lesson)}


def learned(pulse_store: store.PulseStore) -> list[dict]:
    """The lessons in force, newest first. A level lesson an even newer one
    replaced is left out: undoing that newer one brings it back."""
    current = {}
    for lesson in pulse_store.lessons(what=LEVEL):
        current[lesson["kind"]] = lesson["id"]
    return [lesson_entry(lesson) for lesson in reversed(pulse_store.lessons())
            if lesson["what"] != LEVEL or current.get(lesson["kind"]) == lesson["id"]]


def levels(pulse_store: store.PulseStore) -> dict[str, str]:
    return {kind: level(pulse_store, kind) for kind in DEFAULT_LEVELS}
