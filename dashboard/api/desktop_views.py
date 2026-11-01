"""Desktop views: the chats Hermes Desktop or the web dashboard has open."""

from __future__ import annotations

import atexit
import functools
import importlib.util
import inspect
import json
import math
import os
import sys
import tempfile
import threading
import time
import weakref
import contextlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core, link_status, pairing

logger = core.logger
router = core.router


# --- Desktop views (#454) ----------------------------------------------------
#
# Which chats Hermes Desktop (or the web dashboard) has open on this host, so
# Conduit can count them as read without writing Hermes' own read flag (that
# flag would light Desktop's unread dot on the chat it has open). Hermes has
# no hook for "a client opened a chat", so this wraps the gateway's
# session.activate / session.resume handlers in this process. That is an
# unofficial seam: every attribute is feature-detected, the wrapper never
# raises and always returns the handler's own response, and when something
# is missing (a Hermes refactor, plugins.isolation: host) the route says
# observing: false and Conduit keeps its own read tracking.
#
# Per chat it keeps ``opened_at`` (the newest open) and ``seen_through``: how
# long the chat then stayed the one that Desktop connection had selected. A
# connection's selection moves with its next open and ends when its socket
# closes; the host can't tell whether anyone is looking at the window.

DESKTOP_VIEW_METHODS = ("session.activate", "session.resume")
DESKTOP_VIEWS_FILE = "conduit-desktop-views.json"
DESKTOP_VIEWS_MAX = 2000
DESKTOP_VIEWS_FLUSH_DELAY_S = 2.0
DESKTOP_VIEWS_INSTALL_POLL_S = 1.0
DESKTOP_VIEWS_INSTALL_WINDOW_S = 600.0
# How often a selected chat's connection is checked: a chat stops counting as
# seen at most this long after its Desktop disconnects.
DESKTOP_VIEWS_SWEEP_S = 5.0
# Set on each wrapper (to the handler it wraps), so a second load of this
# module in the same process never wraps a wrapper.
_DESKTOP_VIEW_MARKER = "__conduit_desktop_view__"
# The hook a wrapper reports to: the newest load of this module takes over
# the wrappers an earlier load installed.
_DESKTOP_VIEW_OWNER = "__conduit_desktop_view_owner__"


def desktop_view_client(transport: Any, socket_type: Optional[type] = None) -> Optional[str]:
    """Returns "desktop" or "browser" for a browser engine's gateway socket, else None.

    Desktop's renderer and the web dashboard open /api/ws from a browser
    engine, whose User-Agent starts with Mozilla/ (Desktop's adds Electron/).
    Conduit's socket comes from URLSession and also names itself in
    X-Conduit-Client. Stdio, hosted rooms and this plugin's own job transport
    aren't WebSocket transports, so they never count: a socket is the
    gateway's when it is an instance of ``socket_type`` (Hermes'
    tui_gateway.ws.WSTransport, subclasses included), or else when its class
    is named WSTransport. The User-Agent is the client's own
    claim: this is best-effort sorting among clients that already hold a
    dashboard login, not proof of which app is on the other end.
    """
    if not _is_gateway_socket(transport, socket_type):
        return None
    return _client_from_headers(_transport_headers(transport))


def _client_from_headers(headers: Optional[Dict[str, Any]]) -> Optional[str]:
    if headers is None or headers.get("x-conduit-client") is not None:
        return None
    agent = str(headers.get("user-agent") or "")
    if not agent.startswith("Mozilla/"):
        return None
    return "desktop" if "Electron/" in agent else "browser"


def _is_gateway_socket(transport: Any, socket_type: Optional[type]) -> bool:
    """Hermes' WSTransport: an instance of the gateway's class (``socket_type``,
    a single class; subclasses included), or a class named WSTransport, which
    also covers sockets opened before the gateway's socket module was
    reloaded."""
    if transport is None:
        return False
    if isinstance(socket_type, type) and isinstance(transport, socket_type):
        return True
    return type(transport).__name__ == "WSTransport"


