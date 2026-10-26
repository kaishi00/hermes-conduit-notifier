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
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

STATE_DB = "state.db"
ROOM_SOURCE = "bot_room"
# A member whose room lives on another installation runs in room_<digest>
# (gateway/session_api.room_session_id); a local member's session has an
# ordinary id, so its stored source decides.
ROOM_SESSION_ID = re.compile(r"room_[0-9a-f]{32}")
# Compaction continuations and delegated children followed back to the
# room's own session.
LINEAGE_HOPS = 32
# Hermes' own budget for a read-only reader (hermes_state): a rollback-journal
# store keeps readers waiting while another process commits, and a WAL
# writer's checkpoint shows them "disk I/O error" for a few milliseconds.
READ_TIMEOUT = 5.0
READ_RETRIES, READ_RETRY_PAUSE = 3, 0.05
# Set on a MemoryManager while its session is a room's; a compaction that
# moves the session to a new id keeps it.
MARK = "_conduit_room_session"
HOME = "_conduit_room_home"
_GUARD_FLAG = "__conduit_room_guard__"

_lock = threading.Lock()
# The MemoryManager this load guards, and each method name -> (Hermes' own method, this load's guard)
_guarded_class: Any = None
_guarded: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {}
_odd_id_logged = False
_unread_count = 0


def is_room_session(session_id: Any, home: Any = None) -> bool:
    """Whether ``session_id`` is a hosted room member's session, or a
    compaction continuation of one. Anything unreadable counts as no: the
    guard only skips saving for a session it can tell is a room."""
    value = str(session_id or "")
    if not value:
        return False
    if ROOM_SESSION_ID.fullmatch(value):
        return True
    global _odd_id_logged
    if value.startswith("room_") and not _odd_id_logged:
        # Hermes may have changed how it names remote room sessions; then only
        # the stored source below still finds them.
        _odd_id_logged = True
        logger.warning("conduit_push: session %s looks like a group room's but isn't named like one", value)
    if home is None:
        return False
    for attempt in range(READ_RETRIES + 1):
        try:
            return _stored_room(Path(home) / STATE_DB, value)
        except Exception as error:  # noqa: BLE001 - the agent starts whatever this finds
            if attempt == READ_RETRIES or "disk i/o error" not in str(error).lower():
                _store_unread(value)
                return False
            time.sleep(READ_RETRY_PAUSE)
    return False


def _store_unread(session_id: str) -> None:
    """Warns on the first failure and every 100th after: the session then
    counts as an ordinary chat, so if it is a room its turns reach the memory
    provider, and an operator should see that."""
    global _unread_count
    _unread_count += 1
    if _unread_count % 100 == 1:
        logger.warning("conduit_push: couldn't read the session store to tell whether session %s is a group "
                       "room (%d times so far); if it is, its turns are saved into the memory provider",
                       session_id, _unread_count, exc_info=True)
    else:
        logger.debug("conduit_push: couldn't tell whether session %s is a group room", session_id, exc_info=True)


def _stored_room(path: Path, session_id: str) -> bool:
    if not path.exists():
        return False
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
        config = "model_config" if "model_config" in columns else "NULL"
        current = session_id
        for _ in range(LINEAGE_HOPS):
            row = conn.execute(f"SELECT source, parent_session_id, {config} FROM sessions WHERE id = ?",
                               (current,)).fetchone()
            if row is None:
                return False
            if str(row[0] or "").strip().lower() == ROOM_SOURCE or ROOM_SESSION_ID.fullmatch(current):
                return True
            parent = row[1]
            if not parent:
                return False
            # A compaction carries the room on, and so does a delegated child
            # (it works for the room's turn, as room_rules counts it); a branch
            # is a conversation of its own. Hermes ends the parent and writes
            # the child in one transaction, so a child is never seen half-made.
            if not _delegated(row[0], row[2]):
                ended = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
                if ended is None or ended[0] != "compression":
                    return False
            current = parent
        return False
    finally:
        conn.close()


def _delegated(source: Any, config: Any) -> bool:
    """Whether a session is a delegated child: Hermes marks one in its
    model_config (tools/delegate_tool), and gives one its subagent source."""
    try:
        marks = json.loads(config) if isinstance(config, str) and config else {}
    except (ValueError, RecursionError):
        marks = {}
    if not isinstance(marks, dict):
        marks = {}
    return not marks.get("_branched_from") and (
        bool(marks.get("_delegate_from")) or str(source or "").strip().lower() == "subagent")


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes runtime here
        return None


