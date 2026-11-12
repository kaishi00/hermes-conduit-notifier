"""Group room turns and Pulse's own turns stay out of the memory provider.

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
from what others say there. A host that keeps the plugin to Conduit's rooms
(room_settings.conduit_rooms_only, notifier #443) leaves other rooms' turns to
the provider, as Hermes alone would; a room whose id can't be read yet is
guarded.

Pulse's turns (designs/proactivity.md) are hidden asks the plugin sends into
the Pulse chat a few times a day, each with tens of thousands of characters of
context that mostly repeat the last one, and a short delivery check every few
hours. Hermes treated them as the user's: every one was retained and recalled
against, and a provider that reprocesses a session's document on each append
(Hindsight) did so for the whole, ever longer chat (notifier #633: about $2 a
day on the provider's model). Pulse keeps its own memory, so its turns reach
no provider: no retain, no recall and no turn tick for one, and the chat's
session end and pre-compress summary see the user's own turns there with
Pulse's left out. A Pulse ask is told apart as Pulse's note hook tells it
(pulse.pre_llm_call): it holds the whole text of an attempt this profile's
Pulse store sent.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import logging
import re
import sqlite3
import sys
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
# Hermes titles each member's session for a room "Group: <room id>", numbered
# " #N" when the title is taken (room_rules reads it the same way).
ROOM_TITLE = re.compile(r"Group: ([A-Za-z0-9][A-Za-z0-9._:-]{0,127})(?: #\d+)?")
# room_settings.py, when this module is loaded on its own (the tests).
SETTINGS_MODULE = "conduit_push_room_settings"
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
# Pulse's store in a profile's home (pulse_store.DB_NAME), and the tag every
# attempt's text holds (pulse_delivery.new_tag: "p", 8 hex digits, "-", the
# attempt's number). Every ask names Pulse (pulse.pre_llm_call reads only those).
PULSE_DB = "conduit-pulse.db"
PULSE_TAG = re.compile(r"\bp[0-9a-f]{8}-[0-9]+\b")
PULSE_NAME = "Pulse"
# A whole text shorter than this (a delivery check's line) is easy to quote,
# so only the ask itself matches it, by its digest.
QUOTABLE = 500

_lock = threading.Lock()
# The MemoryManager this load guards, and each method name -> (Hermes' own method, this load's guard)
_guarded_class: Any = None
_guarded: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {}
_odd_id_logged = False
_unread_count = 0
_pulse_unread_count = 0


def is_room_session(session_id: Any, home: Any = None) -> bool:
    """Whether ``session_id`` is a hosted room member's session, or a
    compaction continuation of one. Anything unreadable counts as no: the
    guard only skips saving for a session it can tell is a room."""
    value = str(session_id or "")
    if not value:
        return False
    remote = bool(ROOM_SESSION_ID.fullmatch(value))
    only = _conduit_rooms_only(home)
    if remote and not only:
        return True
    global _odd_id_logged
    if not remote and value.startswith("room_") and not _odd_id_logged:
        # Hermes may have changed how it names remote room sessions; then only
        # the stored source below still finds them.
        _odd_id_logged = True
        logger.warning("conduit_push: session %s looks like a group room's but isn't named like one", value)
    if home is None:
        return remote
    for attempt in range(READ_RETRIES + 1):
        try:
            room = _stored_room(Path(home) / STATE_DB, value)
        except Exception as error:  # noqa: BLE001 - the agent starts whatever this finds
            if attempt == READ_RETRIES or "disk i/o error" not in str(error).lower():
                if not remote:
                    _store_unread(value)
                return remote
            time.sleep(READ_RETRY_PAUSE)
            continue
        if room is None:
            return remote
        return not only or _settings().applies(room, home)
    return remote


def _settings() -> Any:
    """room_settings.py: the plugin's own, or loaded by path beside this
    file when this module was."""
    try:
        from . import room_settings

        return room_settings
    except ImportError:
        pass
    module = sys.modules.get(SETTINGS_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(SETTINGS_MODULE,
                                                      Path(__file__).resolve().with_name("room_settings.py"))
        if spec is None or spec.loader is None:
            raise ImportError("room_settings.py is missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module = sys.modules.setdefault(SETTINGS_MODULE, module)
    return module


def _conduit_rooms_only(home: Any) -> bool:
    """Whether the host keeps the plugin to Conduit's rooms; no when that
    can't be read, so every room is guarded as before."""
    try:
        return bool(_settings().conduit_rooms_only(home))
    except Exception:  # noqa: BLE001 - a broken install guards every room
        logger.debug("conduit_push: couldn't read the room settings", exc_info=True)
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


