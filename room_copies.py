"""Copies of rooms over Links, for Linked Rooms (designs/cross-owner-agents, N6).

A room's log lives on its home. Every person whose agents are in it keeps a copy on their own Hermes, which their
Conduit reads, so nobody's phone talks to anyone else's server, and they keep what they had when they leave.

The home sends each of them the log a page at a time (room.page), with what the room is doing: whether it ended,
who's answering and how each linked agent's route is. One page waits for its answer (room.copied, the seq the copy
holds) before the next goes, and goes again after a minute without one, so a lost envelope only delays the copy.
The pages leave out one thing: the home's own agents' turn errors, which are Hermes' raw ones.

The person's Hermes takes a page only over the Link that invited it to the room, once it started, from the room's
own authority, and keeps it with Hermes' replica store (gateway/hosted_room_replicas.ingest_page), which refuses a
page with a gap or from an older epoch. It keeps it in ``conduit-rooms/copies.db``, owner only, never in Hermes'
own room database, with who it's from and the room's state in a table of the plugin's beside Hermes'.

Hermes' calls come in through ``Hermes``. Nothing here imports Hermes at module level, so the tests give it a
fake, and a Hermes without the log reader or the replica store sends or keeps no copies, with one log line.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Optional

from . import room_identity, room_links, room_posts

logger = logging.getLogger(__name__)

VERSION = 1
PAGE, COPIED = "room.page", "room.copied"
KINDS = (PAGE, COPIED)
FILE_NAME = "copies.db"
# A page: Hermes' read_events limit, and a JSON body that fits an envelope sealed (room_envelope.MAX_BYTES, 256 KB).
PAGE_EVENTS = 500
PAGE_BYTES = 160_000
# How long a page waits for its answer before it goes again.
WAIT_S = 60
# The copy's reads, as groups.log has them.
MAX_LOG_LIMIT = 500
# What a room ended for, as the home says it.
ENDED = frozenset({"disbanded"})
# A read a writer held past sqlite's own wait is tried again this often (as room_rules).
READ_RETRIES, READ_RETRY_PAUSE = 3, 0.05
_OMITTED = {"omitted": True}

_TABLE = """CREATE TABLE IF NOT EXISTS conduit_room_copies (
    room_id TEXT PRIMARY KEY, link TEXT NOT NULL, home_name TEXT NOT NULL, members_json TEXT NOT NULL,
    state_json TEXT NOT NULL, updated_at REAL NOT NULL)"""
# Who wrote the posts the home made for linked people (room_posts): their name there, or null for this
# installation's own owner.
_POSTS_TABLE = """CREATE TABLE IF NOT EXISTS conduit_room_copy_posts (
    room_id TEXT NOT NULL, event_id TEXT NOT NULL, by_name TEXT, PRIMARY KEY (room_id, event_id))"""
_db_lock = threading.Lock()


# On each member a copy keeps: its profile when it's one of this installation's agents, as its invite had them (the
# home's own value for it is replaced), so the copy still knows its agents once the invite is forgotten.
YOURS = "yours"


class CopiesError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Hermes:
    """Hermes' calls for copies: the home's log reader (gateway/hosted_rooms.read_events) and the member's replica
    store (gateway/hosted_room_replicas.ingest_page and its ReplicaError)."""

    def __init__(self) -> None:
        self._calls: Optional[dict[str, Any]] = None

    def reads_logs(self) -> bool:
        return self._load()["read_events"] is not None

    def keeps_copies(self) -> bool:
        return self._load()["ingest_page"] is not None and self._load()["ReplicaError"] is not None

    def read_events(self, db_path: Any, **fields: Any) -> dict[str, Any]:
        return self._load()["read_events"](db_path, **fields)

    def ingest_page(self, db_path: Any, **fields: Any) -> dict[str, Any]:
        return self._load()["ingest_page"](db_path, **fields)

    @property
    def replica_error(self) -> type:
        return self._load()["ReplicaError"]

    def _load(self) -> dict[str, Any]:
        if self._calls is None:
            calls: dict[str, Any] = {}
            for module_name, names in (("gateway.hosted_rooms", ("read_events",)),
                                       ("gateway.hosted_room_replicas", ("ingest_page", "ReplicaError"))):
                try:
                    module = importlib.import_module(module_name)
                except ImportError:
                    module = None
                calls.update((name, getattr(module, name, None)) for name in names)
            self._calls = calls
        return self._calls


class Copies:
    """The copies of the installation whose rooms over Links are ``rooms`` (room_invites.Rooms): its store, its
    Links' send, its clock and its starter, which finds a started room's service."""

    def __init__(self, rooms: Any, *, hermes: Any = None) -> None:
        self.rooms = rooms
        self.hermes = hermes if hermes is not None else Hermes()
        self.path = path(rooms.root)
        self._told: set[str] = set()
        self._told_lock = threading.Lock()

    # --- The home's pages ------------------------------------------------------------------

    def send_pages(self) -> None:
        """What the rooms loop does each pass on the home: a page to each person whose copy is behind the log or
        the room's state, when none is waiting for its answer."""
        drafts = self.rooms.store.read()["drafts"]
        for room_id, draft in sorted(drafts.items()):
            due = _receiving(draft)
            if not due:
                continue
            if not self.hermes.reads_logs():
                self._tell_once("logs", "Linked Rooms can't send copies of its rooms: this Hermes has no "
                                        "gateway.hosted_rooms.read_events")
                return
            try:
                found = self.rooms.starter.find(draft.get("started_in") or draft["where"])
            except Exception:  # noqa: BLE001 - its service runs in another process, or isn't running yet
                continue
            state = self._state(found, room_id, draft)
            for link_id in due:
                self._send(room_id, draft, link_id, found, state)

    def _state(self, found: Any, room_id: str, draft: dict[str, Any]) -> dict[str, Any]:
        """What the room is doing, as everyone in it may see it: never its pending actions, the home's own
        approvals."""
        ended = draft["reason"] if draft["status"] == "ended" and draft["reason"] in ENDED else None
        state = {"ended": ended, "working": False, "activity": [], "routes": []}
        if ended is not None:
            return state
        try:
            status = found.service.status(room_id)
        except Exception:  # noqa: BLE001 - a room the service can't say about is one with nothing going on
            return state
        if not isinstance(status, dict):
            return state
        state["working"] = status.get("working") is True
        state["activity"] = [{"member_id": each["member_id"], "status": each["status"]}
                             for each in status.get("member_activity") or () if _pair(each)]
        state["routes"] = [{"member_id": each["member_id"], "status": each["status"]}
                           for each in status.get("peer_routes") or () if _pair(each)]
        return state

    def _send(self, room_id: str, draft: dict[str, Any], link_id: str, found: Any, state: dict[str, Any]) -> None:
        copy = _copy_of(draft, link_id)
        now = self.rooms.clock()
        sent = copy["sent"]
        if sent is not None and now - sent["at"] < WAIT_S:
            return
        digest = _digest(state)
        try:
            page = self.hermes.read_events(found.service.db_path, room_id=room_id, since_seq=copy["acked"],
                                           limit=PAGE_EVENTS, include_disbanded=True)
        except Exception as exc:  # noqa: BLE001 - the next pass tries again
            self._tell_once(f"read:{room_id}", "Linked Rooms couldn't read room %s's log for its copies (%s)",
                            room_id, type(exc).__name__)
            return
        if not page.get("events") and digest == copy["state"]:
            if draft["status"] == "ended" and not copy.get("done"):
                with self.rooms.store.update() as stored:
                    kept = stored["drafts"].get(room_id)
                    if kept is not None:
                        _copy_of(kept, link_id, keep=True)["done"] = True
            return
        own = {member["member_id"] for member in draft["members"] if member["link"] is None}
        try:
            posted = room_posts.by_event(self.rooms.root, room_id, [
                event.get("event_id") for event in page.get("events") or ()
                if isinstance(event, dict) and event.get("kind") == "message.user"
                and isinstance(event.get("event_id"), str)])
        except (sqlite3.Error, OSError) as exc:
            # Held for a later pass, so no page goes without who posted what.
            self._tell_once(f"posts:{room_id}", "Linked Rooms can't send room %s's copy: who posted in it can't "
                                                "be read (%s)", room_id, type(exc).__name__)
            return
        posts = {event_id: None if link == link_id else room_posts.poster(draft["links"].get(link, {}).get("name"))
                 for event_id, link in posted.items()}
        body = self._fitted({"v": VERSION, "room_id": room_id, "name": draft["name"],
                             "members": _shown_members(draft), "page": page, "state": state}, own, posts)
        if body is None:
            self._tell_once(f"fit:{room_id}", "Linked Rooms can't send room %s's copy: its page doesn't fit an "
                                              "envelope even with one event and no payload", room_id)
            return
        events = body["page"]["events"]
        to = events[-1]["seq"] if events else copy["acked"]
        tries = sent["tries"] + 1 if sent is not None and (sent["to"], sent["digest"]) == (to, digest) else 1
        message_id = f"{PAGE}:" + hashlib.sha256(json.dumps(
            [room_id, copy["acked"], to, digest, tries]).encode("utf-8")).hexdigest()[:32]
        # Kept before it goes: its answer can come before the send returns.
        waiting = {"to": to, "digest": digest, "at": now, "tries": tries}
        with self.rooms.store.update() as stored:
            kept = stored["drafts"].get(room_id)
            if kept is None or _copy_of(kept, link_id)["sent"] != sent:
                return  # gone, or its answer came while this page was read
            _copy_of(kept, link_id, keep=True)["sent"] = waiting
        try:
            self.rooms.send(link_id, PAGE, body, message_id=message_id)
        except room_links.LinkError as exc:
            gone = exc.code == "not_linked"
            if gone:
                logger.info("Linked Rooms stopped sending room %s's copy: Link %s ended", room_id, link_id)
            else:
                logger.info("Linked Rooms will send room %s's copy over Link %s later (%s)", room_id, link_id,
                            exc.code)
            with self.rooms.store.update() as stored:
                kept = stored["drafts"].get(room_id)
                copy = (kept.get("copies") or {}).get(link_id) if kept is not None else None
                if copy is not None and copy["sent"] == waiting:
                    # Due again now, under the next try's id: the mailbox may have taken this one.
                    copy["sent"] = {**waiting, "at": now - WAIT_S}
                    if gone:
                        copy["gone"] = True

    @staticmethod
    def _fitted(body: dict[str, Any], own: set[str], posts: Optional[dict[str, Optional[str]]] = None) \
            -> Optional[dict[str, Any]]:
        """The page as it goes: without the home's own agents' turn errors, and as many of its events as fit
        PAGE_BYTES, with who wrote those of them the plugin posted (``posts``: their name, or None for the copy's
        own person); one that fits no page goes without its payload, so the copy has no gap. None when even
        that doesn't fit."""
        page = body["page"]
        events = [_kept_event(event, own) for event in page.get("events") or ()]
        posts = posts or {}

        def built(count: int) -> dict[str, Any]:
            shown = events[:count]
            cursor = shown[-1]["seq"] if shown else page.get("cursor", 0)
            built_body = {**body, "page": {**page, "events": shown, "cursor": cursor,
                                           "has_more": cursor < page.get("latest_seq", cursor)}}
            shown_posts = [{"event_id": event["event_id"], "by": posts[event["event_id"]]} for event in shown
                           if isinstance(event, dict) and event.get("event_id") in posts]
            if shown_posts:
                built_body["posts"] = shown_posts
            return built_body

        def fits(count: int) -> bool:
            # As room_envelope.seal encodes it.
            return len(json.dumps(built(count), separators=(",", ":"), ensure_ascii=False).encode("utf-8")) \
                <= PAGE_BYTES

        if fits(len(events)):
            return built(len(events))
        low, high = 0, len(events)
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if fits(middle) else (low, middle - 1)
        if low == 0 and events:
            events[0] = {**events[0], "payload": dict(_OMITTED)}
            low = 1
        return built(low) if fits(low) else None

    def on_copied(self, received: room_links.Received) -> None:
        """A copy's answer: what it holds, from the Link a page went to. A page it took clears the wait, and one
        it refused (a gap) has the next page start where the copy is."""
        body = received.body
        if not isinstance(body, dict) or set(body) != {"v", "room_id", "seq"} or body["v"] != VERSION \
                or not isinstance(body["room_id"], str) or type(body["seq"]) is not int or body["seq"] < 0:
            logger.info("Linked Rooms dropped a room.copied from Link %s that isn't one", received.link_id)
            return
        with self.rooms.store.update() as state:
            draft = state["drafts"].get(body["room_id"])
            copy = (draft.get("copies") or {}).get(received.link_id) if draft is not None else None
            if copy is None:
                logger.info("Linked Rooms dropped a room.copied from Link %s for a room it has no copy of",
                            received.link_id)
                return
            sent = copy["sent"]
            if body["seq"] > max(copy["acked"], sent["to"] if sent is not None else 0):
                logger.info("Linked Rooms dropped a room.copied from Link %s for more of room %s than it sent",
                            received.link_id, body["room_id"])
                return
            copy["acked"] = body["seq"]
            if sent is not None and body["seq"] >= sent["to"]:
                copy["state"] = sent["digest"]
            copy["sent"] = None

    # --- The copy ----------------------------------------------------------------------------

    def on_page(self, received: room_links.Received) -> None:
        """A page of a room this installation's agents are in, kept, and answered with what the copy holds."""
        body = _page_from(received.body)
        key = f"{received.link_id}/{body['room_id']}" if body is not None else None
        invite = self.rooms.store.read()["invites"].get(key) if key is not None else None
        if body is None or invite is None or invite["started_at"] is None \
                or body["page"]["authority"].get("gateway_id") != invite["authority_gateway_id"]:
            logger.info("Linked Rooms dropped a room.page from Link %s that isn't one for a room it's in",
                        received.link_id)
            return
        if not self.hermes.keeps_copies():
            self._tell_once("copies", "Linked Rooms can't keep copies of rooms other people host: this Hermes has "
                                      "no gateway.hosted_room_replicas.ingest_page")
            return
        room_id = body["room_id"]
        with _db_lock:
            _create(self.path)
            try:
                # Who posted what goes first, so a page whose events are kept is never without it: a page that
                # failed after them and comes again may find nothing left to ingest.
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute(_POSTS_TABLE)
                    conn.executemany("INSERT OR REPLACE INTO conduit_room_copy_posts VALUES (?, ?, ?)",
                                     [(room_id, each["event_id"], each["by"]) for each in body.get("posts", ())])
                result = self.hermes.ingest_page(self.path, room_id=room_id, room_name=body["name"],
                                                 members=body["members"], page=body["page"])
                seq = int(result["stored_seq"])
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute(_TABLE)
                    conn.execute("INSERT OR REPLACE INTO conduit_room_copies VALUES (?, ?, ?, ?, ?, ?)",
                                 (room_id, received.link_id, invite["from"],
                                  json.dumps(_marked(body["members"], invite)),
                                  json.dumps(body["state"]), float(self.rooms.clock())))
            except self.hermes.replica_error as exc:
                logger.info("Linked Rooms' copy of room %s didn't take a page (%s)", room_id, exc)
                seq = _held(self.path, room_id)
            except Exception as exc:  # noqa: BLE001 - the home sends it again
                logger.warning("Linked Rooms couldn't keep a page of room %s (%s)", room_id, type(exc).__name__)
                return
        try:
            self.rooms.send(received.link_id, COPIED, {"v": VERSION, "room_id": room_id, "seq": seq},
                            message_id=f"{COPIED}:" + hashlib.sha256(
                                received.message_id.encode("utf-8")).hexdigest()[:32])
        except room_links.LinkError as exc:
            logger.info("Linked Rooms couldn't answer room %s's page (%s); its home sends it again",
                        room_id, exc.code)

    def _tell_once(self, key: str, message: str, *args: Any) -> None:
        with self._told_lock:
            if key in self._told:
                return
            self._told.add(key)
        logger.info(message, *args)


