"""Done chats: the chats the user or their agent marked done, kept on the host
so every Conduit (iPhone, iPad, Mac, desktop) shows the same mark
(hermes-conduit: Done chats).

A chat is known by more than one id: its lineage root (the id it started
with, which a compaction keeps), its current session and the sessions it
continued through. A mark keeps every id it was given, and any of them finds
it, so a chat compacted after it was marked stays done. Marking a chat again,
or opening it again, first drops every mark that shares one of its ids.

A mark stays until someone opens the chat again: a new turn doesn't, since a
"thanks" after the work is done would.

State lives in the profile's ``conduit-chat-marks.json``. The dashboard loads
this module by path, so it imports nothing from the plugin package.
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
from typing import Any, Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-chat-marks.json"
# The oldest marks go first past this many.
MAX_DONE = 5_000
MAX_IDS = 8
MAX_ID_CHARS = 200
BY = ("user", "agent")
# 2100-01-01: later (or inf) is a corrupt time.
MAX_AT = 4_102_444_800

_lock = threading.Lock()


class ChatMarksError(ValueError):
    pass


def clean_ids(value: Any) -> list[str]:
    """A chat's ids as given, cleaned and deduplicated, at most ``MAX_IDS``."""
    if not isinstance(value, list):
        raise ChatMarksError("ids is a list of the chat's ids")
    ids: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        text = "".join(char for char in raw if unicodedata.category(char) != "Cc").strip()
        if text and len(text) <= MAX_ID_CHARS and text not in ids:
            ids.append(text)
        if len(ids) == MAX_IDS:
            break
    if not ids:
        raise ChatMarksError("ids needs the chat's id")
    return ids


def _well_formed(entry: Any) -> bool:
    return (isinstance(entry, dict)
            and isinstance(entry.get("ids"), list) and 0 < len(entry["ids"]) <= MAX_IDS
            and all(isinstance(item, str) and 0 < len(item) <= MAX_ID_CHARS for item in entry["ids"])
            and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)
            and 0 <= entry["at"] < MAX_AT
            and entry.get("by") in BY)


class ChatMarksStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time):
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # --- reading and writing the file ---------------------------------------

    def _load(self, for_write: bool = False) -> dict[str, Any]:
        """The file's marks; a missing or garbled file reads as none. A file
        that's there but can't be read raises when it's read for a write,
        so a mark never replaces every other one."""
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            return {"done": []}
        except OSError:
            if for_write:
                raise
            return {"done": []}
        if not isinstance(state, dict) or not isinstance(state.get("done"), list):
            return {"done": []}
        return {"done": [entry for entry in state["done"] if _well_formed(entry)]}

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save, under a lock file: the
        dashboard marks a chat while the agent's tool does, in another
        process."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            state = self._load(for_write=True)
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".conduit-chat-marks-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False)
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

    # --- what Conduit and the tool call ---------------------------------------

    def done(self) -> list[dict[str, Any]]:
        """Every done chat, newest mark first."""
        return sorted(self._load()["done"], key=lambda entry: entry["at"], reverse=True)

    def mark(self, ids: Any, done: bool, *, by: str = "user") -> Optional[dict[str, Any]]:
        """Marks the chat done, or open again; answers its mark (None when open)."""
        wanted = clean_ids(ids)
        if by not in BY:
            raise ChatMarksError("by is user or agent")
        if not isinstance(done, bool):
            raise ChatMarksError("done is true or false")
        with self._locked() as state:
            kept = [entry for entry in state["done"] if not set(entry["ids"]) & set(wanted)]
            entry = None
            if done:
                entry = {"ids": wanted, "at": float(self.clock()), "by": by}
                kept.append(entry)
                kept = sorted(kept, key=lambda item: item["at"])[-MAX_DONE:]
            state["done"] = kept
        return entry

    def is_done(self, ids: Any) -> bool:
        wanted = set(clean_ids(ids))
        return any(set(entry["ids"]) & wanted for entry in self._load()["done"])


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
        for attempt in range(6):
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
