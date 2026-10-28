"""Gemini Live and Qwen Live tokens, and the web search their lookups use.

Gemini Live: Conduit talks to Gemini Live directly from the phone, but the
Gemini API key stays on this host. Conduit asks for a short-lived ephemeral
token per Live connection; the token is locked to one model, one use, and
must open its session within a minute.

Web search: Gemini Live's quick lookups can run on this host's own web
search backend (whatever `hermes tools` configured: SearXNG, Firecrawl,
Tavily…) instead of Google Search, which is metered separately.

Qwen Live: like Gemini Live, Conduit talks to Alibaba Cloud (DashScope)
directly from the phone or Watch, and the DASHSCOPE_API_KEY stays on this
host. Conduit asks for a short-lived temporary key per connection; a failed
mint never falls back to the permanent key or to Gemini.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core

logger = core.logger
router = core.router


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
        raise core.TokenError(502, f"Google rejected the token request ({exc.code}){': ' + detail if detail else ''}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # The reason can name proxies or hosts; keep it in the log, not the response.
        logger.warning("Gemini Live token request could not reach Google: %s", getattr(exc, "reason", exc))
        raise core.TokenError(502, "Could not reach Google")
    except ValueError:
        raise core.TokenError(502, "Google returned an unreadable token response")


def gemini_live_status(get_env: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    get_env = get_env or _env_value
    model = resolve_model(get_env)
    if resolve_api_key(get_env) is None:
        return {"available": False, "reason": "no_api_key", "model": model}
    return {"available": True, "model": model}


# Direct Live shares Conduit's phone/Watch call shell. Only credentials pass
# through this host; audio connects directly to Google or Alibaba Cloud.
QWEN_DEFAULT_MODEL = "qwen3.8-omni-flash-realtime"
QWEN_DEFAULT_VOICE = "Tina"


def qwen_live_settings(get_env: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, str]:
    get_env = get_env or _env_value
    region = str(get_env("CONDUIT_QWEN_LIVE_REGION") or "cn-beijing").strip()
    domains = {"cn-beijing": "dashscope.aliyuncs.com", "ap-southeast-1": "dashscope-intl.aliyuncs.com"}
    if region not in domains:
        raise core.TokenError(503, "Qwen Live region must be cn-beijing or ap-southeast-1")
    workspace = str(get_env("CONDUIT_QWEN_LIVE_WORKSPACE") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace):
        raise core.TokenError(503, "Set CONDUIT_QWEN_LIVE_WORKSPACE to the Alibaba Cloud workspace ID")
    model = str(get_env("CONDUIT_QWEN_LIVE_MODEL") or QWEN_DEFAULT_MODEL).strip()
    if not re.fullmatch(r"qwen[A-Za-z0-9._-]*realtime[A-Za-z0-9._-]*", model):
        raise core.TokenError(503, "Set a Qwen Realtime model in CONDUIT_QWEN_LIVE_MODEL")
    # Bounded like the others: it goes back to the device and on to Alibaba Cloud.
    voice = str(get_env("CONDUIT_QWEN_LIVE_VOICE") or "").strip() or QWEN_DEFAULT_VOICE
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", voice):
        raise core.TokenError(503, "Set a Qwen voice name (letters, digits, - or _) in CONDUIT_QWEN_LIVE_VOICE")
    return {"model": model, "token_url": f"https://{domains[region]}/api/v1/tokens?expire_in_seconds=120",
            "websocket_url": f"wss://{workspace}.{region}.maas.aliyuncs.com/api-ws/v1/realtime?"
                             + urllib.parse.urlencode({"model": model}),
            "voice": voice}


def qwen_live_status(get_env: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    get_env = get_env or _env_value
    try:
        settings = qwen_live_settings(get_env)
    except core.TokenError as exc:
        return {"provider": "qwen", "available": False, "reason": str(exc), "model": QWEN_DEFAULT_MODEL}
    if not str(get_env("DASHSCOPE_API_KEY") or "").strip():
        # The same reason code as /gemini-live/status; Conduit words it.
        return {"provider": "qwen", "available": False, "reason": "no_api_key", "model": settings["model"]}
    return {"provider": "qwen", "available": True, "model": settings["model"]}


def mint_qwen_live_token(get_env: Optional[Callable[[str], Optional[str]]] = None,
                         post: Optional[Callable[[urllib.request.Request], Any]] = None,
                         limiter_key: Optional[str] = None) -> Dict[str, Any]:
    """A DashScope temporary API key (POST /api/v1/tokens: top-level `token`,
    `expires_at` in Unix seconds) for one Qwen Live connection."""
    get_env = get_env or _env_value
    settings = qwen_live_settings(get_env)
    key = str(get_env("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise core.TokenError(503, "Set DASHSCOPE_API_KEY on the Hermes host")
    if limiter_key is not None:
        _qwen_mint_limiter.acquire(limiter_key)
    request = urllib.request.Request(settings["token_url"], data=b"", method="POST",
                                     headers={"Authorization": f"Bearer {key}"})
    try:
        if post is not None:
            result = post(request)
        else:
            with _opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
                result = json.loads(response.read(64 * 1024))
    except urllib.error.HTTPError as exc:
        raise core.TokenError(502, f"Alibaba Cloud rejected the temporary token request (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise core.TokenError(502, "Could not obtain a Qwen Live temporary token") from None
    token = result.get("token") if isinstance(result, dict) else None
    expiry = result.get("expires_at") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token.startswith("st-") or not isinstance(expiry, (int, float)) \
            or not math.isfinite(expiry) or not time.time() < expiry <= time.time() + 1800:
        raise core.TokenError(502, "Alibaba Cloud returned an invalid temporary token")
    # Never substitute the permanent key when minting is unsupported or fails.
    return {"provider": "qwen", "token": token, "expires_at": _timestamp(datetime.fromtimestamp(expiry, timezone.utc)),
            "model": settings["model"], "websocket_url": settings["websocket_url"], "voice": settings["voice"]}


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
        raise core.TokenError(400, f"query is longer than {SEARCH_MAX_QUERY_CHARS} characters")
    query = " ".join(raw_query.split())
    if not query:
        raise core.TokenError(400, "query is required")
    if len(query) > SEARCH_MAX_QUERY_CHARS:
        raise core.TokenError(400, f"query is longer than {SEARCH_MAX_QUERY_CHARS} characters")
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
        raise core.TokenError(503, "This Hermes version has no web search tool")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise core.TokenError(502, "The web search backend returned an unreadable response")
    if not isinstance(payload, dict) or not payload.get("success", False):
        error = payload.get("error") if isinstance(payload, dict) else None
        logger.warning("Web search for Conduit failed on the backend: %s", error)
        raise core.TokenError(502, _user_error(error))
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
        raise core.TokenError(503, "GEMINI_API_KEY is not set on this Hermes host")
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
        raise core.TokenError(502, "Google's token response had no token")
    return {
        "token": token,
        # Google may clamp the requested window; report what it granted.
        "expires_at": payload.get("expireTime") or body["expireTime"],
        "new_session_expires_at": payload.get("newSessionExpireTime") or body["newSessionExpireTime"],
        "model": model,
        "websocket_url": websocket_url(api_version),
    }


_mint_limiter = core._MintLimiter(MINT_LIMIT, MINT_WINDOW_S)
_qwen_mint_limiter = core._MintLimiter(MINT_LIMIT, MINT_WINDOW_S, message="Too many Qwen Live token requests; try again shortly")
_search_limiter = core._MintLimiter(SEARCH_LIMIT, SEARCH_WINDOW_S, message="Too many web searches; try again shortly")


_search_executor = ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="conduit-web-search")


@router.get("/gemini-live/status")
async def get_gemini_live_status(profile: Optional[str] = None) -> Dict[str, Any]:
    try:
        return {"ok": True, **(await core._run_scoped(profile, gemini_live_status))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except HTTPException:
        raise  # Hermes' own 400/404 for a bad or unknown profile
    except Exception as exc:
        raise core._unexpected("status", exc)


@router.post("/gemini-live/token")
async def create_gemini_live_token(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body is a usable credential: keep it out of any cache on the way.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        result = await core._run_scoped(profile, lambda: mint_gemini_live_token(limiter_key=core._limiter_key(profile)))
    except core.TokenError as exc:
        logger.warning("Gemini Live token request failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("token", exc)
    return {"ok": True, **result}


@router.get("/qwen-live/status")
async def get_qwen_live_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **await core._run_scoped(profile, qwen_live_status)}
    except core.TokenError as exc:
        raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        # Hermes' own 400/404 for a bad or unknown profile
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Qwen Live")


@router.post("/qwen-live/token")
async def create_qwen_live_token(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    # The body is a usable credential: keep it out of any cache on the way.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        result = await core._run_scoped(profile, lambda: mint_qwen_live_token(limiter_key=core._limiter_key(profile)))
    except core.TokenError as exc:
        logger.warning("Qwen Live token request failed: %s", exc)
        raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("token", exc, feature="Qwen Live")
    return {"ok": True, **result}


@router.get("/web-search/status")
async def get_web_search_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        # A quick local check (no network), so it stays off the search pool
        # and answers even while searches are backed up.
        return {"ok": True, **(await core._run_scoped(profile, web_search_status))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Web search")


@router.post("/web-search")
async def post_web_search(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request)
    query = body.get("query") if isinstance(body, dict) else None
    limit = body.get("limit", 3) if isinstance(body, dict) else 3
    try:
        result = await asyncio.wait_for(
            core._run_scoped(profile, lambda: run_web_search(query, limit, limiter_key=core._limiter_key(profile)), _search_executor),
            timeout=SEARCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Web search for Conduit timed out after %ss", SEARCH_TIMEOUT_S)
        raise HTTPException(status_code=504, detail="Web search timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        logger.warning("Web search for Conduit failed: %s", exc)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("request", exc, feature="Web search")
    return {"ok": True, **result}
