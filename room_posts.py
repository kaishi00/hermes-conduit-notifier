"""Posting in rooms over Links, for Linked Rooms (designs/cross-owner-agents, N7a).

A person whose agents are in a room someone else hosts posts from their own Conduit to their own Hermes, never to
the room's home: their Hermes keeps the post, sending, and sends it to the home over the Link that invited it
(room.post). The home posts it in the room's running service as the room's user, as Conduit's own groups.send does,
with every line of it named for them ("Mona: "), so no line of theirs reads as the home's owner's in Hermes' room
prompt, and answers with the seq it got, or why it didn't (room.posted).

Both sides keep what they need in ``conduit-rooms/posts.db``, owner only: the home, which events it posted for
whom, so its pages (room_copies) and Conduit's people read (room_invites.people) say who wrote what; the person's
Hermes, what its owner sent and how it went, which Conduit lists and the framing of its agents' turns checks the
room's lines against (room_rules).

Hermes' calls come in through ``Hermes``. Nothing here imports Hermes at module level, so the tests give it a fake.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from . import room_identity, room_links

logger = logging.getLogger(__name__)

VERSION = 1
POST, POSTED = "room.post", "room.posted"
KINDS = (POST, POSTED)
FILE_NAME = "posts.db"
# A post's text, before the home names its lines: well under Hermes' MAX_USER_TEXT_BYTES (64 KB) and an envelope.
MAX_TEXT_BYTES = 16_000
# The post as the home sends it, its lines named: Hermes' MAX_USER_TEXT_BYTES.
MAX_NAMED_BYTES = 64 * 1024
# Posts a person may make in one room in an hour: the home's agents answer each.
PER_HOUR = 60
HOUR_S = 3600
# A post still unanswered after this long is sent again under an id of its own when the person posts it again.
UNANSWERED_S = 600
# The room thread Conduit's composer posts in (AppState.GroupRoomSurface.mainThreadID).
THREAD = "main"
# Who a post is from when the home's Link for its person has no name.
GUEST = "Guest"
# What Conduit's people read lists of a room's posts, the latest.
PEOPLE_POSTS = 500
# What the framing checks a room's lines against: its owner's latest posts in the room.
SENT_TEXTS = 200
# Why a post wasn't posted, as room.posted says it.
ERRORS = frozenset({"ended", "not_in", "too_long", "too_many", "unavailable", "bad_post"})
EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS posted (
        room_id TEXT NOT NULL, event_id TEXT NOT NULL, seq INTEGER NOT NULL, link TEXT NOT NULL,
        client_id TEXT NOT NULL, posted_at REAL NOT NULL, PRIMARY KEY (room_id, event_id))""",
    """CREATE TABLE IF NOT EXISTS sent (
        link TEXT NOT NULL, room_id TEXT NOT NULL, event_id TEXT NOT NULL, text TEXT NOT NULL,
        status TEXT NOT NULL, seq INTEGER, error TEXT, created_at REAL NOT NULL,
        PRIMARY KEY (link, room_id, event_id))""",
)
_db_lock = threading.Lock()


class PostsError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Hermes:
    """Hermes' calls for posts: the event id Conduit's own groups.send gives a retry key
    (gateway/hosted_rooms.user_event_id)."""

    def __init__(self) -> None:
        self._call: Any = None

    def user_event_id(self, client_event_id: str) -> str:
        if self._call is None:
            try:
                self._call = getattr(importlib.import_module("gateway.hosted_rooms"), "user_event_id", None)
            except ImportError:
                self._call = None
            if self._call is None:
                raise PostsError("unsupported", "This Hermes has no gateway.hosted_rooms.user_event_id", 501)
        return self._call(client_event_id)


