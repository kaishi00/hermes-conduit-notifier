"""Conduit routes on the Hermes dashboard, mounted at /api/plugins/conduit_push/.

Gemini Live: Conduit talks to Gemini Live directly from the phone, but the
Gemini API key stays on this host. Conduit asks for a short-lived ephemeral
token per Live connection; the token is locked to one model, one use, and
must open its session within a minute.

Web search: Gemini Live's quick lookups can run on this host's own web
search backend (whatever `hermes tools` configured: SearXNG, Firecrawl,
Tavily…) instead of Google Search, which is metered separately.

Memory: Gemini Live can see what this host's Hermes remembers without Conduit
knowing the backend: the built-in MEMORY.md / USER.md snapshot plus whatever
external provider `memory.provider` names (Honcho, Mem0, …). Read-only.

Routes sit behind the dashboard's own auth, the same as /api/audio/*.
The API key is never returned, logged, or written anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_MODEL = "gemini-3.8-live"
# Ephemeral tokens are served on v1alpha only (google-genai 2.25 pins it and
# warns on anything else); the env override covers a later v1beta rollout.
DEFAULT_API_VERSION = "v1alpha"
API_VERSION_ENV_VAR = "CONDUIT_GEMINI_LIVE_API_VERSION"
# Same lookup order as Hermes' Gemini TTS provider.
API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
MODEL_ENV_VAR = "CONDUIT_GEMINI_LIVE_MODEL"
TOKEN_LIFETIME = timedelta(minutes=30)
NEW_SESSION_WINDOW = timedelta(minutes=1)
REQUEST_TIMEOUT_S = 15.0
# Conduit mints a token per Live connection (resuming a session doesn't use one up), so allow
# bursts, but stop a looping client from burning the host's Gemini quota.
MINT_LIMIT = 20
MINT_WINDOW_S = 60.0
# A voice lookup is one query; cap a looping client well above that.
SEARCH_LIMIT = 30
SEARCH_WINDOW_S = 60.0
SEARCH_MAX_RESULTS = 5
SEARCH_MAX_QUERY_CHARS = 500
SEARCH_MAX_SNIPPET_CHARS = 500
SEARCH_MAX_URL_CHARS = 2000
# Bounds the wait on a slow backend. Hermes' own provider timeouts end the
# worker thread; this only stops Conduit from waiting on it.
SEARCH_TIMEOUT_S = 20.0
# Searches get their own small pool, so a slow backend can hold at most this
# many threads and never starves the dashboard's shared executor.
SEARCH_WORKERS = 4
SEARCH_MAX_BODY_BYTES = 16 * 1024


class TokenError(Exception):
    """A token request failed. ``status`` is the HTTP status to return to Conduit."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _env_value(key: str) -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        logger.warning("hermes_cli.config unavailable; reading %s from the process environment", key)
        return os.environ.get(key)
    return get_env_value(key)


