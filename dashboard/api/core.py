"""What three or more features share: errors, rate limits, request bodies, the
profile a request acts for, and the plugin's own files.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import types
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.requests import HTTPConnection

from . import link_status, session_route

# The plugin's logger, under the name it always had (the module Hermes loads,
# plugin_api.py): every module logs through it.
logger = logging.getLogger(__package__.rpartition(".")[0] or __name__)


def _dashboard_dir() -> str:
    """dashboard/ (this file is dashboard/api/core.py)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plugin_root() -> str:
    """The plugin's folder, which dashboard/ is in."""
    return os.path.dirname(_dashboard_dir())


# --- The router, which every module adds its routes to ------------------------

async def _note_conduit_request(connection: HTTPConnection) -> None:
    """Every route's dependency: a request from Conduit counts as the app
    being seen, for the dashboard's Conduit card (see "Link status" in
    api/link_status.py). Defined before the router, which takes it."""
    link_status._note_app_request(connection)
    # Before the app can pick a model, its pick sticks (api/session_route.py).
    session_route.ensure_installed()


router = APIRouter(dependencies=[Depends(_note_conduit_request)])


# --- Errors, rate limits and a profile's scope --------------------------------

# Keys one limiter tracks at once. Keys are profile names a scope accepted,
# so this only bites a host with that many profiles busy in one window.
LIMITER_MAX_KEYS = 256
SEARCH_MAX_BODY_BYTES = 16 * 1024


class TokenError(Exception):
    """A token request failed. ``status`` is the HTTP status to return to Conduit;
    ``code``, when set, is the store's short machine word for the refusal."""

    def __init__(self, status: int, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class _MintLimiter:
    """Sliding-window cap per profile (token mints, web searches)."""

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        message: str = "Too many Gemini Live token requests; try again shortly",
        max_keys: int = LIMITER_MAX_KEYS,
    ) -> None:
        self.limit = limit
        self.message = message
        self.window_s = window_s
        self.clock = clock
        self.max_keys = max_keys
        self._mints: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, limit: Optional[int] = None) -> float:
        """Takes a slot and returns its mint (for ``release``). ``limit`` overrides
        the cap for this call (a cap read from the profile's config)."""
        limit = self.limit if limit is None else limit
        now = self.clock()
        with self._lock:
            # Drop buckets whose newest mint has aged out (or that are empty), so idle keys don't pile up.
            for stale in [k for k, q in self._mints.items() if not q or now - q[-1] >= self.window_s]:
                del self._mints[stale]
            if limit <= 0:
                raise TokenError(429, self.message)
            # A full map turns new keys away, so it stays bounded whatever
            # callers send; the dashboard's own profile ("") always fits.
            if key and key not in self._mints and len(self._mints) >= self.max_keys:
                raise TokenError(429, self.message)
            mints = self._mints.setdefault(key, deque())
            while mints and now - mints[0] >= self.window_s:
                mints.popleft()
            if len(mints) >= limit:
                raise TokenError(429, self.message)
            mints.append(now)
            return now

    def release(self, key: str, mint: float) -> None:
        """Gives back the slot ``acquire`` returned, for a request that failed
        before it cost anything. The other mints keep their own times."""
        with self._lock:
            mints = self._mints.get(key)
            if not mints:
                return
            try:
                mints.remove(mint)
            except ValueError:
                return  # already aged out
            if not mints:
                del self._mints[key]


def _limiter_key(profile: Optional[str]) -> str:
    # Same notion of "the dashboard's own profile" as Hermes' scope, so
    # ?profile=current / " " / "Current" can't each get a fresh window.
    name = (profile or "").strip().lower()
    return "" if name in ("", "current") else name


def _profile_scope(profile: Optional[str]):
    """Resolve .env/config for ``profile`` the way /api/audio/* does.

    Always enters Hermes' scope, even with no profile: once the dashboard has
    served any ``?profile=`` request it hosts several profiles, and an unscoped
    secret read then raises instead of reading the dashboard's own .env.
    Fails closed: a requested profile that can't be scoped must never fall back
    to the default profile's key.
    """
    try:
        from hermes_cli.web_server_profiles import _config_profile_scope
    except ImportError as exc:
        # Only a missing Hermes or profile module (tests, or a Hermes without
        # profiles, including a renamed private scope helper) falls back; a broken
        # import deeper inside Hermes must not silently read unscoped.
        if exc.name not in ("hermes_cli", "hermes_cli.web_server_profiles"):
            raise
        if not profile:
            return nullcontext()
        logger.warning("Cannot scope Gemini Live request to profile %r: profile scoping unavailable", profile)
        raise TokenError(503, "This Hermes version can't resolve per-profile keys")
    return _config_profile_scope(profile or None)