class Posts:
    """The posts of the installation whose rooms over Links are ``rooms`` (room_invites.Rooms): its store, its
    outbox, its clock and its starter, which finds a started room's service."""

    def __init__(self, rooms: Any, *, hermes: Any = None) -> None:
        self.rooms = rooms
        self.hermes = hermes if hermes is not None else Hermes()
        self.path = path(rooms.root)
        self._told: set[str] = set()

    # --- The person's side ---------------------------------------------------------------

    def post(self, room_id: Any, event_id: Any, text: Any) -> dict[str, Any]:
        """Sends ``text`` to the room ``room_id`` someone else hosts, which this installation's agents are in:
        kept as sending until the home answers. ``event_id`` is the retry key: the same one again is the same
        post."""
        from . import room_invites

        if not isinstance(room_id, str) or not room_id:
            raise PostsError("bad_room", "Name the room")
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise PostsError("bad_event_id", "event_id is 1 to 64 letters, digits and . _ : -, starting with a "
                                             "letter or digit")
        text = _text(text)
        invite = _in(self.rooms.store.read()["invites"], room_id)
        if invite is None:
            raise PostsError("not_in", "Your agents aren't in that room, so you can't post in it", 409)
        link_id, now = invite["link"], float(self.rooms.clock())
        with _db_lock, closing(_open(self.path)) as conn, conn:
            row = conn.execute("SELECT text, status, created_at FROM sent WHERE link=? AND room_id=? AND event_id=?",
                               (link_id, room_id, event_id)).fetchone()
            if row is not None and row["text"] != text:
                raise PostsError("event_id_taken", "That event_id is another post's", 409)
            if row is None:
                conn.execute("INSERT INTO sent VALUES (?, ?, ?, ?, 'sending', NULL, NULL, ?)",
                             (link_id, room_id, event_id, text, now))
            elif row["status"] == "refused":
                # Tried again: under an id of its own, since the home's Link drops one it's had.
                conn.execute("UPDATE sent SET status='sending', error=NULL WHERE link=? AND room_id=? AND "
                             "event_id=?", (link_id, room_id, event_id))
            sending = row is None or row["status"] != "posted"
            # Refused, or unanswered long enough that it or its answer was lost: under an id of its own too.
            again = row is not None and (row["status"] == "refused" or (
                row["status"] == "sending" and now - float(row["created_at"]) > UNANSWERED_S))
            tried = repr(now) if again else ""
        if sending:
            with self.rooms.store.update() as state:
                room_invites._queue(state, link_id, POST, room_id,
                                    {"v": VERSION, "room_id": room_id, "event_id": event_id, "text": text},
                                    int(now), parts=(event_id, tried) if tried else (event_id,))
            self.rooms.flush()
        return self._shown(link_id, room_id, event_id)

    def listed(self, room_id: Any) -> list[dict[str, Any]]:
        """What this installation's owner posted in the room ``room_id``, oldest first, and how each went."""
        if not isinstance(room_id, str) or not room_id:
            raise PostsError("bad_room", "Name the room")
        rows = _rows(self.path, "SELECT * FROM sent WHERE room_id=? ORDER BY created_at, rowid", (room_id,))
        return [_shown_sent(row) for row in rows]

    def sent_texts(self, room_id: str) -> list[str]:
        return sent_texts(self.rooms.root, room_id)

    def on_posted(self, received: room_links.Received) -> None:
        """The home's answer to a post: posted at a seq, or refused and why."""
        body = received.body
        if not isinstance(body, dict) or set(body) != {"v", "room_id", "event_id", "seq", "error"} \
                or body["v"] != VERSION or not isinstance(body["room_id"], str) \
                or not isinstance(body["event_id"], str) \
                or (body["error"] is None) == (body["seq"] is None) \
                or (body["seq"] is not None and (type(body["seq"]) is not int or body["seq"] < 1)) \
                or (body["error"] is not None and body["error"] not in ERRORS):
            logger.info("Linked Rooms dropped a room.posted from Link %s that isn't one", received.link_id)
            return
        status = "posted" if body["error"] is None else "refused"
        changed = 0
        if self.path.exists():
            with _db_lock, closing(_open(self.path)) as conn, conn:
                # A post the home posted stays posted, whatever an older answer to an earlier try says.
                changed = conn.execute("UPDATE sent SET status=?, seq=?, error=? WHERE link=? AND room_id=? "
                                       "AND event_id=? AND (?='posted' OR status!='posted')",
                                       (status, body["seq"], body["error"], received.link_id, body["room_id"],
                                        body["event_id"], status)).rowcount
                changed = changed or conn.execute("SELECT 1 FROM sent WHERE link=? AND room_id=? AND event_id=?",
                                                  (received.link_id, body["room_id"],
                                                   body["event_id"])).fetchone() is not None
        if not changed:
            logger.info("Linked Rooms dropped a room.posted from Link %s for a post it didn't send",
                        received.link_id)

    def _shown(self, link_id: str, room_id: str, event_id: str) -> dict[str, Any]:
        [row] = _rows(self.path, "SELECT * FROM sent WHERE link=? AND room_id=? AND event_id=?",
                      (link_id, room_id, event_id))
        return _shown_sent(row)

    # --- The home's side ----------------------------------------------------------------

    def on_post(self, received: room_links.Received) -> None:
        """A person's post in a room this installation hosts: posted in the room, named for them, if they have an
        agent in it; answered either way."""
        from . import room_invites

        body = received.body
        if not isinstance(body, dict) or set(body) != {"v", "room_id", "event_id", "text"} or body["v"] != VERSION \
                or not isinstance(body["room_id"], str) or not isinstance(body["event_id"], str) \
                or not EVENT_ID.fullmatch(body["event_id"]):
            logger.info("Linked Rooms dropped a room.post from Link %s that isn't one", received.link_id)
            return
        room_id, link_id, client_id = body["room_id"], received.link_id, body["event_id"]
        draft = self.rooms.store.read()["drafts"].get(room_id)
        seq, error = None, None
        if draft is None or not any(member["link"] == link_id for member in draft.get("members") or ()):
            error = "not_in"
        elif draft["status"] != "started":
            error = "ended"
        elif not any(member["link"] == link_id and member.get("status", "in") == "in"
                     for member in draft["members"]):
            error = "not_in"
        else:
            try:
                text = _text(body["text"])
            except PostsError as exc:
                error = exc.code if exc.code in ERRORS else "bad_post"
        if error is None:
            try:
                event_id = self.hermes.user_event_id(f"post:{room_invites.peer_id(link_id)}:{client_id}")
            except Exception as exc:  # noqa: BLE001 - no hosted rooms here: they can post again
                self._tell_once("event_id", "Linked Rooms can't take posts in its rooms: %s",
                                exc if isinstance(exc, PostsError) else type(exc).__name__)
                event_id, error = None, "unavailable"
        seq = None
        if error is None:
            try:
                seq = self._posted_seq(room_id, event_id)
                recent = self._recent(room_id, link_id) if seq is None else 0
            except sqlite3.Error as exc:
                logger.info("Linked Rooms couldn't read the posts of room %s (%s)", room_id, type(exc).__name__)
                error = "unavailable"
        if error is None:
            if seq is None and recent >= PER_HOUR:
                error = "too_many"
            elif seq is None:
                named = _named(text, draft["links"][link_id].get("name") or GUEST)
                if len(named.encode("utf-8")) > MAX_NAMED_BYTES:
                    error = "too_long"
            if error is None and seq is None:
                # Kept before it's sent, with no seq yet: a page the rooms loop reads meanwhile says who it's by.
                with _db_lock, closing(_open(self.path)) as conn, conn:
                    conn.execute("INSERT OR IGNORE INTO posted VALUES (?, ?, 0, ?, ?, ?)",
                                 (room_id, event_id, link_id, client_id, float(self.rooms.clock())))
                try:
                    found = self.rooms.starter.find(draft.get("started_in") or draft["where"])
                    event = found.service.send(room_id=room_id, event_id=event_id,
                                               payload={"thread_id": THREAD, "text": named})
                    seq = int(event["seq"])
                except Exception as exc:  # noqa: BLE001 - no service here, or it refused: they can post again
                    logger.info("Linked Rooms couldn't post in room %s for Link %s (%s)", room_id, link_id,
                                getattr(exc, "code", type(exc).__name__))
                    error = "unavailable"
                # A send that failed may still have posted it: who it's for stays, its seq waits for a resend.
                if error is None:
                    with _db_lock, closing(_open(self.path)) as conn, conn:
                        conn.execute("UPDATE posted SET seq=? WHERE room_id=? AND event_id=?", (seq, room_id, event_id))
        answer = {"v": VERSION, "room_id": room_id, "event_id": client_id, "seq": seq if error is None else None,
                  "error": error}
        with self.rooms.store.update() as state:
            room_invites._queue(state, link_id, POSTED, room_id, answer, int(self.rooms.clock()),
                                parts=(client_id, error or "", received.message_id))
        self.rooms.flush()

    def _posted_seq(self, room_id: str, event_id: str) -> Optional[int]:
        rows = _rows(self.path, "SELECT seq FROM posted WHERE room_id=? AND event_id=?", (room_id, event_id))
        return int(rows[0]["seq"]) if rows and rows[0]["seq"] else None

    def _recent(self, room_id: str, link_id: str) -> int:
        rows = _rows(self.path, "SELECT COUNT(*) AS n FROM posted WHERE room_id=? AND link=? AND posted_at>?",
                     (room_id, link_id, float(self.rooms.clock()) - HOUR_S))
        return int(rows[0]["n"]) if rows else 0

    def _tell_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._told:
            self._told.add(key)
            logger.info(message, *args)


