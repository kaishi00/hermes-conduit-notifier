"""Room files carried over Links, for Linked Rooms (Eric 2026-11-08: every participant in a room sees the same
thing).

A picture or file sent into a room is stored on the room's home (Conduit's ``POST /room-files``, under
``<hermes home>/conduit/room-files/<upload>/<name>``) and the message carries an ``@file:`` reference to that
path. The home's own agents read it there; an agent on someone else's Hermes can't, and Hermes' peer route
carries no files (a member dispatch has no field for them). So the home carries each file a room message names
to every Link with an agent in the room, as it sends the room's pages (room_copies): ``room.file`` envelopes of
CHUNK_BYTES each, sealed like every envelope, and the member's Hermes keeps the file under its own room-files
root at the same ``<upload>/<name>``, answering ``room.file_got`` once it has the whole file. A turn that comes
for one of its agents names the home's path; room_bridge_member swaps in the kept copy's (``localized``), so the
agent reads the same file on its own host, and the member's Conduit draws it from there too.

Only files under the home's room-files root of at most MAX_FILE_BYTES go, one at a time per Link, each again
after WAIT_S without its answer, up to MAX_TRIES. A member takes them only from the Link of a room it's in,
under an upload folder and name of the shape the home writes, and only when the bytes match the digest. What
went and what's coming is kept in ``conduit-rooms/files.json`` beside rooms.json, owner only; chunks wait in
``conduit-rooms/incoming/`` until the file is whole.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import room_identity, room_links

logger = logging.getLogger(__name__)

VERSION = 1
CHUNK, GOT = "room.file", "room.file_got"
KINDS = (CHUNK, GOT)
FILE_NAME = "files.json"
INCOMING = "incoming"
# Raw bytes in one envelope: base64 in a JSON body under a room page's (room_copies.PAGE_BYTES), sealed.
CHUNK_BYTES = 96 * 1024
# Hermes' own data URL cap; a larger file stays on the home (Conduit names it).
MAX_FILE_BYTES = 16 * 1024 * 1024
WAIT_S = 120
MAX_TRIES = 5
# How long a member's turn waits for a file its prompt names that's still on its way: well inside the home's
# wait for its answer (room_bridge_home.WAIT_S), which Hermes' client asks again after anyway.
ARRIVAL_WAIT_S = 8.0
# Kept per Link: older entries go first.
MAX_KEPT = 2000
# Chunks of a file that never finished go after this long.
INCOMING_TTL_S = 24 * 3600

ROOM_FILES = ("conduit", "room-files")
# An upload's folder and name as dashboard/api/rooms.py writes them.
_FOLDER = re.compile(r"^\d{8}-[0-9a-f]{32}\Z")
_UNSAFE_NAME = re.compile(r"[\\/\x00-\x1f\x7f]")
_DIGEST = re.compile(r"^[0-9a-f]{64}\Z")
# An `@file:` reference, quoted as Hermes quotes one (format_reference_value) or bare.
_REFERENCE = re.compile(r"@file:(?:`([^`\r\n]+)`|\"([^\"\r\n]+)\"|'([^'\r\n]+)'|([^\s`\"'<>]+))")
_TAIL = re.compile(r"/conduit/room-files/(\d{8}-[0-9a-f]{32})/([^/]+)\Z")
_thread_lock = threading.Lock()


def files_root(root: Any) -> Path:
    """Where this installation keeps room files: its root home's, where Conduit's uploads go."""
    return Path(root).joinpath(*ROOM_FILES)


def _name_ok(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name.encode("utf-8")) <= 255 and name not in (".", "..") \
        and not _UNSAFE_NAME.search(name)


def references(text: Any) -> list[tuple[str, str, str]]:
    """Each ``@file:`` reference in ``text`` to a room file, as (the path it names, its upload folder, name)."""
    if not isinstance(text, str) or "@file:" not in text:
        return []
    found = []
    for match in _REFERENCE.finditer(text):
        path = next(group for group in match.groups() if group is not None)
        tail = _TAIL.search(path)
        if tail is not None and _name_ok(tail.group(2)):
            found.append((path, tail.group(1), tail.group(2)))
    return found


def _quoted(path: str) -> str:
    """A path as Hermes' format_reference_value quotes one (dashboard/api/rooms.py _room_file_reference)."""
    if not re.search(r"[\s\[\](){}'\"`]", path):
        return path
    for quote in ("`", '"', "'"):
        if quote not in path:
            return quote + path + quote
    return path


