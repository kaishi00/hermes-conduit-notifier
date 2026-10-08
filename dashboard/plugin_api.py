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

Watch tools: a Conduit Watch call's lookups while the Watch can't reach the
iPhone. Each call gets a short-lived grant on the push relay; the plugin
answers the Watch's sealed calls by polling the relay, so nothing new is
reachable here from outside.

Routes sit behind the dashboard's own auth, the same as /api/audio/*.
The API key is never returned, logged, or written anywhere.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextvars
import functools
import hashlib
import importlib.util
import inspect
import itertools
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
import contextlib
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

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
        # Some providers skip recall as well as writes outside "primary". The
        # recall provider never writes (no sync_turn, no memory tool), so it is
        # safe there; a call's end needs it to write the call to memory.
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
GPT_LIVE_MAX_GREETING_CHARS = 200


def _greet_first(greeting: str) -> str:
    """The opening policy for a user who asked to be greeted when a call connects."""
    say = f' Say: "{greeting}"' if greeting else ""
    return ("Opening policy: speak first. As soon as the call starts, greet the user in one short "
            f"sentence so they know you're there and listening.{say} Then wait for them. "
            "This applies whatever the persona above says.")


def _clean_greeting(greeting: Any) -> Optional[str]:
    """None keeps the call silent until the user speaks; text (or "" for a default
    greeting) makes the model greet first. Kept to one printable line without
    double quotes so it reads as the line to say. It is the user's own setting
    for their own call, not untrusted input: nothing here stops it from reading
    as an instruction."""
    if greeting is None:
        return None
    if not isinstance(greeting, str):
        raise TokenError(400, "greeting must be text")
    if len(greeting) > GPT_LIVE_MAX_GREETING_CHARS * 4:
        raise TokenError(400, "greeting is too long")
    greeting = "".join(ch if ch.isprintable() else " " for ch in greeting.replace('"', "'"))
    greeting = " ".join(greeting.split())
    if len(greeting) > GPT_LIVE_MAX_GREETING_CHARS:
        raise TokenError(400, "greeting is too long")
    return greeting


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


def _built_has_instructions(config: Any, text: Optional[str]) -> bool:
    """True only when Hermes' config really carries the text (the briefing or the
    opening policy), so Conduit never drops it unsent."""
    if not text:
        return False
    instructions = _built_session(config).get("instructions")
    return isinstance(instructions, str) and text in instructions


def create_gpt_live_session(
    sdp: Any,
    history: Any = None,
    limiter_key: Optional[str] = None,
    post: Optional[Callable[..., tuple]] = None,
    *,
    voice: Any = None,
    briefing: Any = None,
    greeting: Any = None,
) -> Dict[str, Any]:
    if not isinstance(sdp, str) or not sdp.startswith("v=0"):
        raise TokenError(400, "sdp must be a WebRTC SDP offer")
    history = _clean_history(history)
    voice = _clean_voice(voice)
    briefing = _clean_briefing(briefing)
    greeting = _clean_greeting(greeting)
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
    opening = GPT_LIVE_WAIT_FOR_USER if greeting is None else _greet_first(greeting)
    extra = str(live.get("instructions") or "").strip()
    live["instructions"] = f"{extra}\n\n{opening}" if extra else opening
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
                "voice": applied_voice, "briefing_applied": _built_has_instructions(config, briefing),
                "greeting_applied": greeting is not None and _built_has_instructions(config, opening)}
    config = gpt_live_session_config(history, live)
    return {**_plugin_gpt_live_session(sdp, config, post or _post_sdp), "source": "plugin", "voice": applied_voice,
            "briefing_applied": bool(briefing), "greeting_applied": greeting is not None}


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
                                                briefing=body.get("briefing"), greeting=body.get("greeting")),
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
# Conduit saves a call once, when it ends (retried from its outbox if that
# fails), plus a tag or summary; this only stops a runaway client from growing
# state.db without bound.
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


def save_voice_turns(body: Dict[str, Any],
                     on_end: Optional[Callable[[str, str, list], None]] = None) -> Dict[str, Any]:
    """Create a voice-call session or append to one; turns already written for the call are skipped.

    ``on_end(session_id, engine, messages)`` runs once new turns are stored:
    Conduit saves a call only after it has ended, so those turns end it. It runs
    with the store closed but the save lock held, so a session's ends keep the
    order its turns were stored in; it must be quick and must not raise (the
    turns are already stored).
    """
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
            result = {"session_id": session_id, "written": last + 1, "appended": len(messages),
                      "skipped": skipped, "created": created}
        finally:
            db.close()
        if messages and on_end is not None:
            on_end(session_id, engine, messages)
    return result


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

    def ended(session_id: str, engine: str, messages: list) -> None:
        _queue_voice_call_end(profile, session_id, engine, messages)

    return {"ok": True, **(await _run_voice(profile, lambda: save_voice_turns(body, on_end=ended), "save"))}


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


# --- Call end ------------------------------------------------------------------
#
# A live call never runs a Hermes turn, so Hermes never learns it is over: no
# session-end hook fires and the memory provider (Honcho, Mem0, …) never sees
# the conversation (hermes-conduit#416). Conduit saves a call only once it has
# ended (hung up, dropped or timed out), so a save that stores new turns is
# that call's end. The plugin then ends the session the way Hermes ends a
# Desktop chat (tui_gateway's _finalize_session): the on_session_end hook, the
# memory provider's sync_turn per exchange and its on_session_end, then
# on_session_finalize. A retried save that stores nothing new ends nothing.
#
# It runs off the request, one call at a time, so a slow provider never holds
# up a save. Memory gets only the call's new turns: a resumed call's earlier
# turns went there when their own call ended.
# Hermes runs a "desktop" row as the desktop platform.
VOICE_END_PLATFORM = VOICE_SESSION_SOURCE
VOICE_END_HOOK_REASON = "voice_call_ended"
# Past this a wedged hook or provider is left running, so the next call still ends.
VOICE_END_TIMEOUT_S = 120.0
# The wait for queued sync_turn writes before the provider's on_session_end.
VOICE_END_FLUSH_TIMEOUT_S = 30.0
# Calls waiting behind a slow one; past this a call's end is skipped (and logged).
VOICE_END_MAX_PENDING = 20
# Ends left running past VOICE_END_TIMEOUT_S in one profile; past this that
# profile's calls end without these steps (and logged) until one finishes.
VOICE_END_MAX_ABANDONED = 3

_VOICE_END_MODULES = _MISSING_MEMORY_MODULES | {"hermes_cli.lifecycle", "hermes_cli.plugins", "agent.memory_manager"}

# A daemon worker, not an executor: an executor's worker is joined at exit,
# so a wedged provider could hold up a gateway restart. An end still running
# when the dashboard exits is dropped, as Hermes drops its own memory writes.
_voice_end_jobs: "queue.SimpleQueue[Callable[[], None]]" = queue.SimpleQueue()
_voice_end_worker: Optional[threading.Thread] = None
_voice_end_guard = threading.Lock()
_voice_end_pending = 0
_voice_end_abandoned: Dict[str, int] = {}


def _voice_exchanges(messages: list) -> list:
    """(user, assistant) pairs as Hermes syncs a turn: what the user said, then
    everything said back. Hermes skips a turn missing either side, so a greeting
    before the user spoke and an unanswered last question stay out."""
    exchanges = []
    user: list = []
    assistant: list = []
    for message in messages:
        if message["role"] == "user":
            if assistant:
                if user:
                    exchanges.append(("\n".join(user), "\n".join(assistant)))
                user, assistant = [], []
            user.append(message["content"])
        else:
            assistant.append(message["content"])
    if user and assistant:
        exchanges.append(("\n".join(user), "\n".join(assistant)))
    return exchanges


def _fire_voice_hook(name: str, **kwargs: Any) -> None:
    """Fire a lifecycle hook as Hermes does: its own observers, then plugins.
    on_session_finalize goes through finalize_session, which also closes the
    session's Relay conversation. A Hermes without hermes_cli.lifecycle calls
    the plugin hooks directly."""
    try:
        from hermes_cli import lifecycle
    except ImportError as exc:
        if exc.name not in ("hermes_cli", "hermes_cli.lifecycle"):
            raise
        from hermes_cli.plugins import invoke_hook

        invoke_hook(name, **kwargs)
        return
    if name == "on_session_finalize" and hasattr(lifecycle, "finalize_session"):
        lifecycle.finalize_session(**kwargs)
    else:
        lifecycle.invoke_hook(name, **kwargs)


def _voice_session_title(session_id: str) -> Dict[str, str]:
    """The row's title and its provenance, which Hermes hands a provider too
    (Honcho can name its session after the title). Empty when unreadable."""
    try:
        db = _open_voice_db()
    except Exception:  # noqa: BLE001 — the title is optional
        return {}
    try:
        title = db.get_session_title(session_id) if hasattr(db, "get_session_title") else None
        if not title:
            return {}
        found = {"session_title": title}
        source = db.get_session_title_source(session_id) if hasattr(db, "get_session_title_source") else None
        if source:
            found["session_title_source"] = source
        return found
    except Exception:  # noqa: BLE001
        logger.debug("Couldn't read the title of Conduit voice session %s", session_id, exc_info=True)
        return {}
    finally:
        db.close()


def _commit_voice_call_memory(session_id: str, messages: list) -> None:
    """Write a finished call to the profile's memory provider the way Hermes
    writes a chat: initialize it for the session, sync_turn each exchange, then
    on_session_end and shutdown. A provider instance of its own: the cached
    recall one is bound to the conduit-voice session."""
    name = _configured_memory_provider(_hermes_memory_config())
    if name is None:
        return
    from agent.memory_manager import MemoryManager

    provider = _hermes_load_memory_provider(name)
    if provider is None or not provider.is_available():
        logger.info("Memory provider %r is not available; a Conduit voice call isn't written to it", name)
        return
    manager = MemoryManager()
    manager.add_provider(provider)
    kwargs = {**_memory_provider_init_kwargs(), "platform": VOICE_END_PLATFORM, **_voice_session_title(session_id)}
    try:
        manager.initialize_all(session_id=session_id, **kwargs)
        for user, assistant in _voice_exchanges(messages):
            manager.sync_all(user, assistant, session_id=session_id)
        # sync_all writes on the manager's worker: let those land before the end.
        flush = getattr(manager, "flush_pending", None)
        if callable(flush):
            flush(timeout=VOICE_END_FLUSH_TIMEOUT_S)
        manager.on_session_end([{"role": m["role"], "content": m["content"]} for m in messages])
    finally:
        manager.shutdown_all()


def _end_voice_call(session_id: str, engine: str, messages: list) -> None:
    """End a saved call's session in Hermes (inside the profile's scope). Each
    step runs even if an earlier one failed."""
    hook = {"session_id": session_id, "platform": VOICE_END_PLATFORM, "reason": VOICE_END_HOOK_REASON}
    steps = (
        ("on_session_end hook", lambda: _fire_voice_hook(
            "on_session_end", completed=True, interrupted=False, model=engine, **hook)),
        ("memory write", lambda: _commit_voice_call_memory(session_id, messages)),
        ("on_session_finalize hook", lambda: _fire_voice_hook("on_session_finalize", **hook)),
    )
    for what, step in steps:
        try:
            step()
        except ImportError as exc:
            if exc.name not in _VOICE_END_MODULES:
                logger.warning("Conduit voice call end: %s failed for %s", what, session_id, exc_info=True)
            else:
                logger.debug("Conduit voice call end: this Hermes has no %s (%s)", what, exc.name)
        except Exception:  # noqa: BLE001 — a hook or provider must never stop the rest
            logger.warning("Conduit voice call end: %s failed for %s", what, session_id, exc_info=True)


def _run_voice_call_end(profile: Optional[str], session_id: str, engine: str, messages: list) -> None:
    """Runs the end on its own thread and waits up to VOICE_END_TIMEOUT_S, so a
    wedged provider costs one abandoned thread, not every later call's end.

    The next call's end can then overlap the abandoned one, each with its own
    provider instance. Hermes' gateway does the same (one provider per agent,
    abandoned on a cleanup timeout), so providers already allow it. A profile
    keeps at most VOICE_END_MAX_ABANDONED of them, so a provider that never
    returns can't pile up threads, and other profiles' calls still end."""
    key = profile or ""
    with _voice_end_guard:
        if _voice_end_abandoned.get(key, 0) >= VOICE_END_MAX_ABANDONED:
            logger.warning("Conduit voice session %s ends without hooks: earlier call ends in this profile "
                           "are still stuck", session_id)
            return
    done = threading.Event()
    abandoned = False

    def run() -> None:
        try:
            with _profile_scope(profile):
                _end_voice_call(session_id, engine, messages)
        except Exception:  # noqa: BLE001 — the scope itself failed; nothing ran
            logger.warning("Conduit voice call end failed for %s", session_id, exc_info=True)
        finally:
            with _voice_end_guard:
                done.set()
                if abandoned:
                    _voice_end_abandoned[key] -= 1
                    if not _voice_end_abandoned[key]:
                        del _voice_end_abandoned[key]

    try:
        threading.Thread(target=run, name="conduit-voice-end-call", daemon=True).start()
    except RuntimeError:  # no thread to spare
        logger.warning("Conduit voice session %s ends without hooks: no thread to run them", session_id)
        return
    if not done.wait(VOICE_END_TIMEOUT_S):
        with _voice_end_guard:
            if not done.is_set():
                abandoned = True
                _voice_end_abandoned[key] = _voice_end_abandoned.get(key, 0) + 1
                logger.warning("Ending Conduit voice session %s took over %ss; moving on (it may still finish)",
                               session_id, VOICE_END_TIMEOUT_S)


def _voice_end_loop() -> None:
    while True:
        job = _voice_end_jobs.get()
        try:
            job()
        except Exception:  # noqa: BLE001 — one call's end never stops the next
            logger.warning("A Conduit voice call end failed", exc_info=True)


def _queue_voice_call_end(profile: Optional[str], session_id: str, engine: str, messages: list) -> None:
    """Ends the call's session off the request. Never raises: the turns are
    already stored, and a failed save would make Conduit send them again."""
    try:
        _enqueue_voice_call_end(profile, session_id, engine, messages)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit voice session %s ends without hooks: queueing failed", session_id, exc_info=True)


def _enqueue_voice_call_end(profile: Optional[str], session_id: str, engine: str, messages: list) -> None:
    global _voice_end_pending, _voice_end_worker

    def run() -> None:
        global _voice_end_pending
        try:
            _run_voice_call_end(profile, session_id, engine, messages)
        finally:
            with _voice_end_guard:
                _voice_end_pending -= 1

    with _voice_end_guard:
        if _voice_end_pending >= VOICE_END_MAX_PENDING:
            logger.warning("Too many Conduit voice calls waiting to end; session %s ends without hooks", session_id)
            return
        try:
            if _voice_end_worker is None or not _voice_end_worker.is_alive():
                _voice_end_worker = threading.Thread(target=_voice_end_loop, name="conduit-voice-end", daemon=True)
                _voice_end_worker.start()
        except RuntimeError:  # no thread to spare, or the dashboard is exiting
            logger.warning("Conduit voice session %s ends without hooks: no thread to run them", session_id)
            return
        # Counted once queued, so a failed put holds no slot (the worker's
        # count down waits for this guard).
        _voice_end_jobs.put(run)
        _voice_end_pending += 1


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
    except (TypeError, ValueError, OverflowError):
        return 0


def _owner_turn_running(turn_marker: Any, home: Any, entry: Dict[str, Any], aliases: list,
                        owner_dead: bool = False) -> bool:
    """True while a turn may be in flight on this chat.

    Every Desktop/TUI turn writes a durable marker at start and clears it when
    the turn ends. The marker is looked up under the owner's id and every id
    the caller passed. Any marker counts unless its writer is provably dead
    (crash evidence, not a running turn): the writer need not be the owner's
    pid, since an isolated turn runs in a compute-host child. A marker that
    can't be read counts as running. For an owner that is gone, a marker counts
    unless its writer is provably dead or it names no writer at all (its
    isolated child may outlive it); those are crash leftovers.

    The marker helpers never take the registry lock: liveness is a pid and
    start-time probe (hermes_cli.active_sessions._pid_liveness).

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
        # A gone owner's marker with no writer identity is a leftover from a
        # build without isolated turns; one naming a writer of unknown
        # liveness may be a child still running, so it counts.
        identified = marker.get("writer_pid") is not None
        if state == "alive" or (state != "dead" and (not owner_dead or identified)):
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


# --- End-to-end encrypted notifications (#431) -------------------------------
#
# Conduit creates a per-pairing secret on the phone and hands it to this
# profile here, over the dashboard connection, so it never passes through the
# push relay. The hooks (client.py) read it from the profile's
# conduit-push.json, seal every notification with it and only accept clarify
# answers sealed by the phone. GET never returns the secret.

E2E_LIMIT = 10
E2E_WINDOW_S = 60.0
E2E_MAX_BODY_BYTES = 4 * 1024
_E2E_KID = re.compile(r"^[0-9a-f]{32}$")
_E2E_B64URL = re.compile(r"^[A-Za-z0-9_-]{43}$")  # 32 bytes, unpadded
_e2e_limiter = _MintLimiter(E2E_LIMIT, E2E_WINDOW_S, message="Too many encryption key requests; try again shortly")
_e2e_lock = threading.Lock()


def _pairing_state_path() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "conduit-push.json"


def _load_pairing_state(path: Any) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(value, dict) or not value.get("credential"):
        return None
    return value


def _pairing_state_lock_path(path: Any) -> Any:
    return path.with_name(f".{path.name}.lock")


@contextlib.contextmanager
def _pairing_state_lock(path: Any):
    # The same lock file client.state_file_lock takes in the agent process,
    # so a hook rewriting the state can't drop a key stored here (or the
    # other way round).
    with open(_pairing_state_lock_path(path), "a+b") as handle:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        try:
            import msvcrt
        except ImportError:
            yield
            return
        # Windows: the same first-byte lock client.state_file_lock takes.
        handle.seek(0)
        for attempt in range(6):  # about a minute
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


def _save_pairing_state(path: Any, value: Dict[str, Any]) -> None:
    # Same atomic, owner-only write as client.save_state.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _e2e_crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False
    return True


def e2e_status(path: Any = None) -> Dict[str, Any]:
    """This profile's pairing and key state. Never includes the secret."""
    state = _load_pairing_state(path if path is not None else _pairing_state_path())
    crypto = _e2e_crypto_available()
    if not state:
        return {"paired": False, "crypto": crypto}
    record = state.get("e2e")
    kid = record.get("kid") if isinstance(record, dict) else None
    secret = record.get("secret") if isinstance(record, dict) else None
    # A key whose secret is unusable reads as none, so the phone replaces it.
    if not isinstance(secret, str) or not _E2E_B64URL.match(secret):
        kid = None
    return {
        "paired": True,
        "installation_id": state.get("installation_id"),
        "gateway_id": state.get("gateway_id"),
        "e2e": {"kid": kid} if isinstance(kid, str) and _E2E_KID.match(kid) else None,
        "crypto": crypto,
    }


