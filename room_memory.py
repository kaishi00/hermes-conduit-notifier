"""Group room turns stay out of the memory provider (designs/cross-owner-agents).

A gateway-hosted room runs each member's turn on the host of that member's
owner, in a hidden session of its own (source ``bot_room``, titled
``Group: <room id>``). The turn's prompt is the room's new messages, written
by several people and bots, but Hermes hands it to the external memory
provider with no author, so Honcho, Hindsight and the rest file everyone's
words in the room as the owner's. Until Hermes tells providers about room
turns itself, the plugin keeps a room session's memory manager from saving
anything: the per-turn sync, the session-end extraction and the summary it
adds before compressing are skipped. Recall still runs, the way MEMORY.md
still loads: the agent knows its owner in the room, it just doesn't learn
from what others say there.
"""

from __future__ import annotations

import functools
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

STATE_DB = "state.db"
ROOM_SOURCE = "bot_room"
# A member whose room lives on another installation runs in room_<digest>
# (gateway/session_api.room_session_id); a local member's session has an
# ordinary id, so its stored source decides.
ROOM_SESSION_ID = re.compile(r"room_[0-9a-f]{32}")
# Compaction continuations followed back to the room's own session.
LINEAGE_HOPS = 32
# Set on a MemoryManager bound to a room session; it outlives a compaction
# that moves the session to a new id.
MARK = "_conduit_room_session"
_GUARD_FLAG = "__conduit_room_guard__"

_lock = threading.Lock()
_guarded: dict[str, Any] = {}  # "class" -> MemoryManager, method name -> Hermes' own method


def is_room_session(session_id: Any, home: Any = None) -> bool:
    """Whether ``session_id`` is a hosted room member's session, or a
    compaction continuation of one. Anything unreadable counts as no: the
    guard only skips saving for a session it can tell is a room."""
    value = str(session_id or "")
    if not value:
        return False
    if ROOM_SESSION_ID.fullmatch(value):
        return True
    if home is None:
        return False
    try:
        return _stored_room(Path(home) / STATE_DB, value)
    except Exception:  # noqa: BLE001 - the agent starts whatever this finds
        logger.debug("conduit_push: couldn't tell whether session %s is a group room", value, exc_info=True)
        return False


def _stored_room(path: Path, session_id: str) -> bool:
    if not path.exists():
        return False
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    try:
        current = session_id
        for _ in range(LINEAGE_HOPS):
            row = conn.execute("SELECT source, parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            if row is None:
                return False
            if str(row[0] or "").strip().lower() == ROOM_SOURCE or ROOM_SESSION_ID.fullmatch(current):
                return True
            parent = row[1]
            if not parent:
                return False
            # Only a compaction carries the room on; a branch or a delegation is
            # a conversation of its own.
            ended = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
            if ended is None or ended[0] != "compression":
                return False
            current = parent
        return False
    finally:
        conn.close()


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes runtime here
        return None


def _in_room(manager: Any, session_id: Any = "") -> bool:
    return bool(getattr(manager, MARK, False)) or bool(ROOM_SESSION_ID.fullmatch(str(session_id or "")))


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    def mark(wrapper: Callable[..., Any]) -> Callable[..., Any]:
        functools.update_wrapper(wrapper, original)
        setattr(wrapper, _GUARD_FLAG, True)
        return wrapper
    return mark


def _initialize_all(original: Callable[..., Any]) -> Callable[..., Any]:
    @_guard(original)
    def initialize_all(self: Any, session_id: Any, **kwargs: Any) -> Any:
        # Hermes binds a manager to one session here, once; a room session's
        # row exists by then (the room's admission writes it).
        if is_room_session(session_id, kwargs.get("hermes_home") or _hermes_home()):
            setattr(self, MARK, True)
        return original(self, session_id, **kwargs)
    return initialize_all


def _sync_all(original: Callable[..., Any]) -> Callable[..., Any]:
    @_guard(original)
    def sync_all(self: Any, user_content: Any, assistant_content: Any, *args: Any, **kwargs: Any) -> Any:
        if _in_room(self, kwargs.get("session_id")):
            return None
        return original(self, user_content, assistant_content, *args, **kwargs)
    return sync_all


def _on_session_end(original: Callable[..., Any]) -> Callable[..., Any]:
    @_guard(original)
    def on_session_end(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _in_room(self):
            return None
        return original(self, *args, **kwargs)
    return on_session_end


def _on_pre_compress(original: Callable[..., Any]) -> Callable[..., Any]:
    @_guard(original)
    def on_pre_compress(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Nothing for the summary, the same as a manager without providers;
        # the room's transcript still compresses.
        if _in_room(self):
            return ""
        return original(self, *args, **kwargs)
    return on_pre_compress


_WRAPPERS = {
    "initialize_all": _initialize_all,
    "sync_all": _sync_all,
    "on_session_end": _on_session_end,
    "on_pre_compress": _on_pre_compress,
}


def install() -> bool:
    """Guards Hermes' MemoryManager, once per process. False when this Hermes
    has no memory manager, or one shaped differently: then room turns are
    saved the way Hermes saves them, and the log says so."""
    with _lock:
        if _guarded:
            return True
        try:
            from agent.memory_manager import MemoryManager
        except Exception:  # noqa: BLE001 - no Hermes memory here (tests, the dashboard)
            logger.debug("conduit_push: no Hermes memory manager to guard", exc_info=True)
            return False
        found = {}
        for name in _WRAPPERS:
            method = getattr(MemoryManager, name, None)
            # A guard an earlier load of the plugin left behind wraps Hermes' own.
            while callable(method) and getattr(method, _GUARD_FLAG, False):
                method = getattr(method, "__wrapped__", None)
            found[name] = method
        missing = sorted(name for name, method in found.items() if not callable(method))
        if missing:
            logger.warning("conduit_push: Hermes' memory manager has no %s, so group room turns are saved into "
                           "the memory provider as Hermes sends them", ", ".join(missing))
            return False
        for name, wrap in _WRAPPERS.items():
            setattr(MemoryManager, name, wrap(found[name]))
        _guarded.update(found, **{"class": MemoryManager})
        return True


def uninstall() -> None:
    """Puts Hermes' own methods back (the plugin unloads)."""
    with _lock:
        manager_class = _guarded.pop("class", None)
        for name, method in _guarded.items():
            if manager_class is not None:
                setattr(manager_class, name, method)
        _guarded.clear()
