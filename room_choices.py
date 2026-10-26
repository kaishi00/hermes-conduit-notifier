"""Each owner's choices for their agent in a Linked Room (designs/cross-owner-agents).

When an agent sits in a group room with someone else's agents, its owner
picks, for that room, what it can do (Talk only, Look things up, Use my data,
Custom), what it may share, whether it can search its owner's past chats, how
it takes part and how many turns a day it gets. The rules that enforce them
run on the owner's own host (room_rules.py), never on the room's.

Choices are per profile, in the profile's ``conduit-rooms.json``: a room's
own choices, and the defaults a room without any gets. The dashboard routes
(dashboard process) and the CLI (``hermes conduit-push rooms``) both use this
module: the dashboard loads it by path, so it imports nothing from the plugin
package.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-rooms.json"
VERSION = 1

TOOLS = ("talk", "look", "data", "custom")
SHARE = ("helps", "ask", "nothing")
TAKES_PART = ("joins", "asked", "listens")
MAX_ROOMS = 512
MAX_CUSTOM_TOOLS = 64
MIN_DAILY_LIMIT, MAX_DAILY_LIMIT = 1, 500
MAX_NOTE_CHARS = 1000
# 2100-01-01: later (or inf) is a corrupt time.
MAX_AT = 4_102_444_800

# Never allowed in a turn of a room with other people, whatever the owner
# picks (decision 9): memory and skill writes (the background review's
# included, since its calls carry the room session's id), delegation,
# messaging, scheduled jobs, and Conduit's own settings and calls. The rules
# see a tool call only when it carries the room session's id, so a tool that
# calls other tools without it (execute_code's script) is never allowed either.
NEVER = frozenset({
    "memory", "skill_manage", "delegate_task", "kanban_create", "execute_code",
    "send_message", "message_agent", "react_to_message", "cronjob", "cronjob_manage",
    "conduit_call_user", "conduit_call_watch", "conduit_settings",
})

# Hermes' hosted room ids (gateway/hosted_rooms_common.IDENTIFIER_RE, 128 chars).
_ROOM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# A tool name as Hermes and MCP servers spell them.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}")

_thread_lock = threading.Lock()
logger = logging.getLogger(__name__)
# Per file: an unreadable one is logged the first time and every 100th.
_unreadable_counts: dict[str, int] = {}
_counts_lock = threading.Lock()


class ChoicesError(ValueError):
    """Choices or a room id this store won't keep."""


@dataclass(frozen=True)
class RoomChoices:
    """What the owner allows their agent in one room. The defaults are the
    careful ones: no tools, ask before anything personal, no past-chat
    search."""

    tools: str = "talk"
    custom_tools: tuple[str, ...] = ()
    share: str = "ask"
    search_chats: bool = False
    takes_part: str = "joins"
    daily_limit: int = 40
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"tools": self.tools, "custom_tools": list(self.custom_tools), "share": self.share,
                "search_chats": self.search_chats, "takes_part": self.takes_part,
                "daily_limit": self.daily_limit, "note": self.note}


_FIELDS = frozenset(field.name for field in fields(RoomChoices))


