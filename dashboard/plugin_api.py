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

Personality: the profile's SOUL.md as Hermes loads it, so voice keeps the
agent's personality. Read-only.

GPT-Live: exchanges Conduit's WebRTC offer for a GPT-Live answer using this
host's Codex sign-in, so live voice bills the ChatGPT subscription. The OAuth
token and account id stay on the host (hermes-agent#108940, host half).

Grok Live: relays Conduit's realtime socket to xAI, adding this host's
SuperGrok sign-in (or XAI_API_KEY) on the way. The bearer stays on the host.

Routes sit behind the dashboard's own auth, the same as /api/audio/*.
The API key is never returned, logged, or written anywhere.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import inspect
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket

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
# The context route starts the provider and reads files: generous, but bounded.
MEMORY_CONTEXT_LIMIT = 30
MEMORY_CONTEXT_WINDOW_S = 60.0
# Budget for one request, provider start included. Each provider call runs on
# its own thread, so a hung call is abandoned rather than holding a worker.
MEMORY_TIMEOUT_S = 10.0
MEMORY_WORKERS = 8
# Requests one profile may have running or queued at once, so a slow backend
# can't fill the shared pool for every profile.
MEMORY_MAX_IN_FLIGHT = 2
# How long provider shutdown may take when the process exits.
MEMORY_SHUTDOWN_TIMEOUT_S = 5.0
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


def _is_missing_memory_modules(exc: ImportError) -> bool:
    # A Hermes without the memory modules, or an older one whose modules lack
    # a name this imports (ImportError.name is then that module). A broken
    # import deeper inside Hermes is a real failure.
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
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _hermes_builtin_memory(config: Dict[str, Any]) -> str:
    """MEMORY.md + USER.md exactly as Hermes renders them into its system prompt."""
    from tools.memory_tool import (
        MemoryStore, get_builtin_memory_config, get_builtin_memory_store_flags, get_memory_dir)

    section = get_builtin_memory_config(config)
    section = section if isinstance(section, dict) else {}
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
    except ImportError as exc:
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
        # Some providers skip recall as well as writes outside "primary"; this
        # caller never writes (no sync_turn, no memory tool), so it is safe.
        "agent_context": "primary",
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


def _memory_cache_key(profile: Optional[str]) -> str:
    """The provider cache key: the profile's resolved Hermes home, exactly.

    Runs inside _run_scoped. Unlike _limiter_key it doesn't fold case, so on a
    case-sensitive host two profiles never share a provider.
    """
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home().resolve())
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
    name = (profile or "").strip()
    return "" if name.lower() in ("", "current") else name


_MEMORY_BUSY = "The memory provider is not responding; try again shortly"


class _ProviderEntry:
    """One profile's provider. Every call into it (initialize, prefetch,
    shutdown) holds ``call_lock``, so they run one at a time, and runs on its
    own thread with a deadline, so a hung call is abandoned, not waited on."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.provider: Any = None
        self.started = False
        self.started_at = 0.0
        self.call_lock = threading.Lock()
        self._state = threading.Lock()
        self._running = False
        self.stuck = False  # a call outlived its deadline and still holds call_lock

    def call(self, fn: Callable[[], Any], deadline: float, what: str) -> Any:
        if self.stuck:
            raise TokenError(503, _MEMORY_BUSY)
        if not self.call_lock.acquire(timeout=max(deadline - time.monotonic(), 0.0)):
            raise TokenError(503 if self.stuck else 504, _MEMORY_BUSY if self.stuck else f"Memory {what} timed out")
        box: Dict[str, Any] = {}
        done = threading.Event()
        # The profile scope lives in contextvars; the call thread must see it.
        ctx = contextvars.copy_context()

        def run() -> None:
            try:
                box["value"] = ctx.run(fn)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
                box["error"] = exc
            finally:
                with self._state:
                    self._running = False
                    self.stuck = False
                self.call_lock.release()
                done.set()

        with self._state:
            self._running = True
        try:
            threading.Thread(target=run, name=f"conduit-memory-{what}", daemon=True).start()
        except BaseException:
            with self._state:
                self._running = False
            self.call_lock.release()
            raise
        if not done.wait(max(deadline - time.monotonic(), 0.0)):
            with self._state:
                if self._running:
                    self.stuck = True
            logger.warning("Memory provider %r %s for Conduit timed out; abandoning the call", self.name, what)
            raise TokenError(504, f"Memory {what} timed out")
        if "error" in box:
            raise box["error"]
        return box.get("value")


def _stop_memory_provider(entry: _ProviderEntry, timeout: float = MEMORY_SHUTDOWN_TIMEOUT_S) -> None:
    if entry.provider is None:
        return
    try:
        entry.call(entry.provider.shutdown, time.monotonic() + timeout, "shutdown")
    except Exception:  # noqa: BLE001
        logger.warning("Memory provider %r shutdown failed", entry.name, exc_info=True)


class _MemoryProviders:
    """One initialized provider per profile, rebuilt when memory.provider changes.

    The registry lock only guards the dict; it is never held across provider
    I/O. The first request for a profile starts its provider under that
    entry's call lock, and concurrent requests wait on it with their deadline.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._entries: Dict[str, _ProviderEntry] = {}
        self._in_flight: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _drop(self, entry: _ProviderEntry) -> None:
        # Shut down off the request path: it waits for the entry's calls.
        threading.Thread(target=_stop_memory_provider, args=(entry,), name="conduit-memory-stop", daemon=True).start()

    def get(self, key: str, name: Optional[str], deadline: Optional[float] = None,
            start: Optional[Callable[[str], Any]] = None) -> Optional[_ProviderEntry]:
        """The started entry for ``key``, or None when no provider is usable."""
        deadline = time.monotonic() + MEMORY_TIMEOUT_S if deadline is None else deadline
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.name != name or (
                    entry.started and entry.provider is None
                    and self.clock() - entry.started_at >= MEMORY_PROVIDER_RETRY_S)):
                del self._entries[key]
                self._drop(entry)
                entry = None
            if name is None:
                return None
            if entry is None:
                entry = self._entries[key] = _ProviderEntry(name)
        if not entry.started:
            def begin() -> None:
                if not entry.started:  # another request may have started it meanwhile
                    entry.provider = (start or _start_memory_provider)(name)
                    entry.started_at = self.clock()
                    entry.started = True

            entry.call(begin, deadline, "start")
        return entry if entry.provider is not None else None

    def enter(self, key: str) -> None:
        with self._lock:
            if self._in_flight.get(key, 0) >= MEMORY_MAX_IN_FLIGHT:
                raise TokenError(503, _MEMORY_BUSY)
            self._in_flight[key] = self._in_flight.get(key, 0) + 1

    def leave(self, key: str) -> None:
        with self._lock:
            count = self._in_flight.get(key, 0) - 1
            if count > 0:
                self._in_flight[key] = count
            else:
                self._in_flight.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            _stop_memory_provider(entry)


_memory_providers = _MemoryProviders()
_memory_limiter = _MintLimiter(MEMORY_RECALL_LIMIT, MEMORY_RECALL_WINDOW_S,
                               message="Too many memory lookups; try again shortly")
_memory_context_limiter = _MintLimiter(MEMORY_CONTEXT_LIMIT, MEMORY_CONTEXT_WINDOW_S,
                                       message="Too many memory requests; try again shortly")
_memory_executor = ThreadPoolExecutor(max_workers=MEMORY_WORKERS, thread_name_prefix="conduit-memory")
# Let providers flush and close their clients when the dashboard exits.
atexit.register(lambda: _memory_providers.clear())


