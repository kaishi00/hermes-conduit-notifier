"""How the agents and people of a room over Links look, for Linked Rooms (designs/cross-owner-agents, N8).

Each person's Conduit draws their own bots with their picture and the glow color Bots Home gives them, and
draws the person with the picture they chose for rooms. None of that lives where another Hermes can read it,
so each Conduit gives its own Hermes how its bots and its person look (``PUT /rooms/looks``): a small picture
and a color for each profile, and for the person. In each started room over Links, each installation sends
the others how its own agents in the room and its person look (room.looks), and the room's home passes on to
each person how everyone else in the room looks, so every Conduit draws every agent and person as their
owner's does.

A room.looks goes again only when what it says changed. What came over each Link is kept per room, owner only,
in ``conduit-rooms/looks.json`` beside rooms.json, with what this installation's Conduit gave it, and a room
nobody's Hermes keeps any more is forgotten. A look is only a picture (a JPEG or PNG of at most
MAX_PICTURE_BYTES) and a color (``#RRGGBB``); one from another Hermes is taken only for that Hermes' own agents
and person, or, from a room's home, for the room's members and people.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import room_identity, room_links, room_posts

logger = logging.getLogger(__name__)

VERSION = 1
LOOKS = "room.looks"
KINDS = (LOOKS,)
FILE_NAME = "looks.json"
# A picture as Conduit makes one (96 pt square, JPEG), with room to spare: a room's whole set, every agent and
# person, still fits one envelope.
MAX_PICTURE_BYTES = 8 * 1024
# The most profiles one Conduit gives its Hermes looks for.
MAX_AGENTS = 32
# A room.looks' body: as a room page's (room_copies.PAGE_BYTES), so it fits an envelope sealed.
BODY_BYTES = 160_000
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}\Z")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_JPEG, _PNG = b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"
_thread_lock = threading.Lock()


class LooksError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --- A look --------------------------------------------------------------------------------


def look_from(value: Any) -> Optional[dict[str, Any]]:
    """A well-formed look (``{picture, color}``, each null when there is none), or None for anything else. An
    empty one (no picture, no color) is None too: there's nothing to draw."""
    if not isinstance(value, dict) or not set(value) <= {"picture", "color"}:
        return None
    picture, color = value.get("picture"), value.get("color")
    if picture is not None and not _picture(picture):
        return None
    if color is not None and not (isinstance(color, str) and _COLOR.match(color)):
        return None
    if picture is None and color is None:
        return None
    return {"picture": picture, "color": color.upper() if color else None}


