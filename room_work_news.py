"""Work news for a bot in a Linked Room someone else hosts (Run work; Eric 2026-11-11).

A bot started work from a Linked Room (conduit_room_work), and the work reported. Where the room is hosted on the
bot's own Hermes, room_work posts a bare @mention into it, and that mention gives the bot a turn there, where it
hears the report privately. A room hosted on another installation can't be posted into from here, so the bot's
Hermes asks the room's home, over the Link that invited it, to post the same mention for it (room.work_news). The
home checks the agent is one of that Link's in the started room, and posts room_work's LINKED_POST under the
bot's name. Only the notice crosses: what the work reported stays on the bot's Hermes and reaches the bot at the
start of that turn (work_tool.frame_room). A home on an older plugin drops the envelope, and the bot hears the
news at its next turn there, as before.

Hermes' calls are imported when they're used, so the tests give them as fakes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

from . import room_links, room_work

logger = logging.getLogger(__name__)

VERSION = 1
NEWS = "room.work_news"
KINDS = (NEWS,)
# Notices the home posts for one agent: at most one a minute, and PER_HOUR an hour (each gives it a turn).
GAP_S = 60
PER_HOUR = 20
HOUR_S = 3600

_lock = threading.Lock()


def ask_home(root: Any, room_id: str, profile: str, rooms: Any = None) -> Optional[str]:
    """Asks the home of the room ``room_id`` to post the notice that wakes this installation's agent on
    ``profile`` there. None once asked (the Link sends it when it can), else why not. ``rooms``: the
    installation's room_invites.Rooms, made for ``root`` when not given."""
    from . import room_invites, room_posts

    try:
        rooms = rooms if rooms is not None else room_invites.Rooms(root)
        state = rooms.store.read()
    except Exception as exc:  # noqa: BLE001 — the bot hears it at its next turn there
        return f"the rooms couldn't be read ({type(exc).__name__})"
    invite = room_posts.current_invite(list(state["invites"].values()), room_id)
    agent = None
    if invite is not None and invite["status"] == "started":
        agent = next((each for each in invite["joined"] if each.get("profile") == profile), None)
    if agent is None:
        return "this bot isn't in that room"
    now = int(rooms.clock())
    try:
        with rooms.store.update() as state:
            room_invites._queue(state, invite["link"], NEWS, room_id,
                                {"v": VERSION, "room_id": room_id, "member_id": agent["member_id"]}, now,
                                parts=(agent["member_id"], uuid.uuid4().hex))
        rooms.flush()
    except Exception as exc:  # noqa: BLE001 — queued or not, the bot hears it at its next turn there
        return f"the notice couldn't be sent ({type(exc).__name__})"
    return None


class News:
    """The home's side: the notices it posts for linked agents in the rooms ``rooms`` (room_invites.Rooms)
    hosts. ``deliver`` is room_work.post's (Hermes' room delivery)."""

    def __init__(self, rooms: Any, *, deliver: Optional[Callable[..., Optional[str]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.rooms = rooms
        self.deliver = deliver
        self.clock = clock
        self._posted: dict[tuple[str, str], list[float]] = {}

    def on_news(self, received: room_links.Received) -> None:
        body = received.body
        if not isinstance(body, dict) or set(body) != {"v", "room_id", "member_id"} or body["v"] != VERSION \
                or not isinstance(body["room_id"], str) or not isinstance(body["member_id"], str):
            logger.info("Linked Rooms dropped a room.work_news from Link %s that isn't one", received.link_id)
            return
        room_id, member_id = body["room_id"], body["member_id"]
        draft = self.rooms.store.read()["drafts"].get(room_id)
        member = None
        if draft is not None and draft["status"] == "started":
            member = next((each for each in draft.get("members") or () if each["member_id"] == member_id
                           and each["link"] == received.link_id and each.get("status", "in") == "in"), None)
        if member is None:
            logger.info("Linked Rooms dropped a room.work_news from Link %s for an agent it hasn't in room %s",
                        received.link_id, room_id)
            return
        now = float(self.clock())
        key = (room_id, member_id)
        with _lock:
            times = [at for at in self._posted.get(key, ()) if now - at < HOUR_S]
            if (times and now - times[-1] < GAP_S) or len(times) >= PER_HOUR:
                self._posted[key] = times
                logger.info("Linked Rooms didn't post work news for %s in room %s: one went just now",
                            member["handle"], room_id)
                return
            self._posted[key] = [*times, now]
        # Posted from a profile of this installation's own in the room: Hermes posts to the store that serves it.
        profiles = [each["profile"] for each in draft["members"] if each["link"] is None and each.get("profile")]
        error = room_work.post({"room": room_id, "handle": member["handle"], "linked": True},
                               bot=member["display_name"], key=f"news-{received.message_id}"[:100], lines=[],
                               profiles=profiles, deliver=self.deliver)
        if error is not None:
            logger.info("Linked Rooms couldn't post work news for %s in room %s (%s)", member["handle"], room_id,
                        error)
