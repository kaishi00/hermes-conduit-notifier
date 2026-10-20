"""Where the message a reply answers came from, and which of those sources
get a "reply ready" push (#115).

Someone who also talks to Hermes in Desktop, a messaging app or the terminal
gets the reply there; a push from Conduit as well is noise. The user picks,
per profile in Conduit's Settings > Notifications, which sources notify.
Every source notifies until the user turns it off, so a host this file was
never written on behaves as before.

A reply's source is where the message that started its turn was sent, not
where the chat began: a Telegram chat continued in Conduit is a Conduit turn.
Hermes' app gateway binds each turn to the connection that submitted it last
before the turn started (a message queued meanwhile belongs to the next
turn), so the turn's hooks read that connection:
- Conduit's socket carries ``X-Conduit-Client`` (and the plugin's own Watch
  jobs say so on their transport): "conduit".
- A browser engine without that header (Hermes Desktop, the web dashboard):
  "desktop".
- No app connection: the turn's ``platform`` tells. ``cli`` is the terminal;
  a Hermes messaging platform (Telegram, Discord, Slack, a plugin platform…)
  is "messaging".
Anything else is unknown and pushes whatever is set, so a turn this can't
place, or a Hermes that changed, can only let a push through.

Only the reply-ready push follows this. Approvals, questions, failures,
background tasks, cron runs (they have their own switch) and calls don't.

The dashboard routes write ``conduit-notifications.json`` in the profile's
home; the hooks (agent process) only read it. The dashboard loads this file
by path, so it imports nothing from the plugin package.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-notifications.json"
VERSION = 1
SOURCES = ("conduit", "desktop", "messaging", "terminal")

# Hermes messaging platforms (gateway.config.Platform), for a Hermes whose
# own list can't be read. "local" is the app's own sessions, not messaging.
_MESSAGING_FALLBACK = frozenset({
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal", "mattermost", "matrix", "email",
    "sms", "dingtalk", "api_server", "webhook", "msgraph_webhook", "feishu", "wecom", "wecom_callback",
    "weixin", "bluebubbles", "qqbot", "yuanbao", "relay", "homeassistant", "teams",
})
_NOT_MESSAGING = frozenset({"", "local", "cli", "tui", "desktop", "cron"})
_thread_lock = threading.Lock()


class ReplySourcesStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def settings(self) -> dict[str, bool]:
        """Each source and whether its replies notify."""
        muted = self._load()
        return {source: source not in muted for source in SOURCES}

    def notifies(self, source: Any) -> bool:
        return source not in SOURCES or source not in self._load()

    def set_notify(self, source: Any, notify: Any) -> dict[str, bool]:
        if source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        if not isinstance(notify, bool):
            raise ValueError("notify must be true or false")
        # Dashboard processes sharing a home serialize the whole update.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            muted = self._load(for_write=True)
            muted.pop(source, None)
            if not notify:
                muted[source] = self.clock()
            if muted or self.path.exists():
                self._save({"version": VERSION, "muted_reply_sources": muted})
        return self.settings()

    def _load(self, *, for_write: bool = False) -> dict[str, float]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            return {}
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the choices it couldn't read, so it fails instead.
            if for_write:
                raise
            return {}
        entries = raw.get("muted_reply_sources") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return {}
        # A hand-edited time too big for a float reads as unset, not a crash.
        return {key: float(at) for key, at in entries.items()
                if key in SOURCES and isinstance(at, (int, float)) and not isinstance(at, bool) and abs(at) < 1e15}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                handle = os.fdopen(descriptor, "w", encoding="utf-8")
            except BaseException:
                os.close(descriptor)
                raise
            with handle:
                handle.write(json.dumps(state, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        if os.name == "posix":
            # Best effort, as the other stores: the rename itself survives a crash.
            try:
                directory = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):
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


def turn_source(platform: Any, transport: Any = None) -> str | None:
    """Where the message this turn answers was sent, or None when unknown."""
    if getattr(transport, "conduit_client", None) == "conduit":
        return "conduit"
    if _is_app_socket(transport):
        headers = _headers(transport)
        if headers is None:
            return None
        if str(headers.get("x-conduit-client") or "").strip().lower() == "conduit":
            return "conduit"
        if str(headers.get("user-agent") or "").startswith("Mozilla/"):
            return "desktop"
        return None
    name = platform.strip().lower() if isinstance(platform, str) else ""
    if name == "cli":
        return "terminal"
    return "messaging" if _is_messaging(name) else None


def _is_app_socket(transport: Any) -> bool:
    """Hermes' WSTransport: an instance of the gateway's class when it is
    loaded, or a class of that name (a socket opened before a reload)."""
    if transport is None:
        return False
    socket_type = getattr(sys.modules.get("tui_gateway.ws"), "WSTransport", None)
    if isinstance(socket_type, type) and isinstance(transport, socket_type):
        return True
    return type(transport).__name__ == "WSTransport"


def current_transport() -> Any:
    """The app connection Hermes bound to this turn, if Hermes' app gateway
    runs in this process. Never imports it: a copy this plugin imported
    would be a private one no client uses."""
    getter = getattr(sys.modules.get("tui_gateway.transport"), "current_transport", None)
    try:
        return getter() if callable(getter) else None
    except Exception:  # noqa: BLE001 — unknown: the push goes out
        return None


def current_platform() -> str | None:
    """The platform Hermes' messaging gateway bound to this turn (telegram,
    discord…), if its session context is loaded in this process. Tools get no
    platform of their own; never imports it, as above."""
    getter = getattr(sys.modules.get("gateway.session_context"), "get_session_env", None)
    try:
        return getter("HERMES_SESSION_PLATFORM", "") if callable(getter) else None
    except Exception:  # noqa: BLE001 — unknown
        return None


def _is_messaging(name: str) -> bool:
    if name in _NOT_MESSAGING:
        return False
    try:
        from gateway.config import Platform
    except Exception:  # noqa: BLE001 — an older or trimmed Hermes
        return name in _MESSAGING_FALLBACK
    try:
        # Plugin platforms (Buzz, IRC…) resolve too; anything else raises,
        # and a name this Hermes doesn't know is unknown here too.
        return Platform(name) is not None
    except Exception:  # noqa: BLE001
        return False


def _headers(transport: Any) -> dict[str, Any] | None:
    """The WebSocket upgrade's headers with lower-cased names, or None."""
    try:
        items = getattr(getattr(getattr(transport, "_ws", None), "headers", None), "items", None)
        if not callable(items):
            return None
        lowered: dict[str, Any] = {}
        for name, value in items():
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            lowered.setdefault(str(name).lower(), value)
        return lowered
    except Exception:  # noqa: BLE001
        return None
