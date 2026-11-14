"""Your other Hermes' bots, for rooms of only your own Hermes (the multi-instance plan's Rooms 1).

Over a Link both sides marked as their owner's own (room_links.own_links), each side tells the other which bots
it has (room.bots): each profile's agent as a room names it (room_invites.own_agent) and whether it's set up to
join a room another Hermes hosts. The owner's Conduit lists them in New Group Chat beside their own
(``GET /rooms/own-bots``), so a room can be made with a bot from either Hermes the way it's made with your own.

A draft that names bots of the other Hermes (``bring``) sends, after its invite, a room.bring with their
profiles. The other Hermes joins them by itself, with no join card: only over an own Link, only for an invite
that names nobody else (``people`` empty, as room_rules.own_guest_room asks), and only profiles it has. One
that can't join (not set up for rooms, or on Codex) leaves the invite waiting on its join card, as before.

A room.bots goes again only when what it says changed, and with each link.hello (a side that restored an older
file hears it again). What each side last said and was told is kept in ``conduit-rooms/own_bots.json``, owner
only, beside rooms.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import room_bridge, room_identity, room_links

logger = logging.getLogger(__name__)

VERSION = 1
BOTS, BRING = "room.bots", "room.bring"
KINDS = (BOTS, BRING)
FILE_NAME = "own_bots.json"
# The most bots one Hermes lists: as many profiles as Conduit gives looks for (room_looks.MAX_AGENTS).
MAX_BOTS = 32
# How often this Hermes looks at its own bots to tell the other side of a change.
CHECK_S = 60
# A room.bring that came before its invite waits this long for it.
PENDING_S = 7 * 24 * 3600
_thread_lock = threading.Lock()


class OwnBots:
    """The bots exchanged over own Links, for ``rooms`` (room_invites.Rooms)."""

    def __init__(self, rooms: Any, *, clock: Any = time.time) -> None:
        self.rooms = rooms
        self.clock = clock
        self.store = Store(rooms.root)
        self._checked_at = 0.0

    # --- What this Hermes says ----------------------------------------------------------------

    def local(self) -> list[dict[str, Any]]:
        """This Hermes' bots as the other side lists them."""
        from . import room_invites  # noqa: PLC0415 - room_invites imports this module

        bots = []
        for profile in self.rooms._profiles()[:MAX_BOTS]:
            agent = room_invites.own_agent(self.rooms.root, profile)
            bots.append({**agent, "ready": self._ready(profile)})
        return bots

    def _ready(self, profile: str) -> bool:
        joiner = self.rooms.joiner
        if joiner is None:
            return False
        try:
            joiner.check(profile)
        except Exception:  # noqa: BLE001 - not set up, on Codex, or unknown: its join card says why
            return False
        return True

    def pass_once(self, force: bool = False) -> None:
        """Tells each own Link whose other side takes room.bots what this Hermes' bots are, when that changed
        since it last said (or ``force``), at most every CHECK_S."""
        now = self.clock()
        if not force and now - self._checked_at < CHECK_S:
            return
        self._checked_at = now
        own = room_links.own_links(self.rooms.root)
        if not own:
            return
        targets = [link_id for link_id in sorted(own) if BOTS in ((self.rooms.link(link_id) or {}).get("kinds") or ())]
        if not targets:
            return
        bots = self.local()
        body = {"v": VERSION, "bots": bots}
        digest = hashlib.sha256(json.dumps(bots, sort_keys=True).encode("utf-8")).hexdigest()[:32]
        said = self.store.read()["said"]
        for link_id in targets:
            hello = (self.rooms.link(link_id) or {}).get("plugin")
            if said.get(link_id) == [digest, hello]:
                continue
            try:
                self.rooms.send(link_id, BOTS, body, message_id=f"{BOTS}:{digest}:{int(now)}")
            except room_links.LinkError as exc:
                logger.info("Linked Rooms will tell Link %s this Hermes' bots later (%s)", link_id, exc.code)
                continue
            with self.store.update() as state:
                # Said again when the other side's plugin changes: it may have restored an older file.
                state["said"][link_id] = [digest, hello]

    # --- What the other side says -------------------------------------------------------------

    def on_bots(self, received: room_links.Received) -> None:
        """A room.bots from a Link: kept only from an own Link."""
        bots = _bots_from(received.body)
        if bots is None:
            logger.info("Linked Rooms dropped a room.bots from Link %s that isn't one", received.link_id)
            return
        if received.link_id not in room_links.own_links(self.rooms.root):
            logger.info("Linked Rooms dropped a room.bots from Link %s, which isn't your own Hermes",
                        received.link_id)
            return
        with self.store.update() as state:
            state["theirs"][received.link_id] = {"bots": bots, "at": int(self.clock())}

    def theirs(self) -> dict[str, Any]:
        """The bots of each own Link's other side, by Link: its name and the bots it last said."""
        own = room_links.own_links(self.rooms.root)
        kept = self.store.read()["theirs"]
        shown = {}
        for link_id in sorted(own):
            link = self.rooms.link(link_id)
            if link is None:
                continue
            told = kept.get(link_id) or {}
            shown[link_id] = {"name": link["name"], "bots": list(told.get("bots") or []),
                              "brings": BRING in (link.get("kinds") or ())}
        return {"links": shown}

    # --- Bringing bots of the other side ------------------------------------------------------

    def brings(self, link_id: str) -> bool:
        """Whether a draft may name bots of ``link_id``'s Hermes: an own Link whose other side takes room.bring."""
        return link_id in room_links.own_links(self.rooms.root) \
            and BRING in ((self.rooms.link(link_id) or {}).get("kinds") or ())

    def on_bring(self, received: room_links.Received) -> None:
        """A room.bring: the bots of this Hermes the room's home asks to join, over an own Link. Kept for an
        invite that hasn't come yet."""
        body = _bring_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.bring from Link %s that isn't one", received.link_id)
            return
        if received.link_id not in room_links.own_links(self.rooms.root):
            logger.info("Linked Rooms dropped a room.bring from Link %s, which isn't your own Hermes",
                        received.link_id)
            return
        key = f"{received.link_id}/{body['room_id']}"
        now = int(self.clock())
        with self.store.update() as state:
            state["pending"] = {name: kept for name, kept in state["pending"].items() if kept["until"] > now}
            state["pending"][key] = {"profiles": body["profiles"], "until": now + PENDING_S}
        self.join_pending(key)

    def join_pending(self, key: str) -> None:
        """Joins the bots a room.bring named for the invite ``key`` (``link/room``), once both are here: only
        an invite still waiting, from an own Link, that names nobody else. Each bring is tried once."""
        link_id, _, room_id = key.partition("/")
        pending = self.store.read()["pending"].get(key)
        if pending is None:
            return
        invite = self.rooms.store.read()["invites"].get(key)
        if invite is None:
            return  # its invite hasn't come yet
        with self.store.update() as state:
            state["pending"].pop(key, None)
        if invite["status"] != "waiting" or invite["people"] or link_id not in room_links.own_links(self.rooms.root):
            logger.info("Linked Rooms left room %s's invite on its join card: it isn't one of only your own "
                        "Hermes, or it was answered", room_id)
            return
        from . import room_invites  # noqa: PLC0415 - room_invites imports this module

        mine = set(self.rooms._profiles())
        agents = [room_invites.own_agent(self.rooms.root, profile) for profile in pending["profiles"]
                  if profile in mine][:len(invite["slots"])]
        if not agents:
            logger.info("Linked Rooms left room %s's invite on its join card: none of the bots it named are "
                        "profiles here", room_id)
            return
        try:
            self.rooms.answer(link_id, room_id, [{**agent, "choices": None} for agent in agents])
        except Exception as exc:  # noqa: BLE001 - its join card still offers it
            logger.info("Linked Rooms left room %s's invite on its join card: its bots couldn't join by "
                        "themselves (%s)", room_id, getattr(exc, "code", type(exc).__name__))