def _in_room(manager: Any, session_id: Any = "") -> bool:
    return bool(getattr(manager, MARK, False)) or bool(ROOM_SESSION_ID.fullmatch(str(session_id or "")))


def _guard(original: Callable[..., Any], wrapper: Callable[..., Any]) -> Callable[..., Any]:
    functools.update_wrapper(wrapper, original)
    setattr(wrapper, _GUARD_FLAG, True)
    return wrapper


# The wrappers take whatever Hermes passes and hand it on as it came, so a
# change to these signatures upstream can't break every chat.

def _initialize_all(original: Callable[..., Any]) -> Callable[..., Any]:
    def initialize_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Hermes binds a manager to the agent's session here; a room session's
        # row exists by then (the room's admission writes it).
        home = kwargs.get("hermes_home") or _hermes_home()
        setattr(self, HOME, home)
        setattr(self, MARK, is_room_session(kwargs.get("session_id", args[0] if args else ""), home))
        return original(self, *args, **kwargs)
    return _guard(original, initialize_all)


def _on_session_switch(original: Callable[..., Any]) -> Callable[..., Any]:
    def on_session_switch(self: Any, *args: Any, **kwargs: Any) -> Any:
        # A compaction continues the same conversation; /new, /resume and
        # /branch move the manager to the session they name. Hermes passes
        # reason by keyword (the method takes it through **kwargs).
        if kwargs.get("reason") != "compression":
            new_session_id = kwargs.get("new_session_id", args[0] if args else "")
            setattr(self, MARK, is_room_session(new_session_id, getattr(self, HOME, None) or _hermes_home()))
        return original(self, *args, **kwargs)
    return _guard(original, on_session_switch)


def _sync_all(original: Callable[..., Any]) -> Callable[..., Any]:
    def sync_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _in_room(self, kwargs.get("session_id")):
            return None
        return original(self, *args, **kwargs)
    return _guard(original, sync_all)


def _on_session_end(original: Callable[..., Any]) -> Callable[..., Any]:
    def on_session_end(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _in_room(self):
            return None
        return original(self, *args, **kwargs)
    return _guard(original, on_session_end)


def _on_pre_compress(original: Callable[..., Any]) -> Callable[..., Any]:
    def on_pre_compress(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Nothing for the summary, the same as a manager without providers;
        # the room's transcript still compresses.
        if _in_room(self):
            return ""
        return original(self, *args, **kwargs)
    return _guard(original, on_pre_compress)


_WRAPPERS = {
    "initialize_all": _initialize_all,
    "on_session_switch": _on_session_switch,
    "sync_all": _sync_all,
    "on_session_end": _on_session_end,
    "on_pre_compress": _on_pre_compress,
}


def install() -> bool:
    """Guards Hermes' MemoryManager, once per process. Each of the methods
    above that this Hermes has is guarded; one it lacks (a Hermes shaped
    differently) is named in the log, because whatever Hermes calls instead
    reaches the provider unguarded. False when there's nothing to guard."""
    global _guarded_class
    with _lock:
        if _guarded_class is not None:
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
            if callable(method):
                found[name] = method
        missing = sorted(set(_WRAPPERS) - set(found))
        if missing:
            logger.warning("conduit_push: Hermes' memory manager has no %s, so group room turns may be saved "
                           "into the memory provider the way Hermes sends them", ", ".join(missing))
        if not found:
            return False
        guards = {name: _WRAPPERS[name](method) for name, method in found.items()}
        for name, guard in guards.items():
            setattr(MemoryManager, name, guard)
        _guarded.update({name: (found[name], guard) for name, guard in guards.items()})
        _guarded_class = MemoryManager
        return True


def uninstall() -> None:
    """Puts Hermes' own methods back (the plugin unloads). A method another
    load of the plugin has guarded since is left to that load."""
    global _guarded_class
    with _lock:
        manager_class, _guarded_class = _guarded_class, None
        for name, (own, guard) in _guarded.items():
            if manager_class is not None and manager_class.__dict__.get(name) is guard:
                setattr(manager_class, name, own)
        _guarded.clear()