def _stored_room(path: Path, session_id: str) -> Optional[str]:
    """The room a session's turns are in, by its id ("" when no session on
    the way is titled yet, the title itself when it names no room), or None
    when it isn't a room's."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
        config = "model_config" if "model_config" in columns else "NULL"
        title = "title" if "title" in columns else "NULL"
        current = session_id
        room = ""
        for _ in range(LINEAGE_HOPS):
            row = conn.execute(f"SELECT source, parent_session_id, {config}, {title} FROM sessions WHERE id = ?",
                               (current,)).fetchone()
            if row is None:
                return None
            # The nearest title: a compaction's continuation carries the room's
            # forward, and a delegated child's own ("Subagent: ...") isn't it.
            # A title that names no room (a legacy Desktop group's display
            # name) is kept as it is, which is no Conduit room's id.
            if not room and isinstance(row[3], str) and row[3].strip() and not _delegated(row[0], row[2]):
                match = ROOM_TITLE.fullmatch(row[3])
                room = match.group(1) if match else row[3]
            if str(row[0] or "").strip().lower() == ROOM_SOURCE or ROOM_SESSION_ID.fullmatch(current):
                return room
            parent = row[1]
            if not parent:
                return None
            # A compaction carries the room on, and so does a delegated child
            # (it works for the room's turn, as room_rules counts it); a branch
            # is a conversation of its own. Hermes ends the parent and writes
            # the child in one transaction, so a child is never seen half-made.
            if not _delegated(row[0], row[2]):
                ended = conn.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
                if ended is None or ended[0] != "compression":
                    return None
            current = parent
        return None
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


def pulse_asks(home: Any, texts: list[Any]) -> list[bool]:
    """Which of ``texts`` are Pulse's own asks: each holds the whole text of
    an attempt the profile's Pulse store sent. Anything unreadable counts as
    no, so a turn is only kept from the provider when it is surely Pulse's."""
    flags = [False] * len(texts)
    tags = {index: set(PULSE_TAG.findall(text)) for index, text in enumerate(texts)
            if isinstance(text, str) and PULSE_NAME in text}
    tags = {index: found for index, found in tags.items() if found}
    if not tags or home is None:
        return flags
    path = Path(home) / PULSE_DB
    wanted = sorted(set().union(*tags.values()))
    for attempt in range(READ_RETRIES + 1):
        try:
            if not path.exists():
                return flags
            sent = _sent(path, wanted)
            break
        except Exception as error:  # noqa: BLE001 - the turn then goes to the provider, as Hermes sends it
            if attempt == READ_RETRIES or "disk i/o error" not in str(error).lower():
                _pulse_unread()
                return flags
            time.sleep(READ_RETRY_PAUSE)
    for index, found in tags.items():
        flags[index] = any(_sent_as(sent[tag], texts[index]) for tag in found if tag in sent)
    return flags