# --- What Conduit reads ------------------------------------------------------------------


def path(root: Any) -> Path:
    return Path(root) / room_identity.DIRECTORY / FILE_NAME


def copies(root: Any) -> list[dict[str, Any]]:
    """Each room hosted elsewhere this installation has a copy of, the one that changed last first."""
    from . import room_invites

    invites = room_invites.Store(root).read()["invites"]
    shown = []
    for row in _rows(path(root)):
        invite = invites.get(f"{row['link']}/{row['room_id']}")
        try:
            shown.append(_shown(row, invite))
            if invite is not None:
                _note_yours(path(root), row, invite)
        except (TypeError, ValueError, RecursionError, AttributeError):
            _skipped(f"copy:{row['room_id']}", "Linked Rooms skipped its copy of room %s: it can't read it",
                     row["room_id"])
    return shown


def _note_yours(file: Path, row: sqlite3.Row, invite: dict[str, Any]) -> None:
    """A copy kept before it noted your agents (0.115) notes them while its invite lasts: an ended room's copy
    takes no more pages."""
    members = json.loads(row["members_json"])
    if all(YOURS in member for member in members):
        return
    try:
        with _db_lock, closing(sqlite3.connect(file)) as conn, conn:
            conn.execute("UPDATE conduit_room_copies SET members_json=? WHERE room_id=? AND members_json=?",
                         (json.dumps(_marked(members, invite)), row["room_id"], row["members_json"]))
    except sqlite3.Error:  # noted on a later read
        pass