# --- Profiles this dashboard serves itself (hermes-conduit#1336) ---------------
#
# Hermes 0.22.3+ (PLG-3) sends a plugin request whose ?profile= names a profile
# other than the one the dashboard started with to that profile's own plugin
# host. The host carries plain HTTP only (a WebSocket gets HTTP 501) and none
# of this process's state: Local Live's key and calls, and the chats and jobs
# Watch calls use, the chats this process's gateway runs (takeover, Desktop
# views, predictions), and the room services and gateway restarts Linked Rooms'
# drafts and setup use. A workaround until Hermes carries them: Conduit names the
# profile as ?conduit_profile= on the routes that need this process, Hermes
# leaves the request here, and the route scopes itself to that profile, as it
# does for ?profile=, after the same consent check. Conduit sends it only to a
# plugin that lists the "dashboard-profiles" capability, and to the rooms
# routes only to one that lists "room-dashboard-profiles".

CONDUIT_PROFILE_PARAM = "conduit_profile"
CONDUIT_PROFILE_TICKET_REASON = "A Hermes Desktop ticket acts for its own profile only"


async def _conduit_route_profile(conn: HTTPConnection, profile: Optional[str]) -> Optional[str]:
    """The profile a route acts for: ?conduit_profile= when sent, else ``profile``
    (?profile=). Raises HTTPException when the request can't act for it."""
    named = conn.query_params.get(CONDUIT_PROFILE_PARAM)
    if named is None:
        return profile
    named = named.strip()
    if (profile or "").strip() and profile.strip() != named:
        raise HTTPException(status_code=400, detail="profile and conduit_profile name different profiles")
    if not named:
        return None
    # A native ticket is one profile's principal: it may name its own home, not another.
    ticket_home = _native_ticket_home(conn)
    if ticket_home is not None and not (ticket_home and _job_profile_on_home(named, ticket_home)):
        raise HTTPException(status_code=403, detail=CONDUIT_PROFILE_TICKET_REASON)
    if await _plugin_refused_on(named):
        raise HTTPException(status_code=404, detail="Plugin not found")
    return named


async def _conduit_route_profile_no_store(request: Request, profile: Optional[str]) -> Optional[str]:
    """``_conduit_route_profile`` for a route whose answers are never cached."""
    try:
        return await _conduit_route_profile(request, profile)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise


async def _plugin_refused_on(profile: str) -> bool:
    """Whether ``profile`` turned this plugin off: Hermes' own gate for a
    ?profile= request, asked in that profile's scope."""
    try:
        from hermes_cli.web_server_dashboard import _plugin_api_request_refused
    except ImportError as exc:
        # A Hermes without per-profile consent: ?profile= has no gate there either.
        if exc.name not in ("hermes_cli", "hermes_cli.web_server_dashboard"):
            raise
        return False
    try:
        # Hermes' scope is a context variable, so it holds across this await
        # for this request only; Hermes awaits the same check inside it.
        with _profile_scope(profile):
            return bool(await _plugin_api_request_refused(PLUGIN_NAME))
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except HTTPException:
        raise  # Hermes' own answer, such as 404 for a profile that doesn't exist
    except Exception:  # noqa: BLE001 — Hermes failing to read the profile; a retry may get past it
        logger.exception("Conduit couldn't ask Hermes whether profile %r allows the plugin", profile)
        raise HTTPException(status_code=503, detail="Hermes couldn't check this profile's plugins")


_Scoped = TypeVar("_Scoped")


async def _run_scoped(
    profile: Optional[str],
    fn: Callable[[], _Scoped],
    executor: Optional[ThreadPoolExecutor] = None,
) -> _Scoped:
    def scoped() -> _Scoped:
        with _profile_scope(profile):
            return fn()

    return await asyncio.get_running_loop().run_in_executor(executor, scoped)