def choices_from(value: Any, base: Optional[RoomChoices] = None) -> RoomChoices:
    """``base`` with the fields ``value`` names changed, or ChoicesError. A
    RoomChoices is taken as it is."""
    if isinstance(value, RoomChoices):
        value = value.as_dict()
    if not isinstance(value, Mapping):
        raise ChoicesError("Choices must be a JSON object")
    unknown = set(value) - _FIELDS
    if unknown:
        raise ChoicesError(f"Unknown choice: {', '.join(sorted(str(key) for key in unknown))}")
    changes: dict[str, Any] = {}
    if "tools" in value:
        changes["tools"] = _one_of(value["tools"], TOOLS, "tools")
    if "custom_tools" in value:
        changes["custom_tools"] = _tool_names(value["custom_tools"])
    if "share" in value:
        changes["share"] = _one_of(value["share"], SHARE, "share")
    if "search_chats" in value:
        if not isinstance(value["search_chats"], bool):
            raise ChoicesError("search_chats is true or false")
        changes["search_chats"] = value["search_chats"]
    if "takes_part" in value:
        changes["takes_part"] = _one_of(value["takes_part"], TAKES_PART, "takes_part")
    if "daily_limit" in value:
        limit = value["daily_limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or not MIN_DAILY_LIMIT <= limit <= MAX_DAILY_LIMIT:
            raise ChoicesError(f"daily_limit is a whole number from {MIN_DAILY_LIMIT} to {MAX_DAILY_LIMIT}")
        changes["daily_limit"] = limit
    if "note" in value:
        changes["note"] = _note(value["note"])
    return replace(base or RoomChoices(), **changes)


def room_id_from(value: Any) -> str:
    if not isinstance(value, str) or not _ROOM_ID.fullmatch(value):
        raise ChoicesError("room_id must be a hosted room's id")
    return value


class ChoicesStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def get(self, room_id: Any) -> RoomChoices:
        """The room's choices, or the defaults when it has none."""
        return self._choices(self._load(), room_id_from(room_id))[0]

    def saved(self, room_id: Any) -> bool:
        """Whether the room has choices of its own."""
        return self._choices(self._load(), room_id_from(room_id))[1]

    def set(self, room_id: Any, choices: Any) -> RoomChoices:
        """Changes the fields ``choices`` names for the room, over what it had
        (the defaults for a room with none). Returns the room's choices."""
        room_id = room_id_from(room_id)
        with self._locked() as state:
            current = self._choices(state, room_id)[0]
            updated = choices_from(choices, current)
            state["rooms"][room_id] = {"choices": updated.as_dict(), "at": self.clock()}
            while len(state["rooms"]) > MAX_ROOMS:
                oldest = min(state["rooms"], key=lambda key: state["rooms"][key]["at"])
                del state["rooms"][oldest]
            return updated

    def defaults(self) -> RoomChoices:
        return self._defaults(self._load())

    def set_defaults(self, choices: Any) -> RoomChoices:
        with self._locked() as state:
            updated = choices_from(choices, self._defaults(state))
            state["defaults"] = updated.as_dict()
            return updated

    def view(self) -> dict[str, Any]:
        """The defaults, and each room's choices with when they were set."""
        state = self._load()
        rooms = {room_id: {"choices": self._choices(state, room_id)[0].as_dict(), "at": entry["at"]}
                 for room_id, entry in state["rooms"].items()}
        return {"defaults": self._defaults(state).as_dict(), "rooms": rooms}

    # --- Storage -------------------------------------------------------------

    def _defaults(self, state: dict[str, Any]) -> RoomChoices:
        saved = state.get("defaults")
        if saved is None:
            return RoomChoices()
        try:
            return choices_from(saved)
        except ChoicesError:
            # A hand edit broke them: the careful choices, as for a room.
            self._unreadable("keeps defaults in a shape it can't use", "rooms without their own choices get")
            return RoomChoices()

    def _choices(self, state: dict[str, Any], room_id: str) -> tuple[RoomChoices, bool]:
        defaults = self._defaults(state)
        entry = state["rooms"].get(room_id)
        if entry is None:
            return defaults, False
        try:
            return choices_from(entry["choices"], defaults), True
        except ChoicesError:
            # A hand edit broke it: the careful choices, never the owner's
            # defaults (they may allow more than this room did).
            self._unreadable(f"keeps room {room_id}'s choices in a shape it can't use", "that room gets")
            return RoomChoices(), True

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = None
        except (ValueError, RecursionError):
            # A hand edit broke it: the careful defaults (no tools), and a
            # write starts it over.
            self._unreadable("isn't valid JSON")
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the choices it couldn't read, so it fails instead.
            if for_write:
                raise
            self._unreadable("can't be read")
            raw = None
        dropped: list[str] = []
        state = _normalized(raw, dropped)
        if dropped:
            self._left_out(len(dropped))
        return state

    def _left_out(self, rooms: int) -> None:
        # A hand edit's: Conduit and the CLI refuse such an id. Its choices
        # could never be any room's, and the next save drops them.
        key = f"{self.path}:left out"
        with _counts_lock:
            count = _unreadable_counts[key] = _unreadable_counts.get(key, 0) + 1
        level = logging.WARNING if count % 100 == 1 else logging.DEBUG
        logger.log(level, "conduit_push: the room choices in %s name %d room(s) by an id no room has; their "
                   "choices are left out (%d times so far)", self.path, rooms, count)

    def _unreadable(self, why: str, who: str = "its rooms get") -> None:
        key = str(self.path)
        # Not _thread_lock: a write loads the file while holding it.
        with _counts_lock:
            count = _unreadable_counts[key] = _unreadable_counts.get(key, 0) + 1
        level = logging.WARNING if count % 100 == 1 else logging.DEBUG
        logger.log(level, "conduit_push: couldn't read the room choices in %s (it %s), so %s the careful "
                   "defaults: no tools (%d times so far)", self.path, why, who, count)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock.
        A change the caller refuses (ChoicesError) saves nothing."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load(for_write=True)
            before = _encoded(state)
            yield state
            after = _encoded(state)
            if after != before:
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)


# --- Helpers ---------------------------------------------------------------------


def _one_of(value: Any, allowed: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ChoicesError(f"{name} is one of {', '.join(allowed)}")
    return value


def _tool_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_CUSTOM_TOOLS:
        raise ChoicesError(f"custom_tools is a list of up to {MAX_CUSTOM_TOOLS} tool names")
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ChoicesError("Each custom tool is a tool's name")
        if name in NEVER:
            raise ChoicesError(f"{name} is never allowed in a room with other people")
        if name not in names:
            names.append(name)
    return tuple(names)


def _note(value: Any) -> str:
    if not isinstance(value, str):
        raise ChoicesError("note is text")
    lines = ["".join(char for char in line if char.isprintable()).rstrip() for line in value.splitlines()]
    text = "\n".join(lines).strip()
    if len(text) > MAX_NOTE_CHARS:
        raise ChoicesError(f"note is at most {MAX_NOTE_CHARS} characters")
    return text


def _normalized(raw: Any, dropped: Optional[list[str]] = None) -> dict[str, Any]:
    """The file's state in the shape the store keeps; ``dropped`` gets the
    room keys that aren't room ids."""
    state: dict[str, Any] = {"version": VERSION, "defaults": None, "rooms": {}}
    if not isinstance(raw, dict):
        return state
    if isinstance(raw.get("defaults"), dict):
        state["defaults"] = raw["defaults"]
    rooms = raw.get("rooms")
    if isinstance(rooms, dict):
        for room_id, entry in rooms.items():
            if not isinstance(room_id, str) or not _ROOM_ID.fullmatch(room_id):
                if dropped is not None:
                    dropped.append(str(room_id))
                continue
            # An entry a hand edit broke is kept, so its room gets the careful
            # choices rather than the defaults.
            entry = entry if isinstance(entry, dict) else {}
            at = entry.get("at")
            if not isinstance(at, (int, float)) or isinstance(at, bool) or not 0 <= at < MAX_AT:
                at = 0.0
            state["rooms"][room_id] = {"choices": entry.get("choices"), "at": float(at)}
    return state


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False) + "\n"


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as prefs_store: flock where there is one, a first-byte
    # lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):  # about a minute
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
