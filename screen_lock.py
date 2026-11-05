"""One session at a time drives a bot's screen (Eric 2026-11-05).

Hermes' Bot Screen gives each bot (profile) one desktop: every session of the
bot, its Bot Chat, its workstreams and any other chat, clicks and types on the
same screen with ``computer_use``. Two of them at once fight over one mouse
and keyboard, so the plugin holds ``computer_use`` to one session per screen:

- The first session to call it takes the screen and keeps it until its turn
  ends (a turn's clicks are one piece of work), whatever tool calls it makes
  in between. A delegated child counts as the session that delegated it.
- Any other session's ``computer_use`` is refused, naming who has the screen.
  The browser tools aren't held: each session drives its own pages over
  DevTools, with no shared mouse or keyboard.
- A hold nobody renewed for ``HOLD_IDLE_S``, or whose process is gone, is
  over: a crash never keeps the screen.

Where a host has a real desktop (macOS, Windows, a Linux seat) and no Bot
Screen, every bot drives that one desktop, so the hold is the host's, kept
in the root home; otherwise it's the bot's, in its own home.

What the app shows keeps two things apart (Eric 2026-11-05): who holds the
screen now (``controlling``, only while the hold is live) and who used it
last (``last``), which says nothing about now. Hermes' own takeover lease
(a person on the screen) is separate and refuses the bot's screen tools
itself.

Failing open: this is coordination, not a security fence, so a file that
can't be read or written lets the call through (and logs it).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import client

logger = logging.getLogger("hermes.plugins.conduit_push")

FILE = "conduit-screen.json"
DESKTOP_TOOLS = frozenset({"computer_use"})
# A hold not renewed for this long is over (a turn that stalled or died
# without Hermes reporting its end).
HOLD_IDLE_S = 600.0
# Sessions told to wait, kept this long at most.
WAIT_KEEP_S = 2 * 3600.0
MAX_WAITING = 20
BOT = "bot"
HOST = "host"

_lock = threading.Lock()
# child session -> the session that delegated it (subagent_start)
_parents: dict[str, str] = {}
_HOST_NAME = socket.gethostname()

BLOCK = ("The screen is in use by {holder} (since {since}), and only one session drives it at a time, so this "
         "computer_use call didn't run. Browser tools still work. Do other steps meanwhile; if there's nothing "
         "else, end your turn and say you're waiting for the screen.{told}")
TOLD = " You'll get a message in this chat when it's free."


def child_started(child: Any, parent: Any) -> None:
    if isinstance(child, str) and child and isinstance(parent, str) and parent:
        with _lock:
            if len(_parents) > 2048:
                _parents.clear()
            _parents[child] = parent


def child_stopped(child: Any) -> None:
    if isinstance(child, str):
        with _lock:
            _parents.pop(child, None)


def owner_of(session_id: str) -> str:
    """The session whose turn this is: a delegated child's root."""
    seen = {session_id}
    with _lock:
        while session_id in _parents and len(seen) < 16:
            session_id = _parents[session_id]
            if session_id in seen:
                break
            seen.add(session_id)
    return session_id


def desktop_calls(tool_name: Any, args: Any) -> bool:
    """Whether this call drives the desktop: ``computer_use`` itself, or a
    ``tool_call`` that bridges it."""
    name = str(tool_name or "")
    if name in DESKTOP_TOOLS:
        return True
    if name == "tool_call":
        from . import pulse_tools

        return any(called in DESKTOP_TOOLS for called, _ in pulse_tools.called(args))
    return False


def _has_seat() -> bool:
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def scope() -> str:
    """Whose screen ``computer_use`` acts on in this profile: the bot's own
    (its Bot Screen, in a sandbox or on a headless host) or the host's real
    desktop, shared by every bot."""
    try:
        from tools.bot_desktop import placement, runtime
    except Exception:  # noqa: BLE001 — a Hermes without Bot Screen
        return HOST if _has_seat() else BOT
    try:
        if placement.resolve().where == placement.TERMINAL:
            return BOT
        if runtime.published_env().get("DISPLAY"):
            return BOT
    except Exception:  # noqa: BLE001
        logger.debug("conduit_push: couldn't read where the bot's screen runs", exc_info=True)
    return HOST if _has_seat() else BOT


def _paths(home: Path) -> list[tuple[str, Path]]:
    """The bot's own hold file, then the host's (one file on a default profile)."""
    own = _path(home, BOT)
    host = _path(home, HOST)
    return [(BOT, own)] + ([(HOST, host)] if host != own else [])


def _path(home: Path, where: str) -> Path:
    if where == HOST:
        from . import room_identity

        return room_identity.root_home(home) / FILE
    return Path(home) / FILE


@contextmanager
def _locked(path: Path) -> Iterator[dict]:
    with _lock, client.state_file_lock(path):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError):
            logger.warning("conduit_push: the screen hold file %s couldn't be read; starting it again", path)
            state = {}
        if not isinstance(state, dict):
            state = {}
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".conduit-screen-")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(state, file)
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return sys.platform.startswith("win")  # os.kill(pid, 0) isn't a probe there
    return True