def localized(text: str, root: Any, *, wait_s: float = ARRIVAL_WAIT_S, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> str:
    """``text`` with each reference to another host's room file swapped for this host's copy of it, waiting up
    to ``wait_s`` for copies still on their way. One that never came keeps the home's path."""
    found = references(text)
    if not found:
        return text
    local_root = files_root(root)
    wanted = {(folder, name): local_root / folder / name for _, folder, name in found}
    missing = {key for key, path in wanted.items() if not path.is_file()}
    deadline = clock() + wait_s
    while missing and clock() < deadline:
        sleep(0.25)
        missing = {key for key in missing if not wanted[key].is_file()}

    def swap(match: re.Match) -> str:
        path = next(group for group in match.groups() if group is not None)
        tail = _TAIL.search(path)
        if tail is None or (tail.group(1), tail.group(2)) in missing or (tail.group(1), tail.group(2)) not in wanted:
            return match.group(0)
        local = str(wanted[(tail.group(1), tail.group(2))])
        return match.group(0) if local == path else "@file:" + _quoted(local)

    return _REFERENCE.sub(swap, text)


class Files:
    """The room files of the rooms over Links of ``rooms`` (room_invites.Rooms): what its loop sends and its
    Links bring."""

    def __init__(self, rooms: Any) -> None:
        self.rooms = rooms
        self.store = Store(rooms.root)

    # --- The home ------------------------------------------------------------------------

    def _upload_roots(self) -> set[str]:
        """Where this installation's uploads are: its root home's room files and each profile's, since
        POST /room-files stores under the profile Conduit names."""
        root = Path(self.rooms.root)
        roots = {os.path.realpath(files_root(root))}
        try:
            profiles = list((root / "profiles").iterdir())
        except OSError:
            profiles = []
        roots.update(os.path.realpath(files_root(profile)) for profile in profiles if profile.is_dir())
        return roots

    def note(self, room_id: str, link_id: str, events: Any) -> None:
        """The room files the events of a page going to ``link_id`` name, to send after it."""
        found = [(event, ref) for event in events or () if isinstance(event, dict)
                 and event.get("kind") == "message.user"
                 for ref in references((event.get("payload") if isinstance(event.get("payload"), dict) else {})
                                       .get("text"))]
        if not found:
            return
        local_roots = self._upload_roots()
        wanted: dict[str, dict[str, Any]] = {}
        for _event, (path, folder, name) in found:
            real = os.path.realpath(path)
            if os.path.dirname(os.path.dirname(real)) not in local_roots or os.path.basename(real) != name \
                    or os.path.basename(os.path.dirname(real)) != folder:
                logger.info("Linked Rooms doesn't carry %s/%s to Link %s: it isn't one of this host's uploads",
                            folder, name, link_id)
                continue
            try:
                size = os.stat(real).st_size
            except OSError:
                continue
            if not os.path.isfile(real) or size > MAX_FILE_BYTES:
                if size > MAX_FILE_BYTES:
                    logger.info("Linked Rooms doesn't carry %s/%s to Link %s: it's over %d MB", folder, name,
                                link_id, MAX_FILE_BYTES // (1024 * 1024))
                continue
            wanted[f"{folder}/{name}"] = {"room": room_id, "size": size,
                                          "base": os.path.dirname(os.path.dirname(real))}
        if not wanted:
            return
        with self.store.update() as state:
            out = state["out"].setdefault(link_id, {})
            for key, entry in wanted.items():
                if key not in out:
                    out[key] = {**entry, "at": None, "tries": 0, "done": False, "noted": self.rooms.clock()}
            if len(out) > MAX_KEPT:
                for key in sorted(out, key=lambda each: out[each]["noted"])[:len(out) - MAX_KEPT]:
                    del out[key]

    def send_files(self) -> None:
        """What the rooms loop does each pass on the home: the next file each Link is owed, all its chunks."""
        now = self.rooms.clock()
        state = self.store.read()
        for link_id, out in sorted(state["out"].items()):
            due = next((key for key, entry in sorted(out.items(), key=lambda item: item[1]["noted"])
                        if not entry["done"] and entry["tries"] < MAX_TRIES
                        and (entry["at"] is None or now - entry["at"] >= WAIT_S)), None)
            if due is None:
                continue
            folder, name = due.split("/", 1)
            entry = out[due]
            # Where it was uploaded (an entry from before 0.144's profiles: the root home's), checked again.
            base = entry.get("base") if isinstance(entry.get("base"), str) else os.path.realpath(
                files_root(self.rooms.root))
            path = Path(base) / folder / name
            if base not in self._upload_roots() or os.path.dirname(os.path.dirname(os.path.realpath(path))) != base:
                with self.store.update() as kept:
                    kept["out"].get(link_id, {}).pop(due, None)
                continue
            try:
                data = path.read_bytes()
            except OSError:
                # Swept on the home (a month after its upload): nothing left to carry.
                with self.store.update() as kept:
                    kept["out"].get(link_id, {}).pop(due, None)
                continue
            if len(data) > MAX_FILE_BYTES:
                with self.store.update() as kept:
                    kept["out"].get(link_id, {}).pop(due, None)
                continue
            digest = hashlib.sha256(data).hexdigest()
            count = max(1, math.ceil(len(data) / CHUNK_BYTES))
            tries = entry["tries"] + 1
            with self.store.update() as kept:
                kept_entry = kept["out"].get(link_id, {}).get(due)
                if kept_entry is None or kept_entry["done"]:
                    continue
                kept_entry.update({"at": now, "tries": tries, "sha": digest})
            try:
                for index in range(count):
                    body = {"v": VERSION, "room_id": entry["room"], "folder": folder, "name": name, "sha": digest,
                            "size": len(data), "index": index, "count": count,
                            "data": base64.b64encode(data[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]).decode()}
                    message_id = f"{CHUNK}:" + hashlib.sha256(
                        json.dumps([digest, folder, name, index, tries]).encode()).hexdigest()[:32]
                    self.rooms.send(link_id, CHUNK, body, message_id=message_id)
            except room_links.LinkError as exc:
                if exc.code == "not_linked":
                    logger.info("Linked Rooms stopped carrying room files to Link %s: it ended", link_id)
                    with self.store.update() as kept:
                        kept["out"].pop(link_id, None)
                else:
                    logger.info("Linked Rooms will carry %s to Link %s later (%s)", due, link_id, exc.code)

    def on_got(self, received: room_links.Received) -> None:
        """A member has a whole file: it isn't sent to that Link again."""
        body = received.body if isinstance(received.body, dict) else {}
        if body.get("v") != VERSION or not isinstance(body.get("folder"), str) or not isinstance(body.get("name"), str):
            logger.info("Linked Rooms dropped a room.file_got from Link %s that isn't one", received.link_id)
            return
        key = f"{body['folder']}/{body['name']}"
        with self.store.update() as state:
            entry = state["out"].get(received.link_id, {}).get(key)
            if entry is not None and entry.get("sha") in (None, body.get("sha")):
                entry["done"] = True

    # --- A member ------------------------------------------------------------------------

    def on_chunk(self, received: room_links.Received) -> None:
        """A piece of a room file from the home of a room this installation's agents are in; the file is kept
        once it's whole and its digest matches."""
        body = _chunk_from(received.body)
        if body is None:
            logger.info("Linked Rooms dropped a room.file from Link %s that isn't one", received.link_id)
            return
        state = self.rooms.store.read()
        if not any(invite["link"] == received.link_id and invite["room_id"] == body["room_id"]
                   and invite.get("started_at") is not None for invite in state["invites"].values()):
            logger.info("Linked Rooms dropped a room.file from Link %s for a room it isn't in", received.link_id)
            return
        target = files_root(self.rooms.root) / body["folder"] / body["name"]
        if target.is_file():
            # Kept already (sent again before its answer came): answered once, on its last piece.
            if body["index"] != body["count"] - 1:
                return
        else:
            whole = self._take(received.link_id, body)
            if whole is None or not self._keep(target, whole, body):
                return
        try:
            self.rooms.send(received.link_id, GOT, {"v": VERSION, "room_id": body["room_id"],
                                                    "folder": body["folder"], "name": body["name"],
                                                    "sha": body["sha"]},
                            message_id=f"{GOT}:" + hashlib.sha256(
                                json.dumps([body["sha"], body["folder"], body["name"], body["index"]]).encode()
                            ).hexdigest()[:32])
        except room_links.LinkError as exc:
            logger.info("Linked Rooms couldn't answer a room file from Link %s (%s); the home sends it again",
                        received.link_id, exc.code)

    def _take(self, link_id: str, body: dict[str, Any]) -> Optional[bytes]:
        """Keeps one chunk; the file's bytes once every chunk is here."""
        incoming = Path(self.rooms.root) / room_identity.DIRECTORY / INCOMING
        folder = incoming / hashlib.sha256(f"{link_id}\0{body['sha']}".encode()).hexdigest()[:32]
        with _thread_lock:
            incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
            _sweep(incoming, self.rooms.clock())
            folder.mkdir(exist_ok=True, mode=0o700)
            (folder / f"{body['index']:05d}").write_bytes(body["data"])
            parts = sorted(folder.iterdir())
            if len(parts) < body["count"]:
                return None
            whole = b"".join(part.read_bytes() for part in parts[:body["count"]])
            shutil.rmtree(folder, ignore_errors=True)
        if len(whole) != body["size"] or hashlib.sha256(whole).hexdigest() != body["sha"]:
            logger.info("Linked Rooms dropped room file %s/%s from Link %s: it didn't match its digest",
                        body["folder"], body["name"], link_id)
            return None
        return whole

    def _keep(self, target: Path, whole: bytes, body: dict[str, Any]) -> bool:
        try:
            root = target.parent.parent
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.parent.mkdir(exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(prefix=".carried.", dir=target.parent)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(whole)
            os.chmod(name, 0o600)
            os.replace(name, target)
            return True
        except OSError as exc:
            level = logging.WARNING if exc.errno not in (errno.ENOSPC, errno.EDQUOT) else logging.INFO
            logger.log(level, "Linked Rooms couldn't keep room file %s/%s (%s)", body["folder"], body["name"],
                       type(exc).__name__)
            return False


def _sweep(incoming: Path, now: float) -> None:
    """Chunks of files that never finished go after INCOMING_TTL_S."""
    try:
        for folder in incoming.iterdir():
            try:
                if now - folder.stat().st_mtime > INCOMING_TTL_S:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


def _chunk_from(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict) or value.get("v") != VERSION:
        return None
    room_id, folder, name, digest = value.get("room_id"), value.get("folder"), value.get("name"), value.get("sha")
    size, index, count, data = value.get("size"), value.get("index"), value.get("count"), value.get("data")
    if not isinstance(room_id, str) or not room_id or not isinstance(folder, str) or not _FOLDER.match(folder) \
            or not _name_ok(name) or not isinstance(digest, str) or not _DIGEST.match(digest):
        return None
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES or type(count) is not int \
            or count != max(1, math.ceil(size / CHUNK_BYTES)) or type(index) is not int or not 0 <= index < count:
        return None
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    expected = min(CHUNK_BYTES, size - index * CHUNK_BYTES)
    if len(raw) != expected:
        return None
    return {"room_id": room_id, "folder": folder, "name": name, "sha": digest, "size": size, "index": index,
            "count": count, "data": raw}


# --- The store ----------------------------------------------------------------------------------------


class Store:
    """``<root>/conduit-rooms/files.json``: ``out``, Link to ``<upload>/<name>`` to how its carrying stands."""

    def __init__(self, root: Any) -> None:
        self.directory = Path(root) / room_identity.DIRECTORY
        self.path = self.directory / FILE_NAME

    def read(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError, RecursionError):
            logger.warning("Linked Rooms couldn't read %s; room files are carried again from the start", self.path)
            return _empty()
        if not isinstance(state, dict) or state.get("version") != VERSION or not isinstance(state.get("out"), dict):
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
    return {"version": VERSION, "out": {}}