# --- What pages and Conduit read on the home ---------------------------------------------


def path(root: Any) -> Path:
    return Path(root) / room_identity.DIRECTORY / FILE_NAME


def by_event(root: Any, room_id: str, event_ids: Any) -> dict[str, str]:
    """The Link each of ``event_ids`` was posted for in the room, of those the plugin posted."""
    wanted = sorted(set(event_ids))
    found: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        rows = _rows(path(root), f"SELECT event_id, link FROM posted WHERE room_id=? AND event_id IN "
                                 f"({','.join('?' * len(chunk))})", (room_id, *chunk))
        found.update((row["event_id"], row["link"]) for row in rows)
    return found


def latest(root: Any, room_id: str, limit: int = PEOPLE_POSTS) -> list[dict[str, Any]]:
    """The room's latest posts the plugin made, oldest first: event id, seq and Link."""
    rows = _rows(path(root), "SELECT event_id, seq, link FROM posted WHERE room_id=? AND seq>0 "
                             "ORDER BY seq DESC LIMIT ?", (room_id, limit))
    return [{"event_id": row["event_id"], "seq": int(row["seq"]), "link": row["link"]} for row in reversed(rows)]


def sent_texts(root: Any, room_id: str) -> list[str]:
    """The texts this installation's owner posted in the room, the latest: those the home took, and those on
    their way (its page can come before its answer)."""
    rows = _rows(path(root), "SELECT text FROM sent WHERE room_id=? AND status IN ('posted', 'sending') "
                             "ORDER BY created_at DESC, rowid DESC LIMIT ?", (room_id, SENT_TEXTS))
    return [row["text"] for row in rows]