def copy(root: Any, room_id: Any) -> dict[str, Any]:
    found = next((each for each in copies(root) if each["room_id"] == room_id), None)
    if found is None:
        raise CopiesError("no_copy", "This Hermes has no copy of that room", 404)
    return found


def log(root: Any, room_id: Any, *, since_seq: Any = 0, limit: Any = 100) -> dict[str, Any]:
    """The copy's events after ``since_seq``, in groups.log's shape."""
    if not isinstance(room_id, str) or not room_id:
        raise CopiesError("bad_room", "Name the room")
    if type(since_seq) is not int or since_seq < 0:
        raise CopiesError("bad_since", "since_seq is a whole number, 0 or more")
    if type(limit) is not int or not 1 <= limit <= MAX_LOG_LIMIT:
        raise CopiesError("bad_limit", f"limit is 1 to {MAX_LOG_LIMIT}")
    file = path(root)
    if not file.exists():
        raise CopiesError("no_copy", "This Hermes has no copy of that room", 404)

    def read() -> tuple[Any, list[sqlite3.Row], dict[str, Optional[str]]]:
        with closing(_reader(file)) as conn:
            try:
                room = conn.execute("""SELECT authority_gateway_id, authority_epoch, last_seq FROM
                                    hosted_room_replicas WHERE room_id=?""", (room_id,)).fetchone()
                rows = conn.execute("""SELECT room_id, seq, event_id, kind, actor_json, authority_epoch,
                                    payload_json, created_at FROM hosted_room_replica_events WHERE room_id=? AND
                                    seq>? ORDER BY seq LIMIT ?""", (room_id, since_seq, limit)).fetchall() \
                    if room is not None else []
            except sqlite3.OperationalError as exc:
                if _busy(exc):
                    raise
                return None, [], {}
            try:
                posts = {row["event_id"]: row["by_name"] for row in conn.execute(
                    "SELECT event_id, by_name FROM conduit_room_copy_posts WHERE room_id=?", (room_id,))}
            except sqlite3.OperationalError as exc:
                if _busy(exc):
                    raise
                posts = {}  # a copy whose home never posted for anyone
            return room, rows, posts

    room, rows, posts = _retried(read)
    if room is None:
        raise CopiesError("no_copy", "This Hermes has no copy of that room", 404)
    events = []
    for row in rows:
        try:
            event = {"room_id": row["room_id"], "seq": int(row["seq"]), "event_id": row["event_id"],
                     "kind": row["kind"], "actor": json.loads(row["actor_json"]),
                     "authority_epoch": None if row["authority_epoch"] is None else int(row["authority_epoch"]),
                     "payload": json.loads(row["payload_json"]), "created_at": float(row["created_at"])}
        except (TypeError, ValueError, RecursionError):
            _skipped(f"event:{room_id}", "Linked Rooms skipped an event of room %s's copy it can't read", room_id)
            continue
        if row["event_id"] in posts:
            event["post"] = {"by": posts[row["event_id"]]}
        events.append(event)
    latest = int(room["last_seq"])
    cursor = int(rows[-1]["seq"]) if rows else since_seq
    return {"events": events, "cursor": cursor, "latest_seq": latest, "has_more": cursor < latest,
            "authority": {"gateway_id": room["authority_gateway_id"], "epoch": int(room["authority_epoch"])}}


