"""Pulse's morning brief: every day at the brief time (08:00 in the
report's time zone by default), one Pulse turn in the home chat sums up
what's still outstanding, with a push: open promises and things the user
said they'd do, today's calendar and reminders, and what was held for it.

A kind rated down to ``brief`` on the loudness ladder doesn't speak up when
its news comes in; its cards wait and come with the brief. Delivered, they
are stamped ``briefed_at``; a brief that didn't get through after a few
tries leaves them for tomorrow.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path
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

HELD_TITLE = "## Held for this morning"
TEXT = """Pulse morning brief {tag}.
{context}"""
NOTE = ("This turn was started by Pulse, the part of Hermes that speaks up on its own, for the user's morning "
        "brief: one card that sums up what's still outstanding for them. Start with one short, friendly sentence "
        "under 200 characters that sums up their day (it's also the notification). Then, as short lines starting "
        "with \"- \", at most 8 of what still needs them, the most pressing first: what's due or on the calendar "
        "today, then open promises Hermes made and things the user said they'd do. Leave out anything the "
        "\"Later on the same subject\" lines or the reminders show done or moved past, and anything not worth "
        "their morning. News under \"Held for this morning\" is already shown as its own card under your reply: "
        "give it a few words at most. When nothing is outstanding, say so in the one sentence and stop. Only pass "
        "on what the message says. Don't use any tools.")


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def line(card: dict) -> str:
    where = f' (from the chat "{_clip(card["chat_title"], 80)}")' if card["chat_title"] else ""
    return "- " + _clip(f"{card['news']}{where}", LINE_LIMIT)


def context(home: Path, pulse_store: store.PulseStore, cards: list[dict], *, home_chat_id: str, now: float,
            zone: Any) -> str:
    """What the brief reads: the time, what was held for it, what Pulse
    memory keeps open and what happened since on the same subject, and the
    phone's calendar and reminders, every section that has something in it."""
    sections = [f"## Now\n{datetime.fromtimestamp(now, zone).strftime('%A %d %B %Y, %H:%M')}"]
    if cards:
        sections.append(HELD_TITLE + "\n" + "\n".join(line(card) for card in cards))
    if pulse_store.get("memory"):
        listed, shown = report.remembered_lines(report.remembered_items(pulse_store, now)[0], zone)
        sections += [listed, report.later_lines(home, shown, home_chat_id=home_chat_id, zone=zone) if shown else ""]
    sections.append(report.phone_context(home, now)[0])
    sections = [section for section in sections if section]
    if len(sections) == 1:
        sections.append("Nothing open: Pulse holds nothing for the brief, remembers no open items, and the phone "
                        "shares no calendar or reminders.")
    return "\n\n".join(sections)


def text_for(context_text: str) -> Callable[[str], str]:
    return lambda tag: TEXT.format(tag=tag, context=context_text)


def summary_line(reply: str) -> str:
    """The brief's first line, for its push: the rest is for its card."""
    return next((text.strip() for text in reply.splitlines() if text.strip()), "")


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
        pulse_store.brief_cards(ids, now, pending["item_id"])
        pulse_store.set(PENDING, None)
    elif len(rows) >= TRIES and all(row["state"] not in store.IN_FLIGHT for row in rows):
        pulse_store.set(PENDING, None)  # the cards stay held for tomorrow's