def forget(root: Any, room_id: str) -> None:
    """What's kept of the room's posts, on either side."""
    file = path(root)
    if file.exists():
        with _db_lock, closing(_open(file)) as conn, conn:
            conn.execute("DELETE FROM posted WHERE room_id=?", (room_id,))
            conn.execute("DELETE FROM sent WHERE room_id=?", (room_id,))


# --- Helpers ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise PostsError("bad_text", "text is the post's words")
    # Every kind of line break is one, so no line goes without its name (_named).
    text = "\n".join(value.splitlines()).strip()
    if not text:
        raise PostsError("empty", "Write something to post")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise PostsError("too_long", f"A post is at most {MAX_TEXT_BYTES} bytes")
    return text


def _named(text: str, name: str) -> str:
    return "\n".join(f"{name}: {line}" for line in text.split("\n"))


def _in(invites: dict[str, Any], room_id: str) -> Optional[dict[str, Any]]:
    """The invite a started room someone else hosts came with, while an agent of this installation's is in it."""
    return next((invite for invite in invites.values()
                 if invite["room_id"] == room_id and invite["status"] == "started" and invite["joined"]), None)


def _shown_sent(row: sqlite3.Row) -> dict[str, Any]:
    return {"event_id": row["event_id"], "text": row["text"], "status": row["status"],
            "seq": None if row["seq"] is None else int(row["seq"]), "error": row["error"],
            "created_at": float(row["created_at"])}


def _open(file: Path) -> sqlite3.Connection:
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file.parent.chmod(0o700)
    descriptor = os.open(file, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    file.chmod(0o600)
    conn = sqlite3.connect(file, timeout=5.0)
    conn.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        conn.execute(statement)
    return conn


def _rows(file: Path, sql: str, params: tuple) -> list[sqlite3.Row]:
    if not file.exists():
        return []
    with closing(_open(file)) as conn:
        return conn.execute(sql, params).fetchall()
