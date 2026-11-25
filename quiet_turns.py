"""Whether each chat's last reply was one the plugin keeps quiet, for clients
that tell the user about a finished turn themselves.

Conduit for desktop shows the computer's own notification when a chat's turn
ends, from the gateway's ``message.complete``: the relay's pushes reach phones
only. The plugin decides in ``post_llm_call`` that some replies send no push
(a reply that is only ``[SILENT]``, a bot workstream's turn, a Bot Chat's
answer to an update that only says a workstream waits on the user, a room
lead's routing pick, a script's or Kanban worker's turn, a quiet cron run or
Pulse probe...). Most of those rules rest on what only the turn's own process
knows, so the desktop can't work them out from the event. The hook writes
each reply's decision here, and the desktop asks
``GET /notifications/quiet-turns`` before it notifies, so a turn the phone
hears nothing about stays quiet on the computer too.

The hook writes before Hermes ends the turn, so the decision is on disk
before any client hears ``message.complete``. Each entry is a chat's newest
decision: its session id, whether it was quiet, and when. A client reads only
a decision made since its turn started, so an earlier turn's never stands in.

State lives in the profile's ``conduit-quiet-turns.json``: the newest
MAX_TURNS chats' entries, the oldest dropped first. A file that can't be read
counts as no decision, so a fault here can only let a notification through.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-quiet-turns.json"
VERSION = 1
# Far more chats than end turns in the minute a client asks within.
MAX_TURNS = 200
# The plugin's bounds for a chat's ids (chat_mute.py).
MAX_IDS = 16
MAX_ID_LENGTH = 200
# A decision older than this answers nothing: a client asks as its turn ends.
MAX_AGE_S = 600
_thread_lock = threading.Lock()


def parse_ids(values: Iterable[Any]) -> list[str]:
    """The chat ids a request asks about; ValueError for none or too many."""
    values = list(values)[: MAX_IDS + 1]
    ids = _ids(values, limit=None)
    if not ids or len(values) > MAX_IDS:
        raise ValueError(f"session_id must name 1 to {MAX_IDS} chat ids")
    return ids


class QuietTurnStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def note(self, session_id: Any, quiet: bool) -> None:
        """Keeps ``session_id``'s newest reply decision."""
        ids = _ids([session_id])
        if not ids:
            return
        with self._locked() as state:
            now = self.clock()
            kept = [entry for entry in state["turns"] if entry["id"] != ids[0] and now - entry["at"] <= MAX_AGE_S]
            kept.append({"id": ids[0], "quiet": bool(quiet), "at": now})
            state["turns"] = kept[-MAX_TURNS:]

    def newest(self, session_ids: Iterable[Any]) -> dict[str, Any] | None:
        """The newest decision among ``session_ids`` (a chat and the sessions
        a compaction carried it through): ``{"session_id", "quiet",
        "age_s"}``, or None when none of them has one."""
        wanted = set(_ids(session_ids, limit=None))
        now = self.clock()
        found = None
        for entry in self._load()["turns"]:
            age = now - entry["at"]
            if entry["id"] in wanted and -1 <= age <= MAX_AGE_S and (found is None or entry["at"] >= found["at"]):
                found = entry
        if found is None:
            return None
        return {"session_id": found["id"], "quiet": found["quiet"], "age_s": round(max(0.0, now - found["at"]), 3)}

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace every other chat's decision, so it fails instead.
            if for_write:
                raise
            raw = None
        return _normalized(raw)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load(for_write=True)
            yield state
            self._save(json.dumps(state, separators=(",", ":")) + "\n")

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:  # an integer too large for a float
        return None


def _normalized(raw: Any) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    entries = raw.get("turns") if isinstance(raw, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("quiet"), bool):
                continue
            ids = _ids([entry.get("id")])
            at = _number(entry.get("at"))
            if ids and at is not None:
                turns.append({"id": ids[0], "quiet": entry["quiet"], "at": at})
    turns.sort(key=lambda entry: entry["at"])
    return {"version": VERSION, "turns": turns[-MAX_TURNS:]}


def _ids(values: Iterable[Any], limit: int | None = MAX_IDS) -> list[str]:
    seen: list[str] = []
    for value in values:
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= MAX_ID_LENGTH and value not in seen:
                seen.append(value)
    return seen if limit is None else seen[:limit]


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as chat_mute.py: flock where there is one, a
    # first-byte lock on Windows.
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