def _sent(path: Path, wanted: list[str]) -> dict[str, tuple[Any, Any]]:
    """Each wanted tag's stored text and digest, from Pulse's store, read-only."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(attempts)")}
        digest = "text_sha" if "text_sha" in columns else "''"
        sent: dict[str, tuple[Any, Any]] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            sent.update((tag, (text, sha)) for tag, text, sha in conn.execute(
                f"SELECT tag, text, {digest} FROM attempts WHERE tag IN ({','.join('?' * len(chunk))})",
                chunk).fetchall())
        return sent
    finally:
        conn.close()


def _sent_as(sent: tuple[Any, Any], text: str) -> bool:
    """Whether ``text`` is the attempt's ask. While Pulse keeps the attempt's
    whole text, the ask holds it. Once Pulse has cut a finished attempt to its
    first line (pulse_store.trim_finished), only the digest kept of the whole
    text can say, so a user quoting that first line isn't taken for Pulse (an
    attempt cut before digests were kept has none to match). A store Pulse
    hasn't opened since digests came matches by text, as before."""
    whole, digest = sent
    if not isinstance(whole, str) or not whole:
        return False
    # Hermes strips the text it hands a provider as the last message, so the
    # ask then holds the attempt's text without the whitespace around it. A
    # digest is of the stripped text (pulse_store.text_digest), or of the
    # whole one for an attempt stored before 0.153.4: once that one is cut,
    # the stripped ask matches it with the newline its text ended with put back.
    if not isinstance(digest, str) or not digest:
        return whole.strip() in text
    if digest in (_digest(whole), _digest(whole.strip())) and len(whole) >= QUOTABLE:
        return whole.strip() in text
    return digest in (_digest(text), _digest(text.strip()), _digest(text + "\n"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _pulse_unread() -> None:
    """Warns on the first failure and every 100th after: Pulse's turns then
    reach the memory provider as Hermes sends them, which an operator should see."""
    global _pulse_unread_count
    _pulse_unread_count += 1
    if _pulse_unread_count % 100 == 1:
        logger.warning("conduit_push: couldn't read Pulse's store to tell its turns from the user's (%d times "
                       "so far); until it can be read, Pulse's turns are saved into the memory provider",
                       _pulse_unread_count, exc_info=True)
    else:
        logger.debug("conduit_push: couldn't read Pulse's store", exc_info=True)


def _pulse_ask(manager: Any, text: Any) -> bool:
    return pulse_asks(_home(manager), [text])[0]


def _home(manager: Any) -> Any:
    return getattr(manager, HOME, None) or _hermes_home()


def without_pulse(home: Any, messages: Any) -> Any:
    """``messages`` with Pulse's turns left out: each of its asks and what
    followed it (the reply, its tool calls) up to the next user message. The
    list itself when nothing in it is Pulse's."""
    if not isinstance(messages, list) or not messages:
        return messages
    asks = [i for i, row in enumerate(messages) if isinstance(row, dict) and row.get("role") == "user"]
    flags = pulse_asks(home, [messages[i].get("content") for i in asks])
    if not any(flags):
        return messages
    pulse = {i for i, flag in zip(asks, flags) if flag}
    kept, skipping = [], False
    for index, row in enumerate(messages):
        if isinstance(row, dict) and row.get("role") == "user":
            skipping = index in pulse
        if not skipping:
            kept.append(row)
    return kept


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes runtime here
        return None


def _in_room(manager: Any, session_id: Any = "") -> bool:
    if getattr(manager, MARK, False):
        return True
    value = str(session_id or "")
    return bool(ROOM_SESSION_ID.fullmatch(value)) and is_room_session(value, _home(manager))


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


def _first(args: tuple, kwargs: dict, name: str) -> Any:
    return kwargs.get(name, args[0] if args else None)


def _replace_messages(self: Any, args: tuple, kwargs: dict, name: str = "messages") -> Optional[tuple]:
    """The call's arguments with Pulse's turns left out of its messages (and
    the same for ``evidence_messages``), or None when nothing is left of them."""
    home = _home(self)
    messages = _first(args, kwargs, name)
    kept = without_pulse(home, messages)
    if kept is not messages and not any(isinstance(row, dict) and row.get("role") != "system" for row in kept):
        return None  # nothing but the system prompt is left
    if name in kwargs:
        kwargs = {**kwargs, name: kept}
    elif args:
        args = (kept, *args[1:])
    if isinstance(kwargs.get("evidence_messages"), list):
        kwargs = {**kwargs, "evidence_messages": without_pulse(home, kwargs["evidence_messages"])}
    return args, kwargs


def _sync_all(original: Callable[..., Any]) -> Callable[..., Any]:
    def sync_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _in_room(self, kwargs.get("session_id")) or _pulse_ask(self, _first(args, kwargs, "user_content")):
            return None
        if isinstance(kwargs.get("messages"), list):
            # A turn of the user's own in the Pulse chat: Pulse's earlier turns
            # stay out. Hermes' sync_all takes messages by keyword only.
            kwargs = {**kwargs, "messages": without_pulse(_home(self), kwargs["messages"])}
        return original(self, *args, **kwargs)
    return _guard(original, sync_all)


def _prefetch_all(original: Callable[..., Any]) -> Callable[..., Any]:
    def prefetch_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Nothing recalled for a Pulse turn, as with no provider: its context
        # is Pulse's own, and the whole ask would be the query.
        if _pulse_ask(self, _first(args, kwargs, "query")):
            return ""
        return original(self, *args, **kwargs)
    return _guard(original, prefetch_all)


def _queue_prefetch_all(original: Callable[..., Any]) -> Callable[..., Any]:
    def queue_prefetch_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _pulse_ask(self, _first(args, kwargs, "query")):
            return None
        return original(self, *args, **kwargs)
    return _guard(original, queue_prefetch_all)


def _on_turn_start(original: Callable[..., Any]) -> Callable[..., Any]:
    def on_turn_start(self: Any, *args: Any, **kwargs: Any) -> Any:
        message = kwargs.get("message", args[1] if len(args) > 1 else None)
        if _pulse_ask(self, message):
            return None
        # A turn after a Pulse ask has it as the person's last message (Hermes
        # 0.23 passes it for a provider to redo the prefetch queued after it).
        # Pulse queued none, so the provider gets what a first turn gets.
        if _pulse_ask(self, kwargs.get("previous_message")):
            kwargs = {**kwargs, "previous_message": ""}
        return original(self, *args, **kwargs)
    return _guard(original, on_turn_start)


def _on_session_end(original: Callable[..., Any]) -> Callable[..., Any]:
    def on_session_end(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _in_room(self):
            return None
        call = _replace_messages(self, args, kwargs)
        if call is None:
            return None  # only Pulse's turns: nothing of the user's to extract
        return original(self, *call[0], **call[1])
    return _guard(original, on_session_end)


def _on_pre_compress(original: Callable[..., Any]) -> Callable[..., Any]:
    def on_pre_compress(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Nothing for the summary, the same as a manager without providers;
        # the room's transcript still compresses.
        if _in_room(self):
            return ""
        call = _replace_messages(self, args, kwargs)
        if call is None:
            return ""
        return original(self, *call[0], **call[1])
    return _guard(original, on_pre_compress)


_WRAPPERS = {
    "initialize_all": _initialize_all,
    "on_session_switch": _on_session_switch,
    "sync_all": _sync_all,
    "prefetch_all": _prefetch_all,
    "queue_prefetch_all": _queue_prefetch_all,
    "on_turn_start": _on_turn_start,
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
            logger.warning("conduit_push: Hermes' memory manager has no %s, so group room turns and Pulse's "
                           "turns may reach the memory provider the way Hermes sends them", ", ".join(missing))
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
