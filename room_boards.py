"""Boards in group rooms: the cards an agent shows with conduit_show in a
room this host runs, kept for Conduit to draw under the agent's message.

A room's log carries messages only, so the board is kept here, in the root
Hermes home (every profile's agents sit in the same rooms), and Conduit
reads a room's boards and draws each under the next message its agent posts
(``GET /boards/rooms``). The agents in the room read the message alone, so
the board's own text never reaches them.

Imports nothing from the plugin package, so the dashboard can load it too.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

DIRECTORY = "conduit-rooms"
FILE_NAME = "boards.json"
# What a room keeps, and the whole file: the newest win.
MAX_PER_ROOM = 100
MAX_BOARDS = 1000
# A board nobody saw in this long is forgotten.
KEEP_S = 30 * 24 * 3600
MAX_ID_CHARS = 200

_lock = threading.Lock()


def _clean(text: Any, limit: int = MAX_ID_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text).strip()
    return text[:limit].rstrip()


def _well_formed(entry: Any) -> bool:
    return (isinstance(entry, dict) and all(isinstance(entry.get(key), str) for key in ("room", "profile"))
            and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)
            and isinstance(entry.get("board"), dict))


class RoomBoards:
    def __init__(self, root: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(root) / DIRECTORY / FILE_NAME
        self.clock = clock

    def _load(self) -> list[dict[str, Any]]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        boards = state.get("boards") if isinstance(state, dict) else None
        if not isinstance(boards, list):
            return []
        now = self.clock()
        return [entry for entry in boards if _well_formed(entry) and now - entry["at"] < KEEP_S]

    @contextmanager
    def _locked(self) -> Iterator[list[dict[str, Any]]]:
        """The boards under a lock file: agents in other processes add
        theirs while the dashboard reads."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            boards = self._load()
            yield boards
            self._save(boards)

    def _save(self, boards: list[dict[str, Any]]) -> None:
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".conduit-room-boards-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump({"boards": boards}, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def add(self, room: str, profile: str, board: dict[str, Any]) -> None:
        """A board an agent of ``profile`` showed in ``room`` just now: its
        cards, layout and Markdown, as conduit_show answers them."""
        room, profile = _clean(room), _clean(profile)
        if not room or not profile:
            raise ValueError("a room board needs its room and its agent's profile")
        with self._locked() as boards:
            boards.append({"room": room, "profile": profile, "at": self.clock(), "board": board})
            kept: list[dict[str, Any]] = []
            per_room: dict[str, int] = {}
            for entry in reversed(boards):
                count = per_room.get(entry["room"], 0)
                if count < MAX_PER_ROOM and len(kept) < MAX_BOARDS:
                    per_room[entry["room"]] = count + 1
                    kept.append(entry)
            boards[:] = list(reversed(kept))

    def room(self, room: Any) -> list[dict[str, Any]]:
        """A room's boards, oldest first: who showed each, when, and the board."""
        room = _clean(room)
        if not room:
            raise ValueError("room_id names the room")
        return [{"profile": entry["profile"], "at": entry["at"], "board": entry["board"]}
                for entry in self._load() if entry["room"] == room]


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as checklist_store: flock where there is one, a
    # first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