def live(holder: Any, now: Optional[float] = None) -> bool:
    if not isinstance(holder, dict) or not holder.get("session_id"):
        return False
    now = time.time() if now is None else now
    if now - float(holder.get("renewed") or 0) > HOLD_IDLE_S:
        return False
    if holder.get("host") == _HOST_NAME and not _pid_alive(holder.get("pid")):
        return False
    return True


def _public(entry: Any) -> Optional[dict]:
    if not isinstance(entry, dict) or not entry.get("session_id"):
        return None
    return {key: entry.get(key) for key in ("session_id", "profile", "kind", "ref", "title", "label", "since", "at")
            if entry.get(key) not in (None, "")}


def claim(home: Path, session_id: str, who: dict, where: Optional[str] = None,
          now: Optional[float] = None) -> Optional[dict]:
    """Takes or renews the screen for ``session_id`` (already its owner).
    None when it's this session's; else the live holder that has it."""
    where = where or scope()
    path = _path(home, where)
    now = time.time() if now is None else now
    with _locked(path) as state:
        holder = state.get("holder")
        if live(holder, now) and holder.get("session_id") != session_id:
            return dict(holder)
        since = holder.get("since") if isinstance(holder, dict) and holder.get("session_id") == session_id \
            and live(holder, now) else now
        entry = {**who, "session_id": session_id, "since": since, "renewed": now, "pid": os.getpid(),
                 "host": _HOST_NAME}
        state["holder"] = entry
        state["last"] = {**who, "session_id": session_id, "at": now}
        waiting = [item for item in state.get("waiting") or [] if isinstance(item, dict)
                   and item.get("session_id") != session_id]
        state["waiting"] = waiting
    return None


def release(home: Path, session_id: str, now: Optional[float] = None) -> bool:
    """Its turn ended: the session lets the screen go. True when it held it."""
    now = time.time() if now is None else now
    released = False
    for _, path in _paths(home):
        if not path.exists():
            continue
        with _locked(path) as state:
            holder = state.get("holder")
            if isinstance(holder, dict) and holder.get("session_id") == session_id:
                state["holder"] = None
                last = state.get("last") if isinstance(state.get("last"), dict) else {}
                if last.get("session_id") == session_id:
                    state["last"] = {**last, "at": now}
                released = True
    return released


def wait(home: Path, session_id: str, who: dict, where: Optional[str] = None,
         now: Optional[float] = None) -> None:
    """A session refused the screen asks to be told when it's free."""
    where = where or scope()
    now = time.time() if now is None else now
    with _locked(_path(home, where)) as state:
        waiting = [item for item in state.get("waiting") or [] if isinstance(item, dict)
                   and item.get("session_id") != session_id and now - float(item.get("at") or 0) < WAIT_KEEP_S]
        waiting.append({**who, "session_id": session_id, "home": str(home), "at": now})
        # Past the cap the newest ask goes: the longest wait is woken first.
        state["waiting"] = waiting[:MAX_WAITING]


def take_next_waiting(home: Path, still_waits: Callable[[dict], bool], now: Optional[float] = None) -> Optional[dict]:
    """When the screen is free: the session that waited longest (and still
    does, by ``still_waits``), taken off the list. None while it's held."""
    now = time.time() if now is None else now
    for _, path in _paths(home):
        if not path.exists():
            continue
        with _locked(path) as state:
            if live(state.get("holder"), now):
                continue
            waiting = [item for item in state.get("waiting") or [] if isinstance(item, dict)
                       and now - float(item.get("at") or 0) < WAIT_KEEP_S]
            found = None
            kept = []
            for item in waiting:
                if found is None and item.get("home") == str(home):
                    if still_waits(item):
                        found = item
                    # Work that no longer waits (resolved, failed) leaves the list.
                    continue
                kept.append(item)
            state["waiting"] = kept
            if found is not None:
                return found
    return None


def snapshot(home: Path, now: Optional[float] = None) -> dict:
    """Who holds the bot's screen now and who used it last, for the app and
    the coordinator's note. The bot's own file first; a host-wide hold
    (a real desktop every bot shares) next."""
    now = time.time() if now is None else now
    out: dict = {"controlling": None, "last": None, "waiting": []}
    for where, path in _paths(home):
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        holder = state.get("holder")
        if out["controlling"] is None and live(holder, now):
            out["controlling"] = {**_public(holder), "scope": where}
        last = _public(state.get("last"))
        if last and (out["last"] is None or float(last.get("at") or 0) > float(out["last"].get("at") or 0)):
            out["last"] = {**last, "scope": where}
        out["waiting"] += [_public(item) for item in state.get("waiting") or []
                           if isinstance(item, dict) and item.get("home") == str(home)
                           and now - float(item.get("at") or 0) < WAIT_KEEP_S]
    return out


def since_text(when: Any) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(when)))
    except (TypeError, ValueError):
        return "a moment ago"


def block_message(holder: dict, *, told: bool) -> str:
    return BLOCK.format(holder=str(holder.get("label") or "another chat"), since=since_text(holder.get("since")),
                        told=TOLD if told else "")
