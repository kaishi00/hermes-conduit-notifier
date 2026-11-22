"""Chats the user muted in Conduit: no "reply ready" or "turn failed" push
from them for a while (15 minutes, an hour, a day) or until unmuted.

Conduit's chat menu and a reply's notification offer Mute. The phone sends
the chat's own ids (its runtime id, its stored id and the stored ids of the
rows it continues, as ``chat_on_screen.py`` takes them) and how long:
``PUT /notifications/muted-chats``. The plugin's turn-end hooks skip the
reply and failure pushes of a muted chat, and of a chat that continues one
through compaction (``muted`` follows the lineage the hook passes in), so a
long chat stays quiet after Hermes compresses it. Approvals, questions and
calls still notify: something waits on the user. Cron jobs keep their own
switch (``cron_store.py``).

Muting is kept on the host, so it holds on every device paired with the
profile, and for sealed pushes, which the relay can't read.

State lives in the profile's ``conduit-muted-chats.json`` beside the pairing
state: a list of entries, each the chat's ids, when the mute ends (a Unix
time, or null for until unmuted) and when it was set. An entry past its end
is dropped on the next write and ignored before then. A file that can't be
read counts as nothing muted, so a fault here can only let a push through.

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

FILE_NAME = "conduit-muted-chats.json"
VERSION = 1
# More muted chats than anyone keeps; the oldest choices go first.
MAX_CHATS = 200
# The plugin's bounds for a chat's ids (chat_on_screen.py MAX_IDS, MAX_ID_LENGTH).
MAX_IDS = 16
MAX_ID_LENGTH = 200
# The longest timed mute: a year. Longer is "until unmuted" (null).
MAX_MUTE_S = 366 * 86400
_thread_lock = threading.Lock()


def parse(body: Any) -> tuple[list[str], float | None]:
    """(chat ids, mute seconds) from a request body; ValueError for one
    that isn't. ``mute_s`` null mutes until unmuted, 0 unmutes."""
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object with session_ids and mute_s")
    raw = body.get("session_ids")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_IDS:
        raise ValueError(f"session_ids must be a list of 1 to {MAX_IDS} chat ids")
    if any(not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_ID_LENGTH for value in raw):
        raise ValueError("session_ids must be chat ids")
    if "mute_s" not in body:
        raise ValueError("mute_s is required: seconds, null for until unmuted, or 0 to unmute")
    seconds = body.get("mute_s")
    if seconds is None:
        return _ids(raw), None
    # The range check alone also turns away nan, inf and an integer too large
    # for a float.
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds <= MAX_MUTE_S:
        raise ValueError(f"mute_s must be null or a number of seconds from 0 to {MAX_MUTE_S}")
    return _ids(raw), float(seconds)


class ChatMuteStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def chats(self) -> list[dict[str, Any]]:
        """The chats muted now, oldest choice first: their ids, when the
        mute ends (null: until unmuted) and the seconds left (null too)."""
        now = self.clock()
        return [_public(entry, now) for entry in self._load()["chats"] if _active(entry, now)]

    def muted(self, session_ids: Iterable[Any]) -> bool:
        """True when any of ``session_ids`` (a chat and the chats it
        continues) is in a mute that hasn't ended."""
        wanted = set(_ids(session_ids, limit=None))
        if not wanted:
            return False
        now = self.clock()
        return any(_active(entry, now) and wanted.intersection(entry["ids"]) for entry in self._load()["chats"])

    def set_mute(self, session_ids: Iterable[Any], seconds: float | None) -> list[dict[str, Any]]:
        """Mutes the chat ``session_ids`` names for ``seconds`` (None: until
        unmuted), or unmutes it (0); returns the chats muted now. A chat
        whose ids overlap an entry replaces it."""
        ids = _ids(session_ids)
        if not ids:
            raise ValueError("session_ids must be chat ids")
        if seconds is not None and (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                                    or not 0 <= seconds <= MAX_MUTE_S):
            raise ValueError(f"mute_s must be null or a number of seconds from 0 to {MAX_MUTE_S}")
        with self._locked() as state:
            now = self.clock()
            kept = [entry for entry in state["chats"] if _active(entry, now) and not set(ids).intersection(entry["ids"])]
            if seconds is None or seconds > 0:
                kept.append({"ids": ids, "until": None if seconds is None else now + float(seconds), "at": now})
            state["chats"] = kept[-MAX_CHATS:]
            return [_public(entry, now) for entry in state["chats"]]

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the choices it couldn't read, so it fails instead.
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
            before = _encoded(state)
            yield state
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _active(entry: dict[str, Any], now: float) -> bool:
    return entry["until"] is None or entry["until"] > now


def _public(entry: dict[str, Any], now: float) -> dict[str, Any]:
    until = entry["until"]
    return {"session_ids": list(entry["ids"]), "until": until,
            "remaining_s": None if until is None else max(0, int(math.ceil(until - now)))}


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:  # an integer too large for a float
        return None


def _normalized(raw: Any) -> dict[str, Any]:
    chats: list[dict[str, Any]] = []
    entries = raw.get("chats") if isinstance(raw, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("ids"), list):
                continue
            ids = _ids(entry["ids"])
            at = _number(entry.get("at"))
            if not ids or at is None:
                continue
            until = None
            if entry.get("until") is not None:
                until = _number(entry["until"])
                if until is None:
                    continue
            # A timed mute further out than one request can ask (an edited
            # file, a clock step) counts as nothing.
            if until is not None and until > at + MAX_MUTE_S:
                continue
            chats.append({"ids": ids, "until": until, "at": at})
    # Oldest choice first, so trimming drops the oldest.
    chats.sort(key=lambda entry: entry["at"])
    return {"version": VERSION, "chats": chats[-MAX_CHATS:]}


def _ids(values: Iterable[Any], limit: int | None = MAX_IDS) -> list[str]:
    seen: list[str] = []
    for value in values:
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= MAX_ID_LENGTH and value not in seen:
                seen.append(value)
    return seen if limit is None else seen[:limit]


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":")) + "\n"


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as cron_store.py: flock where there is one, a
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
