"""Hermes reaches for what Conduit shares before other routes to the same data.

A user who set up a calendar CLI (such as gog) or another skill before turning
on Conduit's sharing has two ways to the same answer, and Hermes takes
whichever its skills or memory mention first: often the slower one, a CLI that
asks Google over the network, where Conduit's copy is already on this host
and covers every account on the phone. So each shared tool's description says
to use it first, and each turn of a main conversation gets one line naming
what the phone shares here. Hermes treats ``pre_llm_call`` context as part of
that turn only, so the line never piles up in the chat.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from hermes_constants import get_hermes_home

logger = logging.getLogger("hermes.plugins.conduit_push")

# The last sentence of each shared tool's description. Not sent to the live
# voice models, which have no other route.
CALENDAR_FIRST = ("Use this first for the user's calendar and reminders, before any other calendar or reminders "
                  "tool, skill or command-line tool (such as gog): it is a local copy of every account on their "
                  "phone and answers at once. Use another only when the user asks for it, or for what this doesn't "
                  "have (a calendar the phone doesn't share, or a change since the last sync that matters).")
CONTACTS_FIRST = ("For the people listed here, use this first, before other contacts tools, skills or command-line "
                  "tools (such as gog); look elsewhere only for someone who isn't here, or when the user asks.")
PHONE_FIRST = ("Use this first for the phone's battery, Focus, alarms and timers, before any other tool or skill "
               "for them, unless the user asks for another.")
HEALTH_FIRST = ("Use this first for the user's health and fitness, before any other health or fitness tool or "
                "skill, unless the user asks for another.")
LOCATION_FIRST = ("Use this first for where the user is, before any other way of finding out (another tool, "
                  "skill or IP lookup, or asking them), unless the user asks for another or this has no recent "
                  "location.")

# How long one look at what's shared holds: a turn shouldn't open every store.
CACHE_S = 60.0
# A host has a few profiles; past this many homes the oldest look goes.
MAX_HOMES = 32

_cache_lock = threading.Lock()
# Hermes home -> (when it was looked at, the note or None).
_cache: dict[str, tuple[float, str | None]] = {}


def _shared() -> list[str]:
    """What the phone shares with this profile now, each with its tool."""
    from . import health_tool, location_tool, phone_tool

    checks: tuple[tuple[Callable[[], bool], str], ...] = (
        (phone_tool.calendar_available, "their calendar and reminders (conduit_calendar)"),
        (health_tool.available, "their health data (conduit_health)"),
        (location_tool.available, "their location (conduit_location)"),
        (phone_tool.phone_available, "the phone's status and Focus (conduit_phone)"),
        (phone_tool.contacts_available, "the people they picked (conduit_contacts)"),
        (phone_tool.photos_available, "their photos (conduit_photos)"),
    )
    shared = []
    for available, what in checks:
        # One store that can't be read leaves out only its own kind.
        try:
            if available():
                shared.append(what)
        except Exception:  # noqa: BLE001
            logger.warning("Conduit could not check whether %s is shared", what, exc_info=True)
    return shared


def _join(items: list[str]) -> str:
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def note(clock: Callable[[], float] = time.monotonic) -> str | None:
    """The line for this turn, or None when nothing is shared. Never raises:
    a hook calls this."""
    try:
        # The profile's own home: Hermes scopes it for the turn, as it does
        # for each tool's availability check.
        home = str(get_hermes_home())
        with _cache_lock:
            cached = _cache.get(home)
            if cached is not None and clock() - cached[0] < CACHE_S:
                return cached[1]
        shared = _shared()
        text = (f"[Conduit: the user's iPhone shares {_join(shared)} with you. For these, use the Conduit tool "
                "first, before other tools, skills or command-line tools for the same data (such as gog): it's "
                "on this host and fastest. Use another only if the user asks for it or the Conduit tool doesn't "
                "have what's needed.]") if shared else None
        with _cache_lock:
            # Two turns that missed together both look; either answer is current.
            _cache.pop(home, None)
            _cache[home] = (clock(), text)
            while len(_cache) > MAX_HOMES:
                _cache.pop(next(iter(_cache)))
        return text
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not check what the phone shares for this turn", exc_info=True)
        return None
