"""Pulse's morning brief: what was held for it, once a day (pulse_learning).

A kind rated down to ``brief`` on the loudness ladder doesn't speak up when
its news comes in; its cards wait, and at the brief time (08:00 in the
report's time zone by default) one Pulse turn in the home chat brings them
together, with a push. Delivered, the cards are stamped ``briefed_at``; a
brief that didn't get through after a few tries leaves them for tomorrow.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Callable, Optional

from . import pulse_report as report
from . import pulse_store as store

BRIEF = "brief"
DEFAULT_TIME = "08:00"
# A brief missed by more than this (the host was off all morning) waits for tomorrow.
WINDOW_S = 8 * 3600.0
TRIES = 3
RETRY_S = 1800.0
MAX_CARDS = 12
LINE_LIMIT = 400
PENDING = "brief_cards"

TEXT = """Pulse morning brief {tag}.
What Pulse held for this morning:
{lines}"""
NOTE = ("This turn was started by Pulse, the part of Hermes that speaks up on its own. The message lists news the "
        "user asked to get in a morning brief instead of as it happened. Give them a short morning brief: one "
        "friendly line per item, each starting with what it's about so it makes sense on its own. Only pass on "
        "what the message says. Don't use any tools.")


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def line(card: dict) -> str:
    where = f' (from the chat "{_clip(card["chat_title"], 80)}")' if card["chat_title"] else ""
    return "- " + _clip(f"{card['news']}{where}", LINE_LIMIT)


def text_for(cards: list[dict]) -> Callable[[str], str]:
    lines = "\n".join(line(card) for card in cards)
    return lambda tag: TEXT.format(tag=tag, lines=lines)


def next_attempt(pulse_store: store.PulseStore, now: float, zone: Any, time_value: Any) -> Optional[tuple[str, int]]:
    """The item id and attempt number of the brief to send now, or None:
    not yet time, already sent today, one on its way, or out of tries."""
    at, _ = report.last_slot(now, zone, time_value or DEFAULT_TIME)
    if now - at > WINDOW_S:
        return None
    tries = pulse_store.attempts_since(BRIEF, at)
    if any(row["state"] in store.IN_FLIGHT or row["state"] == store.DELIVERED for row in tries):
        return None
    if len(tries) >= TRIES or (tries and now - tries[-1]["updated_at"] < RETRY_S):
        return None
    return (tries[0]["item_id"] if tries else f"{secrets.token_hex(4)}-brief"), len(tries) + 1


def settle(pulse_store: store.PulseStore, now: float) -> None:
    """Stamps the cards of a delivered brief; gives up on one out of tries."""
    pending = pulse_store.get(PENDING)
    if not isinstance(pending, dict) or not isinstance(pending.get("item_id"), str):
        return
    rows = pulse_store.attempts_for_suffix(pending["item_id"])
    if any(row["state"] == store.DELIVERED for row in rows):
        ids = [value for value in pending.get("ids") or [] if isinstance(value, int) and not isinstance(value, bool)]
        pulse_store.brief_cards(ids, now)
        pulse_store.set(PENDING, None)
    elif len(rows) >= TRIES and all(row["state"] not in store.IN_FLIGHT for row in rows):
        pulse_store.set(PENDING, None)  # the cards stay held for tomorrow's