def _limited(limiter: "_MintLimiter", profile: Optional[str], fn: Callable[[], _Scoped]) -> Callable[[], _Scoped]:
    """``fn`` behind a slot of ``limiter``, for ``_run_scoped``: the slot is
    taken inside the scope, so a ?profile= the scope refuses gets no bucket."""
    def run() -> _Scoped:
        limiter.acquire(_limiter_key(profile))
        return fn()

    return run


async def _scoped_acquire(limiter: "_MintLimiter", profile: Optional[str], key: Optional[str] = None) -> None:
    """A slot of ``limiter`` taken before a route reads its body, once
    Hermes' scope has accepted ``profile`` (an unknown one is refused there
    and never gets a bucket). ``key`` names the bucket when it isn't the
    profile's own."""
    bucket = _limiter_key(profile) if key is None else key
    try:
        await _run_scoped(profile, lambda: limiter.acquire(bucket))
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        # A broken scope is the host's failure, named like the routes' own.
        raise _unexpected("profile check", exc, feature="Conduit")


# Optional flags a Hermes may not know yet, by the name they're passed under.
_unknown_flags_logged: set = set()


def supported_flags(function: Callable[..., Any], flags: Dict[str, Any]) -> Dict[str, Any]:
    """``flags`` without the keyword arguments ``function`` doesn't take, so an
    older Hermes (its get_session_messages without ``inline_images``, say)
    answers with its own default instead of a TypeError on every call. Each
    flag it lacks is logged once per process."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return flags
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return flags
    missing = [name for name in flags if name not in parameters]
    for name in missing:
        key = f"{getattr(function, '__qualname__', function)}:{name}"
        if key not in _unknown_flags_logged:
            _unknown_flags_logged.add(key)
            logger.warning("Conduit: this Hermes' %s has no %s; calling it without", key.split(":")[0], name)
    return {name: value for name, value in flags.items() if name not in missing}


def _unexpected(route: str, exc: Exception, feature: str = "Gemini Live") -> HTTPException:
    # Name the failure so Conduit shows something more useful than a bare 500;
    # the message itself stays in the log since it can carry host details.
    logger.exception("%s %s route failed", feature, route)
    return HTTPException(status_code=500, detail=f"{feature} {route} failed on the host ({type(exc).__name__})",
                         headers={"Cache-Control": "no-store"})


async def _read_json_body(request: Request, max_bytes: int = SEARCH_MAX_BODY_BYTES) -> Any:
    no_store = {"Cache-Control": "no-store"}
    # Read with a hard cap, chunked bodies included, so an oversized body is
    # refused without buffering it.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > max_bytes:
        raise HTTPException(status_code=413, detail="Request body is too large", headers=no_store)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > max_bytes:
            raise HTTPException(status_code=413, detail="Request body is too large", headers=no_store)
    try:
        if len(raw) > 1_000_000:
            # A file upload's body: parse it off the event loop.
            return await asyncio.to_thread(json.loads, bytes(raw))
        return json.loads(bytes(raw) or b"{}")
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Request body is not JSON", headers=no_store)


# --- The pairing state --------------------------------------------------------

def _pairing_state_path() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "conduit-push.json"


def _load_pairing_state(path: Any) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError, RecursionError):
        return None
    if not isinstance(value, dict) or not value.get("credential"):
        return None
    return value


# A profile's pairings with its other devices (hermes-conduit#2758): files of
# conduit-push.json's shape, named by relay installation id, as client.py
# (DEVICES_DIRECTORY, other_devices) keeps them. The main pairing stays in
# conduit-push.json.
PAIRING_DEVICES_DIRECTORY = "conduit-push-devices"
_INSTALLATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _installation_key(installation_id: Any) -> Optional[str]:
    if not isinstance(installation_id, str):
        return None
    key = installation_id.strip().lower()
    return key if _INSTALLATION_ID.fullmatch(key) else None


def _device_pairing_path(installation_id: Any, main_path: Any = None) -> Any:
    """Where ``installation_id``'s pairing among the others is kept, beside
    ``main_path`` (this profile's main pairing by default); None for anything
    that isn't an installation id."""
    key = _installation_key(installation_id)
    if key is None:
        return None
    main_path = main_path if main_path is not None else _pairing_state_path()
    return main_path.parent / PAIRING_DEVICES_DIRECTORY / f"{key}.json"


def _device_pairings(main: Optional[Dict[str, Any]] = None, main_path: Any = None) -> list:
    """The other devices' pairings, the first paired first: (installation id,
    state). None while the main pairing is missing, and never the main
    device's own installation (client.other_devices). ``main_path``: the main
    pairing's file, beside which they're kept (this profile's by default)."""
    main_path = main_path if main_path is not None else _pairing_state_path()
    main = main if main is not None else _load_pairing_state(main_path)
    if not main:
        return []
    directory = main_path.parent / PAIRING_DEVICES_DIRECTORY
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        key = _installation_key(entry.name[:-5]) if entry.name.endswith(".json") else None
        if key is None or key == _installation_key(main.get("installation_id")):
            continue
        state = _load_pairing_state(entry)
        if state and _installation_key(state.get("installation_id")) == key:
            found.append((key, state))
    found.sort(key=lambda item: (str(item[1].get("paired_at") or ""), item[0]))
    return found


def _pairing_path_for(installation_id: Any) -> Any:
    """The file holding ``installation_id``'s pairing: the main one when it's
    that device's (or there's no other), else that device's own."""
    main_path = _pairing_state_path()
    main = _load_pairing_state(main_path)
    key = _installation_key(installation_id)
    if not main or key is None or _installation_key(main.get("installation_id")) == key:
        return main_path
    path = _device_pairing_path(key)
    return path if _load_pairing_state(path) is not None else main_path


# --- The calls store's home ---------------------------------------------------

_calls_store_lock = threading.Lock()


def _calls_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


# --- The plugin's own files, imported as a package ----------------------------

# health_store.py and health_tool.py in the plugin's folder, imported as one
# private package (its __init__ never runs, so nothing registers with Hermes
# here) so the tool's relative import finds the same store module.
DASHBOARD_PACKAGE = "conduit_push_dashboard_pkg"


def _plugin_package_module(name: str) -> Any:
    root = _plugin_root()
    with _calls_store_lock:
        if DASHBOARD_PACKAGE not in sys.modules:
            package = types.ModuleType(DASHBOARD_PACKAGE)
            package.__path__ = [root]
            sys.modules[DASHBOARD_PACKAGE] = package
    try:
        return importlib.import_module(f"{DASHBOARD_PACKAGE}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{DASHBOARD_PACKAGE}.{name}":
            raise
        raise TokenError(501, f"This plugin install is missing {name}.py; update the plugin") from None


# --- Hermes' stores (hermes_store.py) -----------------------------------------

_hermes_store_module: Any = None
_hermes_store_lock = threading.Lock()


def _hermes_store() -> Any:
    """hermes_store.py in the plugin's folder, loaded once by path: a profile's
    session store through whichever door this Hermes has (its storage
    interface, or the SessionDB of a release before it)."""
    global _hermes_store_module
    with _hermes_store_lock:
        if _hermes_store_module is None:
            import importlib.util

            path = os.path.join(_plugin_root(), "hermes_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_hermes_store", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"no loader for {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _hermes_store_module = module
        return _hermes_store_module


# --- CONDUIT_PUSH_PROXY (push_proxy.py) ------------------------------------------

_push_proxy_module: Any = None
_push_proxy_failed = False
_push_proxy_lock = threading.Lock()


def _push_proxy() -> Any:
    """push_proxy.py in the plugin's folder, loaded once by path (as relay_urls.py
    is); None when it can't be, which leaves every request as it was. A failed
    load is tried and logged once, not on every request."""
    global _push_proxy_module, _push_proxy_failed
    with _push_proxy_lock:
        if _push_proxy_module is None and not _push_proxy_failed:
            try:
                import importlib.util

                path = os.path.join(_plugin_root(), "push_proxy.py")
                spec = importlib.util.spec_from_file_location("conduit_push_dashboard_push_proxy", path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"no loader for {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:  # noqa: BLE001 - a missing or broken file must not stop any request
                _push_proxy_failed = True
                logger.warning("Couldn't load push_proxy.py; CONDUIT_PUSH_PROXY is ignored", exc_info=True)
                return None
            _push_proxy_module = module
        return _push_proxy_module


def _proxy_opener(default: Any, build: Callable[[Any], Any]) -> Any:
    """The opener for an HTTPS request: `default`, unless CONDUIT_PUSH_PROXY is set
    (then one `build` made around its ProxyHandler)."""
    module = _push_proxy()
    return default if module is None else module.opener(default, build)


def _proxy_websocket_kwargs(url: str, connect: Any) -> Dict[str, Any]:
    """Extra `websockets` connect arguments for CONDUIT_PUSH_PROXY: {} unless it's
    set and applies to this wss:// URL."""
    module = _push_proxy()
    return {} if module is None else module.websocket_kwargs(url, connect)


# --- The profile a request acts for -------------------------------------------

_NO_PRINCIPAL = object()


def _native_ticket_home(request: Any) -> Optional[str]:
    """The home a native-http ticket binds this request to, or None without one.

    Hermes redeems the gateway ticket on the request and stamps the grant
    (dashboard_auth/native_http.py): one profile's principal, whose
    ``profile_id`` is the home it was minted for. The dashboard's owner may
    run jobs on any profile; a ticket may not, and the grant's jobs would
    otherwise run after the request is gone, where no binding reaches. Empty
    means a ticket whose home this Hermes didn't say: name nothing. A hosted
    plugin host sees no principal and can't run jobs, so only this process's
    request needs the binding.
    """
    if request is None:
        return None
    grant = getattr(request.state, "native_http_principal", _NO_PRINCIPAL)
    if grant is _NO_PRINCIPAL:
        return None
    if not isinstance(grant, dict):
        # A stamp this plugin can't read is still a ticket: name nothing.
        return ""
    home = grant.get("profile_id")
    if not isinstance(home, str) or not home:
        return ""
    if Path(home).is_absolute():
        return home
    # A profile name rather than its home: Hermes' own resolution.
    try:
        return str(_profile_dir(home))
    except Exception:  # noqa: BLE001 — a name this Hermes can't place names nothing
        logger.warning("A native ticket names a profile this Hermes can't place")
        return ""


def _job_profile_on_home(name: str, ticket_home: str) -> bool:
    """Whether profile ``name``'s home is the native ticket's ``ticket_home``."""
    if not ticket_home:
        return False
    try:
        return _profile_dir(name).resolve() == Path(ticket_home).resolve()
    except Exception:  # noqa: BLE001 — an unresolvable home is on no home
        return False


def _profile_dir(name: str) -> Path:
    """Profile ``name``'s home as session.create binds it."""
    try:
        from hermes_cli.profiles import get_profile_dir
    except (ImportError, AttributeError):
        return _job_profile_home_guess(name)
    return Path(get_profile_dir(name))


def _job_profile_home_guess(name: str) -> Path:
    """Profile ``name``'s home on a Hermes without hermes_cli.profiles: the
    homes _hermes_homes() lists, "default" naming the root; names fold case
    as Hermes' normalize_profile_name does."""
    try:
        from hermes_constants import get_default_hermes_root
    except (ImportError, AttributeError):  # an older Hermes: one home
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home())
    else:
        root = Path(get_default_hermes_root())
    canon = name.strip().lower()
    return root if canon == "default" else root / "profiles" / canon


# --- The plugin's name --------------------------------------------------------

PLUGIN_NAME = "conduit_push"



# --- The gateway's session authority (hermes-conduit#1618) --------------------
#
# On the gateway's own listener (the Webapp) chats run on its session
# authority, not in the dashboard's TUI gateway: gateway_chats.py finds them
# and drives the plugin's own chats there. The dashboard's code reads it
# through these.

def _gateway_chats() -> Any:
    return _plugin_package_module("gateway_chats")


def _gateway_runner() -> Any:
    """The gateway serving this process's listener, or None (``hermes dashboard``):
    gateway_chats.runner(), read here so a dashboard never loads that module."""
    return getattr(sys.modules.get("gateway.run_api"), "_ACTIVE_RUNNER", None)


def _authority_session(session_id: Any) -> Optional[tuple]:
    return _gateway_chats().authority_session(session_id) if _gateway_runner() is not None else None


def _authority_agent(session_id: Any) -> Optional[tuple]:
    return _gateway_chats().authority_agent(session_id) if _gateway_runner() is not None else None


def _authority_scope(authority: Any) -> Any:
    return _gateway_chats().authority_scope(authority)


def _authority_session_api() -> Any:
    return _gateway_chats().session_api() if _gateway_runner() is not None else None


def _release_authority_link(transport: Any) -> None:
    if getattr(transport, "_conduit_authority_link", None) is not None:
        _gateway_chats().release(transport)