def forget(root: Any, room_id: Any) -> dict[str, Any]:
    """Deletes this installation's copy of a room that's over for it, and what its owner posted there. A room its
    agents are still in is refused (its posts frame their turns: leave first), and so is one this installation
    hosts, whose posts are the home's record of who wrote what."""
    from . import room_invites

    if not isinstance(room_id, str) or not room_id:
        raise CopiesError("bad_room", "Name the room")
    try:
        state = room_invites.Store(root).read()
    except room_invites.RoomsError:
        raise CopiesError("unreadable", "This Hermes can't read its rooms now: try again", 503) from None
    if room_id in state["drafts"]:
        raise CopiesError("hosted", "This Hermes hosts that room: it keeps no copy of it", 409)
    if any(invite["room_id"] == room_id and invite["status"] == "started" and invite["joined"]
           for invite in state["invites"].values()):
        raise CopiesError("still_in", "Your agents are still in that room: leave it first", 409)
    file = path(root)
    forgotten = False
    if file.exists():
        with _db_lock, closing(sqlite3.connect(file)) as conn, conn:
            for table in ("hosted_room_replica_events", "hosted_room_replicas", "conduit_room_copies",
                          "conduit_room_copy_posts"):
                try:
                    forgotten = conn.execute(f"DELETE FROM {table} WHERE room_id=?", (room_id,)).rowcount > 0 \
                        or forgotten
                except sqlite3.OperationalError:
                    pass  # a table the copy never made
    room_posts.forget(root, room_id)
    return {"room_id": room_id, "forgotten": forgotten}


