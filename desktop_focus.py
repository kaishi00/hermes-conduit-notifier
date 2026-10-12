"""Chats on screen in a focused Hermes Desktop window (#87).

The plugin's Desktop half (``desktop/plugin.js``) reports, per window, the
chat that window shows while the window is in front, and renews it while it
stays there. A reply that finishes in that chat then sends no "reply ready"
push: the user is already reading it. Nothing else changes: other chats,
background jobs, approvals, questions and calls notify as before.

Each window writes its own small file under ``<home>/conduit-desktop-focus/``,
replaced whole and removed when the window loses focus, so two windows never
write the same file and no lock is needed. A report holds for ``hold_s``
seconds; a window that crashes or loses its connection stops counting once
its hold runs out. Anything that can't be read counts as "not on screen", so
a broken report can only ever let a push through, never hold one back.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DIR_NAME = "conduit-desktop-focus"
# Desktop renews every 20 s; a missed renewal or two still holds.
MAX_HOLD_S = 120
MAX_SESSION_IDS = 4
MAX_SESSION_ID_CHARS = 200
# More windows than anyone keeps open; the oldest reports beyond it are
# ignored (and their files removed on the next report).
MAX_CLIENTS = 16
CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def report(home: Any, client: Any, session_ids: Any, hold_s: Any, *, now: float | None = None) -> dict[str, Any]:
    """Records that window ``client`` shows one of ``session_ids`` (a chat's
    runtime and stored ids) in front for ``hold_s`` more seconds, or that it
    shows nothing in front (no ids, or a hold of 0). Raises ValueError on a
    malformed report."""
    if not isinstance(client, str) or not CLIENT_RE.match(client):
        raise ValueError("client must be 8 to 64 letters, digits, '-' or '_'")
    ids = _ids(session_ids)
    hold = _hold(hold_s)
    now = time.time() if now is None else now
    folder = Path(home) / DIR_NAME
    path = folder / f"{client}.json"
    if not ids or not hold:
        _remove(path)
        return {"status": "away"}
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune(folder, now, keep=path.name)
    temporary = folder / f".{client}.{os.getpid()}.{threading.get_ident()}.tmp"
    # Private from the first byte: it names the chat on screen.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"session_ids": ids, "until": now + hold}, handle, separators=(",", ":"))
    temporary.replace(path)
    return {"status": "focused"}


def on_screen(home: Any, session_id: Any, *, now: float | None = None) -> bool:
    """True while a focused Desktop window reports ``session_id`` on screen."""
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    target = session_id.strip()
    now = time.time() if now is None else now
    for state in _reports(Path(home) / DIR_NAME):
        if state["until"] > now and target in state["session_ids"]:
            return True
    return False


def _reports(folder: Path) -> Iterable[dict[str, Any]]:
    try:
        entries = sorted((entry for entry in folder.iterdir() if _is_report(entry)),
                         key=_mtime, reverse=True)[:MAX_CLIENTS]
    except OSError:
        return []
    states = []
    for entry in entries:
        state = _read(entry)
        if state is not None:
            states.append(state)
    return states


def _read(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    until = raw.get("until")
    ids = raw.get("session_ids")
    if not isinstance(until, (int, float)) or isinstance(until, bool) or not math.isfinite(until):
        return None
    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
        return None
    return {"until": float(until), "session_ids": ids}


def _prune(folder: Path, now: float, *, keep: str) -> None:
    """Drops run-out reports, and the oldest beyond ``MAX_CLIENTS``."""
    try:
        entries = sorted((entry for entry in folder.iterdir() if _is_report(entry) and entry.name != keep),
                         key=_mtime, reverse=True)
    except OSError:
        return
    for index, entry in enumerate(entries):
        state = _read(entry)
        if index >= MAX_CLIENTS - 1 or state is None or state["until"] <= now:
            _remove(entry)


def _is_report(entry: Path) -> bool:
    return entry.suffix == ".json" and bool(CLIENT_RE.match(entry.stem)) and entry.is_file()


def _mtime(entry: Path) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # gone already, or not ours to remove: it runs out anyway
        pass


def _ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SESSION_IDS:
        raise ValueError(f"session_ids must list at most {MAX_SESSION_IDS} chat ids")
    ids: list[str] = []
    for value in raw:
        if value is None:
            continue
        if not isinstance(value, str) or len(value.strip()) > MAX_SESSION_ID_CHARS:
            raise ValueError(f"session_ids must be strings of at most {MAX_SESSION_ID_CHARS} characters")
        if value.strip() and value.strip() not in ids:
            ids.append(value.strip())
    return ids


def _hold(raw: Any) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw) or raw < 0:
        raise ValueError(f"hold_s must be a number of seconds from 0 to {MAX_HOLD_S}")
    return float(min(raw, MAX_HOLD_S))
