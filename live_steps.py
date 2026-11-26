"""What each running chat is doing now, for Conduit's bots lists (Eric, 2026-11-26).

Hermes streams a turn's tool progress only to the client attached to that
chat, and its live registry (``session.active_list``) says a chat is working
but not on what. The plugin's hooks see every turn of every chat, so they
note each running turn's step here: its phase (thinking, a tool, waiting on
an approval or on the user's answer) and, for a tool, its category, the same
coarse one the Lock Screen shows (live_activity.tool_category: never a tool's
name, arguments or output). Conduit and Conduit for desktop read it with GET
/live-steps and show the step under the bot's name ("Running a command").

The hooks run in the gateway; the route runs in the dashboard. So the steps
are published per Hermes home in ``conduit-live-steps.json``: the running
turns' steps by session id, and when the file was written. An app shows a
step only for a chat the live registry also says is working, so a file left
behind by a gateway that stopped mid-turn shows nothing; a reader drops a
file older than STALE_S too.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

FILE_NAME = "conduit-live-steps.json"
# A file this old is from a gateway that stopped: nothing in it is running.
STALE_S = 12 * 60 * 60.0
# At most this many running chats per home; the oldest step goes first.
MAX_SESSIONS = 64
PHASES = ("thinking", "tool", "approval", "input")

_lock = threading.Lock()
# home -> session id -> {"phase", "tool", "since"}; session id -> home.
_steps: dict[str, dict[str, dict[str, Any]]] = {}
_home_of: dict[str, str] = {}
# What each home's file says now, so an unchanged step writes nothing.
_written: dict[str, str] = {}


def started(home: Any, session_id: str, *, now: float | None = None) -> None:
    """A turn of ``session_id`` started in ``home``: it's thinking."""
    if not session_id or home is None:
        return
    key = str(home)
    with _lock:
        old = _home_of.get(session_id)
        if old is not None and old != key:
            _steps.get(old, {}).pop(session_id, None)
        _home_of[session_id] = key
        steps = _steps.setdefault(key, {})
        steps.pop(session_id, None)
        steps[session_id] = {"phase": "thinking", "tool": None, "since": time.time() if now is None else now}
        while len(steps) > MAX_SESSIONS:
            oldest = next(iter(steps))
            steps.pop(oldest)
            _home_of.pop(oldest, None)
        homes = {key} | ({old} if old is not None and old != key else set())
    for name in homes:
        _publish(name)


def step(session_id: str, phase: str, tool: str | None = None, *, now: float | None = None) -> None:
    """A running turn moved on: thinking, a tool (by category), or waiting on an approval or an answer.
    A session with no turn noted here (a fork, a delegated child) is left out."""
    if not session_id or phase not in PHASES:
        return
    with _lock:
        key = _home_of.get(session_id)
        entry = _steps.get(key or "", {}).get(session_id)
        if entry is None:
            return
        tool = tool if phase == "tool" else None
        if entry["phase"] == phase and entry["tool"] == tool:
            return
        entry.update(phase=phase, tool=tool, since=time.time() if now is None else now)
    _publish(key)


def ended(session_id: str) -> None:
    """The turn of ``session_id`` is over, however it ended."""
    if not session_id:
        return
    with _lock:
        key = _home_of.pop(session_id, None)
        if key is None or _steps.get(key, {}).pop(session_id, None) is None:
            return
    _publish(key)


def forget() -> None:
    """Forgets every step (the plugin was unloaded); the files go when the next turn writes."""
    with _lock:
        _steps.clear()
        _home_of.clear()
        _written.clear()


def _publish(key: str | None) -> None:
    if key is None:
        return
    with _lock:
        steps = dict(_steps.get(key, {}))
        body = json.dumps({"steps": steps, "at": time.time()}, separators=(",", ":"), sort_keys=True)
        signature = json.dumps(steps, sort_keys=True)
        if _written.get(key) == signature:
            return
        _written[key] = signature
    path = Path(key) / FILE_NAME
    if not steps:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    # A fresh, private (0600) temp file of its own, never one already there.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{FILE_NAME}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(body)
        os.replace(temporary, path)
    except BaseException:
        with _lock:
            _written.pop(key, None)  # the next change tries again
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def read(home: Any, *, now: float | None = None) -> dict[str, dict[str, Any]]:
    """The running chats' steps in ``home`` by session id: ``phase``, ``tool`` (a category, for a
    tool) and ``since`` (when the step began). Anything unreadable, or a stale file, is none."""
    try:
        raw = json.loads((Path(home) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), dict):
        return {}
    at = _number(raw.get("at"))
    now = time.time() if now is None else now
    if at is None or not 0 <= now - at <= STALE_S:
        return {}
    answer: dict[str, dict[str, Any]] = {}
    for session_id, entry in list(raw["steps"].items())[:MAX_SESSIONS]:
        if not isinstance(session_id, str) or not session_id or not isinstance(entry, dict):
            continue
        phase = entry.get("phase")
        if phase not in PHASES:
            continue
        tool = entry.get("tool")
        answer[session_id] = {"phase": phase, "tool": tool if phase == "tool" and isinstance(tool, str) else None,
                              "since": _number(entry.get("since"))}
    return answer