def provision_e2e(body: Any, path: Any = None) -> Dict[str, Any]:
    """Stores the phone's pairing secret for this profile.

    Only for the pairing the phone owns: the installation and gateway ids
    must match this profile's pairing, so a key for another phone (or an old
    pairing) is refused. Replaces an earlier key for the same pairing.
    """
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object")
    installation_id = body.get("installation_id")
    gateway_id = body.get("gateway_id")
    kid = body.get("kid")
    secret = body.get("secret")
    if not isinstance(installation_id, str) or not installation_id or not isinstance(gateway_id, str) or not gateway_id:
        raise TokenError(400, "installation_id and gateway_id are required")
    if not isinstance(kid, str) or not _E2E_KID.match(kid):
        raise TokenError(400, "kid must be 32 lowercase hex characters")
    if not isinstance(secret, str) or not _E2E_B64URL.match(secret):
        raise TokenError(400, "secret must be 32 bytes, base64url without padding")
    if not _e2e_crypto_available():
        raise TokenError(501, "Encrypted notifications need the cryptography package on this host")
    path = path if path is not None else _pairing_state_path()
    with _e2e_lock, _pairing_state_lock(path):
        state = _load_pairing_state(path)
        if not state:
            raise TokenError(409, "This Hermes profile isn't paired with Conduit")
        if not state.get("gateway_id"):
            raise TokenError(409, "Pair this Hermes profile with Conduit again to turn on encryption")
        if state.get("installation_id") != installation_id or state.get("gateway_id") != gateway_id:
            raise TokenError(409, "This Hermes profile is paired with a different device")
        state["e2e"] = {
            "kid": kid,
            "secret": secret,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save_pairing_state(path, state)
    return {"kid": kid}


@router.get("/e2e")
async def get_e2e(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await _run_scoped(profile, e2e_status))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise _unexpected("status", exc, feature="Encrypted notifications")


@router.post("/e2e")
async def post_e2e(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body is a secret: never cached on the way back either.
    response.headers["Cache-Control"] = "no-store"
    try:
        _e2e_limiter.acquire(_limiter_key(profile))
        body = await _read_json_body(request, E2E_MAX_BODY_BYTES)
        return {"ok": True, **(await _run_scoped(profile, lambda: provision_e2e(body)))}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise _unexpected("key", exc, feature="Encrypted notifications")


# --- Watch tools (wrist-down lookups for a Conduit Watch call) ---------------
#
# With the wrist down, a Conduit Watch call can't reach Conduit on the iPhone,
# but it can reach the push relay over its own internet. For each call Conduit
# asks this profile for a grant (dashboard auth, as every route here); the
# plugin opens it on the relay, long-polls the relay for the Watch's calls,
# runs them with the same code as /web-search and /memory/recall, and posts
# the answers back. This host opens no inbound route of its own.
#
# The Watch gets only the grant: a per-call key (HKDF-SHA256 root for the two
# ChaCha20-Poly1305 directions, never sent to the relay) and a relay key the
# relay keeps only as a SHA-256. It allows web_search and recall_memory, for
# this profile, for 30 minutes and 60 calls, with the user's say-so Hermes
# jobs too (Watch jobs, below), and Gemini Live tokens where the profile has
# a key (live_token). Grants live in memory: a dashboard
# restart drops them and the Watch falls back to the iPhone.
# (hermes-conduit designs/apple-watch-voice-direct.md)
#
# A grant can also carry live_token: a fresh single-use Gemini Live token,
# minted as /gemini-live/token mints one, for a Watch whose Gemini session
# broke and can't be resumed while the iPhone is out of reach. It travels
# sealed like any answer, so the relay never sees it; the Gemini key stays
# here.

WATCH_TOOLS = ("web_search", "recall_memory")
WATCH_LIVE_TOKEN = "live_token"
# Tokens one grant may mint: a few fresh sessions per call, never a stream.
# A renewal, which only the iPhone can ask for, counts its own.
WATCH_LIVE_TOKENS_PER_GRANT = 6
WATCH_GRANT_TTL_S = 30 * 60
WATCH_GRANT_MAX_CALLS = 60
# Live grants per profile: a new one closes the oldest.
WATCH_GRANTS_PER_PROFILE = 2
WATCH_GRANT_LIMIT = 10
WATCH_GRANT_WINDOW_S = 60.0
# Tools, the job cap and the phone's job model settings.
WATCH_GRANT_MAX_BODY_BYTES = 2048
# The relay holds a poll this long when no call waits.
WATCH_POLL_WAIT_MS = 25_000
WATCH_POLL_TIMEOUT_S = WATCH_POLL_WAIT_MS / 1000 + 10
WATCH_RELAY_TIMEOUT_S = 10.0
WATCH_POLL_BACKOFF_MAX_S = 10.0
# A sealed call is a tool name and a short query. The relay's bound is the
# same 4 KB sealed (relay/src/watch-tools.mjs MAX_CALL_CT_CHARS), so it passes
# on no call this host would refuse to open.
WATCH_MAX_CALL_BYTES = 4 * 1024
# The relay's bound on a sealed answer (base64url characters).
WATCH_MAX_RESULT_CT_CHARS = 24_000
# A job_news call holds its worker while it waits for news.
WATCH_WORKERS = 8
_WATCH_SALT = b"conduit-watch-tools-v1"
_WATCH_INFO = {
    "call": b"conduit-watch-tools-v1 call watch-to-host",
    "result": b"conduit-watch-tools-v1 result host-to-watch",
}
_WATCH_AAD_TAG = "conduit-watch-tools/1"
_WATCH_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
_WATCH_NONCE = re.compile(r"^[A-Za-z0-9_-]{16}$")
_WATCH_CT = re.compile(r"^[A-Za-z0-9_-]{22,}$")
# The relay keys travel to it: HTTPS only (tests widen this for a local relay).
_WATCH_RELAY_SCHEMES: Tuple[str, ...] = ("https://",)
_watch_grant_limiter = _MintLimiter(WATCH_GRANT_LIMIT, WATCH_GRANT_WINDOW_S,
                                    message="Too many Watch tool grants; try again shortly")


class WatchToolError(Exception):
    """A sealed Watch call or answer that can't be opened or built."""


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64u(value: Any) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", value) or len(value) % 4 == 1:
        raise WatchToolError("malformed base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def watch_tool_keys(secret: bytes) -> Dict[str, bytes]:
    """The grant's two directional keys, derived from its 32-byte root."""
    if len(secret) != 32:
        raise WatchToolError("the grant secret must be 32 bytes")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return {
        direction: HKDF(algorithm=hashes.SHA256(), length=32, salt=_WATCH_SALT, info=info).derive(secret)
        for direction, info in _WATCH_INFO.items()
    }


def watch_tool_aad(direction: str, grant_id: str, rid: str) -> bytes:
    return "\n".join([_WATCH_AAD_TAG, direction, f"grant={grant_id}", f"rid={rid}"]).encode("utf-8")


def seal_watch_tool(key: bytes, direction: str, grant_id: str, rid: str, payload: Dict[str, Any],
                    nonce: Optional[bytes] = None) -> Dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    # A fixed nonce is for test vectors only.
    nonce = nonce if nonce is not None else os.urandom(12)
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, data, watch_tool_aad(direction, grant_id, rid))
    return {"n": _b64u(nonce), "ct": _b64u(ciphertext)}


def open_watch_tool(key: bytes, direction: str, grant_id: str, rid: str, sealed: Any,
                    max_bytes: int = WATCH_MAX_CALL_BYTES) -> Dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if not isinstance(sealed, dict):
        raise WatchToolError("the envelope is malformed")
    nonce_text, ct_text = sealed.get("n"), sealed.get("ct")
    if not isinstance(nonce_text, str) or not _WATCH_NONCE.match(nonce_text):
        raise WatchToolError("the envelope is malformed")
    # Bounded before decoding: base64url is 4 characters per 3 bytes, plus
    # the 16-byte tag.
    if not isinstance(ct_text, str) or not _WATCH_CT.match(ct_text) or len(ct_text) > (max_bytes + 16) * 4 // 3 + 4:
        raise WatchToolError("the envelope is malformed")
    try:
        plain = ChaCha20Poly1305(key).decrypt(_unb64u(nonce_text), _unb64u(ct_text), watch_tool_aad(direction, grant_id, rid))
    except WatchToolError:
        raise
    except Exception as error:
        raise WatchToolError("the envelope did not verify") from error
    try:
        value = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise WatchToolError("the payload is not JSON") from error
    if not isinstance(value, dict):
        raise WatchToolError("the payload is not an object")
    return value


class _NoRedirectRelay(urllib.request.HTTPRedirectHandler):
    # The relay credential travels in a header: never to another host.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_relay_opener = urllib.request.build_opener(_NoRedirectRelay)


def _relay_request(url: str, method: str, credential: str, payload: Optional[Dict[str, Any]],
                   timeout: float) -> Tuple[int, Dict[str, Any]]:
    """One relay request: (status, JSON body). Network failures raise."""
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {credential}",
               "User-Agent": f"Hermes-Conduit-Notifier/{_plugin_version() or 'unknown'}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _relay_opener.open(request, timeout=timeout) as response:
            raw = response.read(64 * 1024)
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read(4 * 1024)
        status = error.code
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


class _WatchGrant:
    def __init__(self, *, grant_id: str, profile: Optional[str], tools: Tuple[str, ...], secret: bytes,
                 relay_url: str, credential: str, expires_at: float,
                 max_calls: Optional[int] = None) -> None:
        keys = watch_tool_keys(secret)
        self.grant_id = grant_id
        self.profile = profile
        self.limiter_key = _limiter_key(profile)
        self.tools = tools
        self.call_key = keys["call"]
        self.result_key = keys["result"]
        self.relay_url = relay_url
        self.credential = credential
        self.expires_at = expires_at
        self.max_calls = max_calls if max_calls is not None else WATCH_GRANT_MAX_CALLS
        self.created_at = time.monotonic()
        # Set when the grant has jobs (_WatchJobs).
        self.jobs: Optional["_WatchJobs"] = None
        # Set when the grant carries a call's audio (_WatchAudioBridge).
        self.audio: Optional["_WatchAudioBridge"] = None
        self.used = 0
        # Gemini Live tokens minted for the Watch (run_watch_live_token).
        self.live_tokens = 0
        self.seen: set = set()
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def url(self, suffix: str = "") -> str:
        return f"{self.relay_url}/v1/watch-tools/grants/{self.grant_id}{suffix}"


class _WatchGrants:
    """The live grants of this dashboard process."""

    def __init__(self) -> None:
        self._grants: Dict[str, _WatchGrant] = {}
        self._lock = threading.Lock()

    def add(self, grant: _WatchGrant) -> list:
        """Adds ``grant``; returns the grants it pushed out (oldest first)."""
        with self._lock:
            owned = sorted((g for g in self._grants.values() if g.limiter_key == grant.limiter_key),
                           key=lambda g: g.created_at)
            evicted = owned[: max(0, len(owned) - WATCH_GRANTS_PER_PROFILE + 1)]
            for old in evicted:
                self._grants.pop(old.grant_id, None)
            self._grants[grant.grant_id] = grant
            return evicted

    def get(self, grant_id: str) -> Optional[_WatchGrant]:
        with self._lock:
            return self._grants.get(grant_id)

    def remove(self, grant: _WatchGrant) -> bool:
        with self._lock:
            if self._grants.get(grant.grant_id) is grant:
                del self._grants[grant.grant_id]
                return True
            return False

    def all(self) -> list:
        with self._lock:
            return list(self._grants.values())


_watch_grants = _WatchGrants()
_watch_executor = ThreadPoolExecutor(max_workers=WATCH_WORKERS, thread_name_prefix="conduit-watch-tools")
# While a call runs: where it registers what to undo if its answer never
# reaches the Watch (answer_watch_call).
_watch_undelivered: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "conduit_watch_undelivered", default=None)


def _close_watch_grant_here(grant: _WatchGrant) -> bool:
    """Stops answering for ``grant``; True when this call closed it."""
    with grant.lock:
        already = grant.closed.is_set()
        grant.closed.set()
    _watch_grants.remove(grant)
    if grant.audio is not None:
        grant.audio.stop()
    # Jobs a renewal carried away live on with the newer grant.
    if grant.jobs is not None and not already and grant.jobs.grant is grant:
        grant.jobs.end()
    return not already


def _close_watch_grant_on_relay(grant: _WatchGrant,
                                relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None) -> None:
    relay = relay or _relay_request
    try:
        relay(grant.url(), "DELETE", grant.credential, None, WATCH_RELAY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — it expires on the relay anyway
        logger.info("Closing a Watch tool grant on the relay failed: %s", type(exc).__name__)


def _log_watch_failure(message: str, exc: BaseException) -> None:
    """Logs a Watch tool failure by its type. The message can carry the
    query, so it isn't logged at any level; debug adds where it failed
    (file, line and function of each frame, no source text)."""
    logger.warning("%s: %s", message, type(exc).__name__)
    if logger.isEnabledFor(logging.DEBUG):
        frames = "".join(f"  {frame.filename}:{frame.lineno} in {frame.name}\n"
                         for frame in traceback.extract_tb(exc.__traceback__))
        logger.debug("%s: %s at\n%s", message, type(exc).__name__, frames)


def _close_watch_grant(grant: _WatchGrant, *, tell_relay: bool,
                       relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None) -> None:
    """Stops answering for ``grant``; best effort at the relay."""
    if _close_watch_grant_here(grant) and tell_relay:
        _close_watch_grant_on_relay(grant, relay)


def _scoped_call(profile: Optional[str], fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    with _profile_scope(profile):
        return fn()


def _clean_watch_args(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items()
            if isinstance(k, str) and (isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool)))}


def run_watch_tool(grant: _WatchGrant, request: Dict[str, Any]) -> Dict[str, Any]:
    """One Watch call, answered with the body its dashboard route would give.

    Failures come back as {"ok": false, "status": ..., "detail": ...}, the
    detail being what the route's HTTPException would carry.
    """
    tool = request.get("tool")
    args = _clean_watch_args(request.get("args"))
    if tool not in grant.tools:
        return {"ok": False, "status": 403, "detail": "This tool isn't available to the Watch"}
    if tool in WATCH_JOB_TOOLS or tool in WATCH_JOB_CALLS or tool == WATCH_JOB_FOLLOW_UP:
        return run_watch_job_call(grant, tool, args)
    if tool == WATCH_LIVE_TOKEN:
        return run_watch_live_token(grant)
    if tool == "web_search":
        feature, what, executor, timeout = "Web search", "request", _search_executor, SEARCH_TIMEOUT_S
        job: Callable[[], Dict[str, Any]] = lambda: run_web_search(  # noqa: E731
            args.get("query"), args.get("limit", 3), limiter_key=grant.limiter_key)
        timeout_detail = "Web search timed out"
    else:
        feature, what, executor, timeout = "Memory", "recall", _memory_executor, MEMORY_TIMEOUT_S + 2.0
        job = _memory_job(grant.profile, lambda key, limiter_key: run_memory_recall(
            args.get("query"), key, limiter_key=limiter_key))
        timeout_detail = "Memory recall timed out"
    future = executor.submit(_scoped_call, grant.profile, job)
    try:
        return {"ok": True, **future.result(timeout=timeout)}
    except (FutureTimeoutError, TimeoutError):
        # Frees the worker if the run hadn't started; a running backend
        # call still ends at its own timeout.
        future.cancel()
        logger.warning("%s for a Conduit Watch timed out after %ss", feature, timeout)
        return {"ok": False, "status": 504, "detail": timeout_detail}
    except TokenError as exc:
        logger.warning("%s for a Conduit Watch failed: %s", feature, exc)
        return {"ok": False, "status": exc.status, "detail": str(exc)}
    except HTTPException as exc:
        return {"ok": False, "status": exc.status_code, "detail": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 — named; the message can carry the query
        _log_watch_failure(f"{feature} {what} for a Conduit Watch failed", exc)
        return {"ok": False, "status": 500, "detail": f"{feature} {what} failed on the host ({type(exc).__name__})"}


def run_watch_live_token(grant: _WatchGrant,
                         mint: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A fresh single-use Gemini Live token for the grant's call, as
    /gemini-live/token answers. A mint that fails, or whose answer never
    reaches the relay, doesn't count against the grant's tokens; the token
    itself is never logged."""
    mint = mint or mint_gemini_live_token
    if WATCH_LIVE_TOKEN not in grant.tools:
        return {"ok": False, "status": 403, "detail": "This tool isn't available to the Watch"}
    with grant.lock:
        if grant.live_tokens >= WATCH_LIVE_TOKENS_PER_GRANT:
            return {"ok": False, "status": 429, "detail": "This call has used all its Gemini Live tokens"}
        grant.live_tokens += 1

    def give_back() -> None:
        with grant.lock:
            grant.live_tokens = max(0, grant.live_tokens - 1)

    try:
        token = _scoped_call(grant.profile, lambda: mint(limiter_key=grant.limiter_key))
    except TokenError as exc:
        give_back()
        logger.warning("Gemini Live token for a Conduit Watch failed: %s", exc)
        return {"ok": False, "status": exc.status, "detail": str(exc)}
    except HTTPException as exc:
        give_back()
        return {"ok": False, "status": exc.status_code, "detail": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 — named only
        give_back()
        _log_watch_failure("Gemini Live token for a Conduit Watch failed", exc)
        return {"ok": False, "status": 500, "detail": f"Minting a Gemini Live token failed on the host ({type(exc).__name__})"}
    undelivered = _watch_undelivered.get()
    if undelivered is not None:
        undelivered.append(give_back)
    return {"ok": True, **token}


def answer_watch_call(grant: _WatchGrant, call: Any,
                      relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
                      run: Optional[Callable[[_WatchGrant, Dict[str, Any]], Dict[str, Any]]] = None) -> Optional[str]:
    """Opens, runs and answers one call the relay handed over.

    Returns what happened, for logs and tests: "answered", "gone" (the Watch
    stopped waiting), or None when the call wasn't the Watch's (it doesn't
    open with this grant's key, or repeats one) and is dropped.
    """
    relay = relay or _relay_request
    run = run or run_watch_tool
    rid = call.get("rid") if isinstance(call, dict) else None
    if not isinstance(rid, str) or not _WATCH_ID.match(rid):
        return None
    try:
        request = open_watch_tool(grant.call_key, "call", grant.grant_id, rid, call)
    except WatchToolError as exc:
        logger.warning("Dropped a Watch tool call that wasn't sealed with its grant: %s", exc)
        return None
    with grant.lock:
        if rid in grant.seen:
            logger.warning("Dropped a repeated Watch tool call")
            return None
        grant.seen.add(rid)
        grant.used += 1
        exhausted = grant.used > grant.max_calls
    # What the answer marked as told (job news) is undone if it never
    # reaches the Watch, so the next job_news carries it again.
    undo: list = []
    if grant.closed.is_set() or time.monotonic() >= grant.expires_at:
        answer = {"ok": False, "status": 410, "detail": "This call's Watch lookups have ended"}
    elif exhausted:
        answer = {"ok": False, "status": 429, "detail": "This call has used all its Watch lookups"}
    else:
        token = _watch_undelivered.set(undo)
        try:
            answer = run(grant, request)
        finally:
            _watch_undelivered.reset(token)
    sealed = seal_watch_tool(grant.result_key, "result", grant.grant_id, rid, answer)
    if len(sealed["ct"]) > WATCH_MAX_RESULT_CT_CHARS:
        _undo_watch_answer(undo)
        undo = []
        sealed = seal_watch_tool(grant.result_key, "result", grant.grant_id, rid,
                                 {"ok": False, "status": 502, "detail": "The answer was too large for the Watch"})
    try:
        status, _ = relay(grant.url(f"/results/{rid}"), "POST", grant.credential, sealed, WATCH_RELAY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("A Watch tool answer didn't reach the relay: %s", type(exc).__name__)
        _undo_watch_answer(undo)
        return "gone"
    if status != 200:
        _undo_watch_answer(undo)
        return "gone"
    return "answered"


def _undo_watch_answer(undo: list) -> None:
    for step in undo:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — best effort, logged by type
            logger.warning("Undoing an undelivered Watch answer failed: %s", type(exc).__name__)


def _answer_watch_call_logged(grant: _WatchGrant, call: Any,
                              relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None) -> Optional[str]:
    """answer_watch_call on a worker: what escapes it is logged, by type only."""
    try:
        return answer_watch_call(grant, call, relay)
    except Exception as exc:  # noqa: BLE001 — the Watch's own wait ends the call
        _log_watch_failure("Answering a Watch tool call failed", exc)
        return None


def poll_watch_grant(grant: _WatchGrant,
                     relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
                     submit: Optional[Callable[[Callable[[], Any]], Any]] = None,
                     clock: Callable[[], float] = time.monotonic) -> None:
    """Answers the grant's calls until it closes, expires or the relay drops it."""
    relay = relay or _relay_request
    submit = submit or (lambda fn: _watch_executor.submit(fn))
    backoff = 1.0
    tell_relay = True
    try:
        while not grant.closed.is_set() and clock() < grant.expires_at:
            try:
                status, body = relay(grant.url(f"/calls?wait_ms={WATCH_POLL_WAIT_MS}"), "GET",
                                     grant.credential, None, WATCH_POLL_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — retried; the grant expires anyway
                logger.info("Watch tool poll failed (%s); retrying in %ss", type(exc).__name__, backoff)
                grant.closed.wait(backoff)
                backoff = min(backoff * 2, WATCH_POLL_BACKOFF_MAX_S)
                continue
            if status in (401, 404, 410):
                # Closed or expired on the relay, or the pairing is gone.
                tell_relay = False
                return
            if status != 200:
                grant.closed.wait(backoff)
                backoff = min(backoff * 2, WATCH_POLL_BACKOFF_MAX_S)
                continue
            backoff = 1.0
            calls = body.get("calls")
            for call in calls if isinstance(calls, list) else []:
                submit(lambda call=call: _answer_watch_call_logged(grant, call, relay))
    finally:
        _close_watch_grant(grant, tell_relay=tell_relay, relay=relay)


def _start_watch_poller(grant: _WatchGrant) -> None:
    threading.Thread(target=poll_watch_grant, args=(grant,), name="conduit-watch-poll", daemon=True).start()


def _watch_grant_ttl(relay_expires_at: Any, now: datetime) -> float:
    """The grant's lifetime here: ours, capped at the relay's expiry.

    A relay time that can't be read, or is already past (a skewed clock),
    leaves ours: the relay still ends the grant at its own expiry.
    """
    ttl = float(WATCH_GRANT_TTL_S)
    if not isinstance(relay_expires_at, str):
        return ttl
    try:
        expires = datetime.fromisoformat(relay_expires_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return ttl
    if expires.tzinfo is None:
        return ttl
    remaining = (expires - now).total_seconds()
    return min(ttl, remaining) if remaining > 0 else ttl


def _watch_max_jobs(value: Any) -> int:
    if value is None:
        return WATCH_JOBS_DEFAULT
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TokenError(400, "max_jobs must be a whole number from 0")
    return min(value, WATCH_JOBS_MAX)


def open_watch_grant(body: Any, *, profile: Optional[str], path: Any = None,
                     relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
                     start: Optional[Callable[[_WatchGrant], None]] = None,
                     session_api: Optional[Callable[[], Any]] = None,
                     live_key: Optional[Callable[[], Optional[str]]] = None,
                     start_audio: Optional[Callable[[_WatchGrant], None]] = None) -> Dict[str, Any]:
    """Opens a grant for one Watch call on this profile's relay pairing.

    Jobs (any of WATCH_JOB_TOOLS asked for, ``max_jobs`` above 0) are granted
    only where this process serves Hermes' chats; elsewhere the grant leaves
    them out and the Watch's jobs go through the iPhone. A renewal names the
    call's previous grant in ``carry_jobs_from``: its jobs move to the new
    grant (_carry_watch_jobs). live_token is granted only where this profile
    has a Gemini key to mint with. ``audio`` (true) adds the call's audio
    bridge for GPT-Live and Grok (Watch audio, below), where the relay has it.
    ``job_profiles`` names the user's other profiles the call's jobs may run
    on ("for Fam, …"), as the phone's own jobs may. Like every route here it
    sits behind the dashboard's own auth, whose holder can already start
    chats on any profile; the list only narrows what the Watch may name.
    """
    relay = relay or _relay_request
    start_audio = start_audio or (lambda grant: grant.audio.start())
    start = start or _start_watch_poller
    session_api = session_api or _hermes_session_api
    live_key = live_key or resolve_api_key
    if not isinstance(body, dict):
        raise TokenError(400, "Expected a JSON object")
    requested = body.get("tools")
    if not isinstance(requested, list) or not requested or not all(isinstance(t, str) for t in requested):
        raise TokenError(400, "tools must be a non-empty list")
    # interrupt_job comes with jobs; named, it's taken rather than refused.
    grantable = WATCH_TOOLS + WATCH_JOB_TOOLS + (WATCH_JOB_FOLLOW_UP, WATCH_LIVE_TOKEN)
    if any(tool not in grantable for tool in requested):
        raise TokenError(400, f"The Watch can only be granted {', '.join(grantable)}")
    max_jobs = _watch_max_jobs(body.get("max_jobs"))
    audio = body.get("audio", False)
    if not isinstance(audio, bool):
        raise TokenError(400, "audio must be true or false")
    if audio and not _watch_audio_client_available():
        raise TokenError(501, "Watch audio needs the websockets package on this host")
    carry_from = body.get("carry_jobs_from")
    if carry_from is not None and (not isinstance(carry_from, str) or not _WATCH_ID.match(carry_from)):
        raise TokenError(400, "carry_jobs_from must be a grant id")
    wants_jobs = any(tool in WATCH_JOB_TOOLS or tool == WATCH_JOB_FOLLOW_UP for tool in requested)
    server = session_api() if max_jobs > 0 and wants_jobs else None
    # Only a grant that runs jobs reads them, as job_options.
    job_profiles = _clean_job_profiles(body.get("job_profiles"), own=profile) if server is not None else ()
    tools = tuple(tool for tool in WATCH_TOOLS if tool in requested)
    if server is not None:
        # The job tools asked for, and the Watch app's own calls and
        # follow-ups with them.
        tools += tuple(tool for tool in WATCH_JOB_TOOLS if tool in requested) + WATCH_JOB_CALLS
        # Corrections reach jobs, so they come with the tools that make or
        # stop them: a grant that only lists jobs doesn't get them unasked.
        if WATCH_JOB_FOLLOW_UP in requested or any(tool in WATCH_JOB_WRITE_TOOLS for tool in requested):
            tools += (WATCH_JOB_FOLLOW_UP,)
    if WATCH_LIVE_TOKEN in requested and live_key() is not None:
        tools += (WATCH_LIVE_TOKEN,)
    if not tools:
        if WATCH_LIVE_TOKEN in requested and not any(tool in WATCH_JOB_TOOLS for tool in requested):
            raise TokenError(503, "GEMINI_API_KEY is not set on this Hermes host")
        raise TokenError(501, "This host can't run Hermes jobs for the Watch")
    max_calls = WATCH_JOB_GRANT_MAX_CALLS if server is not None else WATCH_GRANT_MAX_CALLS
    if not _e2e_crypto_available():
        raise TokenError(501, "Watch tools need the cryptography package on this host")
    state = _load_pairing_state(path if path is not None else _pairing_state_path())
    relay_url = str((state or {}).get("relay_url") or "").rstrip("/")
    credential = str((state or {}).get("credential") or "")
    if not state or not state.get("gateway_id") or not credential:
        raise TokenError(409, "This Hermes profile isn't paired with Conduit notifications")
    if not relay_url.startswith(_WATCH_RELAY_SCHEMES):
        raise TokenError(409, "The push relay must use HTTPS")
    _watch_grant_limiter.acquire(_limiter_key(profile))
    secret = os.urandom(32)
    watch_key = _b64u(os.urandom(32))
    try:
        status, response = relay(f"{relay_url}/v1/watch-tools/grants", "POST", credential, {
            "watch_key_sha256": hashlib.sha256(watch_key.encode("ascii")).hexdigest(),
            "ttl_s": WATCH_GRANT_TTL_S,
            "max_calls": max_calls,
            **({"audio": True} if audio else {}),
        }, WATCH_RELAY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Opening a Watch tool grant on the relay failed: %s", type(exc).__name__)
        raise TokenError(502, "Couldn't reach the push relay")
    if status == 404:
        raise TokenError(501, "The push relay doesn't support Watch tools yet")
    if status == 503 and response.get("error") == "watch_grant_capacity":
        # A full relay, not a broken one: the call's lookups go through the
        # iPhone.
        raise TokenError(503, "The push relay is at capacity for Watch tools")
    grant_id = response.get("grant_id")
    if status != 201 or not isinstance(grant_id, str) or not _WATCH_ID.match(grant_id):
        logger.warning("The relay refused a Watch tool grant (%s %s)", status, response.get("error"))
        raise TokenError(502, "The push relay refused the Watch tool grant")
    now = datetime.now(timezone.utc)
    ttl = _watch_grant_ttl(response.get("expires_at"), now)
    grant = _WatchGrant(grant_id=grant_id, profile=profile, tools=tools, secret=secret, relay_url=relay_url,
                        credential=credential, expires_at=time.monotonic() + ttl, max_calls=max_calls)
    if audio:
        if response.get("audio") is not True:
            # A relay from before Watch audio opened a grant without it.
            _close_watch_grant_on_relay_later(grant, relay)
            raise TokenError(501, "The push relay doesn't support Watch audio yet")
        grant.audio = _WatchAudioBridge(grant, secret)
    carried_from: Optional[_WatchGrant] = None
    if server is not None:
        options = _clean_job_options(body.get("job_options"))
        if carry_from is not None:
            # A renewal that leaves out the cap, the options or the profiles
            # keeps the call's.
            carried_from = _carry_watch_jobs(
                carry_from, grant,
                max_jobs=max_jobs if body.get("max_jobs") is not None else None,
                options=options if body.get("job_options") is not None else None,
                profiles=job_profiles if body.get("job_profiles") is not None else None)
        if carried_from is None:
            grant.jobs = _WatchJobs(grant, max_jobs=max_jobs, options=options, server=server, profiles=job_profiles)
    for old in _watch_grants.add(grant):
        _watch_executor.submit(_close_watch_grant, old, tell_relay=True, relay=relay)
    try:
        start(grant)
    except Exception as exc:  # noqa: BLE001 — e.g. no thread to spare
        logger.warning("Starting the Watch tool poller failed: %s", type(exc).__name__)
        if carried_from is not None:
            _hand_back_watch_jobs(grant, carried_from)
        # Nothing would answer its calls: the Watch learns at once instead
        # of meeting host_offline for half an hour.
        if _close_watch_grant_here(grant):
            _close_watch_grant_on_relay_later(grant, relay)
        raise TokenError(503, "This host couldn't start answering Watch lookups")
    if grant.audio is not None:
        try:
            start_audio(grant)
        except Exception as exc:  # noqa: BLE001 — e.g. no thread to spare
            logger.warning("Starting the Watch audio bridge failed: %s", type(exc).__name__)
            if carried_from is not None:
                _hand_back_watch_jobs(grant, carried_from)
            if _close_watch_grant_here(grant):
                _close_watch_grant_on_relay_later(grant, relay)
            raise TokenError(503, "This host couldn't start the Watch call's audio")
    expires_at = now + timedelta(seconds=ttl)
    return {
        "grant_id": grant_id,
        "relay_url": relay_url,
        "key": _b64u(secret),
        "watch_key": watch_key,
        "expires_at": _timestamp(expires_at),
        "tools": list(tools),
        "max_calls": max_calls,
        "max_jobs": grant.jobs.max_jobs if grant.jobs is not None else 0,
        # Present, even empty, where jobs can run on another profile: an
        # older plugin's Watch sends those jobs through the iPhone.
        **({"job_profiles": list(grant.jobs.profiles)} if grant.jobs is not None else {}),
        **({"jobs_carried_from": carried_from.grant_id} if carried_from is not None else {}),
        **({"audio": {"url": grant.audio.watch_url(), "version": WATCH_AUDIO_VERSION,
                      "engines": _watch_audio_grant_engines(grant.audio.runtime)}} if grant.audio is not None else {}),
    }


def _carry_watch_jobs(old_id: str, grant: _WatchGrant, *, max_jobs: Optional[int],
                      options: Optional[Dict[str, str]],
                      profiles: Optional[Tuple[str, ...]] = None) -> Optional[_WatchGrant]:
    """Moves the jobs of a call's previous grant ``old_id`` to its renewal
    ``grant``: the Watch keeps hearing their news and can list, cancel and
    approve them through the new grant, and the job cap counts across the
    call (``max_jobs``, ``options`` and ``profiles`` replace the call's when
    given). Only an open grant of the same profile whose jobs are still its
    own; returns it, or None when nothing moved.

    The old grant keeps answering for the jobs until it closes: the Watch
    may have sent a call through it before it heard of the renewal, and it
    closes the old grant once it has."""
    old = _watch_grants.get(old_id)
    if old is None or old is grant or old.profile != grant.profile:
        return None
    with old.lock:
        jobs = old.jobs
        # Checked under the lock its closing sets `closed` under, so a grant
        # closing now either keeps its jobs (and ends them) or lets them go.
        if old.closed.is_set() or jobs is None or jobs.grant is not old:
            return None
        with jobs.changed:
            if jobs.ended:
                return None
            jobs.grant = grant
            if max_jobs is not None:
                jobs.max_jobs = max_jobs
            if options is not None:
                jobs.options = options
            if profiles is not None:
                jobs.profiles = profiles
            jobs.changed.notify_all()
    grant.jobs = jobs
    return old


def _hand_back_watch_jobs(grant: _WatchGrant, old: _WatchGrant) -> None:
    """A renewal that couldn't start answering gives the carried jobs back
    to the grant they came from, while it's open; otherwise they end with
    the renewal."""
    jobs = grant.jobs
    if jobs is None:
        return
    with old.lock:
        if old.closed.is_set():
            return
        with jobs.changed:
            jobs.grant = old
            jobs.changed.notify_all()
    grant.jobs = None


def _close_watch_grant_on_relay_later(grant: _WatchGrant,
                                      relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None) -> None:
    # Its own short-lived thread: a slow relay doesn't hold a worker that
    # answers lookups. Without a thread to spare, a lookup worker does it.
    try:
        threading.Thread(target=_close_watch_grant_on_relay, args=(grant, relay),
                         name="conduit-watch-revoke", daemon=True).start()
        return
    except RuntimeError as exc:
        logger.warning("Couldn't start closing a Watch tool grant on the relay (%s); queueing it", type(exc).__name__)
    try:
        _watch_executor.submit(_close_watch_grant_on_relay, grant, relay)
    except RuntimeError as exc:
        logger.warning("Couldn't close a Watch tool grant on the relay (%s); it ends there at its expiry",
                       type(exc).__name__)


def revoke_watch_grant(body: Any, *, profile: Optional[str],
                       relay: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
                       tell_relay: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    """Ends one of this profile's grants here at once; the relay hears after."""
    tell_relay = tell_relay or _close_watch_grant_on_relay_later
    if not isinstance(body, dict) or not isinstance(body.get("grant_id"), str):
        raise TokenError(400, "grant_id is required")
    grant = _watch_grants.get(body["grant_id"])
    # Only this profile's own grants.
    if grant is None or grant.limiter_key != _limiter_key(profile):
        return {"revoked": False}
    if _close_watch_grant_here(grant):
        tell_relay(grant, relay)
    return {"revoked": True}


@router.post("/watch-tools/grant")
async def post_watch_tool_grant(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body carries the grant's keys: never cached on the way back.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    no_store = {"Cache-Control": "no-store"}
    body = await _read_json_body(request, WATCH_GRANT_MAX_BODY_BYTES)
    try:
        return {"ok": True, **(await _run_scoped(profile, lambda: open_watch_grant(body, profile=profile)))}
    except TokenError as exc:
        logger.warning("Watch tool grant failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except Exception as exc:
        raise _unexpected("grant", exc, feature="Watch tools")


@router.post("/watch-tools/revoke")
async def post_watch_tool_revoke(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _read_json_body(request, WATCH_GRANT_MAX_BODY_BYTES)
    try:
        # Closing here is quick; the relay is told on a thread of its own.
        return {"ok": True, **revoke_watch_grant(body, profile=profile)}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise _unexpected("revoke", exc, feature="Watch tools")


# --- Watch jobs through the relay ---------------------------------------------
#
# With jobs in its grant, a Watch call's start_job, list_jobs and cancel_job
# run here instead of on the iPhone, so they work with the wrist down. The
# plugin drives Hermes' own session API in this dashboard process (the
# methods the app calls over its WebSocket) through a transport of its own:
# each job is an ordinary Hermes chat on the grant's profile, filed under
# Voice Jobs like a job the phone started. Two more calls come only from the
# Watch app, never from the model as such: job_news (settled jobs and
# approval requests, held until there is some) and answer_approval (Approve
# or Deny on the Watch, or by voice when the user turned that on, after the
# Watch's own checks). Sealed with the same key, the host can't tell who
# sent a call: the Watch app holds that line, and must never pass the model
# either call as it is. Hermes'
# own approval settings decide what needs approving; the Watch can only
# approve once or deny. (hermes-conduit designs/apple-watch-voice-direct.md,
# "Wrist-down jobs through the relay")
#
# interrupt_job puts the user's words into a job Hermes is still working on
# (a correction, "hold that", "never mind"), as the phone's live calls do
# (Conduit #451/#455): Hermes' session.redirect, which keeps the work so far
# and ends the turn with one completion. It comes with every grant that can
# start or cancel jobs, so the Watch never needs to name it (an older plugin
# would refuse a grant that did); named, it is taken rather than refused.

WATCH_JOB_TOOLS = ("start_job", "list_jobs", "cancel_job")
# The job tools that change jobs: a grant with one also takes corrections.
WATCH_JOB_WRITE_TOOLS = ("start_job", "cancel_job")
WATCH_JOB_CALLS = ("job_news", "answer_approval")
WATCH_JOB_FOLLOW_UP = "interrupt_job"
# Tries and the wait between them while Hermes isn't working on the job
# just then, as the phone (VoiceBackgroundJobSupervisor.followUpAttempts,
# followUpRetryInterval); waits for a job still starting count up to three
# times as many.
WATCH_FOLLOW_UP_ATTEMPTS = 4
WATCH_FOLLOW_UP_RETRY_S = 1.0
# Another follow-up to the same job goes first: this one waits at most this
# long for it. A follow-up is answered within WATCH_FOLLOW_UP_DEADLINE_S in
# all, inside the relay's 25 s wait.
WATCH_FOLLOW_UP_QUEUE_S = 8.0
WATCH_FOLLOW_UP_DEADLINE_S = 22.0
# An end held for words Hermes queued settles the job if the words' turn
# shows no sign of life for this long (the phone's liveness poll does it there).
WATCH_FOLLOW_UP_HOLD_S = 30.0
# Frames from the session renew that wait, but never past this in all: a
# session that only chatters doesn't keep the job open forever.
WATCH_FOLLOW_UP_HOLD_MAX_S = 180.0
# Hermes' marker for a reply a correction cut off, never a result
# (Conduit's MessageNormalizer.isUserCorrectionInterruptionNotice).
WATCH_CORRECTION_NOTICE = "[this response was interrupted by a user correction.]"
# Events that show a turn carrying on (Conduit's StreamEventParser names).
WATCH_TURN_PROGRESS = ("message.start", "message.delta", "reasoning.delta", "message.reasoning",
                       "message.reasoning.delta", "tool.start", "tool_call", "tool.complete", "tool_result")
# Jobs one call may start: the user's setting, capped here.
WATCH_JOBS_DEFAULT = 5
WATCH_JOBS_MAX = 20
# Running at once, as on the phone (VoiceBackgroundJobSupervisor.maximumActiveJobs).
WATCH_JOBS_RUNNING_MAX = 3
# A grant with jobs polls job_news while they run: the relay's own cap.
WATCH_JOB_GRANT_MAX_CALLS = 120
# A job_news call waits this long for news; the relay holds the Watch's
# request 25 s.
WATCH_JOB_NEWS_WAIT_S = 15.0
WATCH_JOB_RPC_TIMEOUT_S = 20.0
# A start_job is answered within this, inside the relay's 25 s wait; a
# start Hermes is still taking is answered "accepted" and goes on, its
# outcome then told as news (as the iPhone's broker answers within 8 s).
WATCH_JOB_START_ANSWER_S = 18.0
# Bytes as JSON, so a task that fits here also fits the sealed call
# (WATCH_MAX_CALL_BYTES); the Watch sends a longer one through the iPhone.
WATCH_JOB_MAX_INSTRUCTION_BYTES = 3_000
# As Conduit's VoiceBackgroundJobSupervisor: maximumResultCharacters and
# maximumTitleCharacters.
WATCH_JOB_RESULT_CHARS = 6_000
WATCH_JOB_TITLE_CHARS = 60
WATCH_JOB_MAX_OPTION_CHARS = 120
# The user's other profiles a call's jobs may run on, as the phone lists
# them ("for Fam, …"): at most this many, each a Hermes profile name.
WATCH_JOB_MAX_PROFILES = 32
_WATCH_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Bytes as JSON. A whole job_news answer, sealed, stays inside the relay's
# bound on an answer (WATCH_MAX_RESULT_CT_CHARS, about 17,980 bytes before
# sealing); more news than fits waits for the next call. A job's texts are
# cut so any one item fits next to every open approval.
WATCH_JOB_ANSWER_BYTES = 17_000
WATCH_JOB_RESULT_BYTES = 11_000
WATCH_JOB_COMMAND_BYTES = 6_000
WATCH_JOB_DETAIL_BYTES = 1_200
# What the Watch may answer an approval with: never "session" or "always".
WATCH_APPROVAL_CHOICES = ("once", "deny")
# Mirrors Conduit's VoiceBackgroundJobSupervisor.jobPrompt.
WATCH_JOB_PROMPT = ("[Background job started from a Conduit voice conversation. Nobody is watching this chat live, "
                    "so work on it on your own. When you are done, end your final message with a plain-language "
                    "summary that can be read aloud: what you found or did, with the key details.]\n\n{instructions}")
# Job ids are unique in this process, not per grant: a renewed grant's jobs
# never share an id with the last grant's, so a cancel can't hit the wrong
# job.
_watch_job_numbers = itertools.count(1)


class WatchJobError(Exception):
    """A Hermes session call for a Watch job that failed; the message is Hermes' own."""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def unsupported_redirect(self) -> bool:
        """Hermes has no session.redirect (Conduit's AppState.isUnsupportedRedirect)."""
        message = str(self).lower()
        return (self.code in (4010, -32601) or "does not support active-turn redirect" in message
                or "method not found" in message or "unknown method" in message)


def _hermes_session_api() -> Any:
    """Hermes' session API module when it serves this process's chats, else None.

    Only a module the dashboard itself loaded counts: importing it here (a
    plugin host process, an older Hermes) would make a second, private
    registry whose chats the app never sees.
    """
    server = sys.modules.get("tui_gateway.server")
    if server is None or not callable(getattr(server, "dispatch", None)):
        return None
    sessions = getattr(server, "_sessions", None)
    return server if sessions is not None and callable(getattr(sessions, "get", None)) else None


def watch_job_title(instructions: str) -> str:
    """Mirrors Conduit's VoiceBackgroundJobSupervisor.title(for:)."""
    collapsed = " ".join(instructions.split())
    if len(collapsed) <= WATCH_JOB_TITLE_CHARS:
        return collapsed
    cut = collapsed[:WATCH_JOB_TITLE_CHARS]
    space = cut.rfind(" ")
    if space > WATCH_JOB_TITLE_CHARS // 2:
        return cut[:space] + "…"
    return cut + "…"


def _clip_job_result(text: str) -> str:
    """At most WATCH_JOB_RESULT_CHARS characters and WATCH_JOB_RESULT_BYTES
    bytes as JSON, marked once when cut."""
    return _json_clip(text[:WATCH_JOB_RESULT_CHARS], WATCH_JOB_RESULT_BYTES,
                      cut=len(text) > WATCH_JOB_RESULT_CHARS)


def _rpc_id(value: Any) -> str:
    """A JSON-RPC id as a string: ids can be numbers, 0 among them."""
    return "" if value is None else str(value)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _json_clip(text: str, limit: int, cut: bool = False) -> str:
    """``text`` cut so it takes at most ``limit`` bytes as a JSON string
    (quotes and escapes included), marked when cut here or already (``cut``)."""
    if not cut and _json_bytes(text) <= limit:
        return text
    marker = "\n[…]"
    room = limit - (_json_bytes(marker) - 2)
    if room < 2:
        # Too small for the marker: as much of the text as fits.
        marker, room = "", limit
    if _json_bytes(text) <= room:
        return text + marker
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_bytes(text[:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return text[:low] + marker


def _clean_job_options(value: Any) -> Dict[str, str]:
    """The phone's voice-job model settings for this profile, as it would send them."""
    if not isinstance(value, dict):
        return {}
    options = {key: value[key].strip() for key in ("model", "provider", "reasoning_effort")
               if isinstance(value.get(key), str) and 0 < len(value[key].strip()) <= WATCH_JOB_MAX_OPTION_CHARS}
    # A provider only goes with its model (HermesClient.createSession).
    if "model" not in options:
        options.pop("provider", None)
    return options


def _clean_job_profiles(value: Any, *, own: Optional[str]) -> Tuple[str, ...]:
    """The user's other profiles a Watch call's jobs may run on, as the
    phone names them; the grant's own profile needs no naming."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > WATCH_JOB_MAX_PROFILES:
        raise TokenError(400, f"job_profiles must be a list of at most {WATCH_JOB_MAX_PROFILES} profile names")
    if not all(isinstance(name, str) and _WATCH_PROFILE_NAME.fullmatch(name) for name in value):
        raise TokenError(400, "Each job profile starts with a letter or digit, then up to 63 letters, digits, '.', '_' or '-'")
    names: List[str] = []
    seen = {_limiter_key(own)}
    for name in value:
        # As Hermes' scope folds them: "current" is the grant's own profile.
        key = _limiter_key(name)
        if key and key not in seen:
            seen.add(key)
            names.append(name)
    return tuple(names)


class _WatchJob:
    def __init__(self, job_id: str, title: str, profile: Optional[str] = None) -> None:
        self.job_id = job_id
        self.title = title
        # Another of the user's profiles it runs on; None: the grant's.
        self.profile = profile
        self.session_id = ""
        self.stored_session_id = ""
        self.status = "starting"
        self.result = ""
        self.error = ""
        # The pending approval: Hermes' request id, its server request's id
        # (for request.cancel), the redacted command and its description.
        self.approval: Optional[Dict[str, str]] = None
        # Told to the Watch: the outcome, and the approval request last told
        # (None before any, as a request's key can be empty).
        self.outcome_told = False
        self.approval_told: Optional[str] = None
        self.closed = False
        # The start's own answer: pending until the start settles or is
        # answered "accepted"; its outcome isn't news meanwhile.
        self.answer_pending = True
        self.answered_late = False
        self.start_answer: Optional[Dict[str, Any]] = None
        # While a cancel's interrupt is on its way: the end of the turn,
        # kept in case Hermes refuses the interrupt (cancel).
        self.cancelling = False
        self.held_end: Optional[Tuple[str, Dict[str, Any]]] = None
        # A follow-up going into the running turn (interrupt_job): None,
        # "sending", "accepted" (Hermes took it into the turn) or "queued"
        # (Hermes runs it as the next turn). Until the turn carries on, an
        # end of turn may not be the last: it is held here.
        self.follow_up: Optional[str] = None
        self.held_completion: Optional[Tuple[str, Dict[str, Any]]] = None
        self.held_at = 0.0
        self.held_since = 0.0
        # Follow-ups to one job go one at a time, in the order they came.
        self.follow_up_lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.status in ("starting", "running", "needs_approval")

    def news(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {"job_id": self.job_id, "title": self.title, "status": self.status,
                                "session_id": (self.stored_session_id or self.session_id)[:128],
                                **({"profile": self.profile} if self.profile else {})}
        if self.status == "finished":
            item["result"] = _clip_job_result(self.result)
        if self.error:
            item["error"] = _json_clip(self.error, WATCH_JOB_DETAIL_BYTES)
        if self.approval and self.status == "needs_approval":
            item["approval"] = {
                "request_id": self.approval["request_id"],
                "command": _json_clip(self.approval["command"], WATCH_JOB_COMMAND_BYTES),
                "description": _json_clip(self.approval["description"], WATCH_JOB_DETAIL_BYTES),
            }
        return item

    @property
    def approval_key(self) -> str:
        """Which approval request is pending, for telling each one once."""
        if not self.approval:
            return ""
        return self.approval["request_id"] or self.approval["server_request_id"]


class _WatchJobTransport:
    """This plugin's end of its jobs' sessions: Hermes writes their responses
    and events here, as it would to the app's WebSocket. ``_closed`` is what
    Hermes' session reaper reads."""

    def __init__(self, jobs: "_WatchJobs") -> None:
        self._jobs = jobs
        self._closed = False

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False
        try:
            self._jobs.frame(obj)
        except Exception as exc:  # noqa: BLE001 — Hermes' writer must never see our failure
            _log_watch_failure("A Watch job event couldn't be read", exc)
        return True

    def close(self) -> None:
        self._closed = True


class _RpcWaiter:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class _WatchJobs:
    """One grant's jobs: started, followed and ended through Hermes' session API."""

    def __init__(self, grant: "_WatchGrant", *, max_jobs: int, options: Dict[str, str], server: Any,
                 profiles: Tuple[str, ...] = ()) -> None:
        self.grant = grant
        self.max_jobs = max_jobs
        self.options = options
        self.profiles = profiles
        self.server = server
        self.jobs: Dict[str, _WatchJob] = {}
        self.started = 0
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.transport = _WatchJobTransport(self)
        self._waiters: Dict[str, _RpcWaiter] = {}
        self._rpc_ids = 0
        self.ended = False
        # Follow-ups between their 410 check and their answer: the hold
        # reaper keeps watching while any is in flight.
        self.follow_ups_in_flight = 0
        self.reaping = False

    # Hermes' session API

    def rpc(self, method: str, params: Dict[str, Any], timeout: float = WATCH_JOB_RPC_TIMEOUT_S) -> Dict[str, Any]:
        with self.lock:
            self._rpc_ids += 1
            rid = f"conduit-watch-{self.grant.grant_id[:8]}-{self._rpc_ids}"
            waiter = self._waiters[rid] = _RpcWaiter()
        try:
            response = self.server.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                                            self.transport)
            if response is None:
                # A slow method answers from Hermes' worker pool.
                if not waiter.done.wait(timeout):
                    raise WatchJobError(f"{method} timed out")
                response = waiter.response
        finally:
            with self.lock:
                self._waiters.pop(rid, None)
        if not isinstance(response, dict):
            raise WatchJobError(f"{method} gave no answer")
        error = response.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise WatchJobError(str(message or f"{method} failed")[:300], code=code)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def frame(self, obj: Any) -> None:
        """A frame Hermes wrote to this transport."""
        if not isinstance(obj, dict):
            return
        method = obj.get("method")
        if method is None:
            # A response to one of our calls.
            with self.lock:
                waiter = self._waiters.get(obj.get("id")) if isinstance(obj.get("id"), str) else None
            if waiter is not None:
                waiter.response = obj
                waiter.done.set()
            return
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        sid = params.get("session_id") if isinstance(params.get("session_id"), str) else ""
        if method == "event":
            payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
            self._event(str(params.get("type") or ""), sid, payload)
        elif method == "approval":
            # A server request: its fields sit beside session_id.
            self._approval_requested(sid, _rpc_id(obj.get("id")), params)

    def _job_for(self, sid: str) -> Optional[_WatchJob]:
        if not sid:
            return None
        return next((job for job in self.jobs.values() if sid in (job.session_id, job.stored_session_id)), None)

    def _event(self, kind: str, sid: str, payload: Dict[str, Any]) -> None:
        settled = False
        with self.changed:
            job = self._job_for(sid)
            if job is None:
                return
            if job.held_completion is not None and kind not in ("message.complete", "error"):
                # Hermes is still at work on the session: the end it held
                # waits on, so a quiet stretch isn't read as no turn coming.
                job.held_at = time.monotonic()
            if kind == "request.cancel":
                # Answered elsewhere (the app, the phone), timed out, or the
                # turn ended. The id is compared as a string, as it was
                # stored: JSON-RPC ids can be numbers.
                server_id = job.approval["server_request_id"] if job.approval else ""
                if server_id and _rpc_id(payload.get("id")) == server_id:
                    self._approval_gone(job)
            elif kind == "approval.cancelled":
                ids = payload.get("request_ids")
                if job.approval and (not isinstance(ids, list) or not ids or job.approval["request_id"] in ids):
                    self._approval_gone(job)
            elif kind in WATCH_TURN_PROGRESS:
                # Hermes carries on with the follow-up it took: the end it
                # held was the step it was finishing.
                if job.active and job.follow_up == "accepted":
                    job.follow_up, job.held_completion = None, None
                return
            elif kind in ("message.complete", "error") and (job.active or job.cancelling):
                if not job.active:
                    # Cancelled meanwhile: this end reads as the cancel,
                    # unless Hermes refuses the interrupt.
                    job.held_end = (kind, payload)
                    return
                if kind == "message.complete" and self._cut_by_correction(job, payload):
                    return
                if kind == "message.complete" and job.follow_up is not None and job.follow_up != "accepted":
                    # Maybe not the last: held until Hermes takes the words.
                    job.held_completion, job.held_at = (kind, payload), time.monotonic()
                    job.held_since = job.held_at
                    if job.follow_up == "queued":
                        job.follow_up = "accepted"
                    if self.ended:
                        # No news poll reaps it once the call is gone.
                        self._reap_holds_later()
                    return
                self._settle(job, kind, payload)
                settled = True
            else:
                return
            self.changed.notify_all()
            ended = self.ended
        if settled and ended:
            self._close_session_later(job)

    @staticmethod
    def _cut_by_correction(job: _WatchJob, payload: Dict[str, Any]) -> bool:
        """The end of a reply a follow-up cut off: never the job's result."""
        text = payload.get("text")
        if job.follow_up is None:
            # No correction went in: whatever this says is the job's own.
            return False
        if isinstance(text, str) and text.strip().lower() == WATCH_CORRECTION_NOTICE:
            return True
        return payload.get("status") == "interrupted"

    @staticmethod
    def _settle(job: _WatchJob, kind: str, payload: Dict[str, Any]) -> None:
        """The job's turn ended: ``message.complete`` or ``error``."""
        job.approval = None
        job.follow_up, job.held_completion = None, None
        if kind == "error":
            message = payload.get("message")
            job.status = "failed"
            job.error = (message if isinstance(message, str) else "").strip()[:300] or "Hermes reported an error"
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            text = payload.get("rendered") if isinstance(payload.get("rendered"), str) else ""
        status = payload.get("status")
        if status == "interrupted":
            job.status = "cancelled"
        elif status == "error":
            detail = payload.get("error") if isinstance(payload.get("error"), str) else text
            job.status, job.error = "failed", (detail or "").strip()[:300] or "Hermes reported an error"
        else:
            # News clips it; one more character marks the cut.
            job.status, job.result = "finished", text.strip()[:WATCH_JOB_RESULT_CHARS + 1]

    @staticmethod
    def _approval_gone(job: _WatchJob) -> None:
        job.approval = None
        # A later request, even under the same id, is news again.
        job.approval_told = None
        if job.status == "needs_approval":
            job.status = "running"

    def _approval_requested(self, sid: str, server_request_id: str, params: Dict[str, Any]) -> None:
        def text(key: str, limit: int) -> str:
            value = params.get(key)
            return value.strip()[:limit] if isinstance(value, str) else ""

        with self.changed:
            job = self._job_for(sid)
            if job is None or not job.active:
                return
            if job.held_completion is not None:
                # Still at work, as any frame for the session says.
                job.held_at = time.monotonic()
            job.approval = {
                "request_id": text("request_id", 128),
                "server_request_id": server_request_id,
                "command": text("command", 2_000),
                "description": text("description", 500) or "Approval required",
            }
            job.status = "needs_approval"
            self.changed.notify_all()

    # The Watch's calls

    def start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        instructions = str(args.get("instructions") or "").strip()
        # "Quick:" only routes work on the phone; it isn't part of the task.
        if instructions[:6].lower() == "quick:":
            instructions = instructions[6:].strip()
        if not instructions:
            return {"ok": False, "status": 400, "detail": "instructions is required"}
        named = str(args.get("profile") or "").strip()
        if _json_bytes(instructions) > WATCH_JOB_MAX_INSTRUCTION_BYTES:
            return {"ok": False, "status": 413, "detail": "The task is too long for a Watch job"}
        with self.lock:
            if self.ended:
                return {"ok": False, "status": 410, "detail": "This call's Watch jobs have ended"}
            profile: Optional[str] = None
            if named and _limiter_key(named) not in ("", _limiter_key(self.grant.profile)):
                # Only a profile the phone listed: a name is never guessed at.
                profile = next((name for name in self.profiles if _limiter_key(name) == _limiter_key(named)), None)
                if profile is None:
                    return {"ok": True, "status": "not_started",
                            "message": f"I don't know a profile or bot called {named[:64]}, so I didn't start the job."}
            if self.started >= self.max_jobs:
                return {"ok": True, "status": "not_started",
                        "message": f"This call has started {self.max_jobs} jobs, the most the user allows per call. "
                                   "Tell them; they can raise it in Conduit's Watch settings or start more from "
                                   "their iPhone."}
            running = sum(1 for job in self.jobs.values() if job.active)
            if running >= WATCH_JOBS_RUNNING_MAX:
                return {"ok": True, "status": "not_started",
                        "message": f"You already have {running} background jobs running. Cancel them before "
                                   "starting another."}
            self.started += 1
            job = _WatchJob(f"watch-{next(_watch_job_numbers)}", watch_job_title(instructions), profile)
            self.jobs[job.job_id] = job
        done = threading.Event()

        def run() -> None:
            try:
                answer = self._start_session(job, instructions)
            except Exception as exc:  # noqa: BLE001 — _start_session catches its own
                _log_watch_failure("Starting a Watch job failed", exc)
                answer = {"ok": True, "status": "not_started", "title": job.title,
                          "message": f"Hermes couldn't start the job: {type(exc).__name__}"}
                with self.changed:
                    if job.active:
                        job.status, job.error = "failed", type(exc).__name__
            with self.changed:
                job.answer_pending = False
                if answer.get("status") == "not_started":
                    # Nothing ran: it doesn't spend one of the call's jobs.
                    self.started -= 1
                if not job.answered_late:
                    job.start_answer = answer
                    if answer.get("status") == "not_started":
                        job.outcome_told = True  # this answer tells it
                # Answered late: a job that didn't start is news.
                self.changed.notify_all()
            done.set()

        try:
            threading.Thread(target=run, name="conduit-watch-job-start", daemon=True).start()
        except RuntimeError as exc:
            # No thread to spare: nothing reached Hermes, so the job never was.
            logger.warning("Couldn't start a Watch job (%s)", type(exc).__name__)
            with self.changed:
                self.jobs.pop(job.job_id, None)
                self.started -= 1
                self.changed.notify_all()
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": "Hermes couldn't start the job: the host is too busy right now."}
        done.wait(WATCH_JOB_START_ANSWER_S)
        with self.changed:
            answer = job.start_answer
            if answer is None:
                job.answered_late = True
                job.answer_pending = False
                self.changed.notify_all()
        if answer is None:
            return {"ok": True, "status": "accepted", "job_id": job.job_id, "title": job.title,
                    "message": "Hermes is starting the job. Its result will arrive later as a message; don't wait "
                               "for it."}
        undelivered = _watch_undelivered.get()
        if undelivered is not None and answer.get("status") == "not_started":
            # Lost on the way, the failure or cancel goes out as news instead.
            undelivered.append(lambda: self._untake([(job, "outcome_told", False)]))
        return answer

    def _start_session(self, job: "_WatchJob", instructions: str) -> Dict[str, Any]:
        """Creates the job's Hermes chat and submits its task; the start's answer."""
        try:
            # A job on another profile runs on that profile's own model, as
            # the phone's does: the voice-job model is this profile's.
            params: Dict[str, Any] = {"cols": 96, "source": "desktop", "title": job.title,
                                      **(self.options if job.profile is None else {})}
            profile = job.profile or self.grant.profile
            if profile:
                params["profile"] = profile
            created = self.rpc("session.create", params)
            sid = created.get("session_id")
            if not isinstance(sid, str) or not sid:
                raise WatchJobError("session.create gave no session")
            landed = created.get("profile")
            if job.profile is not None and isinstance(landed, str) and _limiter_key(landed) != _limiter_key(job.profile):
                # As the phone refuses it (AppState's createSession): only a
                # profile Hermes names, as a Hermes that names none is trusted
                # with the one asked for.
                with self.lock:
                    job.session_id = sid
                raise WatchJobError(f"Hermes put it on {landed[:64]} instead of {job.profile}")
            stored = created.get("stored_session_id")
            with self.lock:
                job.session_id = sid
                job.stored_session_id = stored if isinstance(stored, str) else ""
                cancelled = job.status == "cancelled"
            if not cancelled:
                self.rpc("prompt.submit", {"session_id": sid, "text": WATCH_JOB_PROMPT.format(instructions=instructions)})
                with self.lock:
                    cancelled = job.status == "cancelled"
                if cancelled:
                    # Cancelled while its prompt went in: stop that turn.
                    try:
                        self.rpc("session.interrupt", {"session_id": sid})
                    except WatchJobError as exc:
                        logger.info("Cancelling a starting Watch job failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 — the job didn't start; the model is told
            with self.changed:
                if job.active:
                    job.status = "failed"
                    job.error = str(exc)[:300] if isinstance(exc, WatchJobError) else type(exc).__name__
                self.changed.notify_all()
            if not isinstance(exc, WatchJobError):
                _log_watch_failure("Starting a Watch job failed", exc)
            self._close_session(job)
            if job.status == "cancelled":
                return {"ok": True, "status": "not_started", "title": job.title,
                        "message": f"{job.title} was cancelled before it started."}
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"Hermes couldn't start the job: {job.error}"}
        if cancelled:
            # cancel_job got there while it started, and said so.
            self._close_session(job)
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"{job.title} was cancelled before it started."}
        with self.changed:
            if job.status == "starting":
                job.status = "running"
            # A cancel between the prompt and here interrupted the turn
            # itself and told the Watch.
            cancelled = job.status == "cancelled"
            self.changed.notify_all()
        if cancelled:
            self._close_session(job)
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"{job.title} was cancelled before it started."}
        self._tag(job)
        where = f" on {job.profile}" if job.profile else ""
        return {"ok": True, "status": "started", "job_id": job.job_id, "title": job.title,
                "session_id": job.stored_session_id or sid,
                **({"profile": job.profile} if job.profile else {}),
                "message": f"The job is running on Hermes{where}. Its result will arrive later as a message; "
                           "don't wait for it."}

    def list(self) -> Dict[str, Any]:
        """Mirrors the phone's list_jobs answer (GeminiLiveToolBridge.listResult)."""
        with self.lock:
            visible = [job for job in self.jobs.values() if job.active or not job.outcome_told]
            lines = {
                "starting": "{} is still running.", "running": "{} is still running.",
                "needs_approval": "{} is waiting for your approval.", "finished": "{} has finished.",
                "failed": "{} failed.", "cancelled": "{} was cancelled.",
            }
            result: Dict[str, Any] = {
                "ok": True,
                "summary": " ".join(lines[job.status].format(job.title) for job in visible)
                or "No background jobs are running.",
            }
            for index, job in enumerate(visible, 1):
                result[f"job_{index}"] = f"id={job.job_id}; title={job.title}; status={job.status}" + (
                    f"; profile={job.profile}" if job.profile else "")
        return result

    def cancel(self, args: Dict[str, Any]) -> Dict[str, Any]:
        wanted = str(args.get("job_id") or "").strip()
        with self.changed:
            targets = [job for job in self.jobs.values() if job.active and (not wanted or job.job_id == wanted)]
            if not targets:
                return {"ok": True, "message": "There are no background jobs to cancel."}
            # Marked first, as on the phone: the interrupt's own end of turn
            # reads as this cancel, which the user hears in this answer.
            before = {job.job_id: (job.status, job.approval) for job in targets}
            for job in targets:
                job.status = "cancelled"
                job.approval = None
                job.outcome_told = True
                job.cancelling = bool(job.session_id)
            self.changed.notify_all()
        failed = []
        for job in targets:
            if not job.session_id:
                continue  # still starting: its start sees the cancel
            try:
                self.rpc("session.interrupt", {"session_id": job.session_id})
            except Exception as exc:  # noqa: BLE001 — it stays followed
                logger.info("Cancelling a Watch job failed: %s", type(exc).__name__)
                with self.changed:
                    if job.status == "cancelled":
                        job.status, job.approval = before[job.job_id]
                        job.outcome_told = False
                        if job.held_end is not None:
                            # Its turn ended meanwhile: that end stands.
                            self._settle(job, *job.held_end)
                    job.cancelling, job.held_end = False, None
                    self.changed.notify_all()
                failed.append(job)
                if self.ended and not job.active:
                    self._close_session_later(job)
            else:
                with self.changed:
                    job.cancelling, job.held_end = False, None
        cancelled = [job for job in targets if job not in failed]
        undelivered = _watch_undelivered.get()
        if undelivered is not None and cancelled:
            # Lost on the way, the cancel goes out as news instead.
            undelivered.append(lambda: self._untake([(job, "outcome_told", False) for job in cancelled]))
        if self.ended:
            for job in cancelled:
                self._close_session_later(job)
        if wanted:
            title = targets[0].title
            return {"ok": True, "message": f"Couldn't cancel {title}. It may still be running." if failed
                    else f"{title} was cancelled."}
        count = len(targets) - len(failed)
        parts = [f"Cancelled {count} background jobs."] if count else []
        parts += [f"Couldn't cancel {job.title}. It may still be running." for job in failed]
        return {"ok": True, "message": " ".join(parts)}

    def interrupt(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Puts the user's words into a job Hermes is still working on.

        The answer is the outcome, as the phone's VoiceFollowUpOutcome:
        "interrupted" (taken into the running turn), "queued" (Hermes takes
        it right after the step it is finishing), "finished" (too late),
        "failed" (with ``error``) or "unknown_job". The Watch words it for
        its model.
        """
        wanted = str(args.get("job_id") or "").strip()
        words = str(args.get("message") or "").strip()
        if not wanted:
            return {"ok": False, "status": 400, "detail": "job_id is required"}
        if not words:
            return {"ok": False, "status": 400, "detail": "message is required"}
        if _json_bytes(words) > WATCH_JOB_MAX_INSTRUCTION_BYTES:
            return {"ok": False, "status": 413, "detail": "The words are too long for a Watch job"}
        deadline = time.monotonic() + WATCH_FOLLOW_UP_DEADLINE_S
        with self.lock:
            if self.ended:
                return {"ok": False, "status": 410, "detail": "This call's Watch jobs have ended"}
            job = self.jobs.get(wanted)
            if job is None:
                return {"ok": True, "outcome": "unknown_job"}
            self.follow_ups_in_flight += 1
        try:
            if not job.follow_up_lock.acquire(timeout=WATCH_FOLLOW_UP_QUEUE_S):
                return {"ok": True, "outcome": "failed", "title": job.title,
                        "error": "Hermes is still taking the user's last words for this job."}
            try:
                outcome = self._follow_up(job, words, deadline)
            finally:
                job.follow_up_lock.release()
        finally:
            with self.lock:
                self.follow_ups_in_flight -= 1
        return {"ok": True, "title": job.title, **outcome}

    def _follow_up(self, job: _WatchJob, words: str, deadline: float) -> Dict[str, Any]:
        attempts = waits = 0
        while True:
            with self.lock:
                # Ended: the call is gone, so no more words go in.
                if not job.active or self.ended:
                    return {"outcome": "finished"}
                sid = job.session_id
                # Words Hermes already runs as its next turn keep this
                # turn's end from ending the job, whatever comes now.
                was_queued = job.follow_up == "queued"
                if sid and not was_queued:
                    job.follow_up = "sending"
            if sid:
                attempts += 1
                try:
                    result = self._redirect(sid, words, timeout=max(1.0, deadline - time.monotonic()))
                except WatchJobError as exc:
                    if not was_queued:
                        self._end_follow_up(job)
                    return {"outcome": "failed", "error": str(exc)[:300]}
                except Exception as exc:  # noqa: BLE001 — named; the words stay out of the log
                    _log_watch_failure("A Watch job follow-up failed", exc)
                    if not was_queued:
                        self._end_follow_up(job)
                    return {"outcome": "failed", "error": type(exc).__name__}
                if result in ("redirected", "queued"):
                    with self.changed:
                        # Settled meanwhile (an error ended the turn): the
                        # words came too late. A call that ended meanwhile
                        # still hears they went in: they were sent during it.
                        if not job.active:
                            return {"outcome": "finished"}
                        if job.follow_up == "sending":
                            # A queued turn's end held meanwhile was the step
                            # Hermes was finishing; the words' own turn ends the job.
                            job.follow_up = ("accepted" if result == "redirected" or job.held_completion is not None
                                             else "queued")
                    return {"outcome": "interrupted" if result == "redirected" else "queued"}
                if not was_queued:
                    self._end_follow_up(job)
            # Not running: the turn just ended (its end settles the job), or
            # Hermes hasn't started it yet. Asked again shortly while open.
            with self.lock:
                if not job.active or self.ended:
                    return {"outcome": "finished"}
            waits += 1
            if (attempts >= WATCH_FOLLOW_UP_ATTEMPTS or waits >= WATCH_FOLLOW_UP_ATTEMPTS * 3
                    or time.monotonic() + WATCH_FOLLOW_UP_RETRY_S >= deadline):
                return {"outcome": "failed", "error": f"Hermes wasn't working on \"{job.title}\" just then."}
            time.sleep(WATCH_FOLLOW_UP_RETRY_S)

    def _redirect(self, sid: str, words: str, timeout: float = WATCH_JOB_RPC_TIMEOUT_S) -> str:
        """Hermes' answer: "redirected", "queued" or "not_running". A Hermes
        without redirect gets a steer, as the phone sends it."""
        try:
            status = self.rpc("session.redirect", {"session_id": sid, "text": words}, timeout=timeout).get("status")
        except WatchJobError as exc:
            if not exc.unsupported_redirect:
                raise
            status = self.rpc("session.steer", {"session_id": sid, "text": words}, timeout=timeout).get("status")
            return "not_running" if status == "rejected" else "redirected"
        status = status.lower() if isinstance(status, str) else ""
        return status if status in ("redirected", "queued") else "not_running"

    def _end_follow_up(self, job: _WatchJob) -> None:
        """The follow-up didn't take: an end held meanwhile was the turn's last after all."""
        with self.changed:
            held = job.held_completion
            job.follow_up, job.held_completion = None, None
            if held is None or not job.active:
                return
            self._settle(job, *held)
            self.changed.notify_all()
            ended = self.ended
        if ended:
            self._close_session_later(job)

    def news(self, args: Dict[str, Any], caller: Optional["_WatchGrant"] = None) -> Dict[str, Any]:
        """Settled jobs and approval requests not yet told, waiting up to
        ``wait_s`` (at most WATCH_JOB_NEWS_WAIT_S) for some while jobs run.
        The wait also ends with ``caller``, the grant asking: after a carry,
        the call's previous grant still answers until it closes."""
        wait = args.get("wait_s")
        if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait >= 0:
            wait = float(min(wait, WATCH_JOB_NEWS_WAIT_S))
        else:
            wait = WATCH_JOB_NEWS_WAIT_S
        deadline = time.monotonic() + wait
        marks: list = []
        reaped: list = []
        with self.changed:
            while True:
                reaped += self._settle_stale_holds()
                running = sum(1 for job in self.jobs.values() if job.active)
                # Every request still open, so the Watch drops a card
                # answered elsewhere or timed out.
                approvals = [{"job_id": job.job_id, "request_id": job.approval["request_id"]}
                             for job in self.jobs.values() if job.approval and job.status == "needs_approval"]
                answer = {"ok": True, "news": [], "running": running, "more": True, "approvals": approvals}
                news = self._take_news(max(0, WATCH_JOB_ANSWER_BYTES - _json_bytes(answer)), marks)
                remaining = deadline - time.monotonic()
                closed = self.grant.closed.is_set() or (caller is not None and caller.closed.is_set())
                if news or not running or self.ended or closed or remaining <= 0:
                    break
                self.changed.wait(min(remaining, 1.0))
            answer["news"] = news
            answer["more"] = any(self._untold(job) is not False for job in self.jobs.values())
            ended = self.ended
        if ended:
            for job in reaped:
                self._close_session_later(job)
        undelivered = _watch_undelivered.get()
        if undelivered is not None and marks:
            undelivered.append(lambda: self._untake(marks))
        return answer

    def _settle_stale_holds(self) -> list:
        """An end held for queued words whose turn never showed: it was the
        job's last after all. Called with the lock held; the jobs settled."""
        now = time.monotonic()
        settled = []
        for job in self.jobs.values():
            if (job.active and job.held_completion is not None and job.follow_up == "accepted"
                    and (now - job.held_at >= WATCH_FOLLOW_UP_HOLD_S
                         or now - job.held_since >= WATCH_FOLLOW_UP_HOLD_MAX_S)):
                self._settle(job, *job.held_completion)
                settled.append(job)
        return settled

    def _untake(self, marks: list) -> None:
        """Marks news as not told again: its answer never reached the Watch."""
        with self.changed:
            for job, field, previous in marks:
                setattr(job, field, previous)
            self.changed.notify_all()

    @staticmethod
    def _untold(job: "_WatchJob") -> Any:
        """False when the Watch has heard all of ``job``'s news; else None
        for its outcome or the approval request's key."""
        if job.answer_pending:
            return False
        if not job.active and not job.outcome_told:
            return None
        if job.approval and job.status == "needs_approval" and job.approval_told != job.approval_key:
            return job.approval_key
        return False

    def _take_news(self, budget: int, marks: list) -> list:
        """Untold news within ``budget`` bytes, each mark it moves added to
        ``marks``; called with the lock held."""
        news: list = []
        used = 0
        for job in self.jobs.values():
            told = self._untold(job)
            if told is False:
                continue
            item = job.news()
            size = _json_bytes(item) + 1
            if used + size > budget:
                if news:
                    break  # the rest goes with the next call
                # The clips above make any one item fit; should one not,
                # its status still goes rather than holding up all news.
                item = {key: item[key] for key in ("job_id", "title", "status")}
                size = _json_bytes(item) + 1
            used += size
            news.append(item)
            if told is None:
                marks.append((job, "outcome_told", job.outcome_told))
                job.outcome_told = True
            else:
                marks.append((job, "approval_told", job.approval_told))
                job.approval_told = told
        return news

    def answer_approval(self, args: Dict[str, Any]) -> Dict[str, Any]:
        choice = str(args.get("choice") or "")
        if choice not in WATCH_APPROVAL_CHOICES:
            return {"ok": False, "status": 400, "detail": "choice must be once or deny"}
        with self.lock:
            job = self.jobs.get(str(args.get("job_id") or ""))
            approval = dict(job.approval) if job and job.approval and job.status == "needs_approval" else None
        if job is None or approval is None:
            return {"ok": True, "status": "not_pending", "message": "That job isn't waiting for an approval any more."}
        wanted = str(args.get("request_id") or "")
        if wanted != approval["request_id"]:
            # Only the request the Watch showed: a newer one needs its own answer.
            return {"ok": True, "status": "not_pending", "message": "That approval request has been replaced."}
        params: Dict[str, Any] = {"session_id": job.session_id, "choice": choice}
        if approval["request_id"]:
            params["request_id"] = approval["request_id"]
        try:
            resolved = self.rpc("approval.respond", params).get("resolved")
        except WatchJobError as exc:
            return {"ok": True, "status": "failed", "message": f"Hermes didn't take the answer: {exc}"}
        with self.changed:
            if job.approval and job.approval["request_id"] == approval["request_id"]:
                self._approval_gone(job)
            self.changed.notify_all()
        # Hermes answers how many approvals the decision unblocked.
        if not isinstance(resolved, int) or resolved <= 0:
            return {"ok": True, "status": "not_pending", "message": "That job isn't waiting for an approval any more."}
        return {"ok": True, "status": "approved" if choice == "once" else "denied", "job_id": job.job_id}

    # Ending

    def end(self) -> None:
        """The grant ended: settled jobs' sessions close now, running ones'
        once they settle. A job still running keeps running in Hermes."""
        with self.changed:
            if self.ended:
                return
            self.ended = True
            settled = [job for job in self.jobs.values() if not job.active]
            if self._holding():
                # No news poll reaps a held end once the call is gone.
                self._reap_holds_later()
            self.changed.notify_all()
        for job in settled:
            self._close_session_later(job)
        self._close_transport_if_idle()

    def _reap_holds_later(self) -> None:
        """Starts the hold reaper unless it runs. Called with the lock held."""
        if self.reaping:
            return
        try:
            threading.Thread(target=self._reap_holds_after_end, name="conduit-watch-job-holds", daemon=True).start()
        except RuntimeError as exc:
            logger.warning("Couldn't start the Watch job hold reaper (%s)", type(exc).__name__)
            return
        self.reaping = True

    def _holding(self) -> bool:
        """A follow-up in flight or an end held for one. Called with the lock
        held. Words Hermes queued with no end held yet need nothing: their
        job runs on, and its next end starts the reaper again."""
        return self.follow_ups_in_flight > 0 or any(
            job.active and job.held_completion is not None for job in self.jobs.values())

    def _reap_holds_after_end(self) -> None:
        """Settles ends held for follow-up words that never ran, after the
        call ended, so their sessions and the transport close. Runs while
        anything holds; a hold taken later starts it again."""
        holding = True
        try:
            while holding:
                with self.changed:
                    reaped = self._settle_stale_holds()
                    if reaped:
                        self.changed.notify_all()
                    holding = self._holding()
                    if not holding:
                        self.reaping = False
                for job in reaped:
                    self._close_session_later(job)
                self._close_transport_if_idle()
                if holding:
                    time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 — a later hold starts a new reaper
            _log_watch_failure("The Watch job hold reaper failed", exc)
        finally:
            if holding:
                # Left early: a later hold starts a new reaper.
                with self.changed:
                    self.reaping = False

    def _close_session_later(self, job: _WatchJob) -> None:
        try:
            threading.Thread(target=self._close_session, args=(job,), name="conduit-watch-job-close",
                             daemon=True).start()
        except RuntimeError:
            self._close_session(job)

    def _close_session(self, job: _WatchJob) -> None:
        """Closes the job's live session, unless the app has joined it; its
        chat stays in history either way."""
        with self.lock:
            if job.closed or not job.session_id:
                return
            job.closed = True
        record = self.server._sessions.get(job.session_id)
        if isinstance(record, dict) and record.get("transport") is self.transport:
            try:
                self.rpc("session.close", {"session_id": job.session_id})
            except Exception as exc:  # noqa: BLE001 — Hermes' reaper closes it once our transport closes
                _log_watch_failure("Closing a Watch job's session failed", exc)
        self._close_transport_if_idle()

    def _close_transport_if_idle(self) -> None:
        # Once the call has ended and nothing runs: a session the app joined
        # then lives or ends with the app alone.
        with self.lock:
            if self.ended and not any(job.active for job in self.jobs.values()):
                self.transport.close()

    def _tag(self, job: _WatchJob) -> None:
        """Files the job under Voice Jobs in Conduit, as a phone job is."""
        session_id = job.stored_session_id or job.session_id
        try:
            with _profile_scope(job.profile or self.grant.profile):
                with _voice_lock():
                    db = _open_voice_db(_VOICE_WRITE)
                    try:
                        if _voice_tags(db).get(session_id, {}).get("kind") != "call":
                            _set_voice_tag(db, session_id, {"kind": "job"})
                    finally:
                        db.close()
        except Exception as exc:  # noqa: BLE001 — the job runs untagged
            _log_watch_failure("Tagging a Watch job failed", exc)


def run_watch_job_call(grant: "_WatchGrant", tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    jobs = grant.jobs
    if jobs is None:
        return {"ok": False, "status": 403, "detail": "This tool isn't available to the Watch"}
    try:
        if tool == "start_job":
            return jobs.start(args)
        if tool == "list_jobs":
            return jobs.list()
        if tool == "cancel_job":
            return jobs.cancel(args)
        if tool == WATCH_JOB_FOLLOW_UP:
            return jobs.interrupt(args)
        if tool == "job_news":
            return jobs.news(args, caller=grant)
        return jobs.answer_approval(args)
    except Exception as exc:  # noqa: BLE001 — named; the message can carry the task
        _log_watch_failure(f"A Watch {tool} call failed", exc)
        return {"ok": False, "status": 500, "detail": f"The job call failed on the host ({type(exc).__name__})"}


# --- Watch audio: GPT-Live and Grok on a Conduit Watch call -------------------
#
# GPT-Live on the ChatGPT subscription speaks only WebRTC, which the Watch
# can't run, and Grok runs on this host's xAI sign-in, which never leaves it.
# So for those two engines the host holds the provider session and the Watch
# streams to it through the push relay (hermes-conduit
# designs/apple-watch-gpt-live.md, "Bridge design"). A grant opened with
# "audio": true gets a bridge: this host dials the relay's
# /v1/watch-audio/{grant}/host and waits, and the Watch dials .../watch with
# the grant's relay key. The relay pairs the two sockets and copies messages
# (relay/src/watch-audio.mjs). Every message is sealed here and on the Watch
# with a per-stream key from the grant's root, so the relay can neither read
# nor forge one.
#
# Binary messages:
#   [0x00, n]                  the relay's notice: 1 Watch connected, 2 Watch gone
#   [0x04][sid 16][version 1]  the Watch's hello, in clear, first on each connection
#   [type][counter 8 BE][ct]   sealed: 1 audio (PCM16LE mono), 2 engine event
#                              (the engine's own JSON text), 3 control (JSON)
# Keys: HKDF-SHA256 of the grant root, salt conduit-watch-audio-v1, info
# "conduit-watch-audio-v1 {watch-to-host|host-to-watch} stream={sid}", the
# sid in base64url. Nonce: 4 zero bytes and the counter, which starts at 0
# for each stream and direction and must rise. The AAD names the direction,
# grant, stream and type. A stream id is taken once per grant: its keys
# would repeat nonces.
#
# Control, Watch to host: {"type": "start", "engine": "gpt_live" | "grok",
# "voice"?, "briefing"?, "greeting"?, "history"?} and {"type": "end"}. Host
# to Watch: "started" (engine, model, voice, input_rate, output_rate; for
# GPT-Live also briefing_applied and greeting_applied), "ended" (reason) and
# "error" (code, message). The Watch waits for "started" before it sends
# audio or events. It owns the conversation, as the phone does: it speaks each
# engine's own events (GPT-Live's frameless ones, Grok's realtime ones), and
# its tools and jobs go through the grant's lookups. The host adds only what
# has to stay here: the sign-ins, Grok's pacing, and WebRTC for GPT-Live,
# which runs in watch_audio_helper.py under a Python with aiortc: this
# plugin's own environment, made when Conduit asks (/watch-audio/prepare),
# or Hermes' own Python when it already has aiortc.

WATCH_AUDIO_VERSION = 1
WATCH_AUDIO_ENGINES = ("gpt_live", "grok")
WATCH_AUDIO_UP = "watch-to-host"
WATCH_AUDIO_DOWN = "host-to-watch"
WATCH_AUDIO_AUDIO = 1
WATCH_AUDIO_EVENT = 2
WATCH_AUDIO_CONTROL = 3
WATCH_AUDIO_HELLO = 4
WATCH_AUDIO_NOTICE_CONNECTED = 1
WATCH_AUDIO_NOTICE_GONE = 2
_WATCH_AUDIO_SALT = b"conduit-watch-audio-v1"
_WATCH_AUDIO_AAD_TAG = "conduit-watch-audio/1"
# The relay's bound on one message (relay/src/watch-audio.mjs MAX_MESSAGE_BYTES).
WATCH_AUDIO_MAX_MESSAGE_BYTES = 64 * 1024
# What fits in one sealed message: the type, counter and tag go around it.
WATCH_AUDIO_MAX_PLAIN_BYTES = WATCH_AUDIO_MAX_MESSAGE_BYTES - 1 - 8 - 16
# Streams one grant may open: a call and its rejoins.
WATCH_AUDIO_STREAMS_PER_GRANT = 32
# Engine sessions this host holds for Watches at once, every profile together.
WATCH_AUDIO_MAX_SESSIONS = 4
WATCH_AUDIO_RELAY_TIMEOUT_S = 15.0
WATCH_AUDIO_RECONNECT_MAX_S = 10.0
# A host socket the relay drops sooner than this (another host took the
# grant's socket, say) reconnects with backoff rather than at once.
WATCH_AUDIO_STABLE_S = 5.0
# A session that won't stop when asked is cancelled after this.
WATCH_AUDIO_STOP_TIMEOUT_S = 5.0
# GPT-Live: the helper's offer (ICE gathering included), then WebRTC
# connecting after the answer. Past that, one more try with STUN: host
# candidates alone usually reach OpenAI's public media servers, and skip
# STUN's gathering wait.
WATCH_AUDIO_OFFER_TIMEOUT_S = 20.0
WATCH_AUDIO_CONNECT_TIMEOUT_S = 8.0
WATCH_AUDIO_STUN = ("stun:stun.l.google.com:19302",)
WATCH_AUDIO_HELPER_EXIT_S = 3.0
# The helper's frames (watch_audio_helper.py MAX_FRAME).
WATCH_AUDIO_HELPER_MAX_FRAME = 1024 * 1024
GPT_LIVE_WATCH_INPUT_RATE = 16_000
GPT_LIVE_WATCH_OUTPUT_RATE = 24_000
# Conduit's phone Grok uses these too (GrokLiveProtocol.swift).
GROK_WATCH_RATE = 24_000
# Grok sends audio faster than real time: the Watch gets at most this much
# ahead of real time, which keeps the relay's rate limit and the Watch's
# buffer in bounds. Audio goes out in pieces of WATCH_AUDIO_CHUNK_S.
WATCH_AUDIO_LEAD_S = 1.5
WATCH_AUDIO_CHUNK_S = 0.1
# Audio held for pacing past this (a stalled Watch) drops its oldest.
WATCH_AUDIO_MAX_QUEUED_S = 60.0
# Events held behind that audio past this many bytes drop their oldest too.
WATCH_AUDIO_MAX_QUEUED_EVENT_BYTES = 1024 * 1024
# Audio a GPT-Live helper sends before "started" is out, held until it is.
WATCH_AUDIO_MAX_HELD = 200
WATCH_AUDIO_AIORTC = "aiortc==1.15.0"
WATCH_AUDIO_ENV_VAR = "CONDUIT_WATCH_AUDIO_ENV"
_WATCH_AUDIO_MARKER = "conduit-watch-audio.json"
WATCH_AUDIO_VENV_TIMEOUT_S = 180.0
WATCH_AUDIO_PIP_TIMEOUT_S = 900.0
_WATCH_AUDIO_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_audio_helper.py")
# What the helper gets of Hermes' environment: enough to start Python and
# find the system's libraries, never the keys Hermes may hold there.
_WATCH_AUDIO_HELPER_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "TEMP", "TMP",
                           "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
                           "DYLD_FALLBACK_LIBRARY_PATH")
_GROK_AUDIO_DELTAS = ("response.output_audio.delta", "response.audio.delta")
WATCH_AUDIO_NEEDS_RUNTIME = ("GPT-Live on the Watch needs its WebRTC runtime on the Hermes host; "
                             "prepare it from Conduit's Voice settings")
WATCH_AUDIO_STALE_RUNTIME = ("GPT-Live on the Watch needs its WebRTC runtime made again for this plugin version; "
                             "prepare it from Conduit's Voice settings")


class WatchAudioError(Exception):
    """A Watch audio message that can't be opened or built."""


class WatchAudioFailure(Exception):
    """Why a Watch audio session couldn't start or ended badly; ``code`` is for the Watch to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _watch_audio_failure(exc: TokenError) -> WatchAudioFailure:
    codes = {400: "bad_request", 429: "rate_limited", 501: "unavailable", 503: "unavailable",
             502: "refused", 504: "unreachable"}
    return WatchAudioFailure(codes.get(exc.status, "failed"), str(exc))


def watch_audio_keys(secret: bytes, sid: bytes) -> Dict[str, bytes]:
    """A stream's two directional keys, from the grant's 32-byte root."""
    if len(secret) != 32:
        raise WatchAudioError("the grant secret must be 32 bytes")
    if len(sid) != 16:
        raise WatchAudioError("the stream id must be 16 bytes")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    stream = _b64u(sid)
    return {
        direction: HKDF(algorithm=hashes.SHA256(), length=32, salt=_WATCH_AUDIO_SALT,
                        info=f"conduit-watch-audio-v1 {direction} stream={stream}".encode("ascii")).derive(secret)
        for direction in (WATCH_AUDIO_UP, WATCH_AUDIO_DOWN)
    }


def watch_audio_aad(direction: str, grant_id: str, sid: bytes, kind: int) -> bytes:
    return "\n".join([_WATCH_AUDIO_AAD_TAG, direction, f"grant={grant_id}", f"sid={_b64u(sid)}",
                      f"type={kind}"]).encode("ascii")


def _watch_audio_nonce(counter: int) -> bytes:
    return b"\0\0\0\0" + counter.to_bytes(8, "big")


def seal_watch_audio(key: bytes, direction: str, grant_id: str, sid: bytes, kind: int, counter: int,
                     plain: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if kind not in (WATCH_AUDIO_AUDIO, WATCH_AUDIO_EVENT, WATCH_AUDIO_CONTROL):
        raise WatchAudioError("unknown message type")
    if len(plain) > WATCH_AUDIO_MAX_PLAIN_BYTES:
        raise WatchAudioError("the message is too large")
    if not 0 <= counter < 2 ** 64:
        raise WatchAudioError("the counter is out of range")
    ciphertext = ChaCha20Poly1305(key).encrypt(_watch_audio_nonce(counter), bytes(plain),
                                               watch_audio_aad(direction, grant_id, sid, kind))
    return bytes([kind]) + counter.to_bytes(8, "big") + ciphertext


def open_watch_audio(key: bytes, direction: str, grant_id: str, sid: bytes,
                     message: bytes) -> Tuple[int, int, bytes]:
    """(type, counter, plaintext) of one sealed message; WatchAudioError when it doesn't verify."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if not 1 + 8 + 16 <= len(message) <= WATCH_AUDIO_MAX_MESSAGE_BYTES:
        raise WatchAudioError("the message is malformed")
    kind = message[0]
    if kind not in (WATCH_AUDIO_AUDIO, WATCH_AUDIO_EVENT, WATCH_AUDIO_CONTROL):
        raise WatchAudioError("unknown message type")
    counter = int.from_bytes(message[1:9], "big")
    try:
        plain = ChaCha20Poly1305(key).decrypt(_watch_audio_nonce(counter), bytes(message[9:]),
                                              watch_audio_aad(direction, grant_id, sid, kind))
    except Exception as error:
        raise WatchAudioError("the message did not verify") from error
    return kind, counter, plain


class _WatchAudioStream:
    """One Watch connection: its keys and both counters."""

    def __init__(self, grant_id: str, secret: bytes, sid: bytes) -> None:
        keys = watch_audio_keys(secret, sid)
        self.grant_id = grant_id
        self.sid = sid
        self.up_key = keys[WATCH_AUDIO_UP]
        self.down_key = keys[WATCH_AUDIO_DOWN]
        self.received = -1
        self.sent = 0

    def open(self, message: bytes) -> Tuple[int, bytes]:
        kind, counter, plain = open_watch_audio(self.up_key, WATCH_AUDIO_UP, self.grant_id, self.sid, message)
        # Checked once it verifies, so a forged counter can't move the window.
        if counter <= self.received:
            raise WatchAudioError("the message was replayed")
        self.received = counter
        return kind, plain

    def seal(self, kind: int, plain: bytes) -> bytes:
        sealed = seal_watch_audio(self.down_key, WATCH_AUDIO_DOWN, self.grant_id, self.sid, kind, self.sent, plain)
        self.sent += 1
        return sealed


def _watch_audio_event_text(text: str) -> bytes:
    """An engine event for the Watch; one too large to seal goes as its type alone."""
    data = text.encode("utf-8")
    if len(data) <= WATCH_AUDIO_MAX_PLAIN_BYTES:
        return data
    try:
        kind = json.loads(text).get("type")
    except (ValueError, AttributeError):
        kind = None
    return json.dumps({"type": kind if isinstance(kind, str) else "unknown", "conduit_truncated": True}).encode("utf-8")


# --- The helper's Python ----------------------------------------------------


def _watch_audio_env_dir() -> str:
    """Where this plugin keeps its own Python for GPT-Live's WebRTC: one per
    host, beside Hermes' home rather than inside a profile."""
    override = str(os.environ.get(WATCH_AUDIO_ENV_VAR) or "").strip()
    if override:
        return override
    home = str(os.environ.get("HERMES_HOME") or "").strip() or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, "conduit_push", "watch-audio-env")


def _env_python(env_dir: str) -> str:
    if os.name == "nt":
        return os.path.join(env_dir, "Scripts", "python.exe")
    return os.path.join(env_dir, "bin", "python")


class _RuntimeBuildFailed(Exception):
    """Making the helper's Python failed; the message says why, for the user."""


def _last_line(text: Any) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _watch_audio_marker_pin(env_dir: str) -> Optional[str]:
    """The aiortc pin the plugin's environment was made for; None without a readable marker."""
    try:
        with open(os.path.join(env_dir, _WATCH_AUDIO_MARKER), encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return None
    pin = marker.get("requirement") if isinstance(marker, dict) else None
    return pin if isinstance(pin, str) else None


def _watch_audio_marker_current(env_dir: str) -> bool:
    """Whether the plugin's environment was made for this plugin's aiortc pin.

    An upgrade that moves the pin leaves the old environment unready, so
    Conduit's prepare makes it again.
    """
    return _watch_audio_marker_pin(env_dir) == WATCH_AUDIO_AIORTC


def _watch_audio_helper_env(extra: Tuple[str, ...] = ()) -> Dict[str, str]:
    names = frozenset(_WATCH_AUDIO_HELPER_ENV + extra)
    return {name: value for name, value in os.environ.items() if name in names or name.startswith("LC_")}


class _WatchAudioRuntime:
    """The Python that runs watch_audio_helper.py, and making one.

    Any Hermes install works, whatever Python it runs on and however its
    packages got there: the plugin makes a small environment of its own with
    aiortc, from the Python Hermes runs (or uv), and never installs into
    Hermes'. It is made when Conduit asks, never in the middle of a call.
    """

    def __init__(self, env_dir: Optional[Callable[[], str]] = None, run: Optional[Callable[..., Any]] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None,
                 has_module: Optional[Callable[[str], bool]] = None,
                 base_python: Optional[str] = None) -> None:
        self._env_dir = env_dir or _watch_audio_env_dir
        self._run = run or subprocess.run
        self._which = which or shutil.which
        self._has_module = has_module or _has_module
        self._base_python = base_python or sys.executable
        self._lock = threading.Lock()
        self.preparing = False
        self.failure: Optional[str] = None

    def python(self) -> Optional[Tuple[str, Dict[str, str], Tuple[str, ...], str]]:
        """(python, environment, flags, source) for the helper, or None when no Python here has aiortc."""
        env_dir = self._env_dir()
        python = _env_python(env_dir)
        if os.path.isfile(os.path.join(env_dir, _WATCH_AUDIO_MARKER)):
            # The plugin's own environment wins once made; one made for
            # another pin is unready until prepare makes it again.
            if _watch_audio_marker_current(env_dir) and os.path.isfile(python):
                return python, _watch_audio_helper_env(), ("-I",), "plugin"
            return None
        if self._has_module("aiortc") and self._has_module("av"):
            # Some installs add Hermes' packages to sys.path at start, so the
            # helper gets this process's import path. PYTHONHOME too, where
            # a bundled Python needs it to start.
            env = _watch_audio_helper_env(("PYTHONHOME",))
            env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
            return self._base_python, env, (), "hermes"
        return None

    def status(self) -> Dict[str, Any]:
        found = self.python()
        with self._lock:
            preparing, failure = self.preparing, self.failure
        if found is not None:
            return {"runtime": "ready", "source": found[3], "reason": None}
        if preparing:
            return {"runtime": "preparing", "source": None, "reason": None}
        if failure:
            return {"runtime": "failed", "source": None, "reason": failure}
        # The failure snapshot above already said there's none.
        return {"runtime": "missing", "source": None, "reason": self._marker_reason()}

    def missing_reason(self) -> str:
        """Why there's no runtime: a failed prepare's own reason, else whether one was ever made."""
        with self._lock:
            failure = self.failure
        return failure or self._marker_reason()

    def _marker_reason(self) -> str:
        pin = _watch_audio_marker_pin(self._env_dir())
        return WATCH_AUDIO_STALE_RUNTIME if pin is not None and pin != WATCH_AUDIO_AIORTC else WATCH_AUDIO_NEEDS_RUNTIME

    def prepare(self, start: Optional[Callable[[Callable[[], None]], None]] = None) -> Dict[str, Any]:
        """Starts making the environment unless it's there or on its way; returns the status."""
        if self.python() is not None:
            return self.status()
        with self._lock:
            if self.preparing:
                return {"runtime": "preparing", "source": None, "reason": None}
            self.preparing = True
            self.failure = None
        try:
            (start or _start_daemon)(self._prepare)
        except Exception as exc:  # noqa: BLE001 — e.g. no thread to spare
            with self._lock:
                self.preparing = False
                self.failure = f"Couldn't start preparing on the host ({type(exc).__name__})"
        return self.status()

    def _prepare(self) -> None:
        failure: Optional[str] = None
        try:
            self.build()
        except _RuntimeBuildFailed as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 — reported on the status
            failure = f"Preparing failed on the host ({type(exc).__name__})"
        if failure:
            logger.warning("Preparing GPT-Live for the Watch failed: %s", failure)
        else:
            logger.info("GPT-Live for the Watch is ready")
        with self._lock:
            self.preparing = False
            self.failure = failure

    def _step(self, command: list, timeout: float, what: str) -> str:
        try:
            done = self._run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise _RuntimeBuildFailed(f"{what} timed out")
        except OSError as exc:
            raise _RuntimeBuildFailed(f"{what} couldn't run ({type(exc).__name__})")
        if done.returncode != 0:
            detail = _last_line(done.stderr) or _last_line(done.stdout)
            raise _RuntimeBuildFailed(f"{what} failed: {detail}" if detail else f"{what} failed")
        return str(done.stdout or "")

    def build(self) -> None:
        env_dir = self._env_dir()
        python = _env_python(env_dir)
        if os.path.isdir(env_dir):
            # Left by an earlier try: only ever a folder this made.
            if os.listdir(env_dir) and not os.path.isfile(os.path.join(env_dir, "pyvenv.cfg")):
                raise _RuntimeBuildFailed(f"{env_dir} exists and isn't a Python environment")
            shutil.rmtree(env_dir)
        os.makedirs(os.path.dirname(env_dir), exist_ok=True)
        uv = self._which("uv")
        try:
            self._step([self._base_python, "-m", "venv", env_dir], WATCH_AUDIO_VENV_TIMEOUT_S, "Making the environment")
            self._step([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                        "--no-warn-script-location", WATCH_AUDIO_AIORTC], WATCH_AUDIO_PIP_TIMEOUT_S, "Installing aiortc")
        except _RuntimeBuildFailed as first:
            # A Python without venv or pip (a distribution's own, say): uv
            # brings both, when the host has it.
            if not uv:
                raise _RuntimeBuildFailed(f"{first}. Installing uv on the host lets the plugin try again with it")
            shutil.rmtree(env_dir, ignore_errors=True)
            self._step([uv, "venv", "--python", self._base_python, env_dir], WATCH_AUDIO_VENV_TIMEOUT_S,
                       "Making the environment with uv")
            self._step([uv, "pip", "install", "--python", python, WATCH_AUDIO_AIORTC], WATCH_AUDIO_PIP_TIMEOUT_S,
                       "Installing aiortc with uv")
        version = self._step([python, "-I", "-c", "import aiortc, av; print(aiortc.__version__)"], 60.0,
                             "Checking aiortc").strip()
        with open(os.path.join(env_dir, _WATCH_AUDIO_MARKER), "w", encoding="utf-8") as handle:
            json.dump({"aiortc": version, "requirement": WATCH_AUDIO_AIORTC}, handle)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="conduit-watch-audio-prepare", daemon=True).start()


_watch_audio_runtime = _WatchAudioRuntime()


class _WatchAudioSlots:
    """Engine sessions for Watches on this host, every grant together."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def release(self) -> None:
        with self._lock:
            self.used = max(0, self.used - 1)


_watch_audio_slots = _WatchAudioSlots(WATCH_AUDIO_MAX_SESSIONS)


async def _first_done(timeout: Optional[float], *awaitables: Any) -> set:
    """Waits for the first of ``awaitables`` (futures or coroutines); returns the finished ones."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return done
    finally:
        for task, item in zip(tasks, awaitables):
            # Only what this made: a future it was handed is the caller's.
            if task is not item and not task.done():
                task.cancel()


# --- Sessions -----------------------------------------------------------------


class _WatchAudioSession:
    """One engine session for one Watch stream, from its start to its end."""

    engine = ""

    def __init__(self, bridge: "_WatchAudioBridge", stream: _WatchAudioStream, request: Dict[str, Any]) -> None:
        self.bridge = bridge
        self.stream = stream
        self.request = request
        self.profile = bridge.grant.profile
        self.stopping = asyncio.Event()
        self.reason = "ended"

    def stop(self, reason: str) -> None:
        if not self.stopping.is_set():
            self.reason = reason
            self.stopping.set()

    async def send(self, kind: int, plain: bytes) -> bool:
        return await self.bridge.send(self.stream, kind, plain)

    async def run(self) -> None:
        raise NotImplementedError

    async def audio(self, pcm: bytes) -> None:
        raise NotImplementedError

    async def event(self, data: bytes) -> None:
        raise NotImplementedError


class _GptLiveHelper:
    """One run of watch_audio_helper.py."""

    def __init__(self, session: "_GptLiveWatchSession") -> None:
        self.session = session
        self.process: Any = None
        self.offer: asyncio.Future = asyncio.get_running_loop().create_future()
        self.connected = asyncio.Event()
        self.ended = asyncio.Event()
        self.reason: Optional[str] = None
        self.lock = asyncio.Lock()
        self.tasks: list = []

    async def start(self, runtime: Tuple[str, Dict[str, str], Tuple[str, ...], str], stun: Tuple[str, ...]) -> None:
        python, env, flags, _ = runtime
        self.process = await asyncio.create_subprocess_exec(
            python, *flags, _WATCH_AUDIO_HELPER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        self.tasks = [asyncio.ensure_future(self._read()), asyncio.ensure_future(self._log_errors())]
        await self.write("C", json.dumps({"input_rate": GPT_LIVE_WATCH_INPUT_RATE,
                                          "output_rate": GPT_LIVE_WATCH_OUTPUT_RATE, "stun": list(stun)}).encode("utf-8"))

    async def write(self, kind: str, payload: bytes) -> None:
        if self.process is None or self.ended.is_set():
            return
        async with self.lock:
            try:
                self.process.stdin.write(kind.encode("ascii") + len(payload).to_bytes(4, "big") + payload)
                await self.process.stdin.drain()
            except Exception:  # noqa: BLE001 — a helper that exited
                self._end("the WebRTC helper stopped")

    def _end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.ended.set()
        if not self.offer.done():
            self.offer.cancel()

    async def _read(self) -> None:
        stdout = self.process.stdout
        try:
            while True:
                header = await stdout.readexactly(5)
                kind, length = chr(header[0]), int.from_bytes(header[1:], "big")
                if kind not in "ORpeX" or length > WATCH_AUDIO_HELPER_MAX_FRAME:
                    self._end("the WebRTC helper sent a bad frame")
                    return
                payload = await stdout.readexactly(length)
                if kind == "O":
                    if not self.offer.done():
                        self.offer.set_result(payload.decode("utf-8", "replace"))
                elif kind == "R":
                    self.connected.set()
                elif kind == "p":
                    await self.session.forward(WATCH_AUDIO_AUDIO, payload)
                elif kind == "e":
                    await self.session.forward(WATCH_AUDIO_EVENT,
                                               _watch_audio_event_text(payload.decode("utf-8", "replace")))
                else:
                    try:
                        reason = json.loads(payload or b"{}").get("reason")
                    except (ValueError, AttributeError):
                        reason = None
                    self._end(str(reason or "GPT-Live ended the call")[:200])
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            self._end("the WebRTC helper exited")

    async def _log_errors(self) -> None:
        # The helper writes one short line per problem, never audio or SDP.
        try:
            async for line in self.process.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    logger.info("GPT-Live Watch helper: %s", text[:200])
        except Exception:  # noqa: BLE001 — only logging
            return

    async def close(self) -> None:
        if self.process is None:
            return
        await self.write("Q", b"")
        with contextlib.suppress(Exception):
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), WATCH_AUDIO_HELPER_EXIT_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            with contextlib.suppress(Exception):
                await self.process.wait()
        self._end(self.reason or "closed")
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class _GptLiveWatchSession(_WatchAudioSession):
    """GPT-Live through a WebRTC helper: the plugin does the SDP exchange, so
    the Codex sign-in never enters the helper."""

    engine = "gpt_live"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper: Optional[_GptLiveHelper] = None
        self.answer: Dict[str, Any] = {}
        self.live = False
        self.held: list = []

    async def forward(self, kind: int, plain: bytes) -> None:
        if not self.live:
            if len(self.held) < WATCH_AUDIO_MAX_HELD:
                self.held.append((kind, plain))
            return
        await self.send(kind, plain)

    async def run(self) -> None:
        runtime = self.bridge.runtime.python()
        if runtime is None:
            raise WatchAudioFailure("unavailable", self.bridge.runtime.missing_reason())
        for stun in ((), WATCH_AUDIO_STUN):
            helper = _GptLiveHelper(self)
            self.helper = helper
            try:
                await helper.start(runtime, stun)
                if await self._connect(helper, retry=not stun):
                    await self._started(helper)
                    await _first_done(None, self.stopping.wait(), helper.ended.wait())
                    if helper.ended.is_set() and not self.stopping.is_set():
                        self.reason = helper.reason or "GPT-Live ended the call"
                    return
                if self.stopping.is_set():
                    return
            finally:
                self.live = False
                await helper.close()
        raise WatchAudioFailure("unreachable", "GPT-Live's audio connection didn't come up from the Hermes host")

    async def _connect(self, helper: _GptLiveHelper, *, retry: bool) -> bool:
        """True once WebRTC is up; False to try again with STUN (or when stopped)."""
        await _first_done(WATCH_AUDIO_OFFER_TIMEOUT_S, helper.offer, self.stopping.wait(), helper.ended.wait())
        if self.stopping.is_set():
            return False
        if not helper.offer.done() or helper.offer.cancelled():
            if helper.ended.is_set():
                raise WatchAudioFailure("failed", f"GPT-Live failed on the host ({helper.reason})")
            raise WatchAudioFailure("failed", "The WebRTC helper didn't make an offer")
        offer = helper.offer.result()
        request = self.request
        profile = self.profile
        try:
            answer = await asyncio.wait_for(_run_scoped(profile, lambda: create_gpt_live_session(
                offer, request.get("history"), limiter_key=_limiter_key(profile), voice=request.get("voice"),
                briefing=request.get("briefing"), greeting=request.get("greeting")), _gpt_live_executor),
                timeout=GPT_LIVE_REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise WatchAudioFailure("unreachable", "GPT-Live timed out starting the call")
        except TokenError as exc:
            raise _watch_audio_failure(exc)
        self.answer = answer
        await helper.write("A", answer["transport"]["sdp"].encode("utf-8"))
        await _first_done(WATCH_AUDIO_CONNECT_TIMEOUT_S, helper.connected.wait(), self.stopping.wait(),
                          helper.ended.wait())
        if helper.connected.is_set():
            return True
        if self.stopping.is_set():
            return False
        if helper.ended.is_set():
            raise WatchAudioFailure("failed", f"GPT-Live failed on the host ({helper.reason})")
        if retry:
            logger.info("GPT-Live for the Watch didn't connect with host candidates; trying STUN")
            return False
        raise WatchAudioFailure("unreachable", "GPT-Live's audio connection didn't come up from the Hermes host")

    async def _started(self, helper: _GptLiveHelper) -> None:
        answer = self.answer
        await self.bridge.control(self.stream, {
            "type": "started", "engine": self.engine, "voice": answer.get("voice"),
            "input_rate": GPT_LIVE_WATCH_INPUT_RATE, "output_rate": GPT_LIVE_WATCH_OUTPUT_RATE,
            "briefing_applied": bool(answer.get("briefing_applied")),
            "greeting_applied": bool(answer.get("greeting_applied")),
        })
        # What came while "started" was on its way, in order, then live.
        while self.held:
            kind, plain = self.held.pop(0)
            await self.send(kind, plain)
        self.live = True

    async def audio(self, pcm: bytes) -> None:
        helper = self.helper
        if helper is not None and self.live:
            await helper.write("P", pcm)

    async def event(self, data: bytes) -> None:
        helper = self.helper
        if helper is not None and self.live:
            await helper.write("E", data)


class _WatchAudioPacer:
    """Engine output toward the Watch, in order, with audio at real time.

    Grok streams a reply's audio faster than it plays. The Watch gets at most
    WATCH_AUDIO_LEAD_S ahead; events wait their turn behind the audio before
    them, so the Watch sees them when it would have live. When the user
    speaks over the reply, the audio not yet sent is dropped.
    """

    def __init__(self, send: Callable[[int, bytes], Any], rate: int,
                 clock: Callable[[], float] = time.monotonic, lead_s: Optional[float] = None) -> None:
        self.send = send
        self.rate = rate
        self.clock = clock
        self.lead_s = WATCH_AUDIO_LEAD_S if lead_s is None else lead_s
        self.items: deque = deque()
        self.queued_s = 0.0
        self.queued_event_bytes = 0
        self.playout: Optional[float] = None
        self.wake = asyncio.Event()

    def _seconds(self, pcm: bytes) -> float:
        return len(pcm) / 2 / self.rate

    def audio(self, pcm: bytes) -> None:
        step = max(2, int(self.rate * WATCH_AUDIO_CHUNK_S) * 2)
        for start in range(0, len(pcm) - len(pcm) % 2, step):
            piece = pcm[start:start + step]
            self.items.append((WATCH_AUDIO_AUDIO, piece))
            self.queued_s += self._seconds(piece)
        while self.queued_s > WATCH_AUDIO_MAX_QUEUED_S:
            self._drop_oldest_audio()
        self.wake.set()

    def _drop_oldest(self, wanted: int) -> None:
        for index, (kind, data) in enumerate(self.items):
            if kind == wanted:
                del self.items[index]
                self._forget(kind, data)
                return
        if wanted == WATCH_AUDIO_AUDIO:
            self.queued_s = 0.0
        else:
            self.queued_event_bytes = 0

    def _drop_oldest_audio(self) -> None:
        self._drop_oldest(WATCH_AUDIO_AUDIO)

    def _forget(self, kind: int, data: bytes) -> None:
        if kind == WATCH_AUDIO_AUDIO:
            self.queued_s -= self._seconds(data)
        else:
            self.queued_event_bytes -= len(data)

    def event(self, text: str) -> None:
        data = _watch_audio_event_text(text)
        self.items.append((WATCH_AUDIO_EVENT, data))
        self.queued_event_bytes += len(data)
        while self.queued_event_bytes > WATCH_AUDIO_MAX_QUEUED_EVENT_BYTES:
            self._drop_oldest(WATCH_AUDIO_EVENT)
        self.wake.set()

    def interrupt(self) -> None:
        self.items = deque(item for item in self.items if item[0] != WATCH_AUDIO_AUDIO)
        self.queued_s = 0.0
        self.playout = None
        self.wake.set()

    async def run(self) -> None:
        while True:
            if not self.items:
                self.wake.clear()
                await self.wake.wait()
                continue
            kind, data = self.items[0]
            if kind == WATCH_AUDIO_AUDIO:
                now = self.clock()
                if self.playout is None or self.playout < now:
                    self.playout = now
                wait = self.playout - now - self.lead_s
                if wait > 0:
                    self.wake.clear()
                    # New items or an interruption wake it early.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.wake.wait(), wait)
                    continue
                self.playout += self._seconds(data)
            self._forget(kind, data)
            self.items.popleft()
            if await self.send(kind, data) is False:
                return


class _GrokWatchSession(_WatchAudioSession):
    """Grok on this host's xAI sign-in, the way the phone's Grok relay connects."""

    engine = "grok"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.upstream: Any = None
        self.pacer: Optional[_WatchAudioPacer] = None

    async def run(self) -> None:
        profile = self.profile

        def setup() -> Tuple[str, str, str, str]:
            token, auth, model = _grok_live_socket_setup(profile)
            return token, auth, model, grok_live_model_voice()[1]

        try:
            token, auth, model, voice = await asyncio.wait_for(
                _run_scoped(profile, setup, _grok_live_executor), timeout=GROK_LIVE_SETUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise WatchAudioFailure("unreachable", "The Hermes host timed out reading its xAI sign-in")
        except TokenError as exc:
            raise _watch_audio_failure(exc)
        url = f"{GROK_LIVE_URL}?{urllib.parse.urlencode({'model': model})}"
        try:
            upstream = await _connect_xai(url, {"Authorization": f"Bearer {token}"})
        except GrokUpstreamRefused as exc:
            code, message = _grok_refusal(exc.status, auth)
            raise WatchAudioFailure("rate_limited" if code == GROK_CLOSE_RATE_LIMITED else
                                    "unreachable" if code == GROK_CLOSE_UNREACHABLE else "refused", message)
        except Exception as exc:
            # The type only: the text could quote the request.
            logger.warning("Grok Live for the Watch could not reach xAI: %s", type(exc).__name__)
            raise WatchAudioFailure("unreachable", "Could not reach xAI")
        pacer = self.pacer = _WatchAudioPacer(self.send, GROK_WATCH_RATE)
        tasks: list = []
        # Set first: the Watch answers "started" with its session.update.
        self.upstream = upstream
        try:
            # Before anything of xAI's reaches the Watch: it waits for this.
            await self.bridge.control(self.stream, {
                "type": "started", "engine": self.engine, "model": model, "voice": voice,
                "input_rate": GROK_WATCH_RATE, "output_rate": GROK_WATCH_RATE,
            })
            pacing = asyncio.ensure_future(pacer.run())
            reader = asyncio.ensure_future(self._read(upstream, pacer))
            tasks = [reader, pacing]
            await _first_done(None, self.stopping.wait(), reader)
            if reader.done() and not self.stopping.is_set():
                code, reason = _forwardable_close(getattr(upstream, "close_code", None),
                                                  str(getattr(upstream, "close_reason", "") or ""))
                self.reason = reason or ("xAI ended the call" if code in (1000, 1001) else "The connection to xAI was lost")
        finally:
            self.upstream = None
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await upstream.close()

    async def _read(self, upstream: Any, pacer: _WatchAudioPacer) -> None:
        with contextlib.suppress(Exception):
            async for message in upstream:
                if not isinstance(message, str):
                    continue
                try:
                    event = json.loads(message)
                except ValueError:
                    continue
                kind = event.get("type") if isinstance(event, dict) else None
                if kind in _GROK_AUDIO_DELTAS:
                    try:
                        pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
                    except (ValueError, TypeError):
                        continue
                    pacer.audio(pcm)
                    continue
                if kind == "input_audio_buffer.speech_started":
                    pacer.interrupt()
                pacer.event(message)

    async def audio(self, pcm: bytes) -> None:
        upstream = self.upstream
        if upstream is not None and pcm:
            with contextlib.suppress(Exception):
                await upstream.send(json.dumps({"type": "input_audio_buffer.append",
                                                "audio": base64.b64encode(pcm).decode("ascii")}))

    async def event(self, data: bytes) -> None:
        upstream = self.upstream
        if upstream is None or len(data) > GROK_LIVE_MAX_CLIENT_FRAME_BYTES:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        with contextlib.suppress(Exception):
            await upstream.send(text)


_WATCH_AUDIO_SESSIONS = {"gpt_live": _GptLiveWatchSession, "grok": _GrokWatchSession}


# --- The bridge -----------------------------------------------------------------


class WatchAudioRelayRefused(Exception):
    """The relay refused the host's audio socket; ``status`` is its HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _watch_audio_ws_url(relay_url: str, grant_id: str, side: str) -> str:
    base = "wss://" + relay_url[len("https://"):] if relay_url.startswith("https://") else \
        "ws://" + relay_url[len("http://"):]
    return f"{base}/v1/watch-audio/{grant_id}/{side}"


def _watch_audio_client_available() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


async def _connect_watch_audio_relay(url: str, credential: str) -> Any:
    """The host's socket to the relay (the `websockets` package Hermes ships)."""
    headers = {"Authorization": f"Bearer {credential}"}
    # Each client has its own refusal: InvalidStatus from the new one,
    # InvalidStatusCode from the legacy one.
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus as Refused
        kwargs: Dict[str, Any] = {"additional_headers": headers}

        def status_of(exc: Any) -> int:
            return int(getattr(exc.response, "status_code", 0) or 0)
    except ImportError:  # websockets < 13
        from websockets import connect  # type: ignore[no-redef]
        from websockets.exceptions import InvalidStatusCode as Refused  # type: ignore[no-redef]
        kwargs = {"extra_headers": headers}

        def status_of(exc: Any) -> int:
            return int(getattr(exc, "status_code", 0) or 0)
    try:
        return await connect(url, max_size=WATCH_AUDIO_MAX_MESSAGE_BYTES, open_timeout=WATCH_AUDIO_RELAY_TIMEOUT_S,
                             compression=None, **kwargs)
    except Refused as exc:
        raise WatchAudioRelayRefused(status_of(exc))


class _WatchAudioBridge:
    """A grant's audio: this host's socket to the relay, while the grant is open.

    It runs on a thread of its own with its own event loop, reconnecting
    after a drop. One engine session at a time: a new start, a new Watch
    connection or the Watch leaving ends the one before.
    """

    def __init__(self, grant: _WatchGrant, secret: bytes, *, runtime: Optional[_WatchAudioRuntime] = None,
                 connect: Optional[Callable[[str, str], Any]] = None,
                 slots: Optional[_WatchAudioSlots] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.grant = grant
        self.secret = secret
        self.runtime = runtime or _watch_audio_runtime
        self.connect = connect or _connect_watch_audio_relay
        self.slots = slots or _watch_audio_slots
        self.clock = clock
        self.stop_requested = threading.Event()
        self.finished = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.wake: Optional[asyncio.Event] = None
        self.send_lock: Optional[asyncio.Lock] = None
        self.socket: Any = None
        self.stream: Optional[_WatchAudioStream] = None
        self.session: Optional[_WatchAudioSession] = None
        self.session_task: Optional[asyncio.Task] = None
        self.sids: set = set()
        self.rejected = 0

    def watch_url(self) -> str:
        return _watch_audio_ws_url(self.grant.relay_url, self.grant.grant_id, "watch")

    def start(self) -> None:
        threading.Thread(target=self._run, name="conduit-watch-audio", daemon=True).start()

    def stop(self) -> None:
        self.stop_requested.set()
        loop, wake = self.loop, self.wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):  # its loop already closed
                loop.call_soon_threadsafe(wake.set)

    def _done(self) -> bool:
        return self.stop_requested.is_set() or self.grant.closed.is_set() or self.clock() >= self.grant.expires_at

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 — the grant's lookups go on without audio
            logger.warning("The Watch audio bridge stopped: %s", type(exc).__name__)
        finally:
            self.finished.set()

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()
        if self.stop_requested.is_set():
            return
        url = _watch_audio_ws_url(self.grant.relay_url, self.grant.grant_id, "host")
        backoff = 1.0
        while not self._done():
            try:
                socket = await self.connect(url, self.grant.credential)
            except WatchAudioRelayRefused as exc:
                if exc.status in (401, 403, 404, 410):
                    # Closed or expired on the relay, or not an audio grant there.
                    logger.info("The relay closed the Watch audio bridge (HTTP %s)", exc.status)
                    return
                logger.info("The relay refused the Watch audio bridge (HTTP %s); retrying in %ss", exc.status, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, WATCH_AUDIO_RECONNECT_MAX_S)
                continue
            except Exception as exc:  # noqa: BLE001 — retried while the grant is open
                logger.info("The Watch audio bridge couldn't reach the relay (%s); retrying in %ss",
                            type(exc).__name__, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, WATCH_AUDIO_RECONNECT_MAX_S)
                continue
            self.socket = socket
            opened = self.clock()
            try:
                await self._serve(socket)
            finally:
                self.socket = None
                self.stream = None
                await self._end_session("host_offline")
                with contextlib.suppress(Exception):
                    await socket.close()
            if getattr(socket, "close_code", None) == 4010:  # the relay's grant_closed
                return
            if self.clock() - opened >= WATCH_AUDIO_STABLE_S:
                backoff = 1.0
                await self._sleep(0.5)
            else:
                await self._sleep(backoff)
                backoff = min(backoff * 2, WATCH_AUDIO_RECONNECT_MAX_S)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.wake.wait(), seconds)

    async def _serve(self, socket: Any) -> None:
        receiver = asyncio.ensure_future(self._receive(socket))
        remaining = max(0.0, self.grant.expires_at - self.clock())
        done = await _first_done(remaining, receiver, self.wake.wait())
        if receiver not in done:
            receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await receiver
            except Exception as exc:  # noqa: BLE001 — a dropped socket; reconnected
                logger.info("The Watch audio bridge lost the relay: %s", type(exc).__name__)

    async def _receive(self, socket: Any) -> None:
        async for message in socket:
            if isinstance(message, str) or not message:
                continue
            await self._handle(bytes(message))

    async def _handle(self, message: bytes) -> None:
        first = message[0]
        if first == 0:
            if len(message) == 2 and message[1] in (WATCH_AUDIO_NOTICE_CONNECTED, WATCH_AUDIO_NOTICE_GONE):
                # The Watch connection changed: nothing more goes to the old stream.
                self.stream = None
                await self._end_session("watch_left" if message[1] == WATCH_AUDIO_NOTICE_GONE else "watch_rejoined")
            return
        if first == WATCH_AUDIO_HELLO:
            await self._hello(message)
            return
        stream = self.stream
        if stream is None:
            return
        try:
            kind, plain = stream.open(message)
        except WatchAudioError as exc:
            self.rejected += 1
            if self.rejected <= 3:
                logger.info("A Watch audio message didn't open: %s", exc)
            return
        session = self.session
        if kind == WATCH_AUDIO_CONTROL:
            await self._control(stream, plain)
        elif session is not None and session.stream is stream:
            if kind == WATCH_AUDIO_AUDIO:
                await session.audio(plain)
            else:
                await session.event(plain)

    async def _hello(self, message: bytes) -> None:
        if len(message) != 18:
            return
        sid, version = message[1:17], message[17]
        if sid in self.sids or len(self.sids) >= WATCH_AUDIO_STREAMS_PER_GRANT:
            # A stream id's keys are spent once; a Watch that sends one again gets nothing.
            logger.warning("A Watch audio stream was refused (%s)",
                           "its id came twice" if sid in self.sids else "too many streams for one call")
            return
        self.stream = None
        await self._end_session("watch_rejoined")
        self.sids.add(sid)
        stream = _WatchAudioStream(self.grant.grant_id, self.secret, sid)
        self.stream = stream
        if version != WATCH_AUDIO_VERSION:
            await self.control(stream, {"type": "error", "code": "version",
                                        "message": f"This Hermes host speaks Watch audio version {WATCH_AUDIO_VERSION}"})
            self.stream = None

    async def _control(self, stream: _WatchAudioStream, plain: bytes) -> None:
        try:
            request = json.loads(plain)
        except ValueError:
            return
        if not isinstance(request, dict):
            return
        kind = request.get("type")
        if kind == "end":
            await self._end_session("ended")
        elif kind == "start":
            await self._start(stream, request)

    async def _start(self, stream: _WatchAudioStream, request: Dict[str, Any]) -> None:
        await self._end_session("restarted")
        engine = request.get("engine")
        if engine not in WATCH_AUDIO_ENGINES:
            await self.control(stream, {"type": "error", "code": "bad_request",
                                        "message": f"engine must be one of {', '.join(WATCH_AUDIO_ENGINES)}"})
            return
        if not self.slots.acquire():
            await self.control(stream, {"type": "error", "code": "busy",
                                        "message": "Too many Watch calls are open on this Hermes host"})
            return
        session = _WATCH_AUDIO_SESSIONS[engine](self, stream, request)
        self.session = session
        self.session_task = asyncio.ensure_future(self._run_session(session))

    async def _run_session(self, session: _WatchAudioSession) -> None:
        try:
            await session.run()
        except WatchAudioFailure as exc:
            session.reason = "error"
            await self.control(session.stream, {"type": "error", "code": exc.code, "message": str(exc)})
        except asyncio.CancelledError:
            session.reason = session.reason if session.stopping.is_set() else "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 — told to the Watch by its type
            logger.warning("A Watch %s session failed: %s", session.engine, type(exc).__name__)
            session.reason = "error"
            await self.control(session.stream, {"type": "error", "code": "failed",
                                                "message": f"The Watch call failed on the host ({type(exc).__name__})"})
        finally:
            self.slots.release()
            if self.session is session:
                self.session = None
                self.session_task = None
            with contextlib.suppress(Exception):
                await self.control(session.stream, {"type": "ended", "engine": session.engine, "reason": session.reason})

    async def _end_session(self, reason: str) -> None:
        session, task = self.session, self.session_task
        if session is None or task is None:
            return
        self.session = None
        self.session_task = None
        session.stop(reason)
        try:
            await asyncio.wait_for(asyncio.shield(task), WATCH_AUDIO_STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # noqa: BLE001 — the session reported its own failure
            pass

    async def control(self, stream: _WatchAudioStream, payload: Dict[str, Any]) -> bool:
        return await self.send(stream, WATCH_AUDIO_CONTROL, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    async def send(self, stream: _WatchAudioStream, kind: int, plain: bytes) -> bool:
        """Seals and sends to the Watch on ``stream``; False once that stream is gone."""
        socket = self.socket
        if socket is None or stream is not self.stream or self.send_lock is None:
            return False
        async with self.send_lock:
            # Sealed under the lock, so counters reach the relay in order.
            if stream is not self.stream:
                return False
            try:
                message = stream.seal(kind, plain)
            except WatchAudioError:
                return True  # one message too large; the stream goes on
            try:
                await socket.send(message)
            except Exception:  # noqa: BLE001 — the socket dropped; the bridge reconnects
                return False
        return True


def watch_audio_status(runtime: Optional[_WatchAudioRuntime] = None) -> Dict[str, Any]:
    runtime = runtime or _watch_audio_runtime
    return {"version": WATCH_AUDIO_VERSION,
            "engines": {"gpt_live": runtime.status(), "grok": {"runtime": "ready", "source": "hermes", "reason": None}}}


def _watch_audio_grant_engines(runtime: Optional[_WatchAudioRuntime] = None) -> list:
    runtime = runtime or _watch_audio_runtime
    return [engine for engine in WATCH_AUDIO_ENGINES if engine != "gpt_live" or runtime.python() is not None]


@router.get("/watch-audio/status")
async def get_watch_audio_status(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await asyncio.get_running_loop().run_in_executor(None, watch_audio_status))}
    except Exception as exc:
        raise _unexpected("status", exc, feature="Watch audio")


@router.post("/watch-audio/prepare")
async def post_watch_audio_prepare(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        _watch_audio_prepare_limiter.acquire("")
        runtime = await asyncio.get_running_loop().run_in_executor(None, _watch_audio_runtime.prepare)
        return {"ok": True, "version": WATCH_AUDIO_VERSION, "engines": {"gpt_live": runtime}}
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise _unexpected("prepare", exc, feature="Watch audio")


_watch_audio_prepare_limiter = _MintLimiter(5, 300.0, message="Preparing GPT-Live for the Watch was asked too often; "
                                                               "try again in a few minutes")


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


def desktop_view_client(transport: Any) -> Optional[str]:
    """Returns "desktop" or "browser" for a browser engine's gateway socket, else None.

    Desktop's renderer and the web dashboard open /api/ws from a browser
    engine, whose User-Agent starts with Mozilla/ (Desktop's adds Electron/).
    Conduit's socket comes from URLSession and also names itself in
    X-Conduit-Client. Stdio, hosted rooms and this plugin's own job transport
    aren't WebSocket transports, so they never count. The User-Agent is the
    client's own claim: this is best-effort sorting among clients that already
    hold a dashboard login, not proof of which app is on the other end.
    """
    if transport is None or type(transport).__name__ != "WSTransport":
        return None
    headers = _transport_headers(transport)
    try:
        if headers is None or headers.get("x-conduit-client") is not None:
            return None
        agent = str(headers.get("user-agent") or "")
    except Exception:  # noqa: BLE001 — a header object we don't understand counts as no view
        return None
    if not agent.startswith("Mozilla/"):
        return None
    return "desktop" if "Electron/" in agent else "browser"


def _transport_headers(transport: Any) -> Any:
    """The WebSocket upgrade's headers (Starlette's, case-insensitive), or None."""
    try:
        headers = getattr(getattr(transport, "_ws", None), "headers", None)
    except Exception:  # noqa: BLE001
        return None
    return headers if callable(getattr(headers, "get", None)) else None


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


def _merge_desktop_view(current: Optional[Dict[str, Any]], newer: Dict[str, Any]) -> Dict[str, Any]:
    """Each time keeps its newest value; the client follows the newest open."""
    if current is None:
        return dict(newer)
    merged = dict(current)
    if "opened_at" in newer and newer["opened_at"] > merged.get("opened_at", 0.0):
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
    def _load(path: str) -> Dict[str, Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, RecursionError):  # RecursionError: absurdly nested JSON
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
                                "client": str(entry.get("client") or "desktop")}
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
        with _pairing_state_lock(Path(path)):
            merged = self._merged(self._load(path), views)
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
        self._thread: Optional[threading.Thread] = None
        self.reason: Optional[str] = "waiting"
        # Set when a gateway socket's upgrade headers can't be read, which
        # leaves every open unsortable: reported, not silently ignored.
        self.headers_unreadable = False

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
            time.sleep(DESKTOP_VIEWS_SWEEP_S)
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
                return
            self.reason = "waiting"
        self.try_install()

    def try_install(self) -> bool:
        with self._lock:
            if self.installed:
                return True
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
        if type(transport).__name__ == "WSTransport":
            readable = _transport_headers(transport) is not None
            if readable == self.headers_unreadable:
                logger.warning("Conduit: gateway socket headers %s",
                               "readable again" if readable else "unreadable; not marking Desktop's chats read")
            self.headers_unreadable = not readable
            if not readable:
                return
        client = desktop_view_client(transport)
        stored_id = str(result.get("session_key") or "").strip()
        if client is None or not stored_id:
            return
        session = server._sessions.get(str(result.get("session_id") or ""))
        home_of = getattr(server, "_session_home", None)
        if not isinstance(session, dict) or not callable(home_of):
            return  # whose profile it is is unknown: a guess could mark another profile's chat
        home = home_of(session)
        if home is None:
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

    def sweep(self, now: Optional[float] = None) -> None:
        """Ends the selection of connections that closed, at their last check."""
        now = time.time() if now is None else now
        with self._focus_lock:
            for key, focus in list(self._focus.items()):
                transport = focus["ref"]()
                if transport is None or _transport_closed(transport):
                    self.store.record(focus["home"], focus["stored_id"], focus["client"], focus["alive_at"])
                    del self._focus[key]
                else:
                    focus["alive_at"] = now

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
_desktop_views_limiter = _MintLimiter(60, 60.0, message="Too many Desktop view reads; try again shortly")
atexit.register(_desktop_view_store.flush)
if "hermes_cli.web_server" in sys.modules:
    # Loaded by the dashboard (not a test or the CLI): start watching for the
    # gateway module now rather than at Conduit's first read.
    _desktop_view_hook.ensure_started()


@router.get("/sessions/desktop-views")
async def get_desktop_views(response: Response, profile: Optional[str] = None,
                            since: Optional[float] = None) -> Dict[str, Any]:
    """Chats Desktop or the web dashboard opened in ``profile``: per stored id,
    the newest open and how long it then stayed selected (seconds since the
    epoch), optionally only those seen after ``since``."""
    response.headers["Cache-Control"] = "no-store"
    _desktop_view_hook.ensure_started()
    _desktop_view_hook.try_install_quietly()  # a half-loaded gateway never fails the read

    def read() -> Dict[str, Dict[str, Any]]:
        _desktop_views_limiter.acquire(_limiter_key(profile))
        from hermes_constants import get_hermes_home

        return _desktop_view_hook.read(get_hermes_home())

    try:
        views = await _run_scoped(profile, read)
    except TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise  # Hermes' own 400/404 for a bad or unknown profile
    except Exception as exc:
        raise _unexpected("read", exc, feature="Desktop views")
    if since is not None and math.isfinite(since):
        views = {stored_id: entry for stored_id, entry in views.items() if entry["seen_through"] > since}
    observing, reason = _desktop_view_hook.status()
    return {"ok": True, "observing": observing, "reason": reason, "views": views}


# --- Capabilities ------------------------------------------------------------
#
# Conduit reads this once per connection to tell which of its features this
# plugin serves, and nudges an update when one it uses is missing. A plugin
# without this route predates it, which Conduit reads as "update". Names are
# stable feature ids, one per route family; add one with every new route.

ROUTE_CAPABILITIES = (
    "gemini-live",
    "web-search",
    "memory",
    "personality",
    "gpt-live",
    "grok-live",
    "voice-sessions",
    "voice-tags",
    "voice-summary",
    "session-takeover",
    "e2e-notifications",
    "watch-tools",
    "watch-jobs",
    "watch-job-follow-ups",
    "watch-live-token",
    "watch-audio",
    "desktop-views",
)


def _plugin_version() -> Optional[str]:
    """The version in plugin.yaml beside this folder, or None if unreadable."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip() == "version":
                    return value.strip().strip("\"'") or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


@router.get("/capabilities")
async def get_capabilities(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True, "version": _plugin_version(), "capabilities": list(ROUTE_CAPABILITIES)}

