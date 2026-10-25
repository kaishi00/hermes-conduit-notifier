"""Owner rules for turns in rooms with other people's agents (designs/cross-owner-agents).

A Linked Room seats agents of different owners, each running on its owner's
own Hermes. Whatever limits an agent there runs on that host, through the
plugin's hooks: the tools its owner allows in the room, no memory or skill
writes, no search of the owner's past chats unless chosen, a line telling the
agent who's asking, and credential-shaped text kept out of its replies.

The rules apply only to a room with a member from another installation.
Rooms with only this host's own agents (the ones people had before Linked
Rooms) keep working as they did.

A room turn is told apart by its session. A member whose room lives on
another installation runs in ``room_<digest>`` (gateway/session_api); a local
member's session has an ordinary id, source ``bot_room`` and the title
``Group: <room id>``, and the room's roster, in the room store, says whether
anyone in it is from another installation.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

STATE_DB = "state.db"
# hosted_rooms.default_db_path(): the legacy service's rooms, in the root home.
LEGACY_ROOMS_DB = "shared-state.db"
ROOM_SOURCE = "bot_room"
ROOM_SESSION_ID = re.compile(r"room_[0-9a-f]{32}")
ROOM_TITLE = "Group: "
_ROOM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Compaction continuations followed back to the room's own session.
LINEAGE_HOPS = 32
# Profiles whose stores are read for a room another profile owns.
MAX_PROFILE_STORES = 64
MAX_REMEMBERED = 4096
# Hermes' own budget for a read-only reader (as room_memory): a rollback-journal
# store keeps readers waiting while another process commits, and a WAL
# writer's checkpoint shows them "disk I/O error" for a few milliseconds.
READ_TIMEOUT = 5.0
READ_RETRIES, READ_RETRY_PAUSE = 3, 0.05

_Read = TypeVar("_Read")


class RoomStoreError(RuntimeError):
    """A room session's room couldn't be read, so whether it has other
    people's agents in it is unknown."""


@dataclass(frozen=True)
class RoomTurn:
    room_id: str
    side: str  # "home": the room lives on this host; "member": on another installation
    with_others: bool  # a member from another installation sits in the room
    session_id: str


_lock = threading.Lock()
_remembered: "OrderedDict[tuple[str, str], Optional[RoomTurn]]" = OrderedDict()
_MISSING = object()
_unread_count = 0


def room_turn(session_id: Any, home: Any = None) -> Optional[RoomTurn]:
    """The room this session's turns are in, or None for any other session.
    A session store that can't be read counts as no room (logged); a room
    store that can't be read raises RoomStoreError. Answers are remembered
    per session: a room's members never change once it starts."""
    value = str(session_id or "")
    if not value:
        return None
    home_path = Path(home) if home is not None else _hermes_home()
    key = (str(home_path), value)
    with _lock:
        known = _remembered.get(key, _MISSING)
        if known is not _MISSING:
            _remembered.move_to_end(key)
            return known  # type: ignore[return-value]
    turn, settled = _find(value, home_path)
    if settled:
        with _lock:
            _remembered[key] = turn
            while len(_remembered) > MAX_REMEMBERED:
                _remembered.popitem(last=False)
    return turn


def forget() -> None:
    """Drops what room_turn remembers (tests, the plugin unloading)."""
    with _lock:
        _remembered.clear()


def _find(session_id: str, home: Optional[Path]) -> tuple[Optional[RoomTurn], bool]:
    """``(turn, settled)``: an answer that isn't settled is read again next time."""
    remote = bool(ROOM_SESSION_ID.fullmatch(session_id))
    unnamed_member = RoomTurn(room_id="", side="member", with_others=True, session_id=session_id)
    if home is None:
        return (unnamed_member if remote else None), False
    try:
        lineage = _read(lambda: _lineage(home / STATE_DB, session_id))
    except Exception:  # noqa: BLE001 - the agent starts whatever this finds
        if remote:
            return unnamed_member, False
        _store_unread(session_id)
        return None, False
    if not lineage:
        # No row: a session Hermes doesn't keep, never a room's (a room's row
        # is written when it's admitted, before its first turn).
        return (unnamed_member, False) if remote else (None, True)
    in_room = [hop for hop in lineage if hop[1] == ROOM_SOURCE or ROOM_SESSION_ID.fullmatch(hop[0])]
    if not in_room:
        return None, True
    room_id = _room_id(lineage[-1][2])
    if any(ROOM_SESSION_ID.fullmatch(hop[0]) for hop in lineage):
        return RoomTurn(room_id=room_id, side="member", with_others=True, session_id=session_id), True
    members = _room_members(room_id, home) if room_id else None
    return RoomTurn(room_id=room_id, side="home", with_others=_with_others(members), session_id=session_id), True


