"""Group rooms keep answering once a thread passes 24 messages (designs/cross-owner-agents).

Hermes plans a gateway-hosted room's next turn from a window of each thread:
its last 24 messages, and the turn.settled event of each member reply among
them (gateway/hosted_room_policy_checkpoint.py). The planner first works out
how far each member has read (gateway/hosted_room_discussion.py
``_derive_member_watermarks``), and for that it looks up the user message each
reply answered. Once the window starts at a reply whose user message it
trimmed, the lookup fails on every attempt: the room takes no more turns, in
any thread, and nothing says why. Until Hermes fixes it, the plugin fills in
each trimmed user message for that lookup. Its id is the reply's
discussion_event_id, and its place in the room log is the seq the reply's turn
id starts with (Hermes builds turn ids from it), which is all the lookup
reads. So the planner gets the watermarks it would have had with those
messages still in the window. Anything else wrong with a window still fails as
before.
"""

from __future__ import annotations

import functools
import importlib
import logging
import re
import threading
import types
from collections.abc import Mapping
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PLANNER = "gateway.hosted_room_discussion"
DERIVE = "_derive_member_watermarks"
# The one failure the plugin answers, word for word.
TRIMMED_DISCUSSION = "turn.settled references no matching user discussion"
# Hermes' _TURN_ID_RE: d<the discussion's seq>.r<round>.p<position>.s<seen through>.m<member digest>.
TURN_ID = re.compile(r"d(?P<discussion>[1-9][0-9]*)\.r[0-2]\.p[0-5]\.s[1-9][0-9]*\.m[0-9a-f]{24}")
_GUARD_FLAG = "__conduit_room_guard__"

_lock = threading.Lock()
# The planner module this load guards, Hermes' own function and this load's guard.
_guarded: Optional[tuple[Any, Callable[..., Any], Callable[..., Any]]] = None
_filled_logged = False


def _trimmed_discussions(events: Any) -> Optional[list[Any]]:
    """Stand-ins for the user messages the window lost, one per discussion a
    reply in it settled. None when one can't be made from the reply's turn id,
    or the turn ids disagree with the log: then Hermes' error stands."""
    present = {getattr(event, "event_id", None) for event in events}
    # A room log's seqs are unique: one the window holds, or another stand-in
    # took, isn't this discussion's.
    taken = {getattr(event, "seq", None) for event in events}
    found: dict[str, tuple[int, Any]] = {}
    for event in events:
        payload = getattr(event, "payload", None)
        if getattr(event, "kind", None) != "turn.settled" or not isinstance(payload, Mapping) or payload.get("passed"):
            continue
        discussion_id = payload.get("discussion_event_id")
        if not isinstance(discussion_id, str) or not discussion_id:
            return None
        if discussion_id in present:
            continue
        match = TURN_ID.fullmatch(str(payload.get("turn_id") or ""))
        if match is None:
            return None
        seq = int(match.group("discussion"))
        if discussion_id in found:
            # Every reply to one discussion names the same place for it.
            if found[discussion_id][0] != seq:
                return None
            continue
        # A discussion comes before the replies that settle it.
        if seq in taken or seq >= event.seq:
            return None
        taken.add(seq)
        found[discussion_id] = (seq, payload.get("thread_id"))
    return [types.SimpleNamespace(raw={}, seq=seq, event_id=discussion_id, kind="message.user", actor={},
                                  payload={"thread_id": thread_id})
            for discussion_id, (seq, thread_id) in found.items()]


def _log_filled(count: int) -> None:
    global _filled_logged
    if _filled_logged:
        logger.debug("conduit_push: filled in %d trimmed group room message(s) for Hermes' planner", count)
        return
    _filled_logged = True
    logger.info("conduit_push: a group room thread is longer than Hermes' room planner keeps, so the plugin "
                "filled in %d trimmed user message(s) from their replies' turn ids (logged once)", count)


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    # Takes whatever Hermes passes and hands it on as it came.
    def derive_member_watermarks(events: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original(events, *args, **kwargs)
        except ValueError as error:
            if str(error) != TRIMMED_DISCUSSION:
                raise
            try:
                stand_ins = _trimmed_discussions(events)
            except Exception:  # noqa: BLE001 - a window shaped otherwise: Hermes' error stands
                logger.debug("conduit_push: couldn't read a group room window", exc_info=True)
                stand_ins = None
            if not stand_ins:
                raise
        _log_filled(len(stand_ins))
        return original(tuple(sorted([*events, *stand_ins], key=lambda event: event.seq)), *args, **kwargs)

    functools.update_wrapper(derive_member_watermarks, original)
    setattr(derive_member_watermarks, _GUARD_FLAG, True)
    return derive_member_watermarks


def install() -> bool:
    """Guards Hermes' room planner, once per process. Imports it, since the
    plugin can load before the gateway or the dashboard starts its rooms;
    the planner looks the function up by name on every call, so every caller
    gets the guard. False when this Hermes has no such planner."""
    global _guarded
    with _lock:
        if _guarded is not None:
            return True
        try:
            planner = importlib.import_module(PLANNER)
        except Exception:  # noqa: BLE001 - no Hermes rooms here (tests, an older Hermes)
            logger.debug("conduit_push: no Hermes room planner to guard", exc_info=True)
            return False
        own = getattr(planner, DERIVE, None)
        # A guard an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            logger.info("conduit_push: Hermes' room planner has no %s, so the plugin leaves it as it is", DERIVE)
            return False
        guard = _guard(own)
        setattr(planner, DERIVE, guard)
        _guarded = (planner, own, guard)
        return True


def uninstall() -> None:
    """Puts Hermes' own function back (the plugin unloads). A guard another
    load of the plugin has set since is left to that load."""
    global _guarded
    with _lock:
        guarded, _guarded = _guarded, None
        if guarded is not None:
            planner, own, guard = guarded
            if vars(planner).get(DERIVE) is guard:
                setattr(planner, DERIVE, own)