# --- Helpers -------------------------------------------------------------------------------


def _receiving(draft: dict[str, Any]) -> list[str]:
    """The Links a started room's pages go to: each person with an agent still in it, and in a room that ended
    (disbanded) each one until its copy has the end."""
    if draft.get("started_at") is None or draft["status"] not in ("started", "ended") or not draft.get("members"):
        return []
    if draft["status"] == "ended" and draft["reason"] not in ENDED:
        return []
    copies = draft.get("copies") or {}
    links = sorted({member["link"] for member in draft["members"]
                    if member["link"] is not None and member.get("status", "in") == "in"
                    and not copies.get(member["link"], {}).get("gone")})
    if draft["status"] == "ended":
        links = [link_id for link_id in links if not copies.get(link_id, {}).get("done")]
    return links


def _copy_of(draft: dict[str, Any], link_id: str, *, keep: bool = False) -> dict[str, Any]:
    empty = {"acked": 0, "state": None, "sent": None}
    if not keep:
        return {**empty, **(draft.get("copies") or {}).get(link_id, {})}
    return draft.setdefault("copies", {}).setdefault(link_id, empty)


def _shown_members(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The members as room.start names them: their handle, name and owner."""
    return [{"member_id": member["member_id"], "handle": member["handle"], "display_name": member["display_name"],
             "owner": None if member["link"] is None else draft["links"][member["link"]]["name"]}
            for member in draft["members"]]


def _kept_event(event: Any, own: set[str]) -> Any:
    if isinstance(event, dict) and event.get("kind") == "turn.failed" and isinstance(event.get("payload"), dict) \
            and event["payload"].get("member_id") in own:
        return {**event, "payload": {**event["payload"], "error": ""}}
    return event


def _pair(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("member_id"), str) and isinstance(value.get("status"), str)


def _digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()[:16]


_PAGE_FIELDS = frozenset({"v", "room_id", "name", "members", "page", "state"})
_STATE_FIELDS = frozenset({"ended", "working", "activity", "routes"})
_MEMBER_FIELDS = frozenset({"member_id", "handle", "display_name", "owner"})


def _page_from(body: Any) -> Optional[dict[str, Any]]:
    """A room.page's body, or None when it isn't one. Hermes checks the page itself."""
    if not isinstance(body, dict) or set(body) - {"posts"} != _PAGE_FIELDS or body["v"] != VERSION:
        return None
    if "posts" in body and not (isinstance(body["posts"], list) and body["posts"] and all(
            isinstance(each, dict) and set(each) == {"event_id", "by"} and isinstance(each["event_id"], str)
            and (each["by"] is None or isinstance(each["by"], str)) for each in body["posts"])):
        return None
    if not isinstance(body["room_id"], str) or not body["room_id"] or not isinstance(body["name"], str):
        return None
    members, page, state = body["members"], body["page"], body["state"]
    if not isinstance(members, list) or not all(
            isinstance(member, dict) and set(member) == _MEMBER_FIELDS
            and all(isinstance(member[name], str) for name in ("member_id", "handle", "display_name"))
            and (member["owner"] is None or isinstance(member["owner"], str)) for member in members):
        return None
    if not isinstance(page, dict) or not isinstance(page.get("events"), list) \
            or not isinstance(page.get("authority"), dict):
        return None
    if not isinstance(state, dict) or set(state) != _STATE_FIELDS or not isinstance(state["working"], bool) \
            or (state["ended"] is not None and state["ended"] not in ENDED) \
            or not all(isinstance(state[name], list) and all(_pair(each) and set(each) == {"member_id", "status"}
                                                             for each in state[name])
                       for name in ("activity", "routes")):
        return None
    return body


def _create(file: Path) -> None:
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file.parent.chmod(0o700)
    descriptor = os.open(file, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    file.chmod(0o600)


def _reader(file: Path) -> sqlite3.Connection:
    # Quoted, so a home path with ? or # in it is still the file's. Sqlite waits out a writer for 5 s.
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(file))}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _busy(exc: sqlite3.Error) -> bool:
    """Whether a read failed for a writer, not for what the file holds: tried again (_retried)."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text or "disk i/o error" in text


def _retried(read: Callable[[], Any]) -> Any:
    """A read, tried again after a writer held it past sqlite's own wait, or mid-checkpoint (as room_rules)."""
    for attempt in range(READ_RETRIES + 1):
        try:
            return read()
        except sqlite3.OperationalError as exc:
            if attempt == READ_RETRIES or not _busy(exc):
                raise
            time.sleep(READ_RETRY_PAUSE)


_skipped_told: set[str] = set()


def _skipped(key: str, message: str, *args: Any) -> None:
    if key not in _skipped_told:
        _skipped_told.add(key)
        logger.warning(message, *args)


def _held(file: Path, room_id: str) -> int:
    try:
        with closing(_reader(file)) as conn:
            row = conn.execute("SELECT last_seq FROM hosted_room_replicas WHERE room_id=?", (room_id,)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row["last_seq"]) if row is not None else 0


def _rows(file: Path) -> list[sqlite3.Row]:
    if not file.exists():
        return []

    def read() -> list[sqlite3.Row]:
        with closing(_reader(file)) as conn:
            try:
                return conn.execute("""SELECT r.room_id, r.name, r.authority_gateway_id, r.last_seq, r.latest_seq,
                    c.link, c.home_name, c.members_json, c.state_json, c.updated_at
                    FROM hosted_room_replicas r JOIN conduit_room_copies c ON c.room_id = r.room_id
                    ORDER BY c.updated_at DESC, r.room_id""").fetchall()
            except sqlite3.OperationalError as exc:
                if _busy(exc):
                    raise
                return []  # a copy that hasn't taken a page yet

    return _retried(read)


def _shown(row: sqlite3.Row, invite: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A copy as Conduit shows it, with what its invite says while this installation keeps it: your agents in it,
    and whether it goes on."""
    state, members = json.loads(row["state_json"]), json.loads(row["members_json"])
    if invite is not None:
        status, reason, started_at, ended_at = invite["status"], invite["reason"], invite["started_at"], \
            invite["ended_at"]
        yours = [{"member_id": agent["member_id"], "profile": agent["profile"], "handle": agent["handle"]}
                 for agent in invite["joined"]]
    else:  # the invite forgotten: your agents as the copy marked them
        status, reason, started_at, ended_at = "ended", state.get("ended"), None, None
        yours = [{"member_id": member["member_id"], "profile": member[YOURS], "handle": member.get("handle")}
                 for member in members if isinstance(member.get(YOURS), str)]
    if state.get("ended") is not None:
        status, reason = "ended", state["ended"]
    members = [{key: value for key, value in member.items() if key != YOURS} for member in members]
    return {"room_id": row["room_id"], "name": row["name"], "link": row["link"], "from": row["home_name"],
            "members": members, "yours": yours, "status": status, "reason": reason,
            "started_at": started_at, "ended_at": ended_at, "state": state, "last_seq": int(row["last_seq"]),
            "latest_seq": int(row["latest_seq"]), "updated_at": float(row["updated_at"])}


def _marked(members: list[dict[str, Any]], invite: dict[str, Any]) -> list[dict[str, Any]]:
    mine = {agent["member_id"]: agent["profile"] for agent in [*invite["joined"], *invite.get("gone", ())]}
    return [{**member, YOURS: mine.get(member.get("member_id"))} for member in members]