def memory_context(key: str = "", limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """What Hermes injects into its own system prompt from memory, for Gemini Live."""
    deadline = time.monotonic() + MEMORY_TIMEOUT_S
    if limiter_key is not None:
        _memory_context_limiter.acquire(limiter_key)
    try:
        config = _hermes_memory_config()
        builtin = _hermes_builtin_memory(config)
        name = _configured_memory_provider(config)
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        return {"available": False, "reason": "unsupported", "provider": None, "recall": False, "context": ""}
    entry = _memory_providers.get(key, name, deadline)
    # Not the provider's system_prompt_block(): that is mostly instructions
    # for its own tools, which the voice model can't call. Its memory is
    # reached through recall instead.
    context = _clip_block(builtin, MEMORY_CONTEXT_MAX_CHARS) if builtin else ""
    recall = entry is not None
    label = name if recall else ("builtin" if builtin else None)
    result: Dict[str, Any] = {"available": bool(context) or recall, "provider": label, "recall": recall, "context": context}
    if not result["available"]:
        result["reason"] = "disabled"
    return result


def run_memory_recall(query: Any, key: str = "", limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """The external provider's recall for ``query``; built-in memory is already in the context."""
    deadline = time.monotonic() + MEMORY_TIMEOUT_S
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
    except ImportError as exc:
        if not _is_missing_memory_modules(exc):
            raise
        name = None
    entry = _memory_providers.get(key, name, deadline)
    if entry is None:
        return {"available": False, "results": ""}
    provider = entry.provider
    try:
        text = entry.call(lambda: provider.prefetch(query, session_id=MEMORY_SESSION_ID), deadline, "recall")
    except TokenError:
        raise
    except Exception:  # noqa: BLE001 — its text can carry hosts or keys
        logger.warning("Memory provider %r recall for Conduit failed", name, exc_info=True)
        raise TokenError(502, _MEMORY_FAILURE)
    return {"available": True, "results": _clip_block(text, MEMORY_RECALL_MAX_CHARS)}


def _memory_job(profile: Optional[str], job: Callable[[str, str], Dict[str, Any]]) -> Callable[[], Dict[str, Any]]:
    """Wrap ``job(cache_key, limiter_key)`` with the per-profile in-flight cap."""
    def run() -> Dict[str, Any]:
        key = _memory_cache_key(profile)
        _memory_providers.enter(key)
        try:
            return job(key, _limiter_key(profile))
        finally:
            _memory_providers.leave(key)

    return run


async def _run_memory(profile: Optional[str], fn: Callable[[], Dict[str, Any]], what: str) -> Dict[str, Any]:
    try:
        # A backstop: the provider calls enforce MEMORY_TIMEOUT_S themselves.
        return await asyncio.wait_for(_run_scoped(profile, fn, _memory_executor), timeout=MEMORY_TIMEOUT_S + 2.0)
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
    job = _memory_job(profile, lambda key, limiter_key: memory_context(key, limiter_key=limiter_key))
    return {"ok": True, **(await _run_memory(profile, job, "context"))}


@router.post("/memory/recall")
async def post_memory_recall(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request, MEMORY_MAX_BODY_BYTES)
    query = body.get("query") if isinstance(body, dict) else None
    job = _memory_job(profile, lambda key, limiter_key: run_memory_recall(query, key, limiter_key=limiter_key))
    return {"ok": True, **(await _run_memory(profile, job, "recall"))}


# --- Personality ------------------------------------------------------------
#
# The profile's SOUL.md as Hermes itself loads it (injection scan, legacy
# protocol stripped), so a voice model can keep the agent's personality.
# Read-only; the voice-side rules against stage directions live in the
# pre_llm_call hook and in Conduit's own voice prompt.
PERSONALITY_MAX_CHARS = 8000
PERSONALITY_LIMIT = 30
PERSONALITY_WINDOW_S = 60.0
PERSONALITY_TIMEOUT_S = 10.0
PERSONALITY_WORKERS = 2

_MISSING_PERSONALITY_MODULES = frozenset({"agent", "agent.prompt_builder", "hermes_constants"})

# Its own small pool: a hung filesystem read can't pin the default executor
# or the memory and search pools.
_personality_executor = ThreadPoolExecutor(max_workers=PERSONALITY_WORKERS, thread_name_prefix="conduit-personality")
_personality_limiter = _MintLimiter(PERSONALITY_LIMIT, PERSONALITY_WINDOW_S,
                                    message="Too many personality requests; try again shortly")


def _hermes_soul_md() -> Optional[str]:
    """SOUL.md for the active profile (profile-scoped by _run_scoped), or None."""
    from agent.prompt_builder import load_soul_md
    from hermes_constants import get_hermes_home

    # Pin the home explicitly: this runs on a worker thread, and Hermes'
    # ambient lookup there could fall back to the launch profile.
    try:
        return load_soul_md(home_override=get_hermes_home())
    except TypeError:
        # A Hermes whose load_soul_md predates home_override: the profile
        # scope _run_scoped entered is what its ambient lookup reads.
        return load_soul_md()


def personality(limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """The agent's SOUL.md for Conduit voice; ``available`` is false when there is none."""
    if limiter_key is not None:
        _personality_limiter.acquire(limiter_key)
    try:
        soul = _hermes_soul_md()
    except ImportError as exc:
        # An older Hermes without load_soul_md (or without Hermes at all).
        if exc.name not in _MISSING_PERSONALITY_MODULES:
            raise
        return {"available": False, "text": ""}
    text = _clip_block(soul, PERSONALITY_MAX_CHARS) if soul else ""
    return {"available": bool(text), "text": text}


@router.get("/personality")
async def get_personality(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await asyncio.wait_for(
            _run_scoped(profile, lambda: personality(limiter_key=_limiter_key(profile)), _personality_executor),
            timeout=PERSONALITY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Personality for Conduit timed out after %ss", PERSONALITY_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Personality timed out", headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        logger.warning("Personality for Conduit failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("request", exc, feature="Personality")
    return {"ok": True, **result}


# --- GPT-Live on a ChatGPT/Codex subscription --------------------------------
#
# Backport of hermes-agent#108940's host half: exchange a WebRTC SDP offer for
# a GPT-Live answer using this host's Codex OAuth sign-in (`hermes auth` ->
# OpenAI Codex), so the voice layer bills the ChatGPT subscription instead of
# an API key. The bearer and its account id never leave the host. There is no
# fallback: a sign-in, entitlement or transport failure is an error, never an
# API-billed session.
#
# Once Hermes ships the same exchange (tools.voice_live grows
# _create_subscription_session), this route hands the request to Hermes and
# keeps only its URL, so Conduit needs no change when upstream lands.

GPT_LIVE_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
GPT_LIVE_DEFAULT_MODEL = "gpt-live-1-codex"
GPT_LIVE_DEFAULT_VOICE = "cove"
GPT_LIVE_TIMEOUT_S = 30.0
# A call starts once per conversation; cap a looping client well above that.
GPT_LIVE_LIMIT = 10
GPT_LIVE_WINDOW_S = 60.0
GPT_LIVE_WORKERS = 2
# An SDP offer is a few KB; the rest is room for the recent-history items.
GPT_LIVE_MAX_BODY_BYTES = 256 * 1024
GPT_LIVE_MAX_HISTORY_ITEMS = 40
GPT_LIVE_MAX_ANSWER_BYTES = 64 * 1024
# Bounds how long Conduit waits (a 504), above the socket timeout. It can't
# interrupt a Hermes call already running on a worker (a token refresh, the
# upstream exchange), so a Hermes call that never returns holds its worker
# until the process restarts. Status has its own pool: a stuck session never
# blocks the readiness check.
GPT_LIVE_REQUEST_TIMEOUT_S = GPT_LIVE_TIMEOUT_S + 2
GPT_LIVE_NO_FALLBACK = "No API fallback was used."
_GPT_LIVE_VOICE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_GPT_LIVE_CALL_ID = re.compile(r"rtc_[A-Za-z0-9_-]+|[0-9a-fA-F-]{36}")

# Hermes' own voice-layer persona (tools/voice_live.py), for a Hermes that
# predates that module. Short on purpose: the live model delegates real work.
GPT_LIVE_PERSONA = (
    "You are Hermes, a calm and friendly voice assistant. Speak naturally at an unhurried pace. "
    "Be clear and direct, not overly cheerful. If the user is frustrated, acknowledge it briefly "
    "and focus on the next helpful step.\n\n"
    "Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with "
    "the main response.\n\n"
    "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n\n"
    "Delegation policy:\n"
    "Backend tools:\n"
    "- Hermes agent: a full AI agent with tools — it can run commands, read and edit files, "
    "browse the web, search, remember things across sessions, schedule tasks, and reason "
    "carefully about anything. It is the one who actually does work and knows facts.\n\n"
    "Delegate to the backend when:\n"
    "- The user asks a question that needs facts, current information, or careful reasoning.\n"
    "- The user asks you to do, check, find, make, fix, run or remember anything.\n"
    "- A correction changes work already requested.\n\n"
    "Do not delegate to the backend when:\n"
    "- The user greets you, makes small talk, or asks you to repeat a result already provided.\n"
    "- You need a brief clarification to understand the request.\n\n"
    "Delegate before giving an answer that depends on backend work. Do not guess the result "
    "while waiting; say briefly that you are checking, then wait for the result."
)

_gpt_live_executor = ThreadPoolExecutor(max_workers=GPT_LIVE_WORKERS, thread_name_prefix="conduit-gpt-live")
_gpt_live_status_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conduit-gpt-live-status")
_gpt_live_limiter = _MintLimiter(GPT_LIVE_LIMIT, GPT_LIVE_WINDOW_S,
                                 message="Too many GPT-Live session requests; try again shortly")


def _upstream_voice_live() -> Any:
    """Hermes' tools.voice_live when it carries the subscription exchange, else None."""
    try:
        from tools import voice_live
    except ImportError:
        return None
    exchange = getattr(voice_live, "_create_subscription_session", None)
    build = getattr(voice_live, "build_session_config", None)
    if not callable(exchange) or not callable(build):
        return None
    try:
        build_params = inspect.signature(build).parameters
        exchange_params = inspect.signature(exchange).parameters
    except (TypeError, ValueError):
        return None
    # The shape #108940 ships; anything else means Hermes moved on and the plugin keeps serving.
    if "live" not in build_params or len(exchange_params) != 2:
        logger.info("Hermes' GPT-Live subscription exchange has a different shape; using the plugin's own")
        return None
    return voice_live


def _gpt_live_settings() -> Dict[str, Any]:
    """The profile's ``voice.gpt_live`` block (profile-scoped by _run_scoped), or {}."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except ImportError:
        return {}
    except Exception as exc:
        logger.warning("GPT-Live could not read the Hermes config: %s", type(exc).__name__)
        return {}
    voice = config.get("voice") if isinstance(config, dict) else None
    live = voice.get("gpt_live") if isinstance(voice, dict) else None
    return live if isinstance(live, dict) else {}


def _gpt_live_model_voice(live: Dict[str, Any]) -> tuple:
    return (str(live.get("subscription_model") or "").strip() or GPT_LIVE_DEFAULT_MODEL,
            str(live.get("subscription_voice") or "").strip() or GPT_LIVE_DEFAULT_VOICE)


def _gpt_live_instructions(live: Dict[str, Any]) -> str:
    try:
        from tools.voice_live import LIVE_PERSONA as persona
    except ImportError:
        persona = GPT_LIVE_PERSONA
    extra = str(live.get("instructions") or "").strip()
    return f"{persona}\n\n{extra}" if extra else persona


def _codex_credentials(refresh_if_expiring: bool = True) -> tuple:
    """(bearer, ChatGPT account id) from Hermes' Codex sign-in; TokenError(503) when unusable."""
    try:
        from hermes_cli.auth_codex import resolve_codex_runtime_credentials
        from hermes_cli.auth_constants import AuthError, _decode_jwt_claims
    except ImportError:
        raise TokenError(503, "This Hermes version has no Codex sign-in. " + GPT_LIVE_NO_FALLBACK)
    try:
        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=refresh_if_expiring)
    except AuthError:
        raise TokenError(503, "GPT-Live needs a working Codex sign-in on the Hermes host. "
                              "Run `hermes auth` and choose OpenAI Codex. " + GPT_LIVE_NO_FALLBACK)
    except Exception as exc:
        # A refresh that failed on the network, say: the type only, since the text could carry detail.
        logger.warning("GPT-Live could not read the Codex sign-in: %s", type(exc).__name__)
        raise TokenError(503, "GPT-Live could not read the Codex sign-in on the Hermes host. "
                              + GPT_LIVE_NO_FALLBACK)
    token = str(credentials.get("api_key") or "").strip() if isinstance(credentials, dict) else ""
    try:
        claims = _decode_jwt_claims(token)
    except Exception:
        claims = None
    claims = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    account = claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
    if (not token or not isinstance(account, str) or not account.strip()
            or any(c in token + account for c in "\r\n")):
        raise TokenError(503, "GPT-Live needs a Codex sign-in with a ChatGPT account. " + GPT_LIVE_NO_FALLBACK)
    return token, account.strip()


def gpt_live_status() -> Dict[str, Any]:
    """Credential readiness only; the voice entitlement is checked when a call starts."""
    live = _gpt_live_settings()
    model, voice = _gpt_live_model_voice(live)
    status: Dict[str, Any] = {"auth": "subscription", "model": model, "voice": voice,
                              "source": "hermes" if _upstream_voice_live() else "plugin"}
    try:
        _codex_credentials(refresh_if_expiring=False)
    except TokenError as exc:
        # A readiness reason, not a call failure: without the "no fallback" sentence.
        return {**status, "available": False, "reason": str(exc).replace(" " + GPT_LIVE_NO_FALLBACK, "")}
    return {**status, "available": True, "reason": None}


def gpt_live_session_config(history: Optional[list], live: Dict[str, Any]) -> Dict[str, Any]:
    model, voice = _gpt_live_model_voice(live)
    config: Dict[str, Any] = {
        "model": model,
        "instructions": _gpt_live_instructions(live),
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client"},
    }
    if history:
        # The Codex frameless contract names seeded history initial_items.
        config["initial_items"] = history
    return config


def _post_sdp(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> tuple:
    """POST the offer; returns (status, answer text, Location). No redirects: the bearer must not follow one."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={**headers, "Content-Type": "application/json"})
    try:
        with _opener.open(request, timeout=GPT_LIVE_TIMEOUT_S) as response:
            # An SDP answer is a few KB; one byte over the cap is refused, not buffered.
            answer = response.read(GPT_LIVE_MAX_ANSWER_BYTES + 1)
            if len(answer) > GPT_LIVE_MAX_ANSWER_BYTES:
                raise TokenError(502, "GPT-Live returned an invalid WebRTC answer. " + GPT_LIVE_NO_FALLBACK)
            return response.status, answer.decode("utf-8", "replace"), response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        # The body can echo account details; only the status goes back to Conduit.
        status = exc.code
        exc.close()
        return status, "", ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("GPT-Live session request could not reach OpenAI: %s", getattr(exc, "reason", exc))
        raise TokenError(502, "GPT-Live connection failed. " + GPT_LIVE_NO_FALLBACK)
    except ValueError as exc:
        # A header value the request can't carry (a non-Latin-1 token, say): the type only, never the value.
        logger.warning("GPT-Live could not build its session request: %s", type(exc).__name__)
        raise TokenError(502, "GPT-Live connection failed. " + GPT_LIVE_NO_FALLBACK)


def _valid_answer(sdp: Any, call_id: Any) -> bool:
    return (isinstance(sdp, str) and sdp.startswith("v=0")
            and isinstance(call_id, str) and _GPT_LIVE_CALL_ID.fullmatch(call_id) is not None)


def _plugin_gpt_live_session(sdp: str, config: Dict[str, Any], post: Callable[..., tuple]) -> Dict[str, Any]:
    token, account = _codex_credentials()
    status, answer, location = post(GPT_LIVE_URL, {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": account,
        "OpenAI-Alpha": "quicksilver=v2",
    }, {"sdp": sdp, "session": config})
    if not 200 <= status < 300:
        logger.warning("GPT-Live session was rejected: HTTP %s", status)
        raise TokenError(502, f"GPT-Live session was rejected (HTTP {status}); check the Codex sign-in "
                              "and the account's voice access. " + GPT_LIVE_NO_FALLBACK)
    call_id = urllib.parse.urlparse(location).path.rstrip("/").rsplit("/", 1)[-1]
    if not _valid_answer(answer, call_id):
        raise TokenError(502, "GPT-Live returned an invalid WebRTC answer. " + GPT_LIVE_NO_FALLBACK)
    return {"auth": "subscription", "session": {"id": call_id}, "transport": {"type": "webrtc", "sdp": answer}}


def _clean_history(history: Any) -> Optional[list]:
    if history is None:
        return None
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise TokenError(400, "history must be a list of conversation items")
    return history[-GPT_LIVE_MAX_HISTORY_ITEMS:] or None


def _clean_voice(voice: Any) -> Optional[str]:
    """The voice Conduit asked for, or None to keep the profile's configured one."""
    if voice is None:
        return None
    if not isinstance(voice, str):
        raise TokenError(400, "voice must be a voice name such as cove")
    voice = voice.strip().lower()
    if not voice:
        return None
    if _GPT_LIVE_VOICE.fullmatch(voice) is None:
        raise TokenError(400, "voice must be a voice name such as cove")
    return voice


GPT_LIVE_MAX_BRIEFING_CHARS = 32 * 1024
# Last, so a chatty persona doesn't talk the model into an opening greeting.
GPT_LIVE_WAIT_FOR_USER = (
    "Opening policy: do not speak first. When the call starts, stay silent until the user "
    "speaks, then answer what they said. This applies whatever the persona above says."
)


def _clean_briefing(briefing: Any) -> Optional[str]:
    """Conduit's rules/persona/memory for this call, or None when it sent none."""
    if briefing is None:
        return None
    if not isinstance(briefing, str):
        raise TokenError(400, "briefing must be text")
    briefing = briefing.strip()
    if len(briefing) > GPT_LIVE_MAX_BRIEFING_CHARS:
        raise TokenError(400, "briefing is too long")
    return briefing or None


def _built_session(config: Any) -> Dict[str, Any]:
    """The session block of a Hermes-built config, which may or may not wrap it in "session"."""
    if not isinstance(config, dict):
        return {}
    inner = config.get("session")
    return inner if isinstance(inner, dict) else config


def _built_voice(config: Any) -> Optional[str]:
    audio = _built_session(config).get("audio")
    output = audio.get("output") if isinstance(audio, dict) else None
    voice = output.get("voice") if isinstance(output, dict) else None
    if not isinstance(voice, str) or _GPT_LIVE_VOICE.fullmatch(voice.strip().lower()) is None:
        return None
    return voice.strip().lower()


def _built_has_briefing(config: Any, briefing: Optional[str]) -> bool:
    """True only when Hermes' config really carries the briefing, so Conduit never drops it unsent."""
    if not briefing:
        return False
    instructions = _built_session(config).get("instructions")
    return isinstance(instructions, str) and briefing in instructions


def create_gpt_live_session(
    sdp: Any,
    history: Any = None,
    limiter_key: Optional[str] = None,
    post: Optional[Callable[..., tuple]] = None,
    *,
    voice: Any = None,
    briefing: Any = None,
) -> Dict[str, Any]:
    if not isinstance(sdp, str) or not sdp.startswith("v=0"):
        raise TokenError(400, "sdp must be a WebRTC SDP offer")
    history = _clean_history(history)
    voice = _clean_voice(voice)
    briefing = _clean_briefing(briefing)
    if limiter_key is not None:
        _gpt_live_limiter.acquire(limiter_key)
    live = {**_gpt_live_settings(), "auth": "subscription"}
    if voice:
        live["subscription_voice"] = voice
    if briefing:
        # Part of the session's instructions, so the model has it before the first word:
        # sent as context appends after the call starts, it answers each chunk out loud.
        extra = str(live.get("instructions") or "").strip()
        live["instructions"] = f"{extra}\n\n{briefing}" if extra else briefing
    extra = str(live.get("instructions") or "").strip()
    live["instructions"] = f"{extra}\n\n{GPT_LIVE_WAIT_FOR_USER}" if extra else GPT_LIVE_WAIT_FOR_USER
    # Echoed back so Conduit can tell a host that applied the chosen voice from one that ignored it.
    applied_voice = _gpt_live_model_voice(live)[1]
    upstream = _upstream_voice_live()
    if upstream is not None:
        try:
            config = upstream.build_session_config(history, live=live)
            result = upstream._create_subscription_session(sdp, config)
        except Exception as exc:
            # Only the type is logged: Hermes' text could quote a provider response.
            logger.warning("Hermes' GPT-Live subscription exchange failed: %s", type(exc).__name__)
            raise TokenError(502, "GPT-Live session could not be started; check the Codex sign-in "
                                  "and the account's voice access. " + GPT_LIVE_NO_FALLBACK)
        session, transport = (result.get("session"), result.get("transport")) if isinstance(result, dict) else (None, None)
        if (not isinstance(session, dict) or not isinstance(transport, dict)
                or not _valid_answer(transport.get("sdp"), session.get("id"))):
            raise TokenError(502, "GPT-Live returned an invalid WebRTC answer. " + GPT_LIVE_NO_FALLBACK)
        # Prefer the voice Hermes put in the session over the one we asked for.
        built = _built_voice(config)
        if built:
            applied_voice = built
        # Only the fields Conduit reads: nothing else Hermes returns leaves the host.
        return {"auth": "subscription", "session": {"id": session["id"]},
                "transport": {"type": "webrtc", "sdp": transport["sdp"]}, "source": "hermes",
                "voice": applied_voice, "briefing_applied": _built_has_briefing(config, briefing)}
    config = gpt_live_session_config(history, live)
    return {**_plugin_gpt_live_session(sdp, config, post or _post_sdp), "source": "plugin", "voice": applied_voice,
            "briefing_applied": bool(briefing)}


@router.get("/gpt-live/status")
async def get_gpt_live_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await asyncio.wait_for(
            _run_scoped(profile, gpt_live_status, _gpt_live_status_executor), timeout=GPT_LIVE_REQUEST_TIMEOUT_S))}
    except asyncio.TimeoutError:
        logger.warning("GPT-Live status for Conduit timed out after %ss", GPT_LIVE_REQUEST_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="GPT-Live status timed out", headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("status", exc, feature="GPT-Live")


@router.post("/gpt-live/session")
async def post_gpt_live_session(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request, GPT_LIVE_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object",
                            headers={"Cache-Control": "no-store"})
    try:
        result = await asyncio.wait_for(
            _run_scoped(
                profile,
                lambda: create_gpt_live_session(body.get("sdp"), body.get("history"), limiter_key=_limiter_key(profile), voice=body.get("voice"),
                                                briefing=body.get("briefing")),
                _gpt_live_executor,
            ),
            timeout=GPT_LIVE_REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("GPT-Live session for Conduit timed out after %ss", GPT_LIVE_REQUEST_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="GPT-Live session timed out", headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        logger.warning("GPT-Live session for Conduit failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("session", exc, feature="GPT-Live")
    return {"ok": True, **result}


# --- Grok Live --------------------------------------------------------------
#
# Grok Live (xAI's realtime voice model) talks to Conduit through this host:
# the socket below relays Conduit's realtime events to wss://api.x.ai and
# back, adding only the Authorization header. The bearer is this host's
# SuperGrok sign-in (Hermes' xai-oauth, `hermes auth add xai-oauth`) or, without one,
# XAI_API_KEY: the same order Hermes' own xAI endpoints use. Neither ever
# reaches Conduit. Conduit owns the conversation itself (session.update,
# tools, audio); the relay never reads or rewrites it.
#
# A relay rather than a phone-direct token: a SuperGrok bearer is proven on
# the realtime socket (upstream hermes-agent#111940 holds the same socket),
# while minting a phone token from one is not.

GROK_LIVE_URL = "wss://api.x.ai/v1/realtime"
GROK_LIVE_DEFAULT_MODEL = "grok-voice-latest"
GROK_LIVE_DEFAULT_VOICE = "eve"
GROK_LIVE_MODEL_ENV_VAR = "CONDUIT_GROK_LIVE_MODEL"
GROK_LIVE_CONNECT_TIMEOUT_S = 15.0
# A call connects once per conversation (plus a few reconnects); cap a looping client.
GROK_LIVE_LIMIT = 10
GROK_LIVE_WINDOW_S = 60.0
# Open relays at once. A call holds one, and a reconnect briefly overlaps
# the socket it replaces; the global cap bounds every profile together.
GROK_LIVE_MAX_OPEN_PER_PROFILE = 3
GROK_LIVE_MAX_OPEN = 8
# Every profile together: each start or status check can refresh the
# SuperGrok sign-in, so the host bounds them whatever ?profile= says.
GROK_LIVE_HOST_LIMIT = 30
GROK_LIVE_STATUS_LIMIT = 30
# Conduit's frames are 100 ms audio chunks and a session.update carrying the
# instructions (persona and memory included); xAI's carry model audio.
GROK_LIVE_MAX_CLIENT_FRAME_BYTES = 256 * 1024
GROK_LIVE_MAX_SERVER_FRAME_BYTES = 16 * 1024 * 1024
# Close reasons travel in a control frame: at most 123 bytes.
_CLOSE_REASON_MAX_BYTES = 120
_GROK_LIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Close codes Conduit reads. 4xxx means "won't change on retry" except the
# two transient ones (xAI unreachable, xAI dropped mid-call).
GROK_CLOSE_NO_CREDENTIAL = 4503
GROK_CLOSE_UNAUTHORIZED = 4401
GROK_CLOSE_RATE_LIMITED = 4429
GROK_CLOSE_REFUSED = 4400
GROK_CLOSE_UNREACHABLE = 4502
GROK_CLOSE_FAILED = 4500
# Bounds the host work before a call connects (a SuperGrok refresh is a
# network call). It can't interrupt a Hermes call already on a worker, so
# status has its own pool: a stuck refresh never blocks the readiness check.
GROK_LIVE_SETUP_TIMEOUT_S = GROK_LIVE_CONNECT_TIMEOUT_S + 10

_grok_live_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-grok-live")
_grok_live_status_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-grok-live-status")
_grok_live_limiter = _MintLimiter(GROK_LIVE_LIMIT, GROK_LIVE_WINDOW_S,
                                  message="Too many Grok Live connections; try again shortly")
_grok_live_host_limiter = _MintLimiter(GROK_LIVE_HOST_LIMIT, GROK_LIVE_WINDOW_S,
                                       message="Too many Grok Live connections on this host; try again shortly")
# Refused upgrades on the whole host: past this, they're turned away before
# the handshake completes (a bare 403) instead of with a 4401 close.
GROK_LIVE_AUTH_FAILURE_LIMIT = 30
_grok_live_auth_failure_limiter = _MintLimiter(GROK_LIVE_AUTH_FAILURE_LIMIT, GROK_LIVE_WINDOW_S,
                                               message="Too many refused Grok Live connections")
_grok_live_status_limiter = _MintLimiter(GROK_LIVE_STATUS_LIMIT, GROK_LIVE_WINDOW_S,
                                         message="Too many Grok Live status checks; try again shortly")
# Open relays by limiter key. Only touched on the event loop, so no lock.
_grok_live_open: Dict[str, int] = {}


def _grok_live_settings() -> Dict[str, Any]:
    """The profile's ``voice.grok_live`` block (profile-scoped by _run_scoped), or {}."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except ImportError:
        return {}
    except Exception as exc:
        logger.warning("Grok Live could not read the Hermes config: %s", type(exc).__name__)
        return {}
    voice = config.get("voice") if isinstance(config, dict) else None
    live = voice.get("grok_live") if isinstance(voice, dict) else None
    return live if isinstance(live, dict) else {}


def _grok_live_name(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if _GROK_LIVE_NAME.fullmatch(text) else fallback


def grok_live_model_voice(live: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    live = _grok_live_settings() if live is None else live
    model = _grok_live_name(_env_value(GROK_LIVE_MODEL_ENV_VAR) or live.get("model"), GROK_LIVE_DEFAULT_MODEL)
    return model, _grok_live_name(live.get("voice"), GROK_LIVE_DEFAULT_VOICE)


GROK_LIVE_NO_CREDENTIAL = ("Grok Live needs xAI on the Hermes host: sign in with SuperGrok "
                           "(`hermes auth add xai-oauth`) or set XAI_API_KEY.")
GROK_LIVE_SIGN_IN_FAILED = ("The Hermes host couldn't read its xAI sign-in; try again, or sign in "
                            "again with `hermes auth add xai-oauth`.")


def grok_live_credentials() -> Tuple[str, str]:
    """(bearer, "subscription" | "api_key") for the realtime socket; TokenError(503) when there is none.

    Hermes' resolver when it has one (SuperGrok first, then XAI_API_KEY,
    refreshing an expiring sign-in), else XAI_API_KEY alone. The resolver's
    answer is final: it already checks XAI_API_KEY itself, and reading the
    key past a resolver that failed would quietly switch a SuperGrok host to
    per-token billing.
    """
    token, auth = "", ""
    try:
        from tools.xai_http import resolve_xai_http_credentials
    except ImportError as exc:
        # Only a Hermes without the module falls back; a broken import
        # inside it is a failed sign-in, not an absent one.
        if exc.name not in ("tools", "tools.xai_http"):
            logger.warning("Grok Live could not load the xAI sign-in: %s", type(exc).__name__)
            raise TokenError(503, GROK_LIVE_SIGN_IN_FAILED)
        resolve_xai_http_credentials = None
    if resolve_xai_http_credentials is None:
        token, auth = str(_env_value("XAI_API_KEY") or "").strip(), "api_key"
    else:
        try:
            credentials = resolve_xai_http_credentials()
        except Exception as exc:
            # A failed refresh, say: the type only, since the text could carry detail.
            logger.warning("Grok Live could not read the xAI sign-in: %s", type(exc).__name__)
            raise TokenError(503, GROK_LIVE_SIGN_IN_FAILED)
        if credentials is not None and not isinstance(credentials, dict):
            # A resolver this plugin doesn't understand: refusing beats
            # guessing past a SuperGrok sign-in to the billed key.
            logger.warning("Grok Live does not recognize the xAI sign-in (%s)", type(credentials).__name__)
            raise TokenError(503, GROK_LIVE_SIGN_IN_FAILED)
        if isinstance(credentials, dict):
            token = str(credentials.get("api_key") or "").strip()
            auth = "subscription" if credentials.get("provider") == "xai-oauth" else "api_key"
    if not token or any(c in token for c in "\r\n"):
        raise TokenError(503, GROK_LIVE_NO_CREDENTIAL)
    return token, auth


def grok_live_status() -> Dict[str, Any]:
    """Credential readiness only; whether xAI accepts it is learnt when a call connects.

    ``voice`` is the configured voice Conduit puts in its session.update;
    the relay itself never sets one.
    """
    _grok_live_status_limiter.acquire("")
    model, voice = grok_live_model_voice()
    status: Dict[str, Any] = {"model": model, "voice": voice, "transport": "relay"}
    try:
        _, auth = grok_live_credentials()
    except TokenError as exc:
        return {**status, "available": False, "auth": None, "reason": str(exc)}
    return {**status, "available": True, "auth": auth, "reason": None}


@router.get("/grok-live/status")
async def get_grok_live_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await asyncio.wait_for(
            _run_scoped(profile, grok_live_status, _grok_live_status_executor), timeout=GROK_LIVE_SETUP_TIMEOUT_S))}
    except asyncio.TimeoutError:
        logger.warning("Grok Live status for Conduit timed out")
        raise HTTPException(status_code=504, detail="Grok Live status timed out", headers={"Cache-Control": "no-store"})
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise _unexpected("status", exc, feature="Grok Live")


def _close_reason(text: str) -> str:
    data = text.encode("utf-8", "replace")[:_CLOSE_REASON_MAX_BYTES]
    return data.decode("utf-8", "ignore")


async def _ws_authorized(ws: WebSocket) -> bool:
    """The dashboard's own WebSocket auth (the one /api/audio/speak-stream uses).

    Plugin routes inherit the dashboard's HTTP auth middleware, but WebSocket
    upgrades skip it, so the socket checks for itself. Fails closed when this
    Hermes has no such check.
    """
    try:
        from hermes_cli.web_server_chat import _ws_auth_ok, _ws_request_is_allowed
    except ImportError:
        logger.warning("Grok Live socket refused: this Hermes has no dashboard WebSocket auth")
        return False
    try:
        for check in (_ws_auth_ok, _ws_request_is_allowed):
            result = check(ws)
            # An async check would otherwise be a coroutine, which is truthy.
            if inspect.isawaitable(result):
                result = await result
            if not result:
                return False
        return True
    except Exception as exc:
        logger.warning("Grok Live socket auth check failed: %s", type(exc).__name__)
        return False


class GrokUpstreamRefused(Exception):
    """xAI refused the WebSocket upgrade; ``status`` is its HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


async def _connect_xai(url: str, headers: Dict[str, str]) -> Any:
    """Opens the realtime socket to xAI (the `websockets` package Hermes ships)."""
    try:
        from websockets.asyncio.client import connect
        kwargs: Dict[str, Any] = {"additional_headers": headers}
    except ImportError:  # websockets < 13
        from websockets import connect  # type: ignore[no-redef]
        kwargs = {"extra_headers": headers}
    try:
        from websockets.exceptions import InvalidStatus
    except ImportError:
        InvalidStatus = None  # type: ignore[assignment]
    try:
        from websockets.exceptions import InvalidStatusCode
    except ImportError:
        InvalidStatusCode = None  # type: ignore[assignment]
    try:
        return await connect(url, max_size=GROK_LIVE_MAX_SERVER_FRAME_BYTES,
                             open_timeout=GROK_LIVE_CONNECT_TIMEOUT_S, **kwargs)
    except Exception as exc:
        if InvalidStatus is not None and isinstance(exc, InvalidStatus):
            raise GrokUpstreamRefused(int(getattr(exc.response, "status_code", 0) or 0))
        if InvalidStatusCode is not None and isinstance(exc, InvalidStatusCode):
            raise GrokUpstreamRefused(int(getattr(exc, "status_code", 0) or 0))
        raise


def _grok_refusal(status: int, auth: str) -> Tuple[int, str]:
    if status in (401, 403):
        who = "the SuperGrok sign-in" if auth == "subscription" else "XAI_API_KEY"
        return GROK_CLOSE_REFUSED, f"xAI refused {who} (HTTP {status})"
    if status == 429:
        return GROK_CLOSE_RATE_LIMITED, "xAI is rate limiting voice (HTTP 429)"
    if 400 <= status < 500:
        return GROK_CLOSE_REFUSED, f"xAI refused the call (HTTP {status})"
    return GROK_CLOSE_UNREACHABLE, f"xAI voice is unavailable (HTTP {status or 'error'})"


def _forwardable_close(code: Optional[int], reason: str) -> Tuple[int, str]:
    """xAI's close as one Conduit can be sent.

    Standard codes pass through (bar 1005/1006/1015, which can't go on the
    wire). xAI's own 3000-4999 codes become the plugin's refusal, so the
    plugin's codes (4401, 4503, 4502…) always mean what the plugin says.
    """
    if code is not None and 3000 <= code <= 4999:
        return GROK_CLOSE_REFUSED, f"xAI closed the call ({code}): {reason}" if reason else f"xAI closed the call ({code})"
    if code is None or code in (1005, 1006, 1015) or not 1000 <= code <= 1014:
        return GROK_CLOSE_UNREACHABLE, "The connection to xAI was lost"
    return code, reason


def _grok_live_socket_setup(profile: Optional[str]) -> Tuple[str, str, str]:
    """(bearer, auth, model) for one relay connection, inside the profile's scope.

    The limiter runs here, after the scope has accepted the profile, as the
    other per-profile routes do: an unknown ?profile= gets no bucket.
    """
    _grok_live_host_limiter.acquire("")
    _grok_live_limiter.acquire(_limiter_key(profile))
    token, auth = grok_live_credentials()
    return token, auth, grok_live_model_voice()[0]


@router.websocket("/grok-live/socket")
async def grok_live_socket(ws: WebSocket) -> None:
    authorized = await _ws_authorized(ws)
    if not authorized:
        try:
            _grok_live_auth_failure_limiter.acquire("")
        except TokenError:
            await ws.close(code=GROK_CLOSE_UNAUTHORIZED)
            return
    # Accepted before refusing: a close before the accept reaches Conduit as
    # a bare HTTP 403, and every refusal below should say why.
    await ws.accept()
    profile = (ws.query_params.get("profile") or "").strip() or None

    async def refuse(code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code=code, reason=_close_reason(reason))

    if not authorized:
        await refuse(GROK_CLOSE_UNAUTHORIZED, "Dashboard sign-in required")
        return
    try:
        token, auth, model = await asyncio.wait_for(
            _run_scoped(profile, lambda: _grok_live_socket_setup(profile), _grok_live_executor),
            timeout=GROK_LIVE_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Grok Live socket setup for Conduit timed out")
        await refuse(GROK_CLOSE_UNREACHABLE, "The Hermes host timed out reading its xAI sign-in")
        return
    except TokenError as exc:
        if exc.status == 429:
            code = GROK_CLOSE_RATE_LIMITED
        elif str(exc) in (GROK_LIVE_NO_CREDENTIAL, GROK_LIVE_SIGN_IN_FAILED):
            code = GROK_CLOSE_NO_CREDENTIAL
        else:
            # The host itself (profile scoping, say): not an xAI sign-in problem.
            code = GROK_CLOSE_FAILED
        logger.warning("Grok Live socket for Conduit refused: %s", exc)
        await refuse(code, str(exc))
        return
    except HTTPException as exc:
        # Hermes' own 400/404 for a bad or unknown profile.
        await refuse(GROK_CLOSE_REFUSED, str(exc.detail))
        return
    except Exception as exc:
        # The type only: the text could quote the credential being handled.
        logger.warning("Grok Live socket setup failed: %s", type(exc).__name__)
        await refuse(GROK_CLOSE_FAILED, f"Grok Live failed on the host ({type(exc).__name__})")
        return

    key = _limiter_key(profile)
    if _grok_live_open.get(key, 0) >= GROK_LIVE_MAX_OPEN_PER_PROFILE or \
            sum(_grok_live_open.values()) >= GROK_LIVE_MAX_OPEN:
        await refuse(GROK_CLOSE_RATE_LIMITED, "Too many Grok Live calls are open on this host")
        return
    _grok_live_open[key] = _grok_live_open.get(key, 0) + 1
    try:
        await _grok_live_relay(ws, refuse, token, auth, model)
    finally:
        remaining = _grok_live_open.get(key, 1) - 1
        if remaining > 0:
            _grok_live_open[key] = remaining
        else:
            _grok_live_open.pop(key, None)


async def _grok_live_relay(ws: WebSocket, refuse: Callable[[int, str], Any], token: str, auth: str, model: str) -> None:
    """One call's relay, from connecting to xAI to forwarding its close.

    The bearer lives only in this call's upgrade headers; it is never
    stored or logged.
    """
    url = f"{GROK_LIVE_URL}?{urllib.parse.urlencode({'model': model})}"
    try:
        upstream = await _connect_xai(url, {"Authorization": f"Bearer {token}"})
    except GrokUpstreamRefused as exc:
        logger.warning("xAI refused the Grok Live socket: HTTP %s", exc.status)
        await refuse(*_grok_refusal(exc.status, auth))
        return
    except Exception as exc:
        # The type only: the text could quote the request.
        logger.warning("Grok Live could not reach xAI: %s", type(exc).__name__)
        await refuse(GROK_CLOSE_UNREACHABLE, "Could not reach xAI")
        return

    async def client_to_xai() -> None:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text, data = message.get("text"), message.get("bytes")
            size = len(text.encode("utf-8")) if text is not None else len(data or b"")
            if size > GROK_LIVE_MAX_CLIENT_FRAME_BYTES:
                raise ValueError("frame too large")
            if text is not None:
                await upstream.send(text)
            elif data is not None:
                await upstream.send(data)

    async def xai_to_client() -> None:
        async for frame in upstream:
            if isinstance(frame, str):
                await ws.send_text(frame)
            else:
                await ws.send_bytes(bytes(frame))

    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        ConnectionClosed = ()  # type: ignore[assignment,misc]

    outbound = asyncio.create_task(client_to_xai())
    inbound = asyncio.create_task(xai_to_client())
    try:
        done, _ = await asyncio.wait({outbound, inbound}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Also on cancellation (shutdown): the tasks and the credentialed
        # upstream socket never outlive the handler. The handler's own
        # cancellation still propagates: gather only collects the tasks'.
        for task in (outbound, inbound):
            task.cancel()
        with contextlib.suppress(Exception):
            await upstream.close()
        await asyncio.gather(outbound, inbound, return_exceptions=True)
    # Both finished by now; read both so neither logs an unretrieved exception.
    outbound_failure = None if outbound.cancelled() else outbound.exception()
    inbound_failure = None if inbound.cancelled() else inbound.exception()
    upstream_gone = isinstance(outbound_failure, ConnectionClosed)
    if outbound in done and not upstream_gone:
        if isinstance(outbound_failure, ValueError):
            await refuse(1009, "Frame too large")
        # Otherwise Conduit hung up (or its socket broke): nothing to tell it.
        return
    # xAI ended the call, seen by either side of the relay.
    failure = inbound_failure
    if failure is not None and not isinstance(failure, ConnectionClosed):
        logger.warning("Grok Live relay from xAI failed: %s", type(failure).__name__)
    close_code = getattr(upstream, "close_code", None)
    close_reason = str(getattr(upstream, "close_reason", "") or "")
    logger.info("xAI closed the Grok Live socket: code=%s", close_code)
    await refuse(*_forwardable_close(close_code, close_reason))


# --- Voice transcripts -------------------------------------------------------
#
# Conduit's live voice calls (Gemini Live, GPT-Live) never run a Hermes turn,
# so nothing records them. These routes write a call's settled turns straight
# into this profile's session store as an ordinary session row: no gateway,
# no agent, no model call. A resumed call appends to the same row.
#
# Rows keep source "desktop" and no model on purpose: Hermes treats a
# session's source as the agent platform (it picks the toolsets) and restores
# the stored model on resume, so typing into a saved call must look exactly
# like a Conduit chat. Conduit's own labels (voice call / classic voice chat /
# voice job) live in state_meta, which the agent never reads.
VOICE_MAX_BODY_BYTES = 1024 * 1024
VOICE_MAX_TURNS = 500
VOICE_MAX_INDEX = 100_000
VOICE_MAX_TURN_CHARS = 16000
VOICE_MAX_TITLE_CHARS = 120
VOICE_MAX_SUMMARY_CHARS = 8000
VOICE_MAX_TAGS = 2000
VOICE_PRUNE_BATCH = 50
VOICE_TIMEOUT_S = 20.0
VOICE_WRITE_LIMIT = 120
VOICE_WRITE_WINDOW_S = 60.0
VOICE_READ_LIMIT = 600
VOICE_MAX_TIMESTAMP = 32_503_680_000  # year 3000
VOICE_SESSION_SOURCE = "desktop"
VOICE_END_REASON = "conduit_voice"
VOICE_TAG_KINDS = frozenset({"call", "classic", "job"})
VOICE_ENGINES = frozenset({"gemini-live", "gpt-live", "grok-live", "classic"})
VOICE_TAGS_KEY = "conduit.voice.tags"
VOICE_CALLS_KEY = "conduit.voice.calls:{session_id}"
VOICE_CREATED_KEY = "conduit.voice.created"
VOICE_MAX_CALLS = 500
VOICE_SUMMARY_KEY = "conduit.voice.summary:{session_id}"
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VOICE_UNSUPPORTED = "This Hermes version can't store voice transcripts; update Hermes"

_voice_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-voice")
# One writer at a time: the tag map is a read-modify-write JSON value, and a
# call's written-turn counter must not race a retried flush of the same call.
_voice_locks: Dict[str, threading.Lock] = {}
_voice_locks_guard = threading.Lock()


def _voice_lock() -> threading.Lock:
    """One writer at a time per profile, so a slow store in one profile never
    holds up another's (called inside the profile scope)."""
    from hermes_constants import get_hermes_home
    with _voice_locks_guard:
        return _voice_locks.setdefault(str(get_hermes_home()), threading.Lock())
# Conduit writes about once a minute per call plus a tag or summary; this only
# stops a runaway client from growing state.db without bound.
_voice_write_limiter = _MintLimiter(VOICE_WRITE_LIMIT, VOICE_WRITE_WINDOW_S,
                                    message="Too many voice history writes; try again shortly")
_voice_read_limiter = _MintLimiter(VOICE_READ_LIMIT, VOICE_WRITE_WINDOW_S,
                                   message="Too many voice history reads; try again shortly")
_VOICE_READ = ("get_meta",)
_VOICE_WRITE = ("get_meta", "set_meta", "get_session")
_VOICE_SAVE = _VOICE_WRITE + ("ensure_session",)


def _voice_id(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not _VOICE_ID_RE.match(text):
        raise TokenError(400, f"{what} is missing or invalid")
    return text


def _open_voice_db(needs=_VOICE_READ, appends: bool = False):
    """This profile's session store (profile-scoped by _run_scoped); 501 without the methods *needs*,
    or, with *appends*, without either message writer."""
    try:
        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB
    except ImportError as exc:
        if exc.name not in ("hermes_state", "hermes_constants"):
            raise
        raise TokenError(501, _VOICE_UNSUPPORTED)
    # Pin the path: this runs on a worker thread, where Hermes' ambient home
    # lookup could fall back to the launch profile.
    db = SessionDB(db_path=get_hermes_home() / "state.db")
    can_append = hasattr(db, "append_messages_batch") or hasattr(db, "append_message")
    if (appends and not can_append) or not all(hasattr(db, name) for name in needs):
        db.close()
        raise TokenError(501, _VOICE_UNSUPPORTED)
    return db


def _voice_tags(db) -> Dict[str, Dict[str, Any]]:
    try:
        tags = json.loads(db.get_meta(VOICE_TAGS_KEY) or "{}")
    except ValueError:
        logger.warning("Conduit voice tags were unreadable; starting over")
        return {}
    return {k: v for k, v in tags.items() if isinstance(v, dict)} if isinstance(tags, dict) else {}


def _set_voice_tag(db, session_id: str, tag: Dict[str, Any]) -> None:
    # The session isn't required to exist yet: Conduit may tag a chat it just
    # created before Hermes has persisted it. Dangling tags go at the cap.
    tags = _voice_tags(db)
    tags.pop(session_id, None)
    tags[session_id] = tag  # re-inserted last, so it's the newest
    if len(tags) > VOICE_MAX_TAGS:
        # Drop tags whose sessions were deleted, then the oldest if still over;
        # the tag being written always stays.
        # Checks only the oldest few, so a request's work stays bounded.
        oldest = [sid for sid in tags if sid != session_id][:VOICE_PRUNE_BATCH]
        gone = [sid for sid in oldest if not db.get_session(sid)]
        for sid in gone:
            del tags[sid]
            _drop_voice_meta(db, sid)  # a row deleted elsewhere leaves no meta behind
        while len(tags) > VOICE_MAX_TAGS:
            tags.pop(next(iter(tags)))
    db.set_meta(VOICE_TAGS_KEY, json.dumps(tags, separators=(",", ":")))


def _voice_turns(raw: Any) -> list:
    if not isinstance(raw, list):
        raise TokenError(400, "turns must be a list")
    if len(raw) > VOICE_MAX_TURNS:
        raise TokenError(413, f"At most {VOICE_MAX_TURNS} turns per request")
    turns = []
    for item in raw:
        if not isinstance(item, dict):
            raise TokenError(400, "Each turn must be an object")
        role = item.get("role")
        text = item.get("text")
        index = item.get("index")
        if role not in ("user", "assistant") or not isinstance(index, int) or isinstance(index, bool) \
                or not 0 <= index <= VOICE_MAX_INDEX:
            raise TokenError(400, "Each turn needs a role (user or assistant) and an index from 0 to "
                                  f"{VOICE_MAX_INDEX}")
        if text is not None and not isinstance(text, str):
            raise TokenError(400, "A turn's text must be a string")
        # A turn is final once sent: an empty one could only be a turn that
        # isn't done yet, and its index must never change content later.
        text = (text or "").strip()
        if not text:
            raise TokenError(400, "Each turn needs text; send a turn only once it's final")
        at = item.get("at")
        turns.append({
            "index": index,
            "role": role,
            "content": text[:VOICE_MAX_TURN_CHARS],
            "timestamp": at if isinstance(at, (int, float)) and not isinstance(at, bool)
            and math.isfinite(at) and 0 < at < VOICE_MAX_TIMESTAMP else None,
        })
    turns.sort(key=lambda turn: turn["index"])
    return turns


def _new_voice_session_id() -> str:
    try:
        from hermes_state_ids import new_session_id
        return new_session_id()
    except ImportError:
        import uuid
        return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"


def _set_voice_title(db, session_id: str, title: str) -> None:
    # Titles are unique per profile; a clash gets the id's tail, and a second
    # failure leaves the row untitled rather than failing the save.
    for candidate in (title, f"{title} ({session_id[-6:]})"):
        try:
            db.set_session_title(session_id, candidate)
            return
        except ValueError:  # the title is taken
            continue
        except Exception:
            # A title is never worth the transcript: keep the row untitled.
            logger.warning("Couldn't title Conduit voice session %s", session_id, exc_info=True)
            return
    logger.info("Conduit voice session %s kept no title", session_id)


def _voice_text(value: Any, limit: int) -> str:
    # Only strings: an object or list must not be stored as its repr.
    return _clip(value, limit) if isinstance(value, str) else ""


def _append_voice_messages(db, session_id: str, messages: list) -> None:
    if hasattr(db, "append_messages_batch"):
        db.append_messages_batch(session_id, messages)
    else:  # older Hermes: one row at a time
        for message in messages:
            db.append_message(session_id, message["role"], content=message["content"],
                              timestamp=message.get("timestamp"))


def _voice_calls(db, session_id: str) -> Dict[str, int]:
    """The highest turn index written per call_id for this session."""
    try:
        calls = json.loads(db.get_meta(VOICE_CALLS_KEY.format(session_id=session_id)) or "{}")
    except ValueError:
        return {}
    if not isinstance(calls, dict):
        return {}
    return {k: v for k, v in calls.items() if isinstance(v, int) and not isinstance(v, bool)}


def _record_voice_call(db, session_id: str, calls: Dict[str, int], call_id: str, last: int) -> None:
    # One small int per call, in one row per session. Past VOICE_MAX_CALLS
    # calls the oldest are dropped; a call that old is never retried.
    calls.pop(call_id, None)
    calls[call_id] = last
    while len(calls) > VOICE_MAX_CALLS:
        calls.pop(next(iter(calls)))
    db.set_meta(VOICE_CALLS_KEY.format(session_id=session_id), json.dumps(calls, separators=(",", ":")))


def _created_voice_rows(db) -> Dict[str, str]:
    try:
        created = json.loads(db.get_meta(VOICE_CREATED_KEY) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in created.items() if isinstance(v, str)} if isinstance(created, dict) else {}


def _forget_created_voice_row(db, session_id: str) -> None:
    created = _created_voice_rows(db)
    kept = {call: sid for call, sid in created.items() if sid != session_id}
    if len(kept) != len(created):
        db.set_meta(VOICE_CREATED_KEY, json.dumps(kept, separators=(",", ":")))


def _remember_created_voice_row(db, call_id: str, session_id: str) -> None:
    # Which row each call created, so a create whose response was lost and is
    # retried lands in that row instead of a second one. Only recent calls
    # can be retried, so the map keeps the last VOICE_MAX_CALLS.
    created = _created_voice_rows(db)
    created.pop(call_id, None)
    created[call_id] = session_id
    while len(created) > VOICE_MAX_CALLS:
        created.pop(next(iter(created)))
    db.set_meta(VOICE_CREATED_KEY, json.dumps(created, separators=(",", ":")))


def _is_voice_row(db, session_id: str, row: Any) -> bool:
    """Appends go only to a row this plugin saved, never to an ordinary chat."""
    if isinstance(row, dict) and row.get("end_reason") == VOICE_END_REASON:
        return True
    return _voice_tags(db).get(session_id, {}).get("kind") == "call"


def _drop_voice_meta(db, session_id: str) -> None:
    """Clears a gone row's call record, summary and create record (Hermes has no meta delete)."""
    for key in (VOICE_CALLS_KEY, VOICE_SUMMARY_KEY):
        key = key.format(session_id=session_id)
        if db.get_meta(key):
            db.set_meta(key, "")
    _forget_created_voice_row(db, session_id)


def _discard_new_voice_session(db, session_id: str) -> None:
    """Best effort: don't leave an empty or half-written row behind a failed create."""
    try:
        if hasattr(db, "delete_session"):
            db.delete_session(session_id)
        _drop_voice_meta(db, session_id)
        tags = _voice_tags(db)
        if tags.pop(session_id, None) is not None:
            db.set_meta(VOICE_TAGS_KEY, json.dumps(tags, separators=(",", ":")))
    except Exception:
        logger.warning("Couldn't discard the failed Conduit voice session %s", session_id, exc_info=True)


def save_voice_turns(body: Dict[str, Any]) -> Dict[str, Any]:
    """Create a voice-call session or append to one; turns already written for the call are skipped."""
    call_id = _voice_id(body.get("call_id"), "call_id")
    engine = str(body.get("engine") or "").strip()
    if engine not in VOICE_ENGINES:
        raise TokenError(400, "engine is missing or invalid")
    turns = _voice_turns(body.get("turns"))
    title = _voice_text(body.get("title"), VOICE_MAX_TITLE_CHARS)
    requested = body.get("session_id")
    session_id = _voice_id(requested, "session_id") if requested else None
    with _voice_lock():
        db = _open_voice_db(_VOICE_SAVE, appends=True)
        try:
            created = adopted = False
            if session_id:
                row = db.get_session(session_id)
                if not row:
                    _drop_voice_meta(db, session_id)
                    raise TokenError(422, "That voice session no longer exists; start a new one")
                if not _is_voice_row(db, session_id, row):
                    raise TokenError(422, "That session isn't a saved voice call; start a new one")
            else:
                earlier = _created_voice_rows(db).get(call_id)
                if earlier and db.get_session(earlier):
                    session_id = earlier  # a retried create: continue that row
                    adopted = True
                elif earlier:
                    _drop_voice_meta(db, earlier)
            if not session_id:
                if not turns:
                    return {"session_id": None, "written": 0, "appended": 0, "skipped": 0, "created": False}
                if sorted({turn["index"] for turn in turns}) != list(range(len({turn["index"] for turn in turns}))):
                    raise TokenError(400, "Turns must continue from index 0 for this call")
                session_id = _new_voice_session_id()
                db.ensure_session(session_id, source=VOICE_SESSION_SOURCE)
                created = True
            try:
                if created:
                    _remember_created_voice_row(db, call_id, session_id)
                calls = _voice_calls(db, session_id)
                last = calls.get(call_id, -1)
                # A call's indices are contiguous from 0 and a row is
                # append-only, so every index up to `last` is already stored:
                # those are replays of a retried save and are skipped. New
                # turns must continue at last + 1; a gap is refused rather
                # than stored out of order or silently lost.
                fresh = []
                for turn in turns:  # sorted by index
                    if turn["index"] <= last:
                        continue
                    if turn["index"] != last + 1:
                        raise TokenError(400, f"Turns must continue from index {last + 1} for this call")
                    fresh.append(turn)
                    last = turn["index"]
                skipped = len(turns) - len(fresh)
                if skipped:
                    logger.debug("Conduit voice save for %s skipped %d already-written turn(s)", session_id, skipped)
                messages = [{k: v for k, v in turn.items() if k != "index" and v is not None} for turn in fresh]
                if messages:
                    _append_voice_messages(db, session_id, messages)
                if fresh:
                    _record_voice_call(db, session_id, calls, call_id, last)
                if hasattr(db, "end_session") and (created or adopted or fresh):
                    # A saved call is never a live chat; the first end wins, so
                    # later flushes are no-ops. Hermes refuses appends only to rows
                    # that compression closed, so this end doesn't block them.
                    db.end_session(session_id, VOICE_END_REASON)
                if created:
                    if title and hasattr(db, "set_session_title"):
                        _set_voice_title(db, session_id, title)
                    _set_voice_tag(db, session_id, {"kind": "call", "engine": engine})
                elif adopted and _voice_tags(db).get(session_id, {}).get("kind") != "call":
                    # The first attempt died before tagging its row: finish it.
                    _set_voice_tag(db, session_id, {"kind": "call", "engine": engine})
            except Exception:
                if created:
                    _discard_new_voice_session(db, session_id)
                raise
            # `written` is one past the highest index the host has for this
            # call: where the next save starts.
            return {"session_id": session_id, "written": last + 1, "appended": len(messages),
                    "skipped": skipped, "created": created}
        finally:
            db.close()


def set_voice_tag(body: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _voice_id(body.get("session_id"), "session_id")
    kind = body.get("kind")
    if kind not in VOICE_TAG_KINDS - {"call"}:
        raise TokenError(400, "kind must be classic or job")
    tag: Dict[str, Any] = {"kind": kind}
    if body.get("parent_id"):
        tag["parent_id"] = _voice_id(body.get("parent_id"), "parent_id")
    parent_title = _voice_text(body.get("parent_title"), VOICE_MAX_TITLE_CHARS)
    if parent_title:
        tag["parent_title"] = parent_title
    with _voice_lock():
        db = _open_voice_db(_VOICE_WRITE)
        try:
            if _voice_tags(db).get(session_id, {}).get("kind") == "call":
                raise TokenError(422, "That session is a saved voice call; its tag can't change")
            _set_voice_tag(db, session_id, tag)
        finally:
            db.close()
    return {"session_id": session_id, **tag}


def voice_tags() -> Dict[str, Any]:
    db = _open_voice_db()
    try:
        return {"tags": _voice_tags(db)}
    finally:
        db.close()


def voice_summary(session_id: Any) -> Dict[str, Any]:
    session_id = _voice_id(session_id, "session_id")
    db = _open_voice_db()
    try:
        raw = db.get_meta(VOICE_SUMMARY_KEY.format(session_id=session_id))
    finally:
        db.close()
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        stored = {}
    text = str(stored.get("text") or "") if isinstance(stored, dict) else ""
    covers = stored.get("covers") if isinstance(stored, dict) else None
    return {"available": bool(text), "text": text, "covers": covers if isinstance(covers, int) else 0}


def set_voice_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _voice_id(body.get("session_id"), "session_id")
    text = _clip_block(body.get("text"), VOICE_MAX_SUMMARY_CHARS) if isinstance(body.get("text"), str) else ""
    covers = body.get("covers")
    if not text or not isinstance(covers, int) or isinstance(covers, bool) or covers < 0:
        raise TokenError(400, "A summary needs text and a non-negative covers count")
    with _voice_lock():
        db = _open_voice_db(_VOICE_WRITE)
        try:
            row = db.get_session(session_id)
            if not row:
                raise TokenError(422, "That voice session no longer exists; start a new one")
            if not _is_voice_row(db, session_id, row):
                raise TokenError(422, "That session isn't a saved voice call")
            db.set_meta(VOICE_SUMMARY_KEY.format(session_id=session_id),
                        json.dumps({"text": text, "covers": covers}, separators=(",", ":")))
        finally:
            db.close()
    return {"session_id": session_id, "covers": covers}


def _voice_limit(write: bool) -> None:
    """Checked first, before a body is read or a worker is taken. One budget
    for the whole dashboard: the profile isn't resolved yet, and keying on the
    raw query value would let made-up names mint fresh windows."""
    try:
        (_voice_write_limiter if write else _voice_read_limiter).acquire("")
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})


async def _run_voice(profile: Optional[str], fn: Callable[[], Dict[str, Any]], what: str) -> Dict[str, Any]:
    no_store = {"Cache-Control": "no-store"}
    try:
        return await asyncio.wait_for(_run_scoped(profile, fn, _voice_executor), timeout=VOICE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Voice %s for Conduit timed out after %ss", what, VOICE_TIMEOUT_S)
        raise HTTPException(status_code=504, detail=f"Voice {what} timed out", headers=no_store)
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        # Hermes' write guards: a compression in flight is transient (Conduit
        # keeps the turns and retries); a row that compression already closed
        # never takes appends again, so Conduit starts a new row instead.
        names = {cls.__name__ for cls in type(exc).__mro__}
        if "CompressionSessionClosedError" in names:
            raise HTTPException(status_code=422, detail="That session was compacted; start a new one", headers=no_store)
        if "CompressionSessionBusyError" in names:  # includes SessionCompressionInProgressError
            raise HTTPException(status_code=409, detail="The session is busy; try again shortly", headers=no_store)
        raise _unexpected(what, exc, feature="Voice")


async def _voice_body(request: Request) -> Dict[str, Any]:
    body = await _read_json_body(request, VOICE_MAX_BODY_BYTES)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object",
                            headers={"Cache-Control": "no-store"})
    return body


@router.post("/voice/sessions")
async def post_voice_session(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)
    return {"ok": True, **(await _run_voice(profile, lambda: save_voice_turns(body), "save"))}


@router.get("/voice/tags")
async def get_voice_tags(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=False)
    return {"ok": True, **(await _run_voice(profile, voice_tags, "tags"))}


@router.post("/voice/tags")
async def post_voice_tag(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)
    return {"ok": True, **(await _run_voice(profile, lambda: set_voice_tag(body), "tag"))}


@router.get("/voice/summary")
async def get_voice_summary(response: Response, session_id: Optional[str] = None,
                            profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=False)
    return {"ok": True, **(await _run_voice(profile, lambda: voice_summary(session_id), "summary"))}


@router.post("/voice/summary")
async def post_voice_summary(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _voice_limit(write=True)
    body = await _voice_body(request)
    return {"ok": True, **(await _run_voice(profile, lambda: set_voice_summary(body), "summary"))}


# --- Chat takeover -----------------------------------------------------------
#
# Hermes lets one live surface own a chat at a time (hermes_cli.active_sessions,
# the runtime/active_sessions.json registry). Hermes Desktop claims a chat on
# its first turn and keeps the claim until the chat is closed there, so the
# same chat refuses Conduit's next send with SESSION_NOT_OWNED. This route is
# the "Take over" Conduit offers on that refusal (#304): it drops the other
# surface's claim, under the registry's own lock, so the next send claims the
# chat for Conduit. A turn the other surface is running is never cut off: its
# turn marker (tui_gateway.turn_marker) answers "busy" and Conduit asks again.
#
# Only another process's claim is dropped. A claim held by this dashboard
# process (Conduit on another device, or the web chat) is left alone: Hermes
# already hands those over itself once their client is gone. "This process"
# is its pid: Hermes serves the dashboard as one process (its chat runtimes
# and turn leases live in that process's memory), as the voice routes assume.
#
# The owner writes its turn marker without the registry lock, so a turn it
# starts in the instant between the marker check and the write can still lose
# its claim. That turn keeps running in the owner (nothing is interrupted);
# it is the same exposure as the known limit that the owner isn't told.
#
# A timed-out request is abandoned: the worker thread can't be cancelled, but
# it checks the abandon flag once it holds the lock and again right before it
# writes, so a 504 almost never drops a claim after the fact (only a write
# already under way when the timeout fires still lands). Conduit retries a
# 504, and asking again reports the real state.

TAKEOVER_MAX_BODY_BYTES = 4096
TAKEOVER_MAX_IDS = 4
TAKEOVER_MAX_ID_CHARS = 200
TAKEOVER_LIMIT = 60
TAKEOVER_WINDOW_S = 60.0
TAKEOVER_TIMEOUT_S = 20.0
_takeover_limiter = _MintLimiter(TAKEOVER_LIMIT, TAKEOVER_WINDOW_S,
                                 message="Too many takeover requests; try again shortly")
_takeover_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-takeover")


def _takeover_modules() -> Tuple[Any, Any]:
    """Hermes' registry and turn-marker modules, or a clear 501. The route
    leans on private helpers, so their shape is checked, not just their
    presence; and without turn markers a running turn can't be seen, so the
    route refuses rather than risk cutting one off."""
    unsupported = TokenError(501, "This Hermes version can't hand a chat over")
    try:
        from hermes_cli import active_sessions as registry
        from tui_gateway import turn_marker
    except ImportError as exc:
        if exc.name not in ("hermes_cli", "hermes_cli.active_sessions", "tui_gateway", "tui_gateway.turn_marker"):
            raise
        raise unsupported
    # Each helper must accept exactly the call this route makes.
    calls = {
        registry: {"_FileLock": ((None,), {}), "_lease_paths": ((), {"registry_home": None}),
                   "_read_entries": ((None,), {"strict": True}), "_write_entries": ((None, None), {}),
                   "_pid_liveness": ((None, None), {})},
        turn_marker: {"read_turn_marker": ((None, None), {}), "marker_writer_state": ((None,), {})},
    }
    for module, helpers in calls.items():
        for name, (args, kwargs) in helpers.items():
            helper = getattr(module, name, None)
            if not callable(helper):
                raise unsupported
            try:
                inspect.signature(helper).bind(*args, **kwargs)
            except (TypeError, ValueError):
                raise unsupported
    return registry, turn_marker


def _takeover_ids(body: Any) -> list:
    raw = body.get("session_ids") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw or len(raw) > TAKEOVER_MAX_IDS:
        raise TokenError(400, f"session_ids must list 1 to {TAKEOVER_MAX_IDS} chat ids")
    ids = []
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > TAKEOVER_MAX_ID_CHARS:
            raise TokenError(400, f"session_ids must be non-empty strings of at most {TAKEOVER_MAX_ID_CHARS} characters")
        if value.strip() not in ids:
            ids.append(value.strip())
    return ids


def _takeover_home() -> Any:
    """Hermes' home directory, or the same clear 501 as a missing registry."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError as exc:
        if exc.name != "hermes_constants":
            raise
        raise TokenError(501, "This Hermes version can't hand a chat over")
    if not callable(get_hermes_home):
        raise TokenError(501, "This Hermes version can't hand a chat over")
    return get_hermes_home()


def _entry_session_id(entry: Dict[str, Any]) -> str:
    value = entry.get("session_id")
    return "" if value is None or isinstance(value, bool) else str(value)


def _owner_pid(entry: Dict[str, Any]) -> int:
    pid = entry.get("pid")
    if isinstance(pid, bool):
        return 0
    try:
        return int(pid)
    except (TypeError, ValueError):
        return 0


def _owner_turn_running(turn_marker: Any, home: Any, entry: Dict[str, Any], aliases: list,
                        owner_dead: bool = False) -> bool:
    """True while a turn may be in flight on this chat.

    Every Desktop/TUI turn writes a durable marker at start and clears it when
    the turn ends. The marker is looked up under the owner's id and every id
    the caller passed. Any marker counts unless its writer is provably dead
    (crash evidence, not a running turn): the writer need not be the owner's
    pid, since an isolated turn runs in a compute-host child. A marker that
    can't be read counts as running. For an owner that is gone, only a marker
    whose writer is provably alive counts (its isolated child may outlive
    it); anything else there is a crash leftover, as Hermes' prune treats it.

    Keys are looked up in Hermes' marker JSON, never used as a path.
    """
    own_key = _entry_session_id(entry)
    keys = ([own_key] if own_key else []) + [alias for alias in aliases if alias != own_key]
    for key in keys:
        try:
            marker = turn_marker.read_turn_marker(home, key)
        except Exception:  # noqa: BLE001 — can't tell, so never cut a turn off
            return True
        if marker is None:
            continue
        if not isinstance(marker, dict):
            return True  # an unexpected shape can't be read, so it counts as running
        try:
            state = turn_marker.marker_writer_state(marker)
        except Exception:  # noqa: BLE001 — can't tell, so never cut a turn off
            return True
        if state == "alive" or (state != "dead" and not owner_dead):
            return True
    return False


def _owner_alive(registry: Any, entry: Dict[str, Any]) -> Optional[bool]:
    """Hermes' pid + start-time liveness; an error is unknown, which counts as live."""
    try:
        return registry._pid_liveness(_owner_pid(entry), entry.get("process_start_time"))
    except Exception:  # noqa: BLE001
        return None


def take_over_session(session_ids: list, *, registry: Any, turn_marker: Any, home: Any,
                      own_pid: Optional[int] = None,
                      abandoned: Optional[threading.Event] = None) -> Dict[str, Any]:
    """Drop other processes' claims on the chat. ``session_ids`` are the ids
    of ONE chat (its stored id and live id); a running turn under any of
    them makes the whole request busy. Statuses:

    ``free``: nobody else holds it (send again); ``taken_over``: the claims
    were dropped (send again); ``busy``: an owner is mid-turn (ask again
    shortly); ``same_host``: this dashboard process still holds one of the
    ids, which this route never touches (other processes' claims may have
    been dropped). Marker reads happen under the registry lock on purpose:
    the decision and the write must be atomic against other claimers; the
    reads are bounded by the caller's ids (at most TAKEOVER_MAX_IDS) per owner.

    A claim whose pid is missing or invalid can't be attributed to another
    process, so it is kept like this dashboard's own. ``abandoned`` is set
    when the request timed out: nothing is written after that.
    """
    own_pid = os.getpid() if own_pid is None else own_pid
    wanted = set(session_ids)
    state_path, lock_path = registry._lease_paths(registry_home=home)
    def check_abandoned() -> None:
        if abandoned is not None and abandoned.is_set():
            raise TokenError(504, "Chat takeover timed out")

    lock = registry._FileLock(lock_path)
    if not (hasattr(lock, "__enter__") and hasattr(lock, "__exit__")):
        raise TokenError(501, "This Hermes version can't hand a chat over")
    with lock:
        check_abandoned()
        try:
            entries = registry._read_entries(state_path, strict=True)
        except Exception as exc:  # noqa: BLE001 — ActiveSessionRegistryError: never guess ownership
            logger.warning("Chat takeover: active-session registry unreadable: %s", exc)
            raise TokenError(503, "Hermes can't read who owns this chat right now")
        # Registry entries are keyed by session_id alone; the caller passes
        # every id the chat goes by so whichever one the owner used matches.
        owners = [entry for entry in entries
                  if isinstance(entry, dict) and _entry_session_id(entry) in wanted]
        if not owners:
            return {"status": "free"}
        # Kept: this dashboard's own claims, and any claim with no usable pid.
        foreign, own = [], []
        for entry in owners:
            pid = _owner_pid(entry)
            (foreign if pid > 0 and pid != own_pid else own).append(entry)
        if not foreign:
            return {"status": "same_host", "surface": str(own[0].get("surface") or "")}
        # A dead owner is dropped like Hermes' own prune would, unless a
        # live writer is still running its turn; unknown liveness counts as live.
        dead = {id(entry) for entry in foreign if _owner_alive(registry, entry) is False}
        live = [entry for entry in foreign if id(entry) not in dead]
        surface = str((live or foreign)[0].get("surface") or "")
        if any(_owner_turn_running(turn_marker, home, entry, session_ids, owner_dead=id(entry) in dead)
               for entry in foreign):
            return {"status": "busy", "surface": surface}
        dropped = {id(entry) for entry in foreign}  # by identity: never let a blank lease id match others
        check_abandoned()
        registry._write_entries(state_path, [entry for entry in entries if id(entry) not in dropped])
    logger.info("Chat takeover: Conduit took a chat over (%d claim(s) dropped)", len(foreign))
    if own:
        # This dashboard still holds one of the ids, so a send can still be refused.
        return {"status": "same_host", "surface": str(own[0].get("surface") or "")}
    return {"status": "taken_over", "surface": surface}


def _take_over_scoped(session_ids: list, abandoned: Optional[threading.Event] = None) -> Dict[str, Any]:
    registry, turn_marker = _takeover_modules()
    return take_over_session(session_ids, registry=registry, turn_marker=turn_marker, home=_takeover_home(),
                             abandoned=abandoned)


@router.post("/sessions/takeover")
async def post_session_takeover(request: Request, response: Response,
                                profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    try:
        body = await _read_json_body(request, TAKEOVER_MAX_BODY_BYTES)
        ids = _takeover_ids(body)
        # One budget for the whole dashboard, like the voice routes: the
        # profile isn't resolved yet, so a made-up name can't mint a window.
        _takeover_limiter.acquire("")
        abandoned = threading.Event()
        try:
            result = await asyncio.wait_for(
                _run_scoped(profile, lambda: _take_over_scoped(ids, abandoned), _takeover_executor),
                timeout=TAKEOVER_TIMEOUT_S)
        except asyncio.TimeoutError:
            abandoned.set()
            raise
    except asyncio.TimeoutError:
        logger.warning("Chat takeover timed out after %ss", TAKEOVER_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Chat takeover timed out", headers=no_store)
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise _unexpected("takeover", exc, feature="Chat takeover")
    return {"ok": True, **result}
