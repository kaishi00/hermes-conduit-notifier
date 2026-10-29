"""Use my data: an agent in a room with other people's agents says when its owner is busy, the
``conduit_free_busy`` tool (designs/cross-owner-agents, N8).

In a room whose owner chose Use my data, the agent reads when its owner is busy from the calendar the phone shares
with the profile (phone_store's busy blocks: merged across the shared calendars; free, cancelled and declined events
don't count, nor all-day ones unless marked busy), at most 31 days a call. It gets the busy times and nothing else:
no titles, places, calendars, people or device. Choosing Use my data is the owner's pick of these times, so the agent
shares them without asking, whatever the room's share choice (room_rules.FREE_BUSY_LINE).

The tool is in ``conduit_rooms``, which room setup keeps in every room ceiling, and the room rules let it through only
in a room that allows it (room_rules.allowed_tools); the handler checks again. Anywhere else it refuses.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home

from . import phone_tool, room_asks, room_rules
from .phone_store import PhoneError, PhoneStore

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = room_rules.FREE_BUSY_TOOL
TOOLSET = room_asks.TOOLSET
# Enough to find a date, not a season's pattern (conduit_calendar reads 92).
MAX_DAYS = 31

_CANT = "Say you can't check your owner's calendar here, and share nothing about their plans."
NOTE = ("When your owner is busy, from their calendar as their phone last synced it (as_of): you may share these "
        "times here, nothing else about their plans. Times between them are free, as far as that calendar knows.")

DESCRIPTION = (
    "When your owner is busy, for finding a time in this group room (the room is set to Use my data): only the "
    "busy times from the calendar their phone shares, never what, where or with whom, and you may share them here. "
    "from: a local date (YYYY-MM-DD) or ISO time; to: the end, a local date (inclusive) or ISO time, default the "
    f"from date. At most {MAX_DAYS} days a call."
)

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "The start: a local date YYYY-MM-DD or an ISO time. Default: "
                                                      "today."},
            "to": {"type": "string", "description": "The end: a local date (inclusive) or an ISO time. Default: the "
                                                    f"from date. At most {MAX_DAYS} days after from."},
        },
    },
}


def register(ctx: Any, *, is_child: Callable[[Any], bool]) -> None:
    """Registers the tool, if this Hermes supports plugin tools."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child)

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler,
            "check_fn": room_asks.available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="📅")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False,
           room_turn: Optional[Callable[[str], Any]] = None, choices: Optional[Callable[[Any], Any]] = None,
           store: Optional[PhoneStore] = None, now: Optional[Callable[[], float]] = None) -> str:
    try:
        session = session_id.strip() if isinstance(session_id, str) else ""
        answer = _handle(args if isinstance(args, dict) else {}, session, is_child,
                         room_turn or (lambda sid: room_rules.room_turn(sid, get_hermes_home())),
                         choices or (lambda turn: room_rules.choices_for(turn, get_hermes_home())),
                         store or PhoneStore(get_hermes_home()), now)
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_free_busy failed", exc_info=True)
        answer = {"ok": False, "error": "unavailable", "note": _CANT}
    return json.dumps(answer, ensure_ascii=False)


def _handle(args: dict[str, Any], session: str, is_child: Callable[[Any], bool], room_turn: Callable[[str], Any],
            choices: Callable[[Any], Any], store: PhoneStore, now: Optional[Callable[[], float]]) -> dict[str, Any]:
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the room's own turn can read when your owner is busy. Report back; the main agent can."}
    try:
        turn = room_turn(session) if session else None
    except room_rules.RoomStoreError:
        logger.debug("conduit_push: free/busy from room session %s Hermes hasn't titled yet", session, exc_info=True)
        return {"ok": False, "error": "unavailable", "note": _CANT}
    if turn is None or not turn.with_others:
        return {"ok": False, "error": "not_room",
                "note": "This says when your owner is busy in a group room with other people's agents; this isn't "
                        "one. In a private chat, use conduit_calendar."}
    if not turn.room_id or TOOL_NAME not in room_rules.allowed_tools(choices(turn)):
        return {"ok": False, "error": "not_allowed", "note": room_rules.TOOL_BLOCK.format(tool=TOOL_NAME)}
    if now is not None:
        store.clock = now
    try:
        with store.reader("events") as opened:
            if opened is None:
                return {"ok": False, "error": "nothing_shared",
                        "note": f"Your owner's calendar isn't shared with this Hermes. {_CANT}"}
            db, device = opened
            tz = device["time_zone"]
            start, end, shown = phone_tool._range(args, tz, store.clock())
            zone = phone_tool._zone(tz)
            # In local time (one tzinfo subtracts wall clocks), so a clock change in the range doesn't count.
            if datetime.fromtimestamp(end, zone) - datetime.fromtimestamp(start, zone) > timedelta(days=MAX_DAYS):
                return {"ok": False, "error": "too_long", "note": f"At most {MAX_DAYS} days per call; split the range."}
            blocks = store.busy_blocks(db, device["id"], start, end)
    except PhoneError as error:
        if error.code in ("too_long", "malformed"):
            return {"ok": False, "error": error.code, "note": str(error)}
        return {"ok": False, "error": "nothing_shared", "note": f"Your owner's calendar can't be read here. {_CANT}"}
    answer = {"ok": True, **shown, "time_zone": tz, "as_of": phone_tool._ago(device["last_seen"], store.clock()),
              "busy": [{"start": phone_tool._local(a, tz), "end": phone_tool._local(b, tz)} for a, b in blocks]}
    if device["window_from"] and (start < _epoch(device["window_from"]) or end > _epoch(device["window_to"])):
        answer["window"] = {"from": device["window_from"], "to": device["window_to"]}
        note = " Only the synced window (window) is known: say so for times outside it."
    else:
        note = ""
    answer["note"] = NOTE + note
    return answer


def _epoch(value: Any) -> int:
    """A window edge (an epoch, or an ISO time as the store keeps it) as an epoch; unreadable: 0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return 0
