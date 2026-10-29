"""Grok Live: Conduit's realtime socket relayed to xAI.

Grok Live: relays Conduit's realtime socket to xAI, adding this host's
SuperGrok sign-in (or XAI_API_KEY) on the way. The bearer stays on the host.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import contextlib
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Response, WebSocket

from . import core, gemini_qwen_live

logger = core.logger
router = core.router


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
# Conduit sends audio throughout the call; release a quiet client's slot.
GROK_LIVE_IDLE_S = 60.0
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
_grok_live_limiter = core._MintLimiter(GROK_LIVE_LIMIT, GROK_LIVE_WINDOW_S,
                                  message="Too many Grok Live connections; try again shortly")
_grok_live_host_limiter = core._MintLimiter(GROK_LIVE_HOST_LIMIT, GROK_LIVE_WINDOW_S,
                                       message="Too many Grok Live connections on this host; try again shortly")
# Refused upgrades on the whole host: past this, they're turned away before
# the handshake completes (a bare 403) instead of with a 4401 close.
GROK_LIVE_AUTH_FAILURE_LIMIT = 30
_grok_live_auth_failure_limiter = core._MintLimiter(GROK_LIVE_AUTH_FAILURE_LIMIT, GROK_LIVE_WINDOW_S,
                                               message="Too many refused Grok Live connections")
_grok_live_status_limiter = core._MintLimiter(GROK_LIVE_STATUS_LIMIT, GROK_LIVE_WINDOW_S,
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
    model = _grok_live_name(gemini_qwen_live._env_value(GROK_LIVE_MODEL_ENV_VAR) or live.get("model"), GROK_LIVE_DEFAULT_MODEL)
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
            raise core.TokenError(503, GROK_LIVE_SIGN_IN_FAILED)
        resolve_xai_http_credentials = None
    if resolve_xai_http_credentials is None:
        token, auth = str(gemini_qwen_live._env_value("XAI_API_KEY") or "").strip(), "api_key"
    else:
        try:
            credentials = resolve_xai_http_credentials()
        except Exception as exc:
            # A failed refresh, say: the type only, since the text could carry detail.
            logger.warning("Grok Live could not read the xAI sign-in: %s", type(exc).__name__)
            raise core.TokenError(503, GROK_LIVE_SIGN_IN_FAILED)
        if credentials is not None and not isinstance(credentials, dict):
            # A resolver this plugin doesn't understand: refusing beats
            # guessing past a SuperGrok sign-in to the billed key.
            logger.warning("Grok Live does not recognize the xAI sign-in (%s)", type(credentials).__name__)
            raise core.TokenError(503, GROK_LIVE_SIGN_IN_FAILED)
        if isinstance(credentials, dict):
            token = str(credentials.get("api_key") or "").strip()
            auth = "subscription" if credentials.get("provider") == "xai-oauth" else "api_key"
    if not token or any(c in token for c in "\r\n"):
        raise core.TokenError(503, GROK_LIVE_NO_CREDENTIAL)
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
    except core.TokenError as exc:
        return {**status, "available": False, "auth": None, "reason": str(exc)}
    return {**status, "available": True, "auth": auth, "reason": None}


@router.get("/grok-live/status")
async def get_grok_live_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await asyncio.wait_for(
            core._run_scoped(profile, grok_live_status, _grok_live_status_executor), timeout=GROK_LIVE_SETUP_TIMEOUT_S))}
    except asyncio.TimeoutError:
        logger.warning("Grok Live status for Conduit timed out")
        raise HTTPException(status_code=504, detail="Grok Live status timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Grok Live")


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


class RealtimeUpstreamRefused(Exception):
    """A realtime upstream (xAI, the user's speech server) refused the WebSocket
    upgrade; ``status`` is its HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


GrokUpstreamRefused = RealtimeUpstreamRefused


async def _connect_xai(url: str, headers: Dict[str, str]) -> Any:
    """Opens the realtime socket to xAI."""
    return await _connect_realtime(url, headers, GROK_LIVE_MAX_SERVER_FRAME_BYTES, GROK_LIVE_CONNECT_TIMEOUT_S)


async def _connect_realtime(url: str, headers: Dict[str, str], max_size: int, open_timeout: float) -> Any:
    """Opens a realtime WebSocket with the `websockets` package Hermes ships;
    RealtimeUpstreamRefused when the server answers the upgrade with an HTTP error."""
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
        return await connect(url, max_size=max_size, open_timeout=open_timeout, **kwargs)
    except Exception as exc:
        if InvalidStatus is not None and isinstance(exc, InvalidStatus):
            raise RealtimeUpstreamRefused(int(getattr(exc.response, "status_code", 0) or 0))
        if InvalidStatusCode is not None and isinstance(exc, InvalidStatusCode):
            raise RealtimeUpstreamRefused(int(getattr(exc, "status_code", 0) or 0))
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
    _grok_live_limiter.acquire(core._limiter_key(profile))
    token, auth = grok_live_credentials()
    return token, auth, grok_live_model_voice()[0]


@router.websocket("/grok-live/socket")
async def grok_live_socket(ws: WebSocket) -> None:
    authorized = await _ws_authorized(ws)
    if not authorized:
        try:
            _grok_live_auth_failure_limiter.acquire("")
        except core.TokenError:
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
        profile = await core._conduit_route_profile(ws, profile)
    except HTTPException as exc:
        # A 5xx is Hermes failing, which a retry may get past; the rest won't change.
        await refuse(GROK_CLOSE_FAILED if exc.status_code >= 500 else GROK_CLOSE_REFUSED, str(exc.detail))
        return
    try:
        token, auth, model = await asyncio.wait_for(
            core._run_scoped(profile, lambda: _grok_live_socket_setup(profile), _grok_live_executor),
            timeout=GROK_LIVE_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Grok Live socket setup for Conduit timed out")
        await refuse(GROK_CLOSE_UNREACHABLE, "The Hermes host timed out reading its xAI sign-in")
        return
    except core.TokenError as exc:
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

    key = core._limiter_key(profile)
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
    except RealtimeUpstreamRefused as exc:
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
            message = await asyncio.wait_for(ws.receive(), timeout=GROK_LIVE_IDLE_S)
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
        elif isinstance(outbound_failure, asyncio.TimeoutError):
            await refuse(1000, "Idle for too long")
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
