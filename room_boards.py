"""Boards in group rooms: the cards an agent shows with conduit_show in a
room this host runs, kept for Conduit to draw under the agent's message.

A room's log carries messages only, so the board is kept here, in the root
Hermes home (every profile's agents sit in the same rooms), and Conduit
reads a room's boards and draws each under the next message its agent posts
(``GET /boards/rooms``). The agents in the room read the message alone, so
the board's own text never reaches them.

Hermes Desktop draws a board only where the agent's reply holds a
``::conduit-board{id="cb-…"}`` line, through the plugin's Desktop half
(``desktop/plugin.js``). A board shown in a Desktop chat is kept here by that
id (``DesktopBoards``) for the half to read (``GET /boards/desktop``), and the
half says it's loaded (``POST /boards/desktop/seen``) so the agent is asked
for the line only where something will draw it.

A diff card too long for conduit_show's answer to carry whole is kept here
too (``KeptDiffs``), by the id its card names, for Conduit and the half to
read when the user opens it (``GET /boards/diffs``).

Imports nothing from the plugin package, so the dashboard can load it too.
"""

from __future__ import annotations

import json
import os
import re
import secrets
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
DESKTOP_DIRECTORY = "conduit-desktop"
FILE_NAME = "boards.json"
# What a room keeps, and the whole file: the newest win.
MAX_PER_ROOM = 100
MAX_BOARDS = 1000
# A board nobody saw in this long is forgotten.
KEEP_S = 30 * 24 * 3600
MAX_ID_CHARS = 200
# Desktop boards: what the file keeps, and how long the half counts as
# loaded after it last said so (it says so every half hour while loaded).
MAX_DESKTOP_BOARDS = 500
HALF_LIVE_S = 30 * 60
DESKTOP_BOARD_ID = re.compile(r"cb-[0-9a-f]{12}")
# Diffs kept for their cards (KeptDiffs).
DIFF_DIRECTORY = "conduit-diffs"
MAX_KEPT_DIFFS = 200
DIFF_ID = re.compile(r"cd-[0-9a-f]{16}")

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


class _BoardFile:
    """One JSON file of boards under the root home, written under a lock
    file: agents in other processes add theirs while the dashboard reads."""

    directory = DIRECTORY

    def __init__(self, root: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(root) / self.directory / FILE_NAME
        self.clock = clock

    def _state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _boards(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        boards = state.get("boards")
        if not isinstance(boards, list):
            return []
        now = self.clock()
        return [entry for entry in boards if self._well_formed(entry) and now - entry["at"] < KEEP_S]

    @staticmethod
    def _well_formed(entry: Any) -> bool:
        return _well_formed(entry)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            state = self._state()
            state["boards"] = self._boards(state)
            yield state
            self._save(state)

    def _save(self, state: dict[str, Any], path: Any = None) -> None:
        path = self.path if path is None else path
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".conduit-boards-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class RoomBoards(_BoardFile):
    def _load(self) -> list[dict[str, Any]]:
        return self._boards(self._state())

    def add(self, room: str, profile: str, board: dict[str, Any]) -> None:
        """A board an agent of ``profile`` showed in ``room`` just now: its
        cards, layout and Markdown, as conduit_show answers them."""
        room, profile = _clean(room), _clean(profile)
        if not room or not profile:
            raise ValueError("a room board needs its room and its agent's profile")
        with self._locked() as state:
            boards = state["boards"]
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


class DesktopBoards(_BoardFile):
    """Boards shown in Hermes Desktop chats, by the id the agent's
    ``::conduit-board`` line names, and when the Desktop half last said it
    was loaded."""

    directory = DESKTOP_DIRECTORY

    def __init__(self, root: Any, clock: Callable[[], float] = time.time,
                 new_id: Callable[[], str] = lambda: "cb-" + secrets.token_hex(6)) -> None:
        super().__init__(root, clock)
        self.new_id = new_id
        # A file of its own, so the half's half-hourly "loaded" doesn't
        # rewrite every board.
        self.half_path = self.path.with_name("half.json")

    @staticmethod
    def _well_formed(entry: Any) -> bool:
        return (isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and bool(DESKTOP_BOARD_ID.fullmatch(entry["id"]))
                and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)
                and isinstance(entry.get("board"), dict))

    def add(self, board: dict[str, Any]) -> str:
        """Keeps a board; answers the id its line names."""
        board_id = self.new_id()
        with self._locked() as state:
            boards = state["boards"]
            boards.append({"id": board_id, "at": self.clock(), "board": board})
            boards[:] = boards[-MAX_DESKTOP_BOARDS:]
        return board_id

    def get(self, board_id: Any) -> dict[str, Any] | None:
        """The board an id names, or None when it's gone."""
        if not isinstance(board_id, str) or not DESKTOP_BOARD_ID.fullmatch(board_id):
            raise ValueError("id names a board: cb- and 12 hex digits")
        return next((entry["board"] for entry in self._boards(self._state()) if entry["id"] == board_id), None)

    def saw_half(self, gone: bool = False) -> None:
        """The Desktop half is loaded, just now, or was just turned off."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._save({"seen": None if gone else self.clock()}, self.half_path)

    def half_live(self) -> bool:
        """Whether a Desktop half said it was loaded lately."""
        try:
            state = json.loads(self.half_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        seen = state.get("seen") if isinstance(state, dict) else None
        if not isinstance(seen, (int, float)) or isinstance(seen, bool):
            return False
        return 0 <= self.clock() - seen < HALF_LIVE_S


class KeptDiffs:
    """Diffs too long for a conduit_show answer to carry whole (a diff
    card), each in a file of its own by the id the card names, for Conduit
    and the Desktop half to read when the user opens a file
    (``GET /boards/diffs``). In the root home, as Desktop's boards are: the
    id is all a reader needs. The newest MAX_KEPT_DIFFS stay, for a month."""

    def __init__(self, root: Any, clock: Callable[[], float] = time.time,
                 new_id: Callable[[], str] = lambda: "cd-" + secrets.token_hex(8)) -> None:
        self.directory = Path(root) / DIFF_DIRECTORY
        self.clock = clock
        self.new_id = new_id

    def add(self, diff: dict[str, Any]) -> str:
        """Keeps a diff; answers its id."""
        diff_id = self.new_id()
        if not DIFF_ID.fullmatch(diff_id):
            raise ValueError("a diff's id is cd- and 16 hex digits")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".conduit-diff-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump({"at": self.clock(), "diff": diff}, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.directory / f"{diff_id}.json")
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self._prune()
        return diff_id

    def get(self, diff_id: Any) -> dict[str, Any] | None:
        """The diff an id names, or None when it's gone."""
        if not isinstance(diff_id, str) or not DIFF_ID.fullmatch(diff_id):
            raise ValueError("id names a diff: cd- and 16 hex digits")
        try:
            entry = json.loads((self.directory / f"{diff_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("diff"), dict):
            return None
        at = entry.get("at")
        if not isinstance(at, (int, float)) or isinstance(at, bool) or self.clock() - at >= KEEP_S:
            return None
        return entry["diff"]

    def _prune(self) -> None:
        """The oldest past MAX_KEPT_DIFFS go, and any kept a month."""
        try:
            kept = sorted(((path.stat().st_mtime, path) for path in self.directory.glob("cd-*.json")),
                          reverse=True)
        except OSError:
            return
        now = self.clock()
        for index, (mtime, path) in enumerate(kept):
            if index >= MAX_KEPT_DIFFS or now - mtime >= KEEP_S:
                try:
                    path.unlink()
                except OSError:
                    pass


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
