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
import os
import re
import threading
import time
import urllib.error
import urllib.parse
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