def _picture(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > (MAX_PICTURE_BYTES * 4 // 3) + 4:
        return False
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return 0 < len(data) <= MAX_PICTURE_BYTES and (data.startswith(_JPEG) or data.startswith(_PNG))


# --- What this installation's Conduit gave it ------------------------------------------------------------


def own(root: Any) -> dict[str, Any]:
    """How this installation's agents and person look, as its Conduit last gave them."""
    return Store(root).read()["own"]


def put_own(root: Any, body: Any) -> dict[str, Any]:
    """Keeps how this installation's agents and person look: ``agents``, profile to look, and ``person``, a look
    or null. It takes the place of what was there; a profile or person without one is drawn as before."""
    if not isinstance(body, dict) or not set(body) <= {"agents", "person"}:
        raise LooksError("bad_looks", "Send {agents: {profile: {picture, color}}, person: {picture, color}}")
    agents = body.get("agents") or {}
    if not isinstance(agents, dict) or len(agents) > MAX_AGENTS:
        raise LooksError("bad_looks", f"Send looks for at most {MAX_AGENTS} profiles")
    kept: dict[str, Any] = {}
    for profile, value in agents.items():
        if not isinstance(profile, str) or not _PROFILE.match(profile):
            raise LooksError("bad_looks", "A profile's name isn't one Hermes gives")
        if value is None:
            continue
        look = look_from(value)
        if look is None:
            raise LooksError("bad_looks", f"{profile}'s look isn't a picture of at most {MAX_PICTURE_BYTES} "
                                          "bytes (JPEG or PNG) and a #RRGGBB color")
        kept[profile] = look
    person = body.get("person")
    if person is not None:
        person = look_from(person)
        if person is None:
            raise LooksError("bad_looks", f"Your look isn't a picture of at most {MAX_PICTURE_BYTES} bytes (JPEG or "
                                          "PNG) and a #RRGGBB color")
    with Store(root).update() as state:
        state["own"] = {"agents": kept, "person": person}
    return {"agents": sorted(kept), "person": person is not None}


# --- In rooms ----------------------------------------------------------------------------------------


class Looks:
    """The looks of the rooms over Links of ``rooms`` (room_invites.Rooms): what its loop sends and its Links
    bring."""

    def __init__(self, rooms: Any) -> None:
        self.rooms = rooms
        self.store = Store(rooms.root)

    def send_looks(self) -> None:
        """What the rooms loop does each pass: to each Link in a started room, how the room looks from here,
        when that changed since it last went."""
        state = self.rooms.store.read()
        try:
            looks = self.store.read()
        except LooksError as exc:
            logger.info("Linked Rooms sends no looks this pass (%s)", exc.code)
            return
        wanted: dict[tuple[str, str], dict[str, Any]] = {}
        for room_id, draft in sorted(state["drafts"].items()):
            for link_id in _receiving(draft):
                wanted[(room_id, link_id)] = _from_home(room_id, draft, link_id, looks)
        for invite in state["invites"].values():
            if invite["status"] == "started" and invite["joined"] and invite["members"] is not None:
                wanted[(invite["room_id"], invite["link"])] = _from_member(invite, looks)
        sent = looks["sent"]
        for (room_id, link_id), body in sorted(wanted.items()):
            body = _fitted(body)
            digest = _digest(body)
            before = sent.get(room_id, {}).get(link_id)
            if before == digest:
                continue
            # The one before it in the id too: looks that change back go again.
            message_id = f"{LOOKS}:" + hashlib.sha256(json.dumps([room_id, before, digest]).encode()).hexdigest()[:32]
            try:
                self.rooms.send(link_id, LOOKS, body, message_id=message_id)
            except room_links.LinkError as exc:
                logger.info("Linked Rooms will send room %s's looks over Link %s later (%s)", room_id, link_id,
                            exc.code)
                continue
            with self.store.update() as kept:
                kept["sent"].setdefault(room_id, {})[link_id] = digest
        rooms = set(state["drafts"]) | {invite["room_id"] for invite in state["invites"].values()}
        if not set(looks["rooms"]) <= rooms or not set(looks["sent"]) <= rooms:
            with self.store.update() as kept:
                kept["rooms"] = {room_id: room for room_id, room in kept["rooms"].items() if room_id in rooms}
                kept["sent"] = {room_id: links for room_id, links in kept["sent"].items() if room_id in rooms}

    def on_looks(self, received: room_links.Received) -> None:
        """A room.looks from a Link: the home takes how that person's agents and they look; a member's Hermes,
        how the room looks from its home."""
        body = _looks_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.looks from Link %s that isn't one", received.link_id)
            return
        room_id = body["room_id"]
        state = self.rooms.store.read()
        draft = state["drafts"].get(room_id)
        if draft is not None and draft.get("members") and received.link_id in draft["links"]:
            # The home: only the sender's own agents and person.
            theirs = {member["member_id"] for member in draft["members"] if member["link"] == received.link_id}
            kept = {"members": {member_id: look for member_id, look in body["members"].items()
                                if member_id in theirs}, "people": [], "person": body["person"]}
        else:
            invite = room_posts.current_invite([invite for invite in state["invites"].values()
                                                if invite["link"] == received.link_id
                                                and invite["status"] in ("started", "left", "ended")], room_id)
            if invite is None:
                logger.info("Linked Rooms dropped room %s's looks from Link %s: it isn't in that room", room_id,
                            received.link_id)
                return
            mine = {agent["member_id"] for agent in invite["joined"]} \
                | {agent["member_id"] for agent in invite.get("gone", [])}
            seated = {member["member_id"] for member in invite["members"]} - mine
            kept = {"members": {member_id: look for member_id, look in body["members"].items()
                                if member_id in seated}, "people": body["people"], "person": body["person"]}
        with self.store.update() as stored:
            stored["rooms"].setdefault(room_id, {})[received.link_id] = kept


def shown(root: Any, room_id: Any) -> dict[str, Any]:
    """How a room over Links looks from here, for Conduit: ``members``, member id to look, for the agents of
    other installations; ``people``, each other person but the home's, as ``{name, poster, look}`` (``name`` as
    your Link names them, ``poster`` the name their posts carry); and ``home``, the home's person's look when it
    isn't you. Empty for a room this Hermes has no looks for."""
    from . import room_invites

    if not isinstance(room_id, str):
        raise LooksError("no_room", "There's no room over Links with that id here", 404)
    state = room_invites.Store(root).read()
    looks = Store(root).read()["rooms"].get(room_id, {})
    draft = state["drafts"].get(room_id)
    members: dict[str, Any] = {}
    people: list[dict[str, Any]] = []
    home = None
    if draft is not None:
        for link_id, person in sorted(draft["links"].items()):
            kept = looks.get(link_id)
            if kept is None:
                continue
            members.update(kept["members"])
            if kept["person"] is not None:
                people.append({"name": person["name"], "poster": room_posts.poster(person["name"]),
                               "look": kept["person"]})
    else:
        invite = room_posts.current_invite([invite for invite in state["invites"].values()
                                            if invite["status"] in ("started", "left", "ended")], room_id)
        kept = looks.get(invite["link"]) if invite is not None else None
        if kept is not None:
            members, people, home = kept["members"], kept["people"], kept["person"]
    return {"room_id": room_id, "members": members, "people": people, "home": home}


def _receiving(draft: dict[str, Any]) -> list[str]:
    """The Links of a started room's people with an agent still in it."""
    if draft.get("status") != "started" or not draft.get("members"):
        return []
    return sorted({member["link"] for member in draft["members"]
                   if member["link"] is not None and member.get("status", "in") == "in"})


def _from_home(room_id: str, draft: dict[str, Any], link_id: str, looks: dict[str, Any]) -> dict[str, Any]:
    """How a room this installation hosts looks to the person of ``link_id``: the home's agents and person as its
    Conduit gave them, and everyone else's as their Links brought them."""
    received = looks["rooms"].get(room_id, {})
    members: dict[str, Any] = {}
    for member in draft["members"]:
        if member.get("status", "in") != "in" or member["link"] == link_id:
            continue
        if member["link"] is None:
            look = looks["own"]["agents"].get(member["profile"])
        else:
            look = received.get(member["link"], {}).get("members", {}).get(member["member_id"])
        if look is not None:
            members[member["member_id"]] = look
    people = []
    for other in _receiving(draft):
        person = received.get(other, {}).get("person")
        if other != link_id and person is not None:
            name = draft["links"][other]["name"]
            people.append({"name": name, "poster": room_posts.poster(name), "look": person})
    return {"v": VERSION, "room_id": room_id, "members": members, "people": people,
            "person": looks["own"]["person"]}


def _from_member(invite: dict[str, Any], looks: dict[str, Any]) -> dict[str, Any]:
    """How this installation's agents in a room elsewhere and its person look, for the room's home."""
    agents = looks["own"]["agents"]
    return {"v": VERSION, "room_id": invite["room_id"],
            "members": {agent["member_id"]: agents[agent["profile"]] for agent in invite["joined"]
                        if agent["profile"] in agents},
            "people": [], "person": looks["own"]["person"]}


def _fitted(body: dict[str, Any]) -> dict[str, Any]:
    """The body, its people's pictures and then its members' left out until it fits an envelope (colors stay)."""
    def size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    if size(body) <= BODY_BYTES:
        return body
    body = json.loads(json.dumps(body))
    for person in body["people"]:
        person["look"] = {**person["look"], "picture": None}
    for member_id in sorted(body["members"]):
        if size(body) <= BODY_BYTES:
            break
        body["members"][member_id] = {**body["members"][member_id], "picture": None}
    body["people"] = [person for person in body["people"] if person["look"]["color"] is not None]
    body["members"] = {member_id: look for member_id, look in body["members"].items()
                       if look["picture"] is not None or look["color"] is not None}
    return body


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


_BODY_FIELDS = frozenset({"v", "room_id", "members", "people", "person"})


def _looks_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.looks' body with its looks checked, or None when it isn't one. A look that isn't well formed is
    left out, never the rest."""
    if not isinstance(body, dict) or set(body) != _BODY_FIELDS or type(body["v"]) is not int \
            or body["v"] != VERSION or not isinstance(body["room_id"], str) or not body["room_id"] \
            or len(body["room_id"]) > 200 or not isinstance(body["members"], dict) \
            or not isinstance(body["people"], list) or len(body["people"]) > 8:
        return None
    members = {}
    for member_id, value in body["members"].items():
        look = look_from(value)
        if isinstance(member_id, str) and 0 < len(member_id) <= 200 and look is not None:
            members[member_id] = look
    people = []
    for person in body["people"]:
        if not isinstance(person, dict) or set(person) != {"name", "poster", "look"}:
            continue
        look = look_from(person["look"])
        if not isinstance(person["name"], str):
            continue
        name = room_links._checked_name(person["name"])
        if look is not None and name is not None and name == person["name"] \
                and person["poster"] == room_posts.poster(name):
            people.append({"name": name, "poster": person["poster"], "look": look})
    person = look_from(body["person"]) if body["person"] is not None else None
    return {"room_id": body["room_id"], "members": members, "people": people, "person": person}


# --- The store ----------------------------------------------------------------------------------------


class Store:
    """``<root>/conduit-rooms/looks.json``: ``own``, what this installation's Conduit gave it; ``rooms``, room id to
    Link to what that Link brought; and ``sent``, room id to Link to the digest of the room.looks that went."""

    def __init__(self, root: Any) -> None:
        self.directory = Path(root) / room_identity.DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise LooksError("unreadable", f"Couldn't read {self.path} ({exc.strerror or exc})", 500) from None
        try:
            state = json.loads(raw)
        except (ValueError, RecursionError):
            state = None
        if not _well_formed(state):
            # Only looks: drawn as before until they come again.
            logger.warning("Linked Rooms couldn't read %s; rooms draw without other people's looks until they "
                           "come again", self.path)
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
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=f".{FILE_NAME}.", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            room_identity._sync_directory(self.directory)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "own": {"agents": {}, "person": None}, "rooms": {}, "sent": {}}


def _is_look(value: Any, empty: bool = False) -> bool:
    """A look as this file keeps it (None where `empty`), so a pass never meets one that isn't."""
    return (empty and value is None) or (value is not None and look_from(value) == value)


def _well_formed(state: Any) -> bool:
    return isinstance(state, dict) and state.get("version") == VERSION \
        and isinstance(state.get("own"), dict) and isinstance(state["own"].get("agents"), dict) \
        and all(_is_look(look) for look in state["own"]["agents"].values()) \
        and _is_look(state["own"].get("person"), empty=True) and isinstance(state.get("rooms"), dict) \
        and all(isinstance(room, dict) and all(
            isinstance(kept, dict) and isinstance(kept.get("members"), dict)
            and all(_is_look(look) for look in kept["members"].values())
            and isinstance(kept.get("people"), list)
            and all(isinstance(person, dict) and _is_look(person.get("look")) for person in kept["people"])
            and _is_look(kept.get("person"), empty=True) for kept in room.values())
            for room in state["rooms"].values()) \
        and isinstance(state.get("sent"), dict) and all(isinstance(links, dict) for links in state["sent"].values())