def resolve_api_key(get_env: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    get_env = get_env or _env_value
    for name in API_KEY_ENV_VARS:
        value = str(get_env(name) or "").strip()
        if value:
            return value
    return None


def resolve_model(get_env: Optional[Callable[[str], Optional[str]]] = None) -> str:
    get_env = get_env or _env_value
    value = str(get_env(MODEL_ENV_VAR) or "").strip()
    return value.removeprefix("models/") or DEFAULT_MODEL


def resolve_api_version(get_env: Optional[Callable[[str], Optional[str]]] = None) -> str:
    get_env = get_env or _env_value
    value = str(get_env(API_VERSION_ENV_VAR) or "").strip()
    return value if value in ("v1alpha", "v1beta", "v1") else DEFAULT_API_VERSION


def token_url(api_version: str) -> str:
    return f"https://generativelanguage.googleapis.com/{api_version}/auth_tokens"


def websocket_url(api_version: str) -> str:
    return (
        "wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.{api_version}.GenerativeService.BidiGenerateContentConstrained"
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_request_body(model: str, now: datetime) -> Dict[str, Any]:
    return {
        "uses": 1,
        "expireTime": _timestamp(now + TOKEN_LIFETIME),
        "newSessionExpireTime": _timestamp(now + NEW_SESSION_WINDOW),
        # The auth-token service takes the Live setup under this name (the SDK's
        # live_connect_constraints). fieldMask locks only the model; without it
        # Google locks the whole setup and Conduit couldn't send its tools.
        "bidiGenerateContentSetup": {"model": f"models/{model}"},
        "fieldMask": "model",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would resend the x-goog-api-key header to wherever it points.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _post_json(url: str, api_key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google's error body names the problem (bad key, quota) without echoing the key.
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise TokenError(502, f"Google rejected the token request ({exc.code}){': ' + detail if detail else ''}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # The reason can name proxies or hosts; keep it in the log, not the response.
        logger.warning("Gemini Live token request could not reach Google: %s", getattr(exc, "reason", exc))
        raise TokenError(502, "Could not reach Google")
    except ValueError:
        raise TokenError(502, "Google returned an unreadable token response")


def gemini_live_status(get_env: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    get_env = get_env or _env_value
    model = resolve_model(get_env)
    if resolve_api_key(get_env) is None:
        return {"available": False, "reason": "no_api_key", "model": model}
    return {"available": True, "model": model}


def _hermes_web_search(query: str, limit: int) -> str:
    from tools.web_tools import web_search_tool

    return web_search_tool(query, limit)


def _is_missing_web_tools(exc: ModuleNotFoundError) -> bool:
    # Only a Hermes without the web tool module; any other missing module
    # (a broken import deeper inside Hermes) is a real failure.
    return exc.name in ("tools", "tools.web_tools")


def _hermes_web_search_status() -> Dict[str, Any]:
    try:
        from tools.web_tools import check_web_api_key
    except ModuleNotFoundError as exc:
        if not _is_missing_web_tools(exc):
            raise
        return {"available": False, "reason": "unsupported"}
    if not check_web_api_key():
        return {"available": False, "reason": "not_configured"}
    backend: Optional[str] = None
    try:
        from agent.web_search_registry import get_active_search_provider

        provider = get_active_search_provider()
        backend = getattr(provider, "name", None)
    except Exception:  # noqa: BLE001 — the name is only a label
        logger.debug("Could not name the active web search backend", exc_info=True)
    return {"available": True, "backend": backend}


def web_search_status(probe: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Whether this host can answer Conduit's quick lookups with its own backend."""
    return (probe or _hermes_web_search_status)()


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Hermes' own configuration messages, which tell the user what to fix and
# carry no backend output. Anything else a backend says stays in the log:
# its errors can quote request URLs, hosts or keys in any shape.
_SHAREABLE_ERRORS = (
    re.compile(r"^No web (?:search )?provider configured\b[^\n]{0,200}$"),
    re.compile(r"^[A-Z][A-Z0-9_]{2,63} is not set\b[^\n]{0,200}$"),
)


def _user_error(error: Any) -> str:
    text = str(error or "").strip()[:400]
    if any(pattern.match(text) for pattern in _SHAREABLE_ERRORS) and "://" not in text:
        return _clip(text, 300)
    return "The web search backend failed; the Hermes log has the details"


def run_web_search(
    query: Any,
    limit: Any = 3,
    search: Optional[Callable[[str, int], str]] = None,
    limiter_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Search with the host's configured backend; titles, URLs and snippets only."""
    raw_query = str(query or "")
    # Bounded before normalizing, so a huge body isn't tokenized first.
    if len(raw_query) > SEARCH_MAX_QUERY_CHARS * 4:
        raise TokenError(400, f"query is longer than {SEARCH_MAX_QUERY_CHARS} characters")
    query = " ".join(raw_query.split())
    if not query:
        raise TokenError(400, "query is required")
    if len(query) > SEARCH_MAX_QUERY_CHARS:
        raise TokenError(400, f"query is longer than {SEARCH_MAX_QUERY_CHARS} characters")
    try:
        limit = min(max(int(limit), 1), SEARCH_MAX_RESULTS)
    except (TypeError, ValueError, OverflowError):
        limit = 3
    if limiter_key is not None:
        _search_limiter.acquire(limiter_key)
    try:
        raw = (search or _hermes_web_search)(query, limit)
    except ModuleNotFoundError as exc:
        if not _is_missing_web_tools(exc):
            raise
        raise TokenError(503, "This Hermes version has no web search tool")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise TokenError(502, "The web search backend returned an unreadable response")
    if not isinstance(payload, dict) or not payload.get("success", False):
        error = payload.get("error") if isinstance(payload, dict) else None
        logger.warning("Web search for Conduit failed on the backend: %s", error)
        raise TokenError(502, _user_error(error))
    data = payload.get("data") or {}
    items = data.get("web") if isinstance(data, dict) else None
    results = []
    for item in items if isinstance(items, list) else []:
        url = str(item.get("url") or "").strip() if isinstance(item, dict) else ""
        if not url.lower().startswith(("http://", "https://")) or len(url) > SEARCH_MAX_URL_CHARS:
            continue
        results.append({
            "title": _clip(item.get("title"), 200),
            "url": url,
            "snippet": _clip(item.get("description"), SEARCH_MAX_SNIPPET_CHARS),
        })
        if len(results) >= limit:
            break
    return {"query": query, "results": results}


def mint_gemini_live_token(
    get_env: Optional[Callable[[str], Optional[str]]] = None,
    post: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    limiter_key: Optional[str] = None,
) -> Dict[str, Any]:
    get_env = get_env or _env_value
    post = post or _post_json
    api_key = resolve_api_key(get_env)
    if api_key is None:
        raise TokenError(503, "GEMINI_API_KEY is not set on this Hermes host")
    if limiter_key is not None:
        # Counted only once the profile resolved and has a key, i.e. for requests that reach Google.
        _mint_limiter.acquire(limiter_key)
    model = resolve_model(get_env)
    now = now or datetime.now(timezone.utc)
    api_version = resolve_api_version(get_env)
    body = token_request_body(model, now)
    payload = post(token_url(api_version), api_key, body)
    token = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise TokenError(502, "Google's token response had no token")
    return {
        "token": token,
        # Google may clamp the requested window; report what it granted.
        "expires_at": payload.get("expireTime") or body["expireTime"],
        "new_session_expires_at": payload.get("newSessionExpireTime") or body["newSessionExpireTime"],
        "model": model,
        "websocket_url": websocket_url(api_version),
    }


class _MintLimiter:
    """Sliding-window cap per profile (token mints, web searches)."""

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        message: str = "Too many Gemini Live token requests; try again shortly",
    ) -> None:
        self.limit = limit
        self.message = message
        self.window_s = window_s
        self.clock = clock
        self._mints: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            # Drop buckets whose newest mint has aged out, so idle keys don't pile up.
            for stale in [k for k, q in self._mints.items() if now - q[-1] >= self.window_s]:
                del self._mints[stale]
            mints = self._mints.setdefault(key, deque())
            while mints and now - mints[0] >= self.window_s:
                mints.popleft()
            if len(mints) >= self.limit:
                raise TokenError(429, self.message)
            mints.append(now)


_mint_limiter = _MintLimiter(MINT_LIMIT, MINT_WINDOW_S)
_search_limiter = _MintLimiter(SEARCH_LIMIT, SEARCH_WINDOW_S, message="Too many web searches; try again shortly")


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


async def _run_scoped(
    profile: Optional[str],
    fn: Callable[[], Dict[str, Any]],
    executor: Optional[ThreadPoolExecutor] = None,
) -> Dict[str, Any]:
    def scoped() -> Dict[str, Any]:
        with _profile_scope(profile):
            return fn()

    return await asyncio.get_running_loop().run_in_executor(executor, scoped)


_search_executor = ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="conduit-web-search")


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
        return json.loads(bytes(raw) or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Request body is not JSON", headers=no_store)


@router.get("/gemini-live/status")
async def get_gemini_live_status(profile: Optional[str] = None) -> Dict[str, Any]:
    try:
        return {"ok": True, **(await _run_scoped(profile, gemini_live_status))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except HTTPException:
        raise  # Hermes' own 400/404 for a bad or unknown profile
    except Exception as exc:
        raise _unexpected("status", exc)


@router.post("/gemini-live/token")
async def create_gemini_live_token(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body is a usable credential: keep it out of any cache on the way.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        result = await _run_scoped(profile, lambda: mint_gemini_live_token(limiter_key=_limiter_key(profile)))
    except TokenError as exc:
        logger.warning("Gemini Live token request failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("token", exc)
    return {"ok": True, **result}


@router.get("/web-search/status")
async def get_web_search_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        # A quick local check (no network), so it stays off the search pool
        # and answers even while searches are backed up.
        return {"ok": True, **(await _run_scoped(profile, web_search_status))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("status", exc, feature="Web search")


@router.post("/web-search")
async def post_web_search(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request)
    query = body.get("query") if isinstance(body, dict) else None
    limit = body.get("limit", 3) if isinstance(body, dict) else 3
    try:
        result = await asyncio.wait_for(
            _run_scoped(profile, lambda: run_web_search(query, limit, limiter_key=_limiter_key(profile)), _search_executor),
            timeout=SEARCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Web search for Conduit timed out after %ss", SEARCH_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Web search timed out", headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        logger.warning("Web search for Conduit failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("request", exc, feature="Web search")
    return {"ok": True, **result}


# --- Memory -----------------------------------------------------------------
#
# Read-only view of the host's memory for Gemini Live: never sync_turn, never
# a write. The built-in store is read fresh per request (it is a couple of
# small files); an external provider is initialized once per profile and kept,
# since initializing one can open network clients.

MEMORY_CONTEXT_MAX_CHARS = 8000
MEMORY_RECALL_MAX_CHARS = 4000
MEMORY_MAX_QUERY_CHARS = 500
# A voice turn recalls at most once; cap a looping client well above that.
MEMORY_RECALL_LIMIT = 30
MEMORY_RECALL_WINDOW_S = 60.0
# Bounds the wait on a slow provider; the worker thread runs on until the
# provider's own call returns.
MEMORY_TIMEOUT_S = 10.0
MEMORY_WORKERS = 4
MEMORY_MAX_BODY_BYTES = 16 * 1024
# A stable session, so providers that key recall state per session reuse it.
MEMORY_SESSION_ID = "conduit-voice"
MEMORY_PLATFORM = "conduit_voice"
# An unavailable or failed provider is retried after this long, so fixing its
# credentials doesn't need a dashboard restart.
MEMORY_PROVIDER_RETRY_S = 60.0
# Same defaults Hermes' agent_init uses for the built-in store.
_BUILTIN_MEMORY_CHAR_LIMIT = 2200
_BUILTIN_USER_CHAR_LIMIT = 1375
_MEMORY_FAILURE = "The memory provider failed; the Hermes log has the details"

_MISSING_MEMORY_MODULES = frozenset({
    "hermes_cli", "hermes_cli.config", "hermes_constants",
    "tools", "tools.memory_tool", "tools.memory_tool_store",
    "agent", "agent.memory_provider", "plugins", "plugins.memory",
})


def _is_missing_memory_modules(exc: ModuleNotFoundError) -> bool:
    # Only a Hermes without the memory modules; a broken import deeper inside
    # Hermes is a real failure.
    return exc.name in _MISSING_MEMORY_MODULES


def _clip_block(value: Any, limit: int) -> str:
    """Like _clip, but keeps line structure: memory blocks are formatted text."""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hermes_memory_config() -> Dict[str, Any]:
    """The active profile's Hermes config (profile-scoped by _run_scoped)."""
    from hermes_cli.config import load_config

    config = load_config()
    return config if isinstance(config, dict) else {}


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _hermes_builtin_memory(config: Dict[str, Any]) -> str:
    """MEMORY.md + USER.md exactly as Hermes renders them into its system prompt."""
    from tools.memory_tool import (
        MemoryStore, get_builtin_memory_config, get_builtin_memory_store_flags, get_memory_dir)

    section = get_builtin_memory_config(config)
    memory_on, user_on = get_builtin_memory_store_flags(config)
    # load_from_disk creates the memories dir; nothing stored means nothing to read.
    if not (memory_on or user_on) or not get_memory_dir().is_dir():
        return ""
    store = MemoryStore(
        _int_or(section.get("memory_char_limit"), _BUILTIN_MEMORY_CHAR_LIMIT),
        _int_or(section.get("user_char_limit"), _BUILTIN_USER_CHAR_LIMIT),
        memory_enabled=memory_on,
        user_profile_enabled=user_on,
    )
    store.load_from_disk()
    blocks = [store.format_for_system_prompt(kind) for on, kind in ((memory_on, "memory"), (user_on, "user")) if on]
    return "\n\n".join(b for b in blocks if b)


def _configured_memory_provider(config: Dict[str, Any]) -> Optional[str]:
    """The external provider ``memory.provider`` names, or None for the built-in store."""
    section = config.get("memory") if isinstance(config, dict) else None
    name = str((section or {}).get("provider") or "").strip() if isinstance(section, dict) else ""
    if not name:
        return None
    try:
        from agent.memory_provider import is_core_memory_provider
    except ModuleNotFoundError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        return None  # a Hermes without external providers
    return None if is_core_memory_provider(name) else name


def _hermes_load_memory_provider(name: str) -> Any:
    from plugins.memory import load_memory_provider

    return load_memory_provider(name)


def _memory_provider_init_kwargs() -> Dict[str, Any]:
    """What agent_init's _memory_provider_init_kwargs gives a provider, minus the agent."""
    from hermes_constants import get_hermes_home

    kwargs: Dict[str, Any] = {
        "platform": MEMORY_PLATFORM,
        "hermes_home": str(get_hermes_home()),
        # Providers skip writes outside "primary" (MemoryProvider.initialize);
        # this caller only reads, so it says so.
        "agent_context": "subagent",
    }
    try:
        from hermes_cli.profiles import get_active_profile_name

        kwargs["agent_identity"] = get_active_profile_name()
        kwargs["agent_workspace"] = "hermes"
    except Exception:  # noqa: BLE001 — identity is optional scoping
        logger.debug("Could not name the active profile for the memory provider", exc_info=True)
    return kwargs


def _start_memory_provider(name: str) -> Any:
    """A loaded, available, initialized provider, or None. Never raises."""
    try:
        provider = _hermes_load_memory_provider(name)
        if provider is None:
            logger.info("Memory provider %r for Conduit voice is not installed", name)
            return None
        if not provider.is_available():
            logger.info("Memory provider %r for Conduit voice is not available", name)
            return None
        provider.initialize(session_id=MEMORY_SESSION_ID, **_memory_provider_init_kwargs())
        return provider
    except Exception:  # noqa: BLE001 — a provider must never break the route
        logger.warning("Memory provider %r failed to start for Conduit voice", name, exc_info=True)
        return None


def _stop_memory_provider(provider: Any) -> None:
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001
        logger.warning("Memory provider shutdown failed", exc_info=True)


class _MemoryProviders:
    """One initialized provider per profile, rebuilt when memory.provider changes."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._entries: Dict[str, tuple] = {}  # key -> (name, provider or None, started_at)
        self._key_locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, key: str, name: Optional[str], start: Optional[Callable[[str], Any]] = None) -> Any:
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        # Per profile, so one slow provider start doesn't hold up other profiles.
        with key_lock:
            entry = self._entries.get(key)
            if entry is not None:
                cached_name, provider, started = entry
                if cached_name == name and (provider is not None or self.clock() - started < MEMORY_PROVIDER_RETRY_S):
                    return provider
                del self._entries[key]
                _stop_memory_provider(provider)
            if name is None:
                return None
            provider = (start or _start_memory_provider)(name)
            self._entries[key] = (name, provider, self.clock())
            return provider

    def clear(self) -> None:
        with self._lock:
            entries, self._entries = self._entries, {}
        for _, provider, _ in entries.values():
            _stop_memory_provider(provider)


_memory_providers = _MemoryProviders()
_memory_limiter = _MintLimiter(MEMORY_RECALL_LIMIT, MEMORY_RECALL_WINDOW_S,
                               message="Too many memory lookups; try again shortly")
_memory_executor = ThreadPoolExecutor(max_workers=MEMORY_WORKERS, thread_name_prefix="conduit-memory")


def memory_context(key: str = "") -> Dict[str, Any]:
    """What Hermes injects into its own system prompt from memory, for Gemini Live."""
    try:
        config = _hermes_memory_config()
        builtin = _hermes_builtin_memory(config)
        name = _configured_memory_provider(config)
    except ModuleNotFoundError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        return {"available": False, "reason": "unsupported", "provider": None, "recall": False, "context": ""}
    provider = _memory_providers.get(key, name)
    block = ""
    if provider is not None:
        try:
            block = str(provider.system_prompt_block() or "").strip()
        except Exception:  # noqa: BLE001 — the built-in snapshot still stands
            logger.warning("Memory provider %r system_prompt_block failed", name, exc_info=True)
    context = _clip_block("\n\n".join(part for part in (builtin, block) if part), MEMORY_CONTEXT_MAX_CHARS)
    recall = provider is not None
    label = name if recall else ("builtin" if builtin else None)
    result: Dict[str, Any] = {"available": bool(context) or recall, "provider": label, "recall": recall, "context": context}
    if not result["available"]:
        result["reason"] = "disabled"
    return result


def run_memory_recall(query: Any, key: str = "", limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """The external provider's recall for ``query``; built-in memory is already in the context."""
    raw_query = str(query or "")
    # Bounded before normalizing, so a huge body isn't tokenized first.
    if len(raw_query) > MEMORY_MAX_QUERY_CHARS * 4:
        raise TokenError(400, f"query is longer than {MEMORY_MAX_QUERY_CHARS} characters")
    query = " ".join(raw_query.split())
    if not query:
        raise TokenError(400, "query is required")
    if len(query) > MEMORY_MAX_QUERY_CHARS:
        raise TokenError(400, f"query is longer than {MEMORY_MAX_QUERY_CHARS} characters")
    if limiter_key is not None:
        _memory_limiter.acquire(limiter_key)
    try:
        name = _configured_memory_provider(_hermes_memory_config())
    except ModuleNotFoundError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        name = None
    provider = _memory_providers.get(key, name)
    if provider is None:
        return {"available": False, "results": ""}
    try:
        text = provider.prefetch(query, session_id=MEMORY_SESSION_ID)
    except Exception:  # noqa: BLE001 — its text can carry hosts or keys
        logger.warning("Memory provider %r recall for Conduit failed", name, exc_info=True)
        raise TokenError(502, _MEMORY_FAILURE)
    return {"available": True, "results": _clip_block(text, MEMORY_RECALL_MAX_CHARS)}


async def _run_memory(profile: Optional[str], fn: Callable[[], Dict[str, Any]], what: str) -> Dict[str, Any]:
    try:
        return await asyncio.wait_for(_run_scoped(profile, fn, _memory_executor), timeout=MEMORY_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Memory %s for Conduit timed out after %ss", what, MEMORY_TIMEOUT_S)
        raise HTTPException(status_code=504, detail=f"Memory {what} timed out", headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        logger.warning("Memory %s for Conduit failed: %s", what, exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected(what, exc, feature="Memory")


@router.get("/memory/context")
async def get_memory_context(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    key = _limiter_key(profile)
    return {"ok": True, **(await _run_memory(profile, lambda: memory_context(key), "context"))}


@router.post("/memory/recall")
async def post_memory_recall(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request, MEMORY_MAX_BODY_BYTES)
    query = body.get("query") if isinstance(body, dict) else None
    key = _limiter_key(profile)
    return {"ok": True, **(await _run_memory(profile, lambda: run_memory_recall(query, key, limiter_key=key), "recall"))}