def _transport_headers(transport: Any) -> Optional[Dict[str, Any]]:
    """The WebSocket upgrade's headers with lower-cased names, or None."""
    try:
        headers = getattr(getattr(transport, "_ws", None), "headers", None)
        items = getattr(headers, "items", None)
        if not callable(items):
            return None
        lowered: Dict[str, Any] = {}
        for name, value in items():
            # Raw ASGI headers are latin-1 bytes pairs.
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            # The first of repeated headers, as Starlette's own get() reads.
            lowered.setdefault(str(name).lower(), value)
        return lowered
    except Exception:  # noqa: BLE001
        return None


def _desktop_views_key(home: Any) -> str:
    return os.path.realpath(str(home))


def _desktop_view_time(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:  # an int too large for a float
        return False


def _transport_closed(transport: Any) -> bool:
    """Hermes' WSTransport.closed; anything else counts as closed, so a
    selection is never held open on a socket whose state can't be read."""
    try:
        closed = getattr(transport, "closed", None)
    except Exception:  # noqa: BLE001 — a property that fails reads as closed
        return True
    return closed if isinstance(closed, bool) else True


def _conduit_socket_open(entry: Dict[str, Any]) -> bool:
    transport = entry["ref"]()
    return transport is not None and not _transport_closed(transport)


def _merge_desktop_view(current: Optional[Dict[str, Any]], newer: Dict[str, Any]) -> Dict[str, Any]:
    """Each time keeps its newest value; the client follows the newest open."""
    if current is None:
        return dict(newer)
    merged = dict(current)
    if "opened_at" in newer and newer["opened_at"] >= merged.get("opened_at", 0.0):
        merged["opened_at"] = newer["opened_at"]
        merged["client"] = newer.get("client", merged.get("client"))
    merged["seen_through"] = max(merged.get("seen_through", 0.0), newer.get("seen_through", 0.0))
    return merged


class _DesktopViewStore:
    """Per Hermes home, in ``<home>/conduit-desktop-views.json``. Records land
    in memory at once and reach disk in one debounced write, so a chat switch
    never waits on I/O."""

    def __init__(self, flush_delay: float = DESKTOP_VIEWS_FLUSH_DELAY_S) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Batches taken by a flush and not yet on disk; reads still see them.
        self._inflight: List[Dict[str, Dict[str, Dict[str, Any]]]] = []
        self._timer: Optional[threading.Timer] = None
        self._flush_delay = flush_delay

    def record(self, home: Any, stored_id: str, client: str, seen_through: float,
               opened_at: Optional[float] = None) -> None:
        entry: Dict[str, Any] = {"client": client, "seen_through": seen_through}
        if opened_at is not None:
            entry["opened_at"] = opened_at
        key = _desktop_views_key(home)
        with self._lock:
            views = self._pending.setdefault(key, {})
            views[stored_id] = _merge_desktop_view(views.get(stored_id), entry)
            if self._timer is None:
                self._timer = threading.Timer(self._flush_delay, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._inflight.append(pending)
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        try:
            for key, views in pending.items():
                try:
                    self._merge_to_disk(key, views)
                except Exception:  # noqa: BLE001 — a lost write only leaves a chat unread
                    logger.warning("Conduit: could not save Desktop views under %s", key, exc_info=True)
        finally:
            with self._lock:
                self._inflight = [batch for batch in self._inflight if batch is not pending]

    def read(self, home: Any) -> Dict[str, Dict[str, Any]]:
        key = _desktop_views_key(home)
        # Memory first, then the file: a flush that lands in between is then
        # in the file, and merging it twice changes nothing.
        with self._lock:
            batches = [dict(batch.get(key, {})) for batch in self._inflight]
            batches.append(dict(self._pending.get(key, {})))
        views = self._load(self._path(key))
        for batch in batches:
            views = self._merged(views, batch)
        return views

    @staticmethod
    def _path(key: str) -> str:
        return os.path.join(key, DESKTOP_VIEWS_FILE)

    @staticmethod
    def _load(path: str, *, for_write: bool = False) -> Dict[str, Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, ValueError, RecursionError):
            return {}
        except OSError:
            if for_write:
                raise
            return {}
        views = value.get("views") if isinstance(value, dict) else None
        if not isinstance(views, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for stored_id, entry in views.items():
            if not isinstance(stored_id, str) or not isinstance(entry, dict):
                continue
            times = [entry.get("opened_at"), entry.get("seen_through")]
            if not all(_desktop_view_time(stamp) for stamp in times):
                continue
            clean[stored_id] = {"opened_at": float(times[0]), "seen_through": float(times[1]),
                                "client": "browser" if entry.get("client") == "browser" else "desktop"}
        return clean

    @staticmethod
    def _merged(base: Dict[str, Dict[str, Any]], newer: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        merged = dict(base)
        for stored_id, entry in newer.items():
            merged[stored_id] = _merge_desktop_view(merged.get(stored_id), entry)
        # A pending focus change for a chat this file no longer has (pruned)
        # carries no open time; it is still at least seen until then.
        for entry in merged.values():
            entry.setdefault("opened_at", entry["seen_through"])
        if len(merged) > DESKTOP_VIEWS_MAX:
            newest = sorted(merged.items(), key=lambda item: item[1]["seen_through"], reverse=True)
            merged = dict(newest[:DESKTOP_VIEWS_MAX])
        return merged

    def _merge_to_disk(self, key: str, views: Dict[str, Dict[str, Any]]) -> None:
        path = self._path(key)
        # Path-generic: locks the sibling ".conduit-desktop-views.json.lock".
        with pairing._pairing_state_lock(Path(path)):
            merged = self._merged(self._load(path, for_write=True), views)
            # A fresh, exclusively created 0600 name, so nothing planted in
            # the home can redirect the write.
            fd, temp = tempfile.mkstemp(dir=key, prefix=f".{DESKTOP_VIEWS_FILE}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "views": merged}, handle, separators=(",", ":"))
                os.replace(temp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise


_desktop_selected_module: Any = None
_desktop_selected_lock = threading.Lock()


def _desktop_selected() -> Any:
    """desktop_selected.py in the plugin's folder, loaded once."""
    global _desktop_selected_module
    with _desktop_selected_lock:
        if _desktop_selected_module is None:
            path = os.path.join(core._plugin_root(), "desktop_selected.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_desktop_selected", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("This plugin install is missing desktop_selected.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _desktop_selected_module = module
        return _desktop_selected_module


class _DesktopViewHook:
    """Wraps the gateway's open handlers once ``tui_gateway.server`` is loaded,
    and follows which chat each Desktop connection has selected.

    The plugin loads before the gateway module (the dashboard imports it in
    its lifespan), so installation waits for it on a daemon thread, and every
    route call retries too. Never imports the gateway itself: a module this
    plugin imported would be a private copy whose chats no client uses.
    """

    def __init__(self, store: _DesktopViewStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._focus_lock = threading.Lock()
        # id(transport) -> the chat that connection has selected.
        self._focus: Dict[int, Dict[str, Any]] = {}
        # id(transport) -> {"ref", "home"} for Conduit's own gateway sockets
        # that opened a chat: the link status counts the open ones.
        self._conduit: Dict[int, Dict[str, Any]] = {}
        # Home key -> (selected stored ids, when written) last published for
        # the reply-ready push (#87, desktop_selected.py).
        self._published: Dict[str, Tuple[frozenset, float]] = {}
        self._publish_lock = threading.Lock()
        # Wakes the sweep early after an open, so a new selection is published
        # within moments rather than at the next 5 s sweep.
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.reason: Optional[str] = "waiting"
        # Set when a gateway socket's upgrade headers can't be read, which
        # leaves every open unsortable: reported, not silently ignored.
        self.headers_unreadable = False
        # tui_gateway.ws.WSTransport, found at install. Like headers_unreadable
        # it is read from handler threads without a lock: one attribute
        # read is atomic, and a stale value only affects that one open.
        self.socket_type: Optional[type] = None

    @property
    def installed(self) -> bool:
        return self.reason is None

    @property
    def observing(self) -> bool:
        return self.installed and not self.headers_unreadable

    def status(self) -> Tuple[bool, Optional[str]]:
        if self.installed and self.headers_unreadable:
            return False, "gateway-unsupported"
        return self.observing, self.reason

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(target=self._run, name="conduit-desktop-views", daemon=True)
            try:
                thread.start()
            except Exception:  # noqa: BLE001 — the next route call tries again
                logger.debug("Conduit: Desktop view watcher not started", exc_info=True)
                return
            self._thread = thread

    def _run(self) -> None:
        try:
            self._watch()
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    # The next route call starts a fresh watcher.
                    self._thread = None

    def try_install_quietly(self) -> bool:
        try:
            return self.try_install()
        except Exception:  # noqa: BLE001 — keep waiting for the gateway
            logger.debug("Conduit: Desktop view install failed", exc_info=True)
            return False

    def _watch(self) -> None:
        deadline = time.monotonic() + DESKTOP_VIEWS_INSTALL_WINDOW_S
        while not self.try_install_quietly():
            if time.monotonic() >= deadline:
                logger.info("Conduit: not marking chats Desktop opens as read here (%s)", self.reason)
                return
            time.sleep(DESKTOP_VIEWS_INSTALL_POLL_S)
        while True:
            self._wake.wait(DESKTOP_VIEWS_SWEEP_S)
            self._wake.clear()
            try:
                if self.installed:
                    self.verify()
                else:
                    self.try_install()  # a re-install that failed earlier
                self.sweep()
            except Exception:  # noqa: BLE001 — keep watching
                logger.debug("Conduit: Desktop view sweep failed", exc_info=True)

    def verify(self) -> None:
        """Re-installs if the gateway's handlers were replaced (a reload), and
        says so meanwhile instead of claiming to observe."""
        with self._lock:
            if not self.installed:
                return
            server = sys.modules.get("tui_gateway.server")
            methods = getattr(server, "_methods", None)
            intact = isinstance(methods, dict) and all(
                getattr(methods.get(name), _DESKTOP_VIEW_MARKER, None) is not None for name in DESKTOP_VIEW_METHODS)
            if intact:
                # The wrappers stayed; follow a reloaded socket module anyway.
                ws_transport = getattr(sys.modules.get("tui_gateway.ws"), "WSTransport", None)
                if isinstance(ws_transport, type):
                    self.socket_type = ws_transport
                return
            self.reason = "waiting"
        self.try_install()

    def try_install(self) -> bool:
        with self._lock:
            if self.installed:
                return True
            if core._gateway_runner() is not None:
                # The gateway's own listener (hermes-conduit#1618): its chats open
                # on the session authority, which says which clients view a chat
                # but not which one Desktop has selected, and the TUI gateway's
                # handlers loaded here never see an open. Not observing, said so.
                self.reason = "gateway-authority"
                return False
            server = sys.modules.get("tui_gateway.server")
            if server is None:
                # Under plugins.isolation: host this process never gets one.
                self.reason = "gateway-not-in-process"
                return False
            methods = getattr(server, "_methods", None)
            sessions = getattr(server, "_sessions", None)
            transports = sys.modules.get("tui_gateway.transport")
            current_transport = getattr(transports, "current_transport", None)
            # Opens are sorted by the WebSocket transport's class name.
            ws_transport = getattr(sys.modules.get("tui_gateway.ws"), "WSTransport", None)
            if (not isinstance(methods, dict) or not callable(getattr(sessions, "get", None))
                    or not callable(getattr(server, "_session_home", None))
                    or not callable(current_transport) or not isinstance(ws_transport, type)
                    or not all(callable(methods.get(name)) for name in DESKTOP_VIEW_METHODS)
                    # The wrapper reads the handler's return value: a
                    # coroutine handler would hand it an awaitable instead.
                    or any(inspect.iscoroutinefunction(methods[name]) for name in DESKTOP_VIEW_METHODS)):
                # Mid-import (the table fills as the gateway loads), or a
                # Hermes that moved these.
                self.reason = "gateway-unsupported"
                return False
            self.socket_type = ws_transport
            for name in DESKTOP_VIEW_METHODS:
                handler = methods[name]
                if getattr(handler, _DESKTOP_VIEW_MARKER, None) is None:
                    # In place: the dispatcher looks handlers up at call time.
                    methods[name] = self._wrap(handler, server, current_transport)
                else:
                    # An earlier load of this module wrapped it: opens now
                    # come here, where the route reads.
                    setattr(handler, _DESKTOP_VIEW_OWNER, self)
            self.reason = None
            logger.info("Conduit: marking chats Desktop opens as read")
            return True

    def _wrap(self, handler: Callable[..., Any], server: Any, current_transport: Callable[[], Any]) -> Callable[..., Any]:
        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            response = handler(*args, **kwargs)
            try:
                owner = getattr(wrapper, _DESKTOP_VIEW_OWNER, self)
                owner._observe(response, server, current_transport())
            except Exception:  # noqa: BLE001 — never fails the client's call
                logger.debug("Conduit: Desktop view not recorded", exc_info=True)
            return response

        setattr(wrapper, _DESKTOP_VIEW_MARKER, handler)
        setattr(wrapper, _DESKTOP_VIEW_OWNER, self)
        return wrapper

    def _observe(self, response: Any, server: Any, transport: Any) -> None:
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return
        if not _is_gateway_socket(transport, self.socket_type):
            return
        headers = _transport_headers(transport)
        readable = headers is not None
        if readable == self.headers_unreadable:
            logger.warning("Conduit: gateway socket headers %s",
                           "readable again" if readable else "unreadable; not marking Desktop's chats read")
        self.headers_unreadable = not readable
        if not readable:
            return
        client = _client_from_headers(headers)
        conduit = headers.get("x-conduit-client") is not None
        stored_id = str(result.get("session_key") or "").strip()
        if (client is None and not conduit) or not stored_id:
            return
        session = server._sessions.get(str(result.get("session_id") or ""))
        home_of = getattr(server, "_session_home", None)
        if not isinstance(session, dict) or not callable(home_of):
            return  # whose profile it is is unknown: a guess could mark another profile's chat
        home = home_of(session)
        if home is None:
            return
        if conduit:
            self._note_conduit(transport, home)
            return
        now = time.time()
        self.store.record(home, stored_id, client, now, opened_at=now)
        try:
            ref = weakref.ref(transport)
        except TypeError:
            return  # can't follow this socket's life: the open alone counts
        with self._focus_lock:
            previous = self._focus.get(id(transport))
            if previous is not None:
                # The connection moved on: its last chat was seen until now
                # (or, for a closed socket whose id was reused, its last check).
                seen_until = now if previous["ref"]() is transport else previous["alive_at"]
                self.store.record(previous["home"], previous["stored_id"], previous["client"], seen_until)
            self._focus[id(transport)] = {"ref": ref, "home": _desktop_views_key(home), "stored_id": stored_id,
                                          "client": client, "alive_at": now}
        self._wake.set()

    def _note_conduit(self, transport: Any, home: Any) -> None:
        """Follows a Conduit socket for the link status; it is seen now."""
        try:
            ref = weakref.ref(transport)
        except TypeError:
            ref = None  # can't follow this socket's life: the open alone counts
        if ref is not None:
            with self._focus_lock:
                self._conduit[id(transport)] = {"ref": ref, "home": _desktop_views_key(home)}
        link_status._link_status().app_seen(home)

    def conduit_sockets(self, home: Any) -> int:
        """Conduit's open gateway sockets in ``home`` (those that opened a chat)."""
        key = _desktop_views_key(home)
        with self._focus_lock:
            return sum(1 for entry in self._conduit.values()
                       if entry["home"] == key and _conduit_socket_open(entry))

    def _sweep_conduit(self, now: float) -> None:
        with self._focus_lock:
            for key, entry in list(self._conduit.items()):
                if not _conduit_socket_open(entry):
                    del self._conduit[key]
            homes = {entry["home"] for entry in self._conduit.values()}
        for home in homes:
            try:
                # Throttled there: one write a minute while the app stays connected.
                link_status._link_status().app_seen(home, now=now)
            except Exception:  # noqa: BLE001 — the card then says less
                logger.debug("Conduit: link status not recorded", exc_info=True)

    def sweep(self, now: Optional[float] = None) -> None:
        """Ends the selection of connections that closed, at their last check."""
        now = time.time() if now is None else now
        self._sweep_conduit(now)
        with self._focus_lock:
            for key, focus in list(self._focus.items()):
                transport = focus["ref"]()
                if transport is None or _transport_closed(transport):
                    self.store.record(focus["home"], focus["stored_id"], focus["client"], focus["alive_at"])
                    del self._focus[key]
                else:
                    focus["alive_at"] = now
            selection: Dict[str, set] = {}
            for focus in self._focus.values():
                selection.setdefault(focus["home"], set()).add(focus["stored_id"])
        self._publish_selection(selection, now)

    def _publish_selection(self, selection: Dict[str, set], now: float) -> None:
        """Writes each home's selected chats for the turn hooks (#87) when they
        change, and renews a selection before readers stop trusting it."""
        try:
            module = _desktop_selected()
        except Exception:  # noqa: BLE001 — a broken install publishes nothing; readers then push
            logger.debug("Conduit: desktop_selected.py unavailable", exc_info=True)
            return
        with self._publish_lock:
            for key in set(selection) | set(self._published):
                ids = frozenset(selection.get(key, ()))
                last = self._published.get(key)
                if last is not None and last[0] == ids and (not ids or now - last[1] < module.RENEW_S):
                    continue
                try:
                    module.publish(key, ids, now=now)
                except Exception:  # noqa: BLE001 — readers then see nothing selected; the next sweep retries
                    logger.debug("Conduit: could not publish Desktop's selected chats under %s", key, exc_info=True)
                    continue
                if ids:
                    self._published[key] = (ids, now)
                else:
                    self._published.pop(key, None)

    def read(self, home: Any) -> Dict[str, Dict[str, Any]]:
        """Saved views plus the chats Desktop has selected right now."""
        self.sweep()
        views = self.store.read(home)
        key = _desktop_views_key(home)
        with self._focus_lock:
            selected = [dict(focus) for focus in self._focus.values() if focus["home"] == key]
        for focus in selected:
            entry = _merge_desktop_view(views.get(focus["stored_id"]),
                                        {"client": focus["client"], "seen_through": focus["alive_at"]})
            entry.setdefault("opened_at", focus["alive_at"])
            entry["open"] = True
            views[focus["stored_id"]] = entry
        return views


_desktop_view_store = _DesktopViewStore()
_desktop_view_hook = _DesktopViewHook(_desktop_view_store)
# Conduit reads every 15 s per profile while its chat list is on screen.
_desktop_views_limiter = core._MintLimiter(60, 60.0, message="Too many Desktop view reads; try again shortly")
atexit.register(_desktop_view_store.flush)
if "hermes_cli.web_server" in sys.modules:
    # Loaded by the dashboard (not a test or the CLI): start watching for the
    # gateway module now rather than at Conduit's first read.
    _desktop_view_hook.ensure_started()


@router.get("/sessions/desktop-views")
async def get_desktop_views(request: Request, response: Response, profile: Optional[str] = None,
                            since: Optional[float] = None) -> Dict[str, Any]:
    """Chats Desktop or the web dashboard opened in ``profile``: per stored id,
    the newest open and how long it then stayed selected (seconds since the
    epoch), optionally only those seen after ``since``."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    _desktop_view_hook.ensure_started()
    _desktop_view_hook.try_install_quietly()  # a half-loaded gateway never fails the read

    def read() -> Dict[str, Dict[str, Any]]:
        _desktop_views_limiter.acquire(core._limiter_key(profile))
        from hermes_constants import get_hermes_home

        return _desktop_view_hook.read(get_hermes_home())

    try:
        views = await core._run_scoped(profile, read)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        # Hermes' own 400/404 for a bad or unknown profile, kept out of caches too.
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("read", exc, feature="Desktop views")
    if since is not None and math.isfinite(since):
        views = {stored_id: entry for stored_id, entry in views.items() if entry["seen_through"] > since}
    observing, reason = _desktop_view_hook.status()
    return {"ok": True, "observing": observing, "reason": reason, "views": views}
