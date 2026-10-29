"""Rooms with other people's agents, over Links, for Linked Rooms (designs/cross-owner-agents, N5a and N5b).

A room whose agents live on more than one Hermes starts as a draft on its home, the Hermes that will host it:
its name, the creator's own agents, and the people invited by Link (room_links.py). Each person's Hermes is sent
a room.invite and shows it (the join card); its answer, a room.answer, brings the agents they chose, each with a
grant their own Hermes minted for its place in the room (room_join.py), or none, to decline. Once everyone has
answered, or the creator starts it anyway, the home creates the room with every member in its running room
service and puts each linked agent's route at the bridge (room_start.py), then tells each person, with a
room.start, that it started with their agents, or that it didn't and why.

While the room goes on, each person's Hermes mints its agents a fresh grant every 12 hours and sends it in a
room.grant, which the home puts in the agent's route, so no grant runs out mid-room. A person takes their agents
out with a room.leave, and the home takes one out, or ends the room when Hermes disbands it (room_cleanup.py), with
a room.ended: an agent that's out is cut off at the home and undone on its own Hermes.

Both sides keep what they need in ``conduit-rooms/rooms.json`` beside links.json, owner only, under a file lock:
the home's drafts, the invites this installation was sent, and the envelopes still to go, each with a message id
of its own, so one sent twice reaches the other side once. Grants travel only inside room.answer and room.grant
envelopes and this file, and are never logged or shown.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import (client, room_bridge, room_choices, room_copies, room_identity, room_join, room_links, room_posts,
               room_start, session_titles)
from .events import event_id, push_event

logger = logging.getLogger(__name__)

VERSION = 1
INVITE, ANSWER, START = "room.invite", "room.answer", "room.start"
GRANT, LEAVE, ENDED = "room.grant", "room.leave", "room.ended"
KINDS = (INVITE, ANSWER, START, GRANT, LEAVE, ENDED, *room_copies.KINDS, *room_posts.KINDS)
DIRECTORY = room_identity.DIRECTORY
FILE_NAME = "rooms.json"

# A draft and its invites last as long as the mailbox holds an envelope.
LASTS_S = room_links.HOLD_S
# Hermes' room limit (hosted_room_discussion.MAX_DISCUSSION_MEMBERS), every owner's agents together.
MAX_AGENTS = 6
MAX_LINKS = 5
MAX_DRAFTS = 20
MAX_INVITES = 50
# Hermes' MAX_ROOM_NAME_CHARS and MAX_ACTOR_LABEL_CHARS.
NAME_MAX = 200
LABEL_MAX = 200
PERSON_MAX = room_links.NAME_MAX
# An ended draft or invite is shown this long, then forgotten.
KEPT_S = 30 * 86400
# How often the rooms loop looks at rooms.json while nothing is waiting.
IDLE_S = 2
# A start that couldn't create the room is tried this many times, then the draft ends (start_failed).
START_TRIES = 3
# The longest the rooms loop waits after a pass that failed.
MAX_BACKOFF_S = 300
# A joined agent gets a fresh grant this often: a grant starts turns for a day (room_join.DISPATCH_S).
RENEW_S = 12 * 3600
# A renewal its API server didn't answer is tried again this soon, and that profile's others wait as long.
RETRY_S = 60
# What a renewal that couldn't be made waits for, by why (R3): its API server's answer, a policy change undone, a
# Hermes updated. Its API server refusing the grant, or the grant past its last day, stops its renewals.
_RENEW_LATER = {"api_server": (RETRY_S, "trying again in a minute"),
                "policy_changed": (RENEW_S, "trying again in 12 hours"),
                "unsupported": (RENEW_S, "trying again in 12 hours")}
# Why the home ends a room on a member's Hermes: one of its agents taken out, or the room disbanded.
ENDED_REASONS = frozenset({"removed", "disbanded"})
# A change to a member's route the rooms loop makes (R15): its fresh grant, cut off, or its grant revoked and cut
# off. One waiting never gives way to one that does less.
_TODO_RANK = {"swap": 0, "cut_off": 1, "revoke": 2}

# Hermes' mention handles: a room identifier, and never @all or @everyone; nor @user, the room's person in
# Conduit (AppState.createGroupRoom).
HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
RESERVED = frozenset({"all", "everyone", "user"})
# The tags Conduit gives a bot (BotMentions.mentionTag), which never take these.
TAG_RESERVED = RESERVED | {"default", "hermes"}
# A reason's code.
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

_thread_lock = threading.Lock()
_answer_lock = threading.Lock()
_start_lock = threading.Lock()


class RoomsError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def slots(link_id: str, count: int) -> list[str]:
    """The member ids a Link's agents take in a room this Hermes hosts: never one of its profiles' (they start
    with ``l`` and a hex digest), and never another Link's."""
    return [f"{peer_id(link_id)}-{n}" for n in range(1, count + 1)]


def peer_id(link_id: str) -> str:
    """The peer a Link's agents are in a room this Hermes hosts (their target's ``peer_id``, which Hermes logs
    as their messages' connection): the slots' prefix, since a Link's id isn't one of Hermes' identifiers."""
    return f"l{hashlib.sha256(link_id.encode('utf-8')).hexdigest()[:12]}"


def people_key(link_ids: Any = (), names: Any = ()) -> list[str]:
    """Who else is in a room, as the room choices remember it (decision 9): the Links, and the other people
    by name."""
    return sorted({*(f"link:{link_id}" for link_id in link_ids), *(f"name:{name.casefold()}" for name in names)})