# --- The store ----------------------------------------------------------------------------


class Store:
    """``<root>/conduit-rooms/own_bots.json``: what this side last said to each own Link, what each told it,
    and the room.brings waiting for their invites."""

    def __init__(self, root: Any) -> None:
        self.directory = Path(root) / room_identity.DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError, RecursionError):
            logger.info("Linked Rooms couldn't read %s; starting it again", self.path)
            return _empty()
        if not isinstance(state, dict) or state.get("version") != VERSION or not all(
                isinstance(state.get(name), dict) for name in ("said", "theirs", "pending")):
            return _empty()
        return state

    @contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        lock_path = self.directory / f".{FILE_NAME}.lock"
        with _thread_lock, open(lock_path, "a+b") as handle, room_identity._exclusive(handle):
            state = self.read()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=f".{FILE_NAME}.", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)
        room_identity._sync_directory(self.directory)


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "said": {}, "theirs": {}, "pending": {}}


def _bots_from(body: Any) -> Optional[list[dict[str, Any]]]:
    """A room.bots' bots, or None when it isn't one."""
    from . import room_invites  # noqa: PLC0415

    if not isinstance(body, dict) or set(body) != {"v", "bots"} or type(body["v"]) is not int \
            or body["v"] != VERSION or not isinstance(body["bots"], list) or len(body["bots"]) > MAX_BOTS:
        return None
    bots = []
    for bot in body["bots"]:
        if not isinstance(bot, dict) or set(bot) != {"profile", "handle", "display_name", "ready"} \
                or not isinstance(bot["profile"], str) or not room_bridge.PROFILE.match(bot["profile"]) \
                or not room_invites._handle(bot["handle"]) \
                or room_invites._label(bot["display_name"]) != bot["display_name"] \
                or type(bot["ready"]) is not bool:
            return None
        bots.append(dict(bot))
    if len({bot["profile"] for bot in bots}) != len(bots):
        return None
    return bots


def _bring_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.bring's room and profiles, or None when it isn't one."""
    from . import room_invites  # noqa: PLC0415

    if not isinstance(body, dict) or set(body) != {"v", "room_id", "profiles"} or type(body["v"]) is not int \
            or body["v"] != VERSION or not room_invites._identifier(body["room_id"]):
        return None
    profiles = body["profiles"]
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= room_invites.MAX_AGENTS - 1 \
            or not all(isinstance(profile, str) and room_bridge.PROFILE.match(profile) for profile in profiles) \
            or len(set(profiles)) != len(profiles):
        return None
    return {"room_id": body["room_id"], "profiles": list(profiles)}
