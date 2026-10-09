"""Hermes calls you (#449): this profile's call settings and job watches.

A watch says "call the user when this job ends". Conduit registers one as
soon as the user asks a Live Voice call for it ("call me when it's done"),
keyed to the job's own Hermes session ids (runtime and stored), never to the
voice conversation. The agent's turn-end hooks fire it: the first turn end of
any of its sessions consumes it, under the file lock, so a replayed hook finds
nothing and the user is called at most once per watch.

While the call is still going the watch is held: Conduit renews a short hold
during the call and releases it at hang-up. A job that ends while held is
told in the call itself (Conduit then removes the watch), or, when the call
ends without that, at release (Conduit tells the user itself) or once the
hold runs out because the phone went away (the hooks' delivery calls then).
So the call never depends on the phone reaching the host after hanging up.

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
# A hold covers a running call between Conduit's renewals.
MAX_HOLD_S = 600
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

    def add_watch(self, session_ids: Any, title: Any, hold_s: Any = 0, ended_within_s: Any = None) -> dict[str, Any]:
        """Watches a job, held for ``hold_s`` seconds while the user's call
        goes on. ``{"status": "watching", "id"}``, or ``{"status": "ended",
        "outcome"}`` when one of the job's sessions ended within the last
        ``ended_within_s`` seconds (how long ago the job's request went out,
        on the phone's clock, so an earlier turn of the same chat never
        counts): no watch is kept and the caller tells the user itself. An
        interrupted turn doesn't count: during a call that is the call putting
        a correction into the job, which goes on in a new turn. Raises ValueError("calls_off") unless calls and "call when I ask" are
        on."""
        ids = _session_ids(session_ids)
        clean_title = _title(title)
        hold = _seconds(hold_s, "hold_s", MAX_HOLD_S)
        within = None if ended_within_s is None else _seconds(ended_within_s, "ended_within_s", RECENT_END_TTL_S)
        with self._locked() as state:
            settings = state["settings"]
            if not settings["enabled"] or not settings["when_asked"]:
                raise ValueError("calls_off")
            now = self.clock()
            if within is not None:
                for end in state["ends"]:
                    if end["session_id"] in ids and end["outcome"] != "stopped" and now - end["at"] <= within:
                        return {"status": "ended", "outcome": end["outcome"]}
            if len(state["watches"]) >= MAX_WATCHES:
                raise ValueError("too_many")
            watch = {
                "id": secrets.token_hex(12),
                "session_ids": ids,
                "title": clean_title,
                "created_at": now,
                "expires_at": now + WATCH_TTL_S,
                "hold_until": now + hold if hold else 0.0,
                "pending": None,
            }
            state["watches"].append(watch)
            return {"status": "watching", "id": watch["id"]}

    def hold(self, watch_id: Any, hold_s: Any) -> dict[str, Any]:
        """Renews a watch's hold for ``hold_s`` seconds while the call goes
        on, or releases it with 0 at hang-up. Releasing a watch whose job
        ended during the hold answers ``{"status": "ended", "outcome"}`` and
        consumes it: the caller tells the user itself. Otherwise
        ``{"status": "watching"}``, or ``{"status": "gone"}`` for a watch that
        no longer exists (fired, removed or expired)."""
        hold = _seconds(hold_s, "hold_s", MAX_HOLD_S)
        if not isinstance(watch_id, str) or not watch_id or not self.path.exists():
            return {"status": "gone"}
        with self._locked() as state:
            now = self.clock()
            index = next((i for i, watch in enumerate(state["watches"]) if watch["id"] == watch_id), None)
            if index is None:
                return {"status": "gone"}
            watch = state["watches"][index]
            if hold:
                watch["hold_until"] = now + hold
                return {"status": "watching"}
            if watch["pending"] is not None:
                state["watches"].pop(index)
                return {"status": "ended", "outcome": watch["pending"]["outcome"]}
            watch["hold_until"] = 0.0
            return {"status": "watching"}

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

        None when no watch covers the session. A held watch keeps the first
        outcome and answers ``held`` (with ``until``): the call waits for the
        hold to end. An interrupted turn isn't kept while held: the call
        interrupts a job to put a correction into it, and the job goes on in
        a new turn (a job the user stops in the call has its watch removed).
        Otherwise the watch is consumed and the result says what
        to do: ``call`` (counted against the limits), ``limited`` (with
        ``reason`` gap, hour or day) or ``off`` (calls were turned off after
        the watch was registered).
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
            watch = state["watches"][index]
            if watch["hold_until"] > now:
                if watch["pending"] is None and outcome != "stopped":
                    watch["pending"] = {"outcome": outcome, "session_id": session_id}
                pending = watch["pending"]
                return {"watch": dict(watch), "outcome": pending["outcome"] if pending else None, "status": "held",
                        "until": watch["hold_until"]}
            state["watches"].pop(index)
            return _decide(state, watch, outcome, now)

    def fire_due(self) -> list[dict[str, Any]]:
        """Watches whose job ended while held and whose hold has run out
        (the call's phone went away without releasing them): consumed, each
        with the result ``fire`` would give, plus the ``session_id`` that
        ended."""
        if not self.path.exists():
            return []
        with self._locked() as state:
            now = self.clock()
            due = [watch for watch in state["watches"] if watch["pending"] is not None and watch["hold_until"] <= now]
            if not due:
                return []
            due_ids = {watch["id"] for watch in due}
            state["watches"] = [watch for watch in state["watches"] if watch["id"] not in due_ids]
            return [{**_decide(state, watch, watch["pending"]["outcome"], now),
                     "session_id": watch["pending"]["session_id"]} for watch in due]

    def next_due(self) -> float | None:
        """When the earliest hold over an ended job runs out, if any."""
        holds = [watch["hold_until"] for watch in self._load()["watches"] if watch["pending"] is not None]
        return min(holds) if holds else None

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


def _decide(state: dict[str, Any], watch: dict[str, Any], outcome: str, now: float) -> dict[str, Any]:
    """Whether a consumed watch calls, within the user's settings and limits."""
    settings = state["settings"]
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
                pending = watch.get("pending")
                if not (isinstance(pending, dict) and pending.get("outcome") in OUTCOMES
                        and isinstance(pending.get("session_id"), str)):
                    pending = None
                hold_until = watch.get("hold_until", 0.0)
                watches.append({
                    "id": str(watch["id"]),
                    "session_ids": _session_ids(watch["session_ids"]),
                    "title": _title(watch.get("title")),
                    "created_at": float(watch["created_at"]),
                    "expires_at": float(watch["expires_at"]),
                    "hold_until": float(hold_until) if isinstance(hold_until, (int, float)) and not isinstance(hold_until, bool) else 0.0,
                    "pending": {"outcome": pending["outcome"], "session_id": pending["session_id"]} if pending else None,
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


def _seconds(value: Any, name: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a whole number of seconds from 0 to {maximum}")
    return value


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
