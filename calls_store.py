"""Hermes calls you (#449): this profile's call settings and job watches.

A watch says "call the user when this job ends". Conduit registers one when
a Live Voice call ends with a job the user asked to be called about; it is
keyed to the job's own Hermes session ids (runtime and stored), never to the
voice conversation. The agent's turn-end hooks fire it: the first turn end of
any of its sessions consumes it, under the file lock, so a replayed hook finds
nothing and the user is called at most once per watch.

State lives in the profile's ``conduit-calls.json`` beside the pairing state,
so watches survive gateway and dashboard restarts. The hooks (agent process)
and the dashboard routes (dashboard process) both use this module: the
dashboard loads it by path, so it imports nothing from the plugin package.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-calls.json"
VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    # Call when the user asked for it ("call me when it's done").
    "when_asked": True,
    "min_gap_s": 120,
    "per_hour": 6,
    "per_day": 20,
}
BOOL_SETTINGS = ("enabled", "when_asked")
# Inclusive bounds. per_day stops at the relay's own daily ceiling.
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "min_gap_s": (30, 3600),
    "per_hour": (1, 30),
    "per_day": (1, 60),
}

OUTCOMES = ("done", "failed", "stopped")
WATCH_TTL_S = 24 * 3600
MAX_WATCHES = 20
MAX_WATCH_SESSIONS = 4
MAX_TITLE_CHARS = 120
# Turn ends remembered so a watch that arrives just after its job ended still
# calls (the user hung up as the job finished). Only kept while calls are on.
RECENT_END_TTL_S = 30 * 60
MAX_RECENT_ENDS = 64
HOUR_S = 3600
DAY_S = 24 * 3600

_SESSION_ID = re.compile(r"^[A-Za-z0-9:_./-]{1,180}$")
_thread_lock = threading.Lock()


class CallStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # --- Settings ---------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        return dict(self._load()["settings"])

    def update_settings(self, changes: Any) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        for key, value in changes.items():
            if key in BOOL_SETTINGS:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
            elif key in INT_BOUNDS:
                low, high = INT_BOUNDS[key]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ValueError(f"{key} must be a whole number from {low} to {high}")
            else:
                raise ValueError(f"unknown setting {key}")
        with self._locked() as state:
            state["settings"].update(changes)
            return dict(state["settings"])

    # --- Watches ------------------------------------------------------------

    def add_watch(self, session_ids: Any, title: Any) -> dict[str, Any]:
        """Watches a job. ``{"status": "watching", "id"}``, or ``{"status":
        "ended", "outcome"}`` when the job's turn already ended (no watch is
        kept; the caller tells the user itself). Raises ValueError("calls_off")
        unless calls and "call when I ask" are on."""
        ids = _session_ids(session_ids)
        clean_title = _title(title)
        with self._locked() as state:
            settings = state["settings"]
            if not settings["enabled"] or not settings["when_asked"]:
                raise ValueError("calls_off")
            for end in state["ends"]:
                if end["session_id"] in ids:
                    return {"status": "ended", "outcome": end["outcome"]}
            if len(state["watches"]) >= MAX_WATCHES:
                raise ValueError("too_many")
            now = self.clock()
            watch = {
                "id": secrets.token_hex(12),
                "session_ids": ids,
                "title": clean_title,
                "created_at": now,
                "expires_at": now + WATCH_TTL_S,
            }
            state["watches"].append(watch)
            return {"status": "watching", "id": watch["id"]}

    def remove_watch(self, watch_id: Any) -> bool:
        if not isinstance(watch_id, str) or not watch_id:
            return False
        if not self.path.exists():
            return False
        with self._locked() as state:
            kept = [watch for watch in state["watches"] if watch["id"] != watch_id]
            removed = len(kept) != len(state["watches"])
            state["watches"] = kept
            return removed

    def watch_count(self) -> int:
        now = self.clock()
        return sum(1 for watch in self._load()["watches"] if watch["expires_at"] > now)

    # --- Firing -------------------------------------------------------------

    def fire(self, session_id: Any, outcome: str) -> dict[str, Any] | None:
        """A turn of ``session_id`` ended with ``outcome``.

        None when no watch covers the session. Otherwise the watch is
        consumed and the result says what to do: ``call`` (counted against the
        limits), ``limited`` (with ``reason`` gap, hour or day) or ``off``
        (calls were turned off after the watch was registered).
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome}")
        if not isinstance(session_id, str) or not session_id.strip() or not self.path.exists():
            return None
        session_id = session_id.strip()
        with self._locked() as state:
            now = self.clock()
            settings = state["settings"]
            if settings["enabled"]:
                ends = [end for end in state["ends"] if end["session_id"] != session_id]
                ends.append({"session_id": session_id, "outcome": outcome, "at": now})
                state["ends"] = ends[-MAX_RECENT_ENDS:]
            index = next((i for i, watch in enumerate(state["watches"]) if session_id in watch["session_ids"]), None)
            if index is None:
                return None
            watch = state["watches"].pop(index)
            result: dict[str, Any] = {"watch": watch, "outcome": outcome}
            if not settings["enabled"]:
                return {**result, "status": "off"}
            history = state["history"]
            if history and now - max(history) < settings["min_gap_s"]:
                return {**result, "status": "limited", "reason": "gap"}
            if sum(1 for at in history if now - at < HOUR_S) >= settings["per_hour"]:
                return {**result, "status": "limited", "reason": "hour"}
            if sum(1 for at in history if now - at < DAY_S) >= settings["per_day"]:
                return {**result, "status": "limited", "reason": "day"}
            history.append(now)
            return {**result, "status": "call"}

    # --- Storage ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            raw = None
        return _normalized(raw, self.clock())

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load()
            yield state
            self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _normalized(raw: Any, now: float) -> dict[str, Any]:
    """A well-formed state from whatever is on disk, with expired entries
    dropped. Anything malformed reads as its default."""
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULT_SETTINGS)
    stored = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    for key in BOOL_SETTINGS:
        if isinstance(stored.get(key), bool):
            settings[key] = stored[key]
    for key, (low, high) in INT_BOUNDS.items():
        value = stored.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            settings[key] = value
    watches = []
    for watch in raw.get("watches") if isinstance(raw.get("watches"), list) else []:
        try:
            if watch["expires_at"] > now:
                watches.append({
                    "id": str(watch["id"]),
                    "session_ids": _session_ids(watch["session_ids"]),
                    "title": _title(watch.get("title")),
                    "created_at": float(watch["created_at"]),
                    "expires_at": float(watch["expires_at"]),
                })
        except (KeyError, TypeError, ValueError):
            continue
    history = [float(at) for at in raw.get("history") if isinstance(at, (int, float)) and not isinstance(at, bool)
               and now - at < DAY_S] if isinstance(raw.get("history"), list) else []
    ends = []
    for end in raw.get("ends") if isinstance(raw.get("ends"), list) else []:
        if (isinstance(end, dict) and isinstance(end.get("session_id"), str) and end.get("outcome") in OUTCOMES
                and isinstance(end.get("at"), (int, float)) and now - end["at"] < RECENT_END_TTL_S):
            ends.append({"session_id": end["session_id"], "outcome": end["outcome"], "at": float(end["at"])})
    return {"v": VERSION, "settings": settings, "watches": watches, "history": history,
            "ends": ends[-MAX_RECENT_ENDS:]}


def _session_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("session_ids must be a list")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _SESSION_ID.match(item.strip()):
            raise ValueError("session_ids must be session ids")
        if item.strip() not in ids:
            ids.append(item.strip())
    if not ids:
        raise ValueError("session_ids must name the job's session")
    return ids[:MAX_WATCH_SESSIONS]


def _title(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:MAX_TITLE_CHARS]


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as client.state_file_lock: flock where there is one,
    # a first-byte lock on Windows.
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