def _lineage(path: Path, session_id: str) -> list[tuple[str, str, str]]:
    """``(id, source, title)`` from the session back to the one it continues,
    through compactions only; empty when the session has no row."""
    if not path.exists():
        return []
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        hops: list[tuple[str, str, str]] = []
        current = session_id
        for _ in range(LINEAGE_HOPS):
            row = conn.execute("SELECT source, title, parent_session_id FROM sessions WHERE id = ?",
                               (current,)).fetchone()
            if row is None:
                break
            hops.append((current, str(row[0] or "").strip().lower(), str(row[1] or "")))
            parent = row[2]
            if not parent:
                break
            # Only a compaction carries the room on; a branch or a delegation
            # is a conversation of its own.
            ended = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
            if ended is None or ended[0] != "compression":
                break
            current = parent
        return hops
    finally:
        conn.close()


def _room_id(title: str) -> str:
    if not title.startswith(ROOM_TITLE):
        return ""
    room_id = title[len(ROOM_TITLE):].strip()
    # A legacy Desktop group's title carries its display name, not a room id.
    return room_id if _ROOM_ID.fullmatch(room_id) else ""


def _room_stores(home: Path) -> list[Path]:
    """Where a room can be: this profile's store, the root's, the legacy
    service's, then each profile's (a canonical room lives in its owner's)."""
    root = home.parent.parent if home.parent.name == "profiles" else home
    paths = [home / STATE_DB, root / STATE_DB, root / LEGACY_ROOMS_DB]
    profiles = root / "profiles"
    try:
        paths += sorted(profiles.glob(f"*/{STATE_DB}"))[:MAX_PROFILE_STORES]
    except OSError:
        pass
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _room_members(room_id: str, home: Path) -> Optional[list[Any]]:
    """The room's roster, None when no store has the room. A store that
    can't be read only matters when no other store has the room."""
    unreadable: list[Path] = []
    for path in _room_stores(home):
        try:
            members = _read(lambda: _members_in(path, room_id))
        except Exception:  # noqa: BLE001
            logger.debug("conduit_push: couldn't read room %s from %s", room_id, path, exc_info=True)
            unreadable.append(path)
            continue
        if members is not None:
            return members
    if unreadable:
        raise RoomStoreError(f"couldn't read room {room_id}'s members")
    return None


def _members_in(path: Path, room_id: str) -> Optional[list[Any]]:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        try:
            row = conn.execute("SELECT members_json FROM hosted_rooms WHERE room_id = ?", (room_id,)).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None
            raise
        if row is None:
            return None
        members = json.loads(row[0])
        if not isinstance(members, list):
            raise ValueError("the room's members aren't a list")
        return members
    finally:
        conn.close()


def _with_others(members: Optional[list[Any]]) -> bool:
    for member in members or ():
        target = member.get("target") if isinstance(member, dict) else None
        if isinstance(target, dict) and target.get("kind") == "peer":
            return True
    return False


def _read(read: Callable[[], _Read]) -> _Read:
    for attempt in range(READ_RETRIES + 1):
        try:
            return read()
        except sqlite3.Error as error:
            if attempt == READ_RETRIES or "disk i/o error" not in str(error).lower():
                raise
            time.sleep(READ_RETRY_PAUSE)
    raise AssertionError("unreachable")


def _store_unread(session_id: str) -> None:
    """Warns on the first failure and every 100th after: the session then
    counts as an ordinary chat, so if it is a room's its turns get no room
    rules, and an operator should see that."""
    global _unread_count
    _unread_count += 1
    if _unread_count % 100 == 1:
        logger.warning("conduit_push: couldn't read the session store to tell whether session %s is a group "
                       "room's (%d times so far); if it is, its turns run without the owner's room rules",
                       session_id, _unread_count, exc_info=True)
    else:
        logger.debug("conduit_push: couldn't tell whether session %s is a group room's", session_id, exc_info=True)


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes runtime here
        return None