class Rooms:
    """The rooms over Links of the installation at ``root``: ``send`` is Links.send, ``link(link_id)`` the Link
    as Links.get shows it, ``profile_home(profile)`` a profile's home, ``joiner`` the member's Hermes calls
    (room_join.Joiner), ``starter`` the home's (room_start.Starter), ``notify(invite)`` what tells the phone
    about a new invite (notify_invite), ``copier`` Hermes' calls for the rooms' copies (room_copies.Hermes) and
    ``poster`` those for posts in them (room_posts.Hermes)."""

    def __init__(self, root: Any, *, send: Optional[Callable[..., Any]] = None,
                 link: Optional[Callable[[str], Optional[dict]]] = None, clock: Callable[[], float] = time.time,
                 profile_home: Optional[Callable[[str], Path]] = None, joiner: Any = None,
                 starter: Any = None, notify: Optional[Callable[[dict], None]] = None, copier: Any = None,
                 poster: Any = None) -> None:
        self.root = Path(root)
        self.store = Store(self.root)
        links = None if send is not None and link is not None else room_links.Links(self.root)
        self.send = send if send is not None else links.send
        self.link = link if link is not None else links.get
        self.clock = clock
        self.profile_home = profile_home if profile_home is not None else self._profile_home
        self.joiner = joiner
        self.starter = starter
        self.notify = notify if notify is not None else notify_invite
        self._told: set[str] = set()
        # Until when a profile's renewals wait for its API server, which didn't answer (R16).
        self._api_waits: dict[str, int] = {}
        self.copies = room_copies.Copies(self, hermes=copier)
        self.posts = room_posts.Posts(self, hermes=poster)
        self._notes: Any = None

    # --- The home's drafts ---------------------------------------------------------

    def draft(self, name: Any, agents: Any, links: Any, *, where: Any) -> dict[str, Any]:
        """A new room with the people of ``links``, waiting for their answers: ``agents`` are this
        installation's own, each ``{profile, handle, display_name}`` with its ``choices`` for the room if they
        aren't the last room's with the same people, and ``where`` the room service it starts in
        (room_start.where_from)."""
        name = _room_name(name)
        where = _where(where)
        if not isinstance(links, list) or not 1 <= len(links) <= MAX_LINKS \
                or len(set(map(str, links))) != len(links):
            raise RoomsError("bad_links", f"Invite 1 to {MAX_LINKS} people, each once")
        own = self._agents(agents, minimum=1)
        try:
            for agent in own:
                self.joiner.check(agent["profile"], joining=False)
        except room_join.JoinError as exc:
            raise RoomsError(exc.code, str(exc), exc.status) from None
        each = (MAX_AGENTS - len(own)) // len(links)
        if each < 1:
            raise RoomsError("too_many", f"A room has up to {MAX_AGENTS} agents: with {len(own)} of yours, invite "
                                         f"at most {MAX_AGENTS - len(own)} people", 409)
        names = {}
        for link_id in links:
            shown = self.link(link_id) if room_links._is_id(link_id) else None
            if shown is None or shown.get("status") != "linked":
                raise RoomsError("not_linked", "This Hermes isn't linked with one of them (yet)", 404)
            names[link_id] = shown["name"]
        supported = self.starter.supported() if self.starter is not None else "no starter"
        if supported is not None:
            raise RoomsError("unsupported", "This Hermes can't host rooms with linked people; update Hermes", 501)
        installation = self.starter.installation()
        now = int(self.clock())
        room_id = f"conduit-{uuid.uuid4()}"
        people = people_key(links)
        for agent in own:
            self._choose(agent["profile"], room_id, agent.pop("choices", None), people)
        with self.store.update() as state:
            if sum(1 for draft in state["drafts"].values() if draft["status"] == "waiting") >= MAX_DRAFTS:
                raise RoomsError("too_many_drafts", f"{MAX_DRAFTS} rooms are already waiting for answers", 409)
            state["drafts"][room_id] = {
                "name": name, "created_at": now, "expires_at": now + LASTS_S, "status": "waiting",
                "start_now": False, "where": where, "home_install_id": installation, "own": own,
                "links": {link_id: {"name": names[link_id], "slots": slots(link_id, each), "status": "waiting",
                                    "join": [], "answered_at": None} for link_id in links},
                "members": None, "routes": {}, "started_at": None, "ended_at": None, "reason": None,
                "attempts": 0,
            }
            for link_id in links:
                body = {"v": VERSION, "room_id": room_id, "name": name, "home_install_id": installation,
                        "authority_gateway_id": installation, "authority_epoch": 1,
                        "people": [names[other] for other in links if other != link_id],
                        "agents": [{"handle": agent["handle"], "display_name": agent["display_name"]}
                                   for agent in own],
                        "slots": slots(link_id, each), "expires_at": now + LASTS_S}
                _queue(state, link_id, INVITE, room_id, body, now)
        self.flush()
        return self._shown_draft(room_id, self.store.read()["drafts"][room_id])

    def drafts(self) -> dict[str, Any]:
        """The drafts, newest first, with no grant, and whether this Hermes can start them: ``starts`` says
        why not (``unsupported`` on a Hermes without a room service the plugin can use)."""
        state = self.store.read()
        shown = [self._shown_draft(room_id, draft) for room_id, draft in sorted(
            state["drafts"].items(), key=lambda item: (-item[1]["created_at"], item[0]))]
        supported = self.starter.supported() if self.starter is not None else "no starter"
        return {"drafts": shown, "starts": {"available": supported is None,
                                            "reason": None if supported is None else "unsupported"}}

    def start(self, room_id: Any) -> dict[str, Any]:
        """Start now: the room starts with whoever has joined on the rooms loop's next pass, and the rest are
        told it started without them."""
        with self.store.update() as state:
            draft = state["drafts"].get(room_id) if isinstance(room_id, str) else None
            if draft is None:
                raise RoomsError("no_draft", "There's no room with that id waiting here", 404)
            if draft["status"] != "waiting" or int(self.clock()) >= draft["expires_at"]:
                raise RoomsError("not_waiting", "That room isn't waiting to start", 409)
            draft["start_now"] = True
        return self._shown_draft(room_id, self.store.read()["drafts"][room_id])

    def withdraw(self, room_id: Any) -> dict[str, Any]:
        """Takes back a draft that's waiting: everyone invited who hasn't declined is told, and their Hermes
        undoes their joins (R14)."""
        with self.store.update() as state:
            draft = state["drafts"].get(room_id) if isinstance(room_id, str) else None
            if draft is None:
                raise RoomsError("no_draft", "There's no room with that id waiting here", 404)
            if draft["status"] != "waiting" or int(self.clock()) >= draft["expires_at"]:
                raise RoomsError("not_waiting", "That room isn't waiting to start", 409)
            self._end(state, room_id, "withdrawn", int(self.clock()))
        self.flush()
        return self._shown_draft(room_id, self.store.read()["drafts"][room_id])

    def remove(self, room_id: Any, member: Any) -> dict[str, Any]:
        """Takes a linked person's agent out of the started room ``room_id`` (R7): ``member`` is its member id, or
        its handle with or without @. On the rooms loop's next pass its grant is revoked on its Hermes through the
        bridge, where that answers, and its route cut off; its Hermes is told (room.ended), which undoes its join.
        The same agent again is the first. Returns the draft as GET /rooms/drafts shows it."""
        with self.store.update() as state:
            draft = state["drafts"].get(room_id) if isinstance(room_id, str) else None
            if draft is None:
                raise RoomsError("no_draft", "There's no room with that id here", 404)
            if draft["status"] == "ended":
                raise RoomsError("ended", "That room has ended", 409)
            if draft["status"] != "started":
                raise RoomsError("not_started", "That room hasn't started yet", 409)
            found = _member(draft, member)
            if found is None:
                raise RoomsError("no_member", "There's no agent with that handle or id in that room", 404)
            if found["link"] is None:
                raise RoomsError("own_agent", "That's one of your own agents: Hermes can't take one out of a room, "
                                              "so disband the room to end it", 409)
            if found.get("status", "in") == "left":
                raise RoomsError("not_in", "That agent left the room", 409)
            if found.get("status", "in") == "in":
                member_id, now = found["member_id"], int(self.clock())
                found["status"] = "removed"
                _todo(draft, member_id, "revoke")
                _queue(state, found["link"], ENDED, room_id, {"v": VERSION, "room_id": room_id, "reason": "removed",
                                                              "member_ids": [member_id]}, now,
                       parts=("removed", member_id))
        self.flush()
        return self._shown_draft(room_id, self.store.read()["drafts"][room_id])

    def disbanded(self, room_id: Any) -> None:
        """Hermes disbanded the room ``room_id`` here (room_cleanup's guard, R8): a room over Links that started
        here ends, and each person with agents still in it is told (room.ended), whose Hermes undoes their joins.
        Hermes' disband revoked the routes itself. What's queued goes on the rooms loop's next pass."""
        if not isinstance(room_id, str) or not self.store.path.exists():
            return
        with self.store.update() as state:
            draft = state["drafts"].get(room_id)
            if draft is None or draft["status"] not in ("starting", "started"):
                return
            now = int(self.clock())
            for link_id, person in draft["links"].items():
                if draft["status"] == "started":
                    ids = sorted(member["member_id"] for member in draft["members"]
                                 if member["link"] == link_id and member.get("status", "in") == "in")
                else:
                    ids = sorted(agent["member_id"] for agent in person["join"]) if person["status"] == "joined" \
                        else []
                if ids:
                    _queue(state, link_id, ENDED, room_id, {"v": VERSION, "room_id": room_id, "reason": "disbanded",
                                                            "member_ids": ids}, now, parts=("disbanded", *ids))
            draft.update(status="ended", reason="disbanded", ended_at=now)
            draft.pop("route_todo", None)

    # --- Invites to this installation ----------------------------------------------------

    def invites(self) -> list[dict[str, Any]]:
        """The rooms this installation's people are invited to, newest first: the join card, with each of its
        profiles' choices for the room filled in from its last room with the same people."""
        state = self.store.read()
        return [self._shown_invite(invite) for _, invite in sorted(
            state["invites"].items(), key=lambda item: (-item[1]["received_at"], item[0]))]

    def answer(self, link_id: Any, room_id: Any, agents: Any) -> dict[str, Any]:
        """Joins the room ``room_id`` the Link ``link_id`` invited this installation to with ``agents``, each
        ``{profile, handle, display_name}`` with its ``choices`` for the room if they aren't the join card's, or
        declines it with none. The same answer again is the first; another is refused (``answered``). A join
        that the home's word on the room or the invite's end crosses is undone (``ended``, ``lapsed``). Returns
        the invite as the join card shows it."""
        key = f"{link_id}/{room_id}"
        with self._answering():
            invite = self.store.read()["invites"].get(key) if isinstance(link_id, str) and isinstance(
                room_id, str) else None
            if invite is None:
                raise RoomsError("no_invite", "This Hermes wasn't invited to that room over that Link", 404)
            agents = self._agents(agents, minimum=0)
            if invite["status"] in ("joined", "declined"):
                if [[agent[name] for name in ("profile", "handle", "display_name")] for agent in agents] == [
                        [agent[name] for name in ("profile", "handle", "display_name")] for agent in invite["joined"]]:
                    return self._shown_invite(invite)
                raise RoomsError("answered", "This invite has been answered", 409)
            ended = _ended(invite, int(self.clock()))
            if ended is not None:
                raise ended
            shown = self.link(link_id)
            if shown is None or shown.get("status") != "linked":
                raise RoomsError("not_linked", "This Hermes isn't linked with them any more", 404)
            if len(agents) > len(invite["slots"]):
                raise RoomsError("too_many", f"This room has places for {len(invite['slots'])} of your agents", 409)
            try:
                for agent in agents:
                    self.joiner.check(agent["profile"])
            except room_join.JoinError as exc:
                raise RoomsError(exc.code, str(exc), exc.status) from None
            people = people_key([link_id], invite["people"])
            for agent in agents:
                self._choose(agent["profile"], room_id, agent.pop("choices"), people)
            joined: list[dict[str, Any]] = []
            try:
                for agent, member_id in zip(agents, invite["slots"]):
                    joined.append({**agent, "member_id": member_id, **self.joiner.join(
                        link_id, room_id=room_id, home_install_id=invite["home_install_id"],
                        authority_gateway_id=invite["authority_gateway_id"],
                        authority_epoch=invite["authority_epoch"], member_id=member_id, profile=agent["profile"])})
            except BaseException as exc:
                for done in joined:
                    self.joiner.leave(link_id, done["grant"])
                if isinstance(exc, room_join.JoinError):
                    raise RoomsError(exc.code, str(exc), exc.status) from None
                raise
            with self.store.update() as state:
                invite, now = state["invites"].get(key), int(self.clock())
                # The home's word on the room can come in while the agents join, or the invite run out.
                ended = _ended(invite, now)
                if ended is None:
                    invite["status"] = "joined" if joined else "declined"
                    invite["ended_at"] = None if joined else now
                    invite["joined"] = [{**{name: agent[name] for name in (
                        "member_id", "profile", "handle", "display_name", "grant", "status_expires_at")},
                        "renew_at": now + RENEW_S} for agent in joined]
                    body = {"v": VERSION, "room_id": room_id, "join": [{name: agent[name] for name in (
                        "member_id", "profile", "handle", "display_name", "grant", "catalog")} for agent in joined]}
                    _queue(state, link_id, ANSWER, room_id, body, now)
            if ended is not None:
                for done in joined:
                    self._leave(link_id, done)
                raise ended
        self.flush()
        return self._shown_invite(self.store.read()["invites"][key])

    def leave(self, link_id: Any, room_id: Any, profiles: Any = None) -> dict[str, Any]:
        """Takes this installation's agents out of the started room ``room_id`` the Link ``link_id`` invited it to
        (R6): those of ``profiles``, or every one still in (None). Each is undone here (unbound, its grant's scope
        revoked) and the home is told (room.leave), which cuts off its route. Their hidden chats for the room stay
        (rooms forget deletes one). A room that hasn't started is declined, not left (R21). Returns the invite as
        the join card shows it."""
        key = f"{link_id}/{room_id}"
        with self._answering():
            invite = self.store.read()["invites"].get(key) if isinstance(link_id, str) and isinstance(
                room_id, str) else None
            if invite is None:
                raise RoomsError("no_invite", "This Hermes wasn't invited to that room over that Link", 404)
            if profiles is not None and (not isinstance(profiles, list) or not profiles or not all(
                    isinstance(profile, str) for profile in profiles) or len(set(profiles)) != len(profiles)):
                raise RoomsError("bad_agents", "Name each agent to take out once, by its profile")
            status = _invite_status(invite, int(self.clock()))
            if status == "waiting":
                raise RoomsError("not_started", "That room hasn't started yet: decline it instead", 409)
            if status == "joined":
                raise RoomsError("not_started", "That room hasn't started yet: you can leave it once it does", 409)
            in_room = {agent["profile"]: agent for agent in invite["joined"]} if status == "started" else {}
            wanted = list(in_room) if profiles is None else profiles
            if not wanted or any(profile not in in_room for profile in wanted):
                raise RoomsError("not_in", "Those agents aren't in that room", 409)
            leaving = [in_room[profile] for profile in wanted]
            for agent in leaving:
                self._leave(link_id, agent)
            with self.store.update() as state:
                invite, now = state["invites"][key], int(self.clock())
                # The home can take them out meanwhile (room.ended): then it knows.
                ids = sorted(agent["member_id"] for agent in invite["joined"]
                             if agent["member_id"] in {each["member_id"] for each in leaving})
                if invite["status"] == "started" and ids:
                    _gone(invite, ids, "left", now)
                    _queue(state, link_id, LEAVE, room_id, {"v": VERSION, "room_id": room_id, "member_ids": ids},
                           now, parts=ids)
        self.flush()
        return self._shown_invite(self.store.read()["invites"][key])

    # --- Envelopes ------------------------------------------------------------------------

    def handle(self, received: room_links.Received) -> None:
        """An envelope from a Link, as the Link's loop hands it over."""
        handler = {INVITE: self.on_invite, ANSWER: self.on_answer, START: self.on_start, GRANT: self.on_grant,
                   LEAVE: self.on_leave, ENDED: self.on_ended, room_copies.PAGE: self.copies.on_page,
                   room_copies.COPIED: self.copies.on_copied, room_posts.POST: self.posts.on_post,
                   room_posts.POSTED: self.posts.on_posted}.get(received.kind)
        if handler is not None:
            handler(received)

    def on_invite(self, received: room_links.Received) -> None:
        body = _invite_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.invite from Link %s that isn't one", received.link_id)
            return
        now = int(self.clock())
        if body["expires_at"] <= now or body["expires_at"] > now + LASTS_S + 86400:
            logger.info("Linked Rooms dropped a room.invite from Link %s that ran out or says it lasts longer "
                        "than invites do", received.link_id)
            return
        shown = self.link(received.link_id)
        key = f"{received.link_id}/{body['room_id']}"
        with self.store.update() as state:
            if key in state["invites"]:
                return  # sent again: the first is the invite
            _forget_old(state, now)
            if len(state["invites"]) >= MAX_INVITES:
                logger.warning("Linked Rooms dropped a room.invite: this Hermes keeps %d", MAX_INVITES)
                return
            invite = state["invites"][key] = {
                **body, "link": received.link_id, "from": shown["name"] if shown else "", "received_at": now,
                "status": "waiting", "joined": [], "members": None, "started_at": None, "ended_at": None,
                "reason": None,
            }
            told = dict(invite)
        self.notify(told)

    def on_answer(self, received: room_links.Received) -> None:
        """A person's answer to a draft: taken only from the Link invited, for its own places, while the draft
        waits, with the installation the Link's answers first came from (R11)."""
        body = _answer_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.answer from Link %s that isn't one", received.link_id)
            return
        now = int(self.clock())
        room_id = body["room_id"]
        with self.store.update() as state:
            draft = state["drafts"].get(room_id)
            person = draft["links"].get(received.link_id) if draft is not None else None
            if person is None:
                logger.info("Linked Rooms dropped a room.answer from Link %s for a room it wasn't invited to",
                            received.link_id)
                return
            if person["status"] != "waiting":
                return  # sent again: the first is the answer
            if draft["status"] != "waiting" or now >= draft["expires_at"]:
                logger.info("Linked Rooms dropped a room.answer from Link %s that came after room %s stopped "
                            "waiting", received.link_id, room_id)
                return
            places = [agent["member_id"] for agent in body["join"]]
            installations = {agent["catalog"]["installation_id"] for agent in body["join"]}
            pinned = state["installations"].get(received.link_id)
            if not set(places) <= set(person["slots"]) or len(installations) > 1 \
                    or (pinned is not None and installations - {pinned}) or any(
                        room_bridge.grant_scope(agent["grant"]) != {
                            "home_install_id": draft["home_install_id"], "room_id": room_id,
                            "member_id": agent["member_id"], "target_profile": agent["profile"]}
                        for agent in body["join"]):
                logger.info("Linked Rooms dropped a room.answer from Link %s for places, grants or a Hermes that "
                            "aren't its own", received.link_id)
                return
            if installations:
                state["installations"].setdefault(received.link_id, next(iter(installations)))
            person.update(status="joined" if body["join"] else "declined", join=body["join"], answered_at=now)

    def on_start(self, received: room_links.Received) -> None:
        """The home's word on a room this installation was invited to: started, with who's in it, or not.
        Agents that aren't in it are left (unbound, their grants revoked)."""
        body = _start_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.start from Link %s that isn't one", received.link_id)
            return
        key = f"{received.link_id}/{body['room_id']}"
        with self.store.update() as state:
            invite = state["invites"].get(key)
            if invite is None or invite["status"] not in ("waiting", "joined"):
                return  # not invited over this Link, sent again, or it ended first (R9)
            if body.get("cancelled"):
                leaving = invite["joined"]
                _cancel(invite, body["reason"], int(self.clock()))
            else:
                kept = {member["member_id"] for member in body["members"]}
                leaving = [agent for agent in invite["joined"] if agent["member_id"] not in kept]
                invite.update(status="started", started_at=body["started_at"], members=body["members"],
                              joined=[agent for agent in invite["joined"] if agent["member_id"] in kept])
        for agent in leaving:
            self._leave(received.link_id, agent)

    def on_grant(self, received: room_links.Received) -> None:
        """A member's fresh grant (R4), from its own Link for its own place while it's in the room: kept when it's
        newer than the home's, and put in its route on the rooms loop's next pass (into the join for a room that
        hasn't started, whose start registers it)."""
        body = _grant_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.grant from Link %s that isn't one", received.link_id)
            return
        room_id, member_id = body["room_id"], body["member_id"]
        with self.store.update() as state:
            draft = state["drafts"].get(room_id)
            person = draft["links"].get(received.link_id) if draft is not None else None
            agent = next((agent for agent in person["join"] if agent["member_id"] == member_id), None) \
                if person is not None and person["status"] == "joined" else None
            if agent is None or room_bridge.grant_scope(body["grant"]) != {
                    "home_install_id": draft["home_install_id"], "room_id": room_id, "member_id": member_id,
                    "target_profile": agent["profile"]}:
                logger.info("Linked Rooms dropped a room.grant from Link %s for a place that isn't its own",
                            received.link_id)
                return
            member = next((each for each in draft["members"] or () if each["member_id"] == member_id), None)
            if draft["status"] not in ("waiting", "starting", "started") or (
                    draft["members"] is not None and (member is None or member.get("status", "in") != "in")):
                return  # the room ended, or the agent is out of it
            issued, kept = room_bridge.grant_issued_at(body["grant"]), room_bridge.grant_issued_at(agent["grant"])
            if issued is None or (kept is not None and issued <= kept):
                return  # sent again, or older than the one the home has
            agent["grant"] = body["grant"]
            if draft["status"] != "waiting":
                _todo(draft, member_id, "swap")

    def on_leave(self, received: room_links.Received) -> None:
        """A person took agents out of a room this installation hosts (R6): each is marked left, and its route cut
        off on the rooms loop's next pass."""
        body = _leave_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.leave from Link %s that isn't one", received.link_id)
            return
        with self.store.update() as state:
            draft = state["drafts"].get(body["room_id"])
            members = {member["member_id"]: member for member in draft["members"] or ()} if draft is not None else {}
            if any(members.get(member_id, {}).get("link") != received.link_id for member_id in body["member_ids"]):
                logger.info("Linked Rooms dropped a room.leave from Link %s for agents that aren't its own",
                            received.link_id)
                return
            if draft["status"] != "started":
                return  # the room ended: a leave comes only after the room.start its start sent
            for member_id in body["member_ids"]:
                if members[member_id].get("status", "in") == "in":
                    members[member_id]["status"] = "left"
                    _todo(draft, member_id, "cut_off")

    def on_ended(self, received: room_links.Received) -> None:
        """The home took this installation's agents out of a room, or Hermes disbanded it there (R9): those it
        names are out, undone here (unbound, their grants revoked), and the invite ends once none is in."""
        body = _ended_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.ended from Link %s that isn't one", received.link_id)
            return
        key = f"{received.link_id}/{body['room_id']}"
        with self.store.update() as state:
            invite = state["invites"].get(key)
            if invite is None or invite["status"] not in ("joined", "started"):
                return  # not invited over this Link, sent again, or it ended already
            leaving = [agent for agent in invite["joined"] if agent["member_id"] in body["member_ids"]]
            if leaving:
                _gone(invite, body["member_ids"], body["reason"], int(self.clock()))
        for agent in leaving:
            self._leave(received.link_id, agent)

    # --- The start ---------------------------------------------------------------------------

    def _start_ready(self) -> None:
        """Starts each draft that's ready: everyone answered, or Start now; then makes the route changes waiting
        in started rooms. One process at a time on this installation, whichever loop gets there first."""
        directory = self.store.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _start_lock.acquire(blocking=False):
            return
        try:
            with open(directory / ".start.lock", "a+b") as handle:
                if not room_links._try_lock(handle):
                    return
                try:
                    now = int(self.clock())
                    for room_id, draft in sorted(self.store.read()["drafts"].items(),
                                                 key=lambda item: (item[1]["created_at"], item[0])):
                        if draft["status"] == "starting" or (
                                draft["status"] == "waiting" and now < draft["expires_at"] and (
                                    draft["start_now"] or all(person["status"] != "waiting"
                                                              for person in draft["links"].values()))):
                            self._start(room_id)
                    self._routes()
                finally:
                    room_links._unlock(handle)
        finally:
            _start_lock.release()

    def _start(self, room_id: str) -> None:
        draft = self.store.read()["drafts"][room_id]
        try:
            found = self.starter.find(draft["where"])
        except room_start.StartError as exc:
            if exc.code == "unsupported":
                self._tell_once(exc.code, "Linked Rooms can't start rooms with linked people on this Hermes: it "
                                          "has no room service the plugin can use (%s)", exc)
            elif exc.code != "elsewhere":
                self._tell_once(exc.code, "Linked Rooms will start rooms with linked people once Hermes' room "
                                          "service runs (%s)", exc)
            return
        if draft["members"] is None and not self._settle_members(room_id, draft, found):
            return
        draft = self.store.read()["drafts"][room_id]
        if draft["status"] != "starting":
            return
        try:
            self.starter.create(found, room_id=room_id, name=draft["name"], members=self._roster(draft))
        except Exception as exc:  # noqa: BLE001 - the draft says it didn't start; the loop goes on
            logger.warning("Linked Rooms couldn't create room %s in Hermes' room service (%s)", room_id,
                           type(exc).__name__)
            with self.store.update() as state:
                draft = state["drafts"][room_id]
                draft["attempts"] += 1
                if draft["attempts"] >= START_TRIES:
                    self._end(state, room_id, "start_failed", int(self.clock()))
            return
        routes = {}
        for member in draft["members"]:
            if member["link"] is None:
                continue
            agent = _joined(draft, member)
            try:
                self.starter.register(found, room_id=room_id, home_install_id=draft["home_install_id"],
                                      link_id=member["link"], agent=agent)
                routes[member["member_id"]] = "live"
            except Exception as exc:  # noqa: BLE001 - its member is cut off, the room goes on
                logger.warning("Linked Rooms couldn't give %s's agent in room %s its route (%s); it's cut off",
                               member["link"], room_id, type(exc).__name__)
                routes[member["member_id"]] = "cut_off"
        now = int(self.clock())
        with self.store.update() as state:
            draft = state["drafts"][room_id]
            if draft["status"] != "starting":
                return  # Hermes disbanded it meanwhile
            draft.update(status="started", started_at=now, routes=routes, started_in=_started_in(found))
            members = [{"member_id": member["member_id"], "handle": member["handle"],
                        "display_name": member["display_name"],
                        "owner": None if member["link"] is None else draft["links"][member["link"]]["name"]}
                       for member in draft["members"]]
            seated = {member["link"] for member in draft["members"]}
            for link_id, person in draft["links"].items():
                if person["status"] == "joined" and link_id in seated:
                    body = {"v": VERSION, "room_id": room_id, "started_at": now, "members": members}
                elif person["status"] == "declined":
                    continue
                else:
                    body = {"v": VERSION, "room_id": room_id, "cancelled": True,
                            "reason": "refused" if person["status"] == "joined" else "started_without_you"}
                _queue(state, link_id, START, room_id, body, now)

    def _settle_members(self, room_id: str, draft: dict[str, Any], found: room_start.Found) -> bool:
        """Who the room starts with: this installation's agents, then each joined agent its Hermes didn't
        refuse at the probe, with handles made unique (plan ruling 3). False when the start waits (for the
        bridge) or the room has too few agents to start (it ends)."""
        kept: list[dict[str, Any]] = []
        for link_id, person in draft["links"].items():
            if person["status"] != "joined":
                continue
            for agent in person["join"]:
                try:
                    answer = self.starter.probe(found, room_id=room_id, home_install_id=draft["home_install_id"],
                                                link_id=link_id, agent=agent)
                except room_bridge.BridgeError as exc:
                    self._tell_once(f"bridge:{room_id}", "Linked Rooms will start room %s once its bridge "
                                                         "listens (%s)", room_id, exc.code)
                    return False
                if answer == room_start.REFUSED:
                    logger.info("Linked Rooms left an agent of %s's out of room %s: their Hermes refused its "
                                "grant, or its catalog changed", person["name"], room_id)
                else:
                    kept.append({**agent, "link": link_id})
        own = [{"member_id": agent["profile"], "profile": agent["profile"], "handle": agent["handle"],
                "display_name": agent["display_name"], "link": None} for agent in draft["own"]]
        used: set[str] = set()
        members = []
        for member in [*own, *kept]:
            handle = _unique(member["handle"], used)
            used.add(handle.casefold())
            members.append({"member_id": member["member_id"], "profile": member["profile"], "handle": handle,
                            "display_name": member["display_name"], "link": member["link"], "status": "in"})
        now = int(self.clock())
        with self.store.update() as state:
            draft = state["drafts"].get(room_id)
            if draft is None or draft["status"] != "waiting":
                return False
            if len(members) < 2:
                self._end(state, room_id, "too_few", now)
                return False
            draft.update(status="starting", members=members)
        return True

    def _roster(self, draft: dict[str, Any]) -> list[dict[str, Any]]:
        """The members as Hermes' create_room takes them: a linked agent's target is its Link's peer, its
        Hermes' installation and its catalog's digest."""
        roster = []
        for member in draft["members"]:
            entry = {name: member[name] for name in ("member_id", "profile", "handle", "display_name")}
            if member["link"] is not None:
                catalog = _joined(draft, member)["catalog"]
                entry["target"] = {"kind": "peer", "peer_id": peer_id(member["link"]),
                                   "installation_id": catalog["installation_id"], "profile": member["profile"],
                                   "capability_digest": catalog["catalog_digest"]}
            roster.append(entry)
        return roster

    def _end(self, state: dict[str, Any], room_id: str, reason: str, now: int) -> None:
        """A draft that won't start, and everyone who answered or hasn't told so."""
        draft = state["drafts"][room_id]
        draft.update(status="ended", reason=reason, ended_at=now)
        for link_id, person in draft["links"].items():
            if person["status"] != "declined":
                _queue(state, link_id, START, room_id, {"v": VERSION, "room_id": room_id, "cancelled": True,
                                                        "reason": reason}, now)

    # --- While a room goes on (N5b) ------------------------------------------------------

    def _routes(self) -> None:
        """The route changes waiting in started rooms (R15), each in the room service its room started in (R5): a
        member's fresh grant, or a member that left or was taken out cut off. One that fails is tried on the next
        passes, START_TRIES times in all."""
        for room_id, draft in sorted(self.store.read()["drafts"].items()):
            if draft["status"] != "started" or not draft.get("route_todo"):
                continue
            try:
                found = self.starter.find(draft.get("started_in") or draft["where"])
            except room_start.StartError:
                continue  # its service runs in another process, or isn't running yet
            for member_id in sorted(draft["route_todo"]):
                self._route(room_id, member_id, found)

    def _route(self, room_id: str, member_id: str, found: room_start.Found) -> None:
        """The change waiting for a member's route as it is when its turn comes, not as the pass read it (R23): a
        leave, a take-out or the end since stops a fresh grant, and one that comes while it's made has its own
        change made after it."""
        draft = self.store.read()["drafts"].get(room_id)
        todo = draft.get("route_todo", {}).get(member_id) if draft is not None and draft["status"] == "started" \
            else None
        if todo is None:
            return  # made meanwhile, or the room ended
        member = next(each for each in draft["members"] if each["member_id"] == member_id)
        agent = _joined(draft, member)
        failed = None
        try:
            if todo["do"] == "swap":
                self.starter.swap(found, room_id=room_id, home_install_id=draft["home_install_id"],
                                  link_id=member["link"], agent=agent,
                                  registered=draft["routes"].get(member_id) == "live")
            elif todo["do"] == "revoke":
                self.starter.revoke(found, room_id=room_id, member_id=member_id)
            else:
                self.starter.cut_off(found, room_id=room_id, member_id=member_id)
        except Exception as exc:  # noqa: BLE001 - tried again on the next pass
            failed = type(exc).__name__
        with self.store.update() as state:
            draft = state["drafts"].get(room_id)
            todos = draft.get("route_todo", {}) if draft is not None else {}
            current = todos.get(member_id)
            if current is None or current["do"] != todo["do"] or (
                    todo["do"] == "swap" and _joined(draft, member)["grant"] != agent["grant"]):
                return  # another change came meanwhile: the next pass makes it
            if failed is None:
                draft["routes"][member_id] = "live" if todo["do"] == "swap" else "cut_off"
                del todos[member_id]
            else:
                current["tries"] += 1
                if current["tries"] >= START_TRIES:
                    del todos[member_id]
                    if todo["do"] == "swap":
                        logger.warning("Linked Rooms couldn't give member %s in room %s its fresh grant (%s); its "
                                       "route keeps the one it has", member_id, room_id, failed)
                    else:
                        logger.warning("Linked Rooms couldn't cut off member %s in room %s (%s); its Hermes refuses "
                                       "its turns instead", member_id, room_id, failed)
            if not todos:
                draft.pop("route_todo", None)

    def _renew(self) -> None:
        """Fresh grants for this installation's agents in rooms over Links, each 12 hours after its last (R3,
        R13): one renewer at a time on the installation, under the answer lock, which a pass never waits for."""
        now = int(self.clock())
        due = [(key, agent["member_id"]) for key, invite in sorted(self.store.read()["invites"].items())
               if invite["status"] in ("joined", "started") for agent in invite["joined"] if _due(agent, now)]
        if not due or not _answer_lock.acquire(blocking=False):
            return
        try:
            with open(self.store.directory / ".answer.lock", "a+b") as handle:
                if not room_links._try_lock(handle):
                    return
                try:
                    for key, member_id in due:
                        self._renew_one(key, member_id)
                finally:
                    room_links._unlock(handle)
        finally:
            _answer_lock.release()

    def _renew_one(self, key: str, member_id: str) -> None:
        """One agent's fresh grant, sent to the home in a room.grant. One that can't be made waits, or stops
        (R3, R16), and never revokes anything; one whose agent went out of the room meanwhile is revoked."""
        now = int(self.clock())
        invite = self.store.read()["invites"].get(key)
        agent = _agent_in(invite, member_id)
        if agent is None or not _due(agent, now) or self._api_waits.get(agent["profile"], 0) > now:
            return
        link_id, room_id = invite["link"], invite["room_id"]
        shown = self.link(link_id)
        if shown is None or shown.get("status") != "linked":
            self._tell_once(f"unlinked:{key}", "Linked Rooms couldn't renew an agent's grant in room %s: this Hermes "
                                               "isn't linked with its host any more", room_id)
            self._renew_at(key, member_id, None)
            return
        try:
            fresh = self.joiner.renew(agent["grant"])
        except room_join.JoinError as exc:
            wait, then = _RENEW_LATER.get(exc.code, (None, "it isn't renewed again"))
            if exc.code == "api_server":
                self._api_waits[agent["profile"]] = now + RETRY_S
            self._tell_once(f"renew:{key}:{member_id}:{exc.code}", "Linked Rooms couldn't renew an agent's grant in "
                                                                   "room %s (%s); %s", room_id, exc.code, then)
            self._renew_at(key, member_id, None if wait is None else now + wait)
            return
        except Exception as exc:  # noqa: BLE001 - tried again in a minute
            self._tell_once(f"renew:{key}:{member_id}:{type(exc).__name__}", "Linked Rooms couldn't renew an agent's "
                            "grant in room %s (%s); trying again in a minute", room_id, type(exc).__name__)
            self._renew_at(key, member_id, now + RETRY_S)
            return
        with self.store.update() as state:
            current = _agent_in(state["invites"].get(key), member_id)
            if current is not None:
                current.update(grant=fresh["grant"], status_expires_at=fresh["status_expires_at"],
                               renew_at=now + RENEW_S)
                # One still to go is older: only the newest goes.
                state["outbox"] = [item for item in state["outbox"] if not (
                    item["kind"] == GRANT and item["link"] == link_id and item["body"].get("room_id") == room_id
                    and item["body"].get("member_id") == member_id)]
                _queue(state, link_id, GRANT, room_id, {"v": VERSION, "room_id": room_id, "member_id": member_id,
                                                        "grant": fresh["grant"]}, now,
                       parts=(member_id, fresh["issued_at"]))
        if current is None:
            # It left the room, or was taken out, while its grant was made: the fresh one goes too.
            self._leave(link_id, {"grant": fresh["grant"]})

    def _renew_at(self, key: str, member_id: str, when: Optional[int]) -> None:
        with self.store.update() as state:
            agent = _agent_in(state["invites"].get(key), member_id)
            if agent is not None:
                agent["renew_at"] = when

    def _leave(self, link_id: str, agent: dict[str, Any]) -> None:
        if not agent.get("grant"):
            return
        try:
            self.joiner.leave(link_id, agent["grant"])
        except Exception as exc:  # noqa: BLE001 - the grant runs out by itself
            logger.warning("Linked Rooms couldn't undo an agent's join over Link %s (%s)", link_id,
                           type(exc).__name__)

    def _tell_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._told:
            self._told.add(key)
            logger.info(message, *args)

    # --- The outbox ------------------------------------------------------------------------

    def flush(self) -> None:
        """Sends the envelopes still to go. One the mailbox didn't take stays for a later pass, until it
        would have run out there; one for a Link that ended goes."""
        now = int(self.clock())
        for item in self.store.read()["outbox"]:
            gone = item["until"] <= now
            if not gone:
                try:
                    self.send(item["link"], item["kind"], item["body"], message_id=item["id"])
                except room_links.LinkError as exc:
                    if exc.code != "not_linked":
                        logger.info("Linked Rooms will send a %s over Link %s later (%s)", item["kind"],
                                    item["link"], exc.code)
                        continue
                    gone = True
            if gone:
                logger.info("Linked Rooms dropped a %s for Link %s it couldn't send", item["kind"], item["link"])
            with self.store.update() as state:
                state["outbox"] = [other for other in state["outbox"]
                                   if (other["id"], other["link"]) != (item["id"], item["link"])]

    def pass_once(self) -> None:
        """What the rooms loop does each time it looks: the envelopes still to go, this installation's agents'
        fresh grants, then the drafts ready to start and the route changes in started rooms, the started rooms'
        copies for the people in them (room_copies), and the notes on rooms elsewhere (room_chats). An installation
        that never had a room over a Link has nothing to look at, but where the dashboard runs in the gateway, the
        loop holds the drafts from the CLI from its first pass (room_start's claim)."""
        if self.starter is not None:
            self.starter.claim()
        if not self.store.path.exists():
            return
        self.flush()
        self._settle()
        if self.joiner is not None:
            self._renew()
        if self.starter is not None:
            self._start_ready()
            self.copies.send_pages()
        self.flush()
        # Notes in the private chats of the rooms elsewhere your agents only listen in (room_chats).
        self.notes.pass_once()

    @property
    def notes(self) -> Any:
        if self._notes is None:
            from . import room_chats

            self._notes = room_chats.Notes(self.root, profile_home=self.profile_home, clock=self.clock)
        return self._notes

    def _settle(self) -> None:
        """What time has ended: a draft that lapsed ends and says so; a join whose room never started is
        undone once word of it can't come any more (its invite's time and the mailbox's hold); what ended a
        month ago is forgotten, with who the home posted for in it. Started rooms are kept until they end (left,
        removed, disbanded)."""
        now = int(self.clock())
        leaving, forgotten = [], []
        with self.store.update() as state:
            for room_id, draft in list(state["drafts"].items()):
                if draft["status"] == "waiting" and now >= draft["expires_at"]:
                    self._end(state, room_id, "lapsed", now)
                elif draft["status"] == "ended" and draft["ended_at"] + KEPT_S < now:
                    del state["drafts"][room_id]
                    forgotten.append(room_id)
            for invite in state["invites"].values():
                if invite["status"] == "joined" and now >= invite["expires_at"] + LASTS_S:
                    leaving.extend((invite["link"], agent) for agent in invite["joined"])
                    _cancel(invite, "lapsed", now)
            _forget_old(state, now)
        for link_id, agent in leaving:
            self._leave(link_id, agent)
        for room_id in forgotten:
            try:
                room_posts.forget(self.root, room_id)
            except (sqlite3.Error, OSError) as exc:
                logger.warning("Linked Rooms couldn't forget who posted in room %s (%s)", room_id,
                               type(exc).__name__)

    # --- Helpers -------------------------------------------------------------------------

    def _agents(self, agents: Any, *, minimum: int) -> list[dict[str, Any]]:
        """This installation's agents for a room, each ``{profile, handle, display_name}`` and its ``choices``
        (None for the join card's)."""
        if not isinstance(agents, list) or not minimum <= len(agents) <= MAX_AGENTS - 1:
            raise RoomsError("bad_agents", f"Bring {minimum} to {MAX_AGENTS - 1} of your agents")
        own: list[dict[str, Any]] = []
        for agent in agents:
            if not isinstance(agent, dict) or not {"profile", "handle", "display_name"} <= set(agent) \
                    or set(agent) - {"profile", "handle", "display_name", "choices"}:
                raise RoomsError("bad_agents", "Each agent is {profile, handle, display_name}, with its choices")
            profile, handle = agent["profile"], agent["handle"]
            if profile not in self._profiles():
                raise RoomsError("bad_agents", "One of those agents isn't a profile on this Hermes")
            if not _handle(handle) or any(other["handle"].casefold() == handle.casefold() for other in own) \
                    or any(other["profile"] == profile for other in own):
                raise RoomsError("bad_agents", "Each agent comes once, with a handle of its own (letters, digits, "
                                               "and . _ : -, never all or everyone)")
            label = _label(agent["display_name"])
            if label is None:
                raise RoomsError("bad_agents", f"An agent's name is 1 to {LABEL_MAX} characters")
            if "choices" in agent and agent["choices"] is not None:
                try:
                    room_choices.choices_from(agent["choices"])
                except room_choices.ChoicesError as exc:
                    raise RoomsError("bad_choices", str(exc)) from None
            own.append({"profile": profile, "handle": handle, "display_name": label,
                        "choices": agent.get("choices")})
        return own

    def _choose(self, profile: str, room_id: str, choices: Any, people: list[str]) -> None:
        store = room_choices.ChoicesStore(self.profile_home(profile), clock=self.clock)
        store.set(room_id, choices if choices is not None else self._filled(profile, room_id, people), people=people)

    def _filled(self, profile: str, room_id: str, people: list[str]) -> room_choices.RoomChoices:
        store = room_choices.ChoicesStore(self.profile_home(profile), clock=self.clock)
        if store.saved(room_id):
            return store.get(room_id)
        last = store.last_with(people)
        return last if last is not None else store.defaults()

    def _profiles(self) -> list[str]:
        """This installation's profiles: the default, and each named one Hermes keeps (a folder under
        profiles/ with something that marks it one, hermes_constants.named_profile_has_identity)."""
        found = ["default"]
        try:
            entries = sorted((self.root / "profiles").iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if entry.is_dir() and room_bridge.PROFILE.match(entry.name) and entry.name != "default" \
                    and any((entry / marker).exists() for marker in _PROFILE_MARKERS):
                found.append(entry.name)
        return found

    def _profile_home(self, profile: str) -> Path:
        # Where Hermes keeps a profile (hermes_cli.profiles.get_profile_dir).
        return self.root if profile == "default" else self.root / "profiles" / profile

    def _shown_invite(self, invite: dict[str, Any]) -> dict[str, Any]:
        people = people_key([invite["link"]], invite["people"])
        return {
            "room_id": invite["room_id"], "link": invite["link"], "from": invite["from"], "name": invite["name"],
            "people": list(invite["people"]), "agents": list(invite["agents"]), "slots": len(invite["slots"]),
            "expires_at": invite["expires_at"], "status": _invite_status(invite, int(self.clock())),
            "joined": [{name: agent[name] for name in ("profile", "handle", "display_name")}
                       for agent in invite["joined"]],
            "gone": [{name: agent[name] for name in ("profile", "handle", "display_name", "reason")}
                     for agent in invite.get("gone", [])],
            "profiles": [{"profile": profile,
                          "choices": self._filled(profile, invite["room_id"], people).as_dict()}
                         for profile in self._profiles()],
            "reason": invite["reason"],
        }

    @contextmanager
    def _answering(self) -> Iterator[None]:
        """One answer at a time on this installation, so the same one twice joins once."""
        self.store.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _answer_lock, open(self.store.directory / ".answer.lock", "a+b") as handle, \
                room_identity._exclusive(handle):
            yield

    def _shown_draft(self, room_id: str, draft: dict[str, Any]) -> dict[str, Any]:
        now = int(self.clock())
        status = "ended" if draft["status"] == "waiting" and now >= draft["expires_at"] else draft["status"]
        statuses = {member["member_id"]: member.get("status", "in") for member in draft["members"] or ()}
        return {
            "room_id": room_id, "name": draft["name"], "status": status, "created_at": draft["created_at"],
            "expires_at": draft["expires_at"], "started_at": draft["started_at"],
            "reason": "lapsed" if status != draft["status"] else draft["reason"],
            "agents": [{name: agent[name] for name in ("profile", "handle", "display_name")}
                       for agent in draft["own"]],
            "people": [{"link": link_id, "name": person["name"], "status": person["status"],
                        "agents": [{name: agent[name] for name in ("member_id", "handle", "display_name")}
                                   for agent in person["join"]]}
                       for link_id, person in draft["links"].items()],
            # Members still in whose Hermes the room can't reach; then those that left, and those taken out.
            "cut_off": sorted(member_id for member_id, route in draft["routes"].items()
                              if route != "live" and statuses.get(member_id, "in") == "in"),
            "left": sorted(member_id for member_id, kept in statuses.items() if kept == "left"),
            "removed": sorted(member_id for member_id, kept in statuses.items() if kept == "removed"),
            "members": None if draft["members"] is None else _members(draft),
        }


# --- What other modules read -------------------------------------------------------------


def drafts(root: Any) -> list[dict[str, Any]]:
    """The installation's drafts as ``GET /rooms/drafts`` shows them, with no grant."""
    return _viewer(root).drafts()["drafts"]


def invites(root: Any) -> list[dict[str, Any]]:
    """The invites to the installation as the join card shows them, with no grant."""
    return _viewer(root).invites()


def people(root: Any, room_id: Any, strict: bool = False) -> Optional[dict[str, Any]]:
    """Who's who in a started room over Links that this installation hosts or has agents in (plan ruling 8):
    ``home``, the host's owner as this installation's Link names them (None when it's this one), and each
    member with its owner's name (None for this installation's own) and its ``status``: in, or left or removed
    (R10; on a member's Hermes, only its own agents' are known). A member's Hermes keeps answering for a room it
    left or that ended, whose hidden chats it keeps, with each member as it was when it ended (the invite says how
    it ended). On the home, ``posts``: the room's latest posts the plugin made for linked people (room_posts), each
    event id and seq with who it's ``by``; on a member's Hermes, ``you``: what the home calls this installation's
    owner, the name their posts carry there. None for any other room, and for any while rooms.json can't be
    read, unless ``strict``, when that raises RoomsError."""
    try:
        state = Store(root).read()
    except RoomsError:
        if strict:
            raise
        return None
    draft = state["drafts"].get(room_id) if isinstance(room_id, str) else None
    if draft is not None and draft["status"] == "started":
        names = {link_id: room_posts.poster(person["name"]) for link_id, person in draft["links"].items()}
        try:
            latest = room_posts.latest(root, room_id)
        except (sqlite3.Error, OSError) as exc:
            if strict:
                raise
            # Who's who still shows; whose each post was waits for posts.db.
            logger.warning("Linked Rooms couldn't read who posted in room %s (%s)", room_id, type(exc).__name__)
            latest = []
        return {"room_id": room_id, "home": None, "members": _members(draft), "posts": [
            {"event_id": each["event_id"], "seq": each["seq"], "by": names.get(each["link"], room_posts.GUEST)}
            for each in latest]}
    # The invite the room is on now: a re-join over a new Link has its own, beside the old one's.
    invite = room_posts.current_invite([invite for invite in state["invites"].values()
                                        if invite["status"] in ("started", "left", "ended")], room_id)
    if invite is not None:
        gone = {agent["member_id"]: agent["reason"] for agent in invite.get("gone", [])}
        mine = {agent["member_id"] for agent in invite["joined"]} | set(gone)
        # What the home calls this installation's owner: the name its posts carry there.
        you = next((room_posts.poster(member["owner"]) for member in invite["members"]
                    if member["member_id"] in mine), None)
        return {"room_id": room_id, "home": invite["from"], "you": you, "members": [
            {"member_id": member["member_id"], "handle": member["handle"],
             "display_name": member["display_name"],
             "owner": None if member["member_id"] in mine else member["owner"] or invite["from"],
             "status": gone[member["member_id"]] if gone.get(member["member_id"]) in ("left", "removed")
             else "in"}
            for member in invite["members"]]}
    return None


INVITE_BODY = "Choose which of your agents join, or decline."


def notify_invite(invite: dict[str, Any]) -> None:
    """Tells the phone about a new invite through the relay (room.invite), with the pairing of this process'
    Hermes home: the profile the dashboard runs as. Never fails the Link's loop: the invite list shows it
    anyway, and a relay from before room invites isn't sent one (client.RELAY_GATED)."""
    try:
        sender = invite.get("from") or ""
        client.enqueue(push_event(
            "room.invite", identifier=event_id("room-invite", invite["link"], invite["room_id"]), profile="",
            title=f"{sender or 'Someone'} invited you to {invite['name']}", title_label="room.invite",
            body=INVITE_BODY,
            room={"link": invite["link"], "id": invite["room_id"], "name": invite["name"], "from": sender}))
    except Exception:  # noqa: BLE001
        logger.warning("Linked Rooms couldn't tell the phone about an invite", exc_info=True)


def disbanded(root: Any, room_id: Any) -> None:
    """What room_cleanup's guard calls once Hermes' disband_room returns on the installation at ``root`` (R8): the
    room ends in the rooms running for it in this process, if they are, and its word goes on a rooms loop's next
    pass."""
    rooms = _running.get(str(Path(root)))
    # Nothing is sent from here: the rooms loop sends what this queues.
    (rooms or Rooms(root, send=lambda *args, **kwargs: None, link=lambda link_id: None)).disbanded(room_id)


def _viewer(root: Any) -> Rooms:
    # Reads only: nothing is sent, no Link is looked at.
    return Rooms(root, send=lambda *args, **kwargs: None, link=lambda link_id: None)


def own_agent(root: Any, profile: str, handle: Optional[str] = None) -> dict[str, Any]:
    """A profile's agent for a room, named as Conduit names it (AppState.createGroupRoom): ``handle`` if given,
    else its bot's mention tag, else the profile's (``hermes`` for the default one), and the bot's name, else
    the profile's."""
    home = Path(root) if profile == "default" else Path(root) / "profiles" / profile
    name = session_titles._given_name(home)
    tag, label = ("hermes", "Hermes") if profile == "default" else (profile, profile)
    return {"profile": profile, "handle": handle or _mention_tag(name) or tag, "display_name": name or label}


def _mention_tag(name: str) -> str:
    """BotMentions.mentionTag's first form of a bot's name: lowercased, each run of anything but a-z, 0-9, _ and
    - made one hyphen, else those taken out; "" when neither is a tag."""
    lowered = name.strip().lower()
    for form in (re.sub(r"[^a-z0-9_-]+", "-", lowered).strip("-"), re.sub(r"[^a-z0-9_-]", "", lowered)):
        if form and form[0] not in "_-" and form not in TAG_RESERVED:
            return form[:64]
    return ""


# --- Where it runs ---------------------------------------------------------------------------


_running: dict[str, Rooms] = {}
_running_lock = threading.Lock()


def start(root: Any) -> Rooms:
    """Rooms over Links for the installation at ``root``, started in this process: the handlers for their
    envelopes on this copy of room_links, which run where its loop reads the inbox, and the rooms loop, which
    sends what's still to go and starts each draft that's ready whose room service runs in this process (R3).
    Again, the rooms already started."""
    key = str(Path(root))
    with _running_lock:
        rooms = _running.get(key)
        if rooms is not None:
            return rooms
        rooms = _running[key] = here(root)
        for kind in KINDS:
            room_links.handles(kind)(_on_envelope)
    threading.Thread(target=_loop, args=(rooms,), name="conduit-rooms", daemon=True).start()
    return rooms


def here(root: Any) -> Rooms:
    """Rooms over Links for the installation at ``root``, with this Hermes' own calls for its joins and starts:
    what the rooms loop runs, and what a command uses (its envelopes' handlers and the loop run only where
    ``start`` runs)."""
    return Rooms(root, joiner=room_join.Joiner(root), starter=room_start.Starter(root))


def _on_envelope(received: room_links.Received) -> None:
    rooms = _running.get(str(received.root)) if received.root is not None else None
    if rooms is None:
        logger.info("Linked Rooms dropped a %s for an installation whose rooms aren't running here", received.kind)
        return
    rooms.handle(received)


def _loop(rooms: Rooms, sleep: Callable[[float], None] = time.sleep) -> None:
    backoff = 0
    while True:
        try:
            rooms.pass_once()
            backoff = 0
            sleep(IDLE_S)
        except Exception as exc:  # noqa: BLE001 - the loop outlives any one pass
            backoff = min(MAX_BACKOFF_S, max(5, backoff * 2))
            logger.warning("Linked Rooms couldn't look at its rooms (%s); trying again in %d s",
                           getattr(exc, "code", type(exc).__name__), backoff)
            sleep(backoff)


# --- The store ----------------------------------------------------------------------------


class Store:
    """``<root>/conduit-rooms/rooms.json``."""

    def __init__(self, root: Any) -> None:
        self.directory = Path(root) / DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise RoomsError("unreadable", f"Couldn't read {self.path} ({exc.strerror or exc})", 500) from None
        try:
            state = json.loads(raw)
        except (ValueError, RecursionError):
            state = None
        if not _well_formed(state):
            raise RoomsError("unreadable", f"{self.path} isn't a rooms file this plugin wrote, so Linked Rooms "
                                           "can't use the rooms it keeps", 500)
        return state

    @contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """The state to change, saved when the block ends without raising."""
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
    return {"version": VERSION, "drafts": {}, "invites": {}, "installations": {}, "outbox": []}


def _well_formed(state: Any) -> bool:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != VERSION:
        return False
    if not all(isinstance(state.get(name), kind) for name, kind in (
            ("drafts", dict), ("invites", dict), ("installations", dict), ("outbox", list))):
        return False
    return all(_kept_draft(draft) for draft in state["drafts"].values()) \
        and all(_kept_invite(invite) for invite in state["invites"].values()) \
        and all(isinstance(value, str) for value in state["installations"].values()) \
        and all(isinstance(item, dict) and all(isinstance(item.get(name), str) for name in ("id", "link", "kind"))
                and isinstance(item.get("body"), dict) and type(item.get("until")) is int
                for item in state["outbox"])


def _kept_draft(draft: Any) -> bool:
    """A draft with every field the home reads: one without them would stop every start."""
    return isinstance(draft, dict) and all(isinstance(draft.get(name), str) for name in (
        "name", "status", "home_install_id")) and all(type(draft.get(name)) is int for name in (
            "created_at", "expires_at")) and isinstance(draft.get("start_now"), bool) \
        and isinstance(draft.get("where"), dict) and isinstance(draft.get("routes"), dict) \
        and isinstance(draft.get("own"), list) and all(_agent(agent) for agent in draft["own"]) \
        and isinstance(draft.get("links"), dict) and all(
            isinstance(person, dict) and isinstance(person.get("name"), str)
            and isinstance(person.get("status"), str) and isinstance(person.get("slots"), list)
            and isinstance(person.get("join"), list)
            and all(_agent(agent) and isinstance(agent.get("member_id"), str) for agent in person["join"])
            for person in draft["links"].values()) \
        and (draft.get("members") is None or isinstance(draft["members"], list) and all(
            _agent(member) and isinstance(member.get("member_id"), str)
            and (member.get("link") is None or member["link"] in draft["links"])
            and member.get("status", "in") in ("in", "left", "removed") for member in draft["members"])) \
        and type(draft.get("attempts")) is int and "started_at" in draft and "reason" in draft \
        and (draft.get("ended_at") is None or type(draft["ended_at"]) is int) and "ended_at" in draft \
        and (draft.get("started_in") is None or _kept_where(draft["started_in"])) and _kept_todos(draft) \
        and _kept_copies(draft)


def _kept_where(value: Any) -> bool:
    try:
        _where(value)
    except RoomsError:
        return False
    return True


def _kept_todos(draft: dict[str, Any]) -> bool:
    """A started draft's route changes, each for one of its linked members."""
    todos = draft.get("route_todo", {})
    linked = {member["member_id"] for member in draft["members"] or () if member.get("link") is not None}
    return isinstance(todos, dict) and all(
        member_id in linked and isinstance(todo, dict) and todo.get("do") in _TODO_RANK
        and type(todo.get("tries")) is int for member_id, todo in todos.items())


def _kept_copies(draft: dict[str, Any]) -> bool:
    """A started draft's copies (room_copies), each for one of its Links."""
    copies = draft.get("copies", {})
    return isinstance(copies, dict) and all(
        link_id in draft["links"] and isinstance(copy, dict) and type(copy.get("acked")) is int
        and (copy.get("state") is None or isinstance(copy["state"], str))
        and (copy.get("sent") is None or isinstance(copy["sent"], dict) and all(
            type(copy["sent"].get(name)) is int for name in ("to", "tries"))
            and isinstance(copy["sent"].get("digest"), str) and isinstance(copy["sent"].get("at"), (int, float)))
        for link_id, copy in copies.items())


def _kept_invite(invite: Any) -> bool:
    return isinstance(invite, dict) and all(isinstance(invite.get(name), str) for name in (
        "link", "room_id", "name", "from", "status", "home_install_id", "authority_gateway_id")) \
        and all(type(invite.get(name)) is int for name in ("expires_at", "received_at", "authority_epoch")) \
        and all(isinstance(invite.get(name), list) for name in ("people", "agents", "slots", "joined")) \
        and all(_agent(agent) and isinstance(agent.get("member_id"), str)
                and (agent.get("renew_at") is None or type(agent["renew_at"]) is int) for agent in invite["joined"]) \
        and isinstance(invite.get("gone", []), list) and all(
            _agent(agent) and isinstance(agent.get("member_id"), str) and isinstance(agent.get("reason"), str)
            for agent in invite.get("gone", [])) \
        and "members" in invite and "started_at" in invite and "reason" in invite \
        and "ended_at" in invite and (invite["ended_at"] is None or type(invite["ended_at"]) is int)


def _agent(agent: Any) -> bool:
    return isinstance(agent, dict) and all(isinstance(agent.get(name), str)
                                           for name in ("profile", "handle", "display_name"))


def _queue(state: dict[str, Any], link_id: str, kind: str, room_id: str, body: dict, now: int,
           parts: Any = ()) -> None:
    """An envelope to go, with its message id: the same one queued again goes once. N5a's kinds go once per room
    and Link (``kind:room_id``); the others' ids name the room and ``parts``, what makes each one (R2), since a
    Link drops an id it's had in the last week."""
    message_id = f"{kind}:{room_id}" if not parts else \
        f"{kind}:{hashlib.sha256(json.dumps([room_id, *parts]).encode('utf-8')).hexdigest()[:32]}"
    if all((item["id"], item["link"]) != (message_id, link_id) for item in state["outbox"]):
        state["outbox"].append({"id": message_id, "link": link_id, "kind": kind, "body": body,
                                "until": now + LASTS_S})


def _joined(draft: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    """A linked member's join, with its grant and catalog."""
    return next(agent for agent in draft["links"][member["link"]]["join"]
                if agent["member_id"] == member["member_id"])


def _members(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """A started draft's members, each with its owner (None for this installation's own, else the name its Link
    has here) and whether it's in the room, left it or was taken out."""
    return [{"member_id": member["member_id"], "handle": member["handle"], "display_name": member["display_name"],
             "owner": None if member["link"] is None else draft["links"][member["link"]]["name"],
             "status": member.get("status", "in")} for member in draft["members"]]


def _member(draft: dict[str, Any], member: Any) -> Optional[dict[str, Any]]:
    """A started draft's member by its member id, else by its handle (casefolded, as Hermes' are, with or without
    @)."""
    if not isinstance(member, str):
        return None
    members = draft["members"] or []
    handle = member.strip().removeprefix("@").casefold()
    return next((each for each in members if each["member_id"] == member), None) or next(
        (each for each in members if each["handle"].casefold() == handle), None)


def _todo(draft: dict[str, Any], member_id: str, do: str) -> None:
    """A change to a member's route for the rooms loop to make: it takes the place of one waiting that does no
    more (R15), with tries of its own."""
    todos = draft.setdefault("route_todo", {})
    if member_id not in todos or _TODO_RANK[do] >= _TODO_RANK[todos[member_id]["do"]]:
        todos[member_id] = {"do": do, "tries": 0}


def _started_in(found: room_start.Found) -> Optional[dict[str, Any]]:
    """Where a room started, so its route changes go to the same room service (R5); None for a canonical one
    whose home isn't known, whose changes go where its draft said."""
    if found.kind != "canonical":
        return {"kind": "legacy"}
    if isinstance(found.home, str) and found.home and isinstance(found.subject, str) and found.subject:
        return {"kind": "canonical", "home": found.home, "subject": found.subject}
    return None


def _agent_in(invite: Optional[dict[str, Any]], member_id: str) -> Optional[dict[str, Any]]:
    """An invite's agent still joined to its room, started or not yet."""
    if invite is None or invite["status"] not in ("joined", "started"):
        return None
    return next((agent for agent in invite["joined"] if agent["member_id"] == member_id), None)


def _due(agent: dict[str, Any], now: int) -> bool:
    """Whether an agent's grant is due a renewal: one from before renewals (N5a's) is due now, and one whose
    renewals stopped never."""
    renew_at = agent.get("renew_at", 0)
    return renew_at is not None and renew_at <= now


def _gone(invite: dict[str, Any], member_ids: Any, reason: str, now: int) -> None:
    """Agents out of an invite's room, with why (left, removed, disbanded): from joined, with their grants, into
    gone. The invite ends once none is in: left, when the last one left."""
    ids = set(member_ids)
    invite.setdefault("gone", []).extend(
        {**{name: agent[name] for name in ("member_id", "profile", "handle", "display_name")}, "reason": reason}
        for agent in invite["joined"] if agent["member_id"] in ids)
    invite["joined"] = [agent for agent in invite["joined"] if agent["member_id"] not in ids]
    if not invite["joined"]:
        invite.update(status="left" if reason == "left" else "ended", reason=reason, ended_at=now)


def _unique(handle: str, used: set[str]) -> str:
    """``handle``, or with ``-2``, ``-3``... when the room has it already (casefolded, as Hermes' are)."""
    if handle.casefold() not in used:
        return handle
    for n in range(2, MAX_AGENTS + 2):
        suffix = f"-{n}"
        candidate = f"{handle[:64 - len(suffix)]}{suffix}"
        if candidate.casefold() not in used:
            return candidate
    raise AssertionError("a room has at most six agents")


def _ended(invite: Optional[dict[str, Any]], now: int) -> Optional[RoomsError]:
    """Why an invite that hasn't been answered can't be any more, if it can't: the home's word on its room came
    (started without this installation, or not at all), or it ran out."""
    if invite is None or invite["status"] != "waiting":
        return RoomsError("ended", "This room isn't waiting for an answer any more", 409)
    if now >= invite["expires_at"]:
        return RoomsError("lapsed", "This invite ran out", 410)
    return None


def _cancel(invite: dict[str, Any], reason: str, now: int) -> None:
    """An invite whose room won't have this installation's agents: its grants go (each is revoked)."""
    invite.update(status="cancelled", reason=reason, ended_at=now,
                  joined=[{name: value for name, value in agent.items() if name != "grant"}
                          for agent in invite["joined"]])


def _forget_old(state: dict[str, Any], now: int) -> None:
    """Invites that ended (declined, cancelled, left, ended, or lapsed unanswered) a month ago."""
    for key, invite in list(state["invites"].items()):
        ended = invite["ended_at"] if invite["status"] in ("declined", "cancelled", "left", "ended") else (
            invite["expires_at"] if invite["status"] == "waiting" else None)
        if ended is not None and ended + KEPT_S < now:
            del state["invites"][key]


def _invite_status(invite: dict[str, Any], now: int) -> str:
    return "lapsed" if invite["status"] == "waiting" and now >= invite["expires_at"] else invite["status"]


# --- What envelopes hold -----------------------------------------------------------------------


_INVITE_FIELDS = frozenset({"v", "room_id", "name", "home_install_id", "authority_gateway_id", "authority_epoch",
                            "people", "agents", "slots", "expires_at"})


def _invite_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.invite's body, or None when it isn't one."""
    if not isinstance(body, dict) or set(body) != _INVITE_FIELDS or type(body["v"]) is not int \
            or body["v"] != VERSION:
        return None
    room_id, name = body["room_id"], _room_label(body["name"])
    if not _identifier(room_id) or name is None or not _identifier(body["home_install_id"]) \
            or not _identifier(body["authority_gateway_id"]):
        return None
    if type(body["authority_epoch"]) is not int or body["authority_epoch"] < 1 \
            or type(body["expires_at"]) is not int:
        return None
    people, agents, places = body["people"], body["agents"], body["slots"]
    if not isinstance(people, list) or len(people) > MAX_LINKS - 1 \
            or not all(room_links._checked_name(person) == person for person in people):
        return None
    if not isinstance(agents, list) or not 1 <= len(agents) <= MAX_AGENTS - 1 or not all(
            isinstance(agent, dict) and set(agent) == {"handle", "display_name"} and _handle(agent["handle"])
            and _label(agent["display_name"]) == agent["display_name"] for agent in agents):
        return None
    if not isinstance(places, list) or not 1 <= len(places) <= MAX_AGENTS - 1 \
            or not all(_identifier(place) for place in places) or len(set(places)) != len(places):
        return None
    return {"room_id": room_id, "name": name, "home_install_id": body["home_install_id"],
            "authority_gateway_id": body["authority_gateway_id"], "authority_epoch": body["authority_epoch"],
            "people": people, "agents": agents, "slots": places, "expires_at": body["expires_at"]}


_JOIN_FIELDS = frozenset({"member_id", "profile", "handle", "display_name", "grant", "catalog"})
_DIGEST = re.compile(r"[0-9a-f]{64}")
# A catalog as Hermes' API server answers it, generously: its own checks run at the start.
MAX_CATALOG_BYTES = 16 * 1024


def _answer_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.answer's body, or None when it isn't one."""
    if not isinstance(body, dict) or set(body) != {"v", "room_id", "join"} or type(body["v"]) is not int \
            or body["v"] != VERSION or not _identifier(body["room_id"]):
        return None
    join = body["join"]
    if not isinstance(join, list) or len(join) > MAX_AGENTS - 1:
        return None
    for agent in join:
        if not isinstance(agent, dict) or set(agent) != _JOIN_FIELDS or not _identifier(agent["member_id"]) \
                or not (isinstance(agent["profile"], str) and room_bridge.PROFILE.match(agent["profile"])) \
                or not _handle(agent["handle"]) or _label(agent["display_name"]) != agent["display_name"] \
                or not isinstance(agent["grant"], str) or room_bridge.grant_scope(agent["grant"]) is None \
                or not _catalog(agent["catalog"]):
            return None
    if len({agent["member_id"] for agent in join}) != len(join) \
            or len({agent["handle"].casefold() for agent in join}) != len(join) \
            or len({agent["profile"] for agent in join}) != len(join):
        return None
    return {"room_id": body["room_id"], "join": join}


_MEMBER_FIELDS = frozenset({"member_id", "handle", "display_name", "owner"})


def _start_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.start's body, or None when it isn't one."""
    if not isinstance(body, dict) or type(body.get("v")) is not int or body["v"] != VERSION \
            or not _identifier(body.get("room_id")):
        return None
    if set(body) == {"v", "room_id", "cancelled", "reason"}:
        return dict(body) if body["cancelled"] is True and isinstance(body["reason"], str) \
            and CODE.fullmatch(body["reason"]) else None
    if set(body) != {"v", "room_id", "started_at", "members"} or type(body["started_at"]) is not int:
        return None
    members = body["members"]
    if not isinstance(members, list) or not 2 <= len(members) <= MAX_AGENTS or not all(
            isinstance(member, dict) and set(member) == _MEMBER_FIELDS and _identifier(member["member_id"])
            and _handle(member["handle"])
            and _label(member["display_name"]) == member["display_name"]
            and (member["owner"] is None or room_links._checked_name(member["owner"]) == member["owner"])
            for member in members):
        return None
    # Each member once, by its id and its handle (R12).
    if len({member["member_id"] for member in members}) != len(members) \
            or len({member["handle"].casefold() for member in members}) != len(members):
        return None
    return dict(body)


def _grant_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.grant's body, or None when it isn't one."""
    if not isinstance(body, dict) or set(body) != {"v", "room_id", "member_id", "grant"} \
            or type(body["v"]) is not int or body["v"] != VERSION or not _identifier(body["room_id"]) \
            or not _identifier(body["member_id"]) or not isinstance(body["grant"], str) \
            or room_bridge.grant_scope(body["grant"]) is None:
        return None
    return dict(body)


def _leave_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.leave's body, or None when it isn't one."""
    if not isinstance(body, dict) or set(body) != {"v", "room_id", "member_ids"} or type(body["v"]) is not int \
            or body["v"] != VERSION or not _identifier(body["room_id"]) or not _member_ids(body["member_ids"]):
        return None
    return dict(body)


def _ended_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.ended's body, or None when it isn't one."""
    if not isinstance(body, dict) or set(body) != {"v", "room_id", "reason", "member_ids"} \
            or type(body["v"]) is not int or body["v"] != VERSION or not _identifier(body["room_id"]) \
            or not isinstance(body["reason"], str) or body["reason"] not in ENDED_REASONS \
            or not _member_ids(body["member_ids"]):
        return None
    return dict(body)


def _member_ids(value: Any) -> bool:
    """One to five member ids, each once: a person's agents in a room."""
    return isinstance(value, list) and 1 <= len(value) <= MAX_AGENTS - 1 \
        and all(_identifier(member_id) for member_id in value) and len(set(value)) == len(value)


def _catalog(value: Any) -> bool:
    """A catalog a room over a Link can use: RoomLink direct, text, its digests sha256 (plan ruling 6)."""
    if not isinstance(value, dict):
        return False
    try:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_CATALOG_BYTES:
            return False
    except (TypeError, ValueError, RecursionError):
        return False
    policy = value.get("execution_policy")
    return _identifier(value.get("installation_id")) and isinstance(value.get("link_modes"), list) \
        and "direct" in value["link_modes"] and value.get("text") is True \
        and isinstance(value.get("catalog_digest"), str) and bool(_DIGEST.fullmatch(value["catalog_digest"])) \
        and isinstance(policy, dict) and isinstance(policy.get("policy_digest"), str) \
        and bool(_DIGEST.fullmatch(policy["policy_digest"]))


# --- Checks -----------------------------------------------------------------------------------


_PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(room_bridge.IDENTIFIER.match(value))


def _handle(value: Any) -> bool:
    return isinstance(value, str) and bool(HANDLE.fullmatch(value)) and value.casefold() not in RESERVED


def _label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    label = value.strip()
    return label if 1 <= len(label) <= LABEL_MAX and label.isprintable() else None


def _room_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if 1 <= len(name) <= NAME_MAX and name.isprintable() else None


def _room_name(value: Any) -> str:
    name = _room_label(value)
    if name is None:
        raise RoomsError("bad_name", f"A room's name is 1 to {NAME_MAX} characters, on one line")
    return name


def _where(value: Any) -> dict[str, Any]:
    """Where a draft starts: ``{"kind": "legacy"}``, ``{"kind": "canonical", "home", "subject"}`` or
    ``{"kind": "any"}`` (room_start.py)."""
    if isinstance(value, dict) and value.get("kind") in ("legacy", "any") and set(value) == {"kind"}:
        return dict(value)
    if isinstance(value, dict) and value.get("kind") == "canonical" and set(value) == {"kind", "home", "subject"} \
            and isinstance(value["home"], str) and value["home"] and isinstance(value["subject"], str) \
            and value["subject"]:
        return dict(value)
    raise RoomsError("bad_service", "That isn't a room service this plugin starts rooms in", 500)
