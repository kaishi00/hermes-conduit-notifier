"""Your own speech server: its settings, its test and its relay."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import stat
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import contextlib
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Response, WebSocket

from . import core, grok_live, local_live_read_back

logger = core.logger
router = core.router


# --- Your own speech server ---------------------------------------------------
#
# Conduit's Classic voice can listen through a speech server the user runs
# themselves (Hugging Face speech-to-speech, the one Hermes Live Voice's local
# mode starts, or any server speaking the OpenAI Realtime WebSocket dialect),
# so recognition never leaves their own machines. The server can sit on
# another box: an LXC, a GPU host, a Mac on the LAN.
#
# Conduit never reaches it directly. The socket below relays Conduit's
# realtime events to the configured URL and back, adding only an optional
# Authorization header, so the phone and the Watch keep talking to Hermes
# alone and the speech server needs no exposure beyond the Hermes host.
#
# Config (per profile): voice.speech_server: {url, api_key}. Plain ws:// is
# allowed only for loopback, private, link-local and Tailscale addresses or
# LAN-style names; anything public needs wss://.

SPEECH_SERVER_DEFAULT_PATH = "/v1/realtime"
# The server is on the user's own network, so a slow connect means it's
# down. Connect plus the test's wait stays under Conduit's 12 s request cap.
SPEECH_SERVER_CONNECT_TIMEOUT_S = 5.0
SPEECH_SERVER_SETUP_TIMEOUT_S = 10.0
# The test waits this long for the server's first event (session.created).
SPEECH_SERVER_TEST_FIRST_EVENT_S = 5.0
# Conduit streams audio for the whole utterance, so a quiet client is gone;
# closing it frees its open-relay slot.
SPEECH_SERVER_IDLE_S = 60.0
SPEECH_SERVER_LIMIT = 30
SPEECH_SERVER_WINDOW_S = 60.0
SPEECH_SERVER_MAX_OPEN_PER_PROFILE = 3
SPEECH_SERVER_MAX_OPEN = 8
SPEECH_SERVER_AUTH_FAILURE_LIMIT = 30
SPEECH_SERVER_MAX_CLIENT_FRAME_BYTES = 256 * 1024
SPEECH_SERVER_MAX_SERVER_FRAME_BYTES = 4 * 1024 * 1024
SPEECH_SERVER_CLOSE_NOT_CONFIGURED = 4503
SPEECH_SERVER_CLOSE_UNAUTHORIZED = 4401
SPEECH_SERVER_CLOSE_RATE_LIMITED = 4429
SPEECH_SERVER_CLOSE_REFUSED = 4400
SPEECH_SERVER_CLOSE_UNREACHABLE = 4502
SPEECH_SERVER_CLOSE_FAILED = 4500
SPEECH_SERVER_NOT_CONFIGURED = "No speech server is set for this profile (voice.speech_server.url)."
_SPEECH_SERVER_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".ts.net")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale addresses

_speech_server_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-speech-server")
_speech_server_limiter = core._MintLimiter(SPEECH_SERVER_LIMIT, SPEECH_SERVER_WINDOW_S,
                                      message="Too many speech server connections; try again shortly")
_speech_server_auth_failure_limiter = core._MintLimiter(SPEECH_SERVER_AUTH_FAILURE_LIMIT, SPEECH_SERVER_WINDOW_S,
                                                   message="Too many refused speech server connections")
_speech_server_open: Dict[str, int] = {}

_speech_server_open_lock = threading.Lock()


def _speech_server_take_open(key: str) -> bool:
    """A slot among the speech server connections open on this host, the
    phone's relays and Watch calls together (they run on several threads)."""
    with _speech_server_open_lock:
        if _speech_server_open.get(key, 0) >= SPEECH_SERVER_MAX_OPEN_PER_PROFILE or \
                sum(_speech_server_open.values()) >= SPEECH_SERVER_MAX_OPEN:
            return False
        _speech_server_open[key] = _speech_server_open.get(key, 0) + 1
        return True


def _speech_server_give_open(key: str) -> None:
    with _speech_server_open_lock:
        remaining = _speech_server_open.get(key, 1) - 1
        if remaining > 0:
            _speech_server_open[key] = remaining
        else:
            _speech_server_open.pop(key, None)


def _speech_server_settings() -> Dict[str, Any]:
    """The profile's ``voice.speech_server`` block (profile-scoped by _run_scoped), or {}."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except ImportError:
        return {}
    except Exception as exc:
        logger.warning("Speech server could not read the Hermes config: %s", type(exc).__name__)
        raise core.TokenError(503, f"The Hermes host could not read its config ({type(exc).__name__}).")
    voice = config.get("voice") if isinstance(config, dict) else None
    server = voice.get("speech_server") if isinstance(voice, dict) else None
    return server if isinstance(server, dict) else {}


def _speech_server_host_is_private(host: str) -> bool:
    """Loopback, private, link-local or Tailscale (CGNAT) address, or a LAN-style name.

    Names are judged by their shape, never resolved: this only decides whether
    plain ws:// is allowed, and the user picks the server anyway.
    """
    name = host.strip("[]").lower().rstrip(".")
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        try:
            # Shorthand IPv4 the resolver still dials, such as 134744072 or 0x08080808.
            address = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return name == "localhost" or "." not in name or name.endswith(_SPEECH_SERVER_LAN_SUFFIXES)
    if address.version == 4 and address in _CGNAT:
        return True
    return address.is_loopback or address.is_private or address.is_link_local


def speech_server_url(raw: Any) -> str:
    """The configured URL as the WebSocket URL to dial; TokenError(503) saying what's wrong.

    http(s) is accepted and turned into ws(s); a bare host gets the OpenAI
    Realtime path. Plain ws:// only reaches private hosts.
    """
    text = str(raw or "").strip()
    if not text:
        raise core.TokenError(503, SPEECH_SERVER_NOT_CONFIGURED)
    if "://" not in text:
        text = "ws://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise core.TokenError(503, "The speech server URL is not valid.")
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme.lower(), parts.scheme.lower())
    host = parts.hostname or ""
    if scheme not in ("ws", "wss") or not host or parts.username or parts.password or parts.fragment \
            or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in text):
        raise core.TokenError(503, "The speech server URL must look like ws://host:8765/v1/realtime.")
    if scheme == "ws" and not _speech_server_host_is_private(host):
        raise core.TokenError(503, f"{host} is a public address, so the speech server needs wss://.")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path if parts.path not in ("", "/") else SPEECH_SERVER_DEFAULT_PATH
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


SPEECH_SERVER_MAX_KEY_FILE_BYTES = 16 * 1024


def _speech_server_key(settings: Dict[str, Any]) -> str:
    """The key: ``api_key``, else the contents of ``api_key_file``.

    The file is read on every connection, so a key that rotates on disk (a
    Kubernetes projected service-account token, a sidecar-refreshed secret)
    is always current. A set file that can't be used (missing, not a regular
    file, empty, too large, not text) is an error, never "no key"; the reason
    stays in the log, so the status can't be used to probe the host's files.
    """
    key = str(settings.get("api_key") or "").strip()
    path = str(settings.get("api_key_file") or "").strip()
    if key or not path:
        return key
    try:
        key = _read_speech_server_key_file(os.path.expanduser(path))
    except (OSError, ValueError) as exc:
        logger.warning("The speech server key file can't be used: %s", exc if isinstance(exc, ValueError)
                       else type(exc).__name__)
        raise core.TokenError(503, "The speech server key file (voice.speech_server.api_key_file) can't be used.")
    return key


def _read_speech_server_key_file(path: str) -> str:
    # Non-blocking, so a FIFO or a device never holds an executor thread.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = handle.read(SPEECH_SERVER_MAX_KEY_FILE_BYTES + 1)
    if len(data) > SPEECH_SERVER_MAX_KEY_FILE_BYTES:
        raise ValueError("larger than 16 KiB")
    try:
        key = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise ValueError("not UTF-8 text") from None
    if not key:
        raise ValueError("empty")
    return key


def speech_server_target(settings: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(url, api_key) for this profile's speech server; TokenError(503) when unset or invalid."""
    if settings is None:
        settings = _speech_server_settings()
    url = speech_server_url(settings.get("url"))
    key = _speech_server_key(settings)
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        raise core.TokenError(503, "The speech server key contains a control character.")
    return url, key


def speech_server_status() -> Dict[str, Any]:
    """What's configured; whether the server answers is the test's job."""
    try:
        settings = _speech_server_settings()
    except core.TokenError as exc:
        return {"configured": False, "available": False, "url": None, "has_key": False, "reason": str(exc)}
    configured = bool(str(settings.get("url") or "").strip())
    has_key = bool(str(settings.get("api_key") or "").strip() or str(settings.get("api_key_file") or "").strip())
    try:
        url, _ = speech_server_target(settings)
    except core.TokenError as exc:
        return {"configured": configured, "available": False, "url": None, "has_key": has_key, "reason": str(exc)}
    return {"configured": True, "available": True, "url": url, "has_key": has_key, "reason": None}


async def _connect_speech_server(url: str, headers: Dict[str, str]) -> Any:
    """Opens the realtime socket to the user's speech server."""
    return await grok_live._connect_realtime(url, headers, SPEECH_SERVER_MAX_SERVER_FRAME_BYTES, SPEECH_SERVER_CONNECT_TIMEOUT_S)


def _speech_server_headers(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _speech_server_refusal(status: int) -> Tuple[int, str]:
    if status in (401, 403):
        return SPEECH_SERVER_CLOSE_REFUSED, f"The speech server refused the key (HTTP {status})"
    if 400 <= status < 500:
        return SPEECH_SERVER_CLOSE_REFUSED, f"The speech server refused the connection (HTTP {status})"
    return SPEECH_SERVER_CLOSE_UNREACHABLE, f"The speech server is unavailable (HTTP {status or 'error'})"


@router.get("/speech-server/status")
async def get_speech_server_status(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **(await asyncio.wait_for(
            core._run_scoped(profile, speech_server_status, _speech_server_executor),
            timeout=SPEECH_SERVER_SETUP_TIMEOUT_S))}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Speech server status timed out", headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Speech server")


def _speech_server_setup(profile: Optional[str]) -> Tuple[str, str]:
    """The target to dial, checked before a rate-limit slot is spent on it."""
    target = speech_server_target()
    _speech_server_limiter.acquire(core._limiter_key(profile))
    return target


@router.post("/speech-server/test")
async def post_speech_server_test(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """Dials the speech server from the Hermes host and waits for its first event.

    Always 200 with ``connected`` true or false, so Conduit can show the
    reason; ``latency_ms`` runs from dialing to that first event.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        url, key = await asyncio.wait_for(
            core._run_scoped(profile, lambda: _speech_server_setup(profile), _speech_server_executor),
            timeout=SPEECH_SERVER_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Speech server test timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        if exc.status == 429:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Cache-Control": "no-store"})
        return {"ok": True, "connected": False, "url": None, "latency_ms": None, "event": None, "reason": str(exc)}
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("test", exc, feature="Speech server")

    def failed(reason: str) -> Dict[str, Any]:
        return {"ok": True, "connected": False, "url": url, "latency_ms": None, "event": None, "reason": reason}

    started = time.monotonic()
    try:
        upstream = await _connect_speech_server(url, _speech_server_headers(key))
    except grok_live.RealtimeUpstreamRefused as exc:
        return failed(_speech_server_refusal(exc.status)[1])
    except Exception as exc:
        logger.warning("Speech server test could not connect: %s", type(exc).__name__)
        return failed(f"Could not reach the speech server from the Hermes host ({type(exc).__name__})")
    try:
        try:
            frame = await asyncio.wait_for(upstream.recv(), timeout=SPEECH_SERVER_TEST_FIRST_EVENT_S)
        except asyncio.TimeoutError:
            return failed("The speech server connected but sent nothing; is it an OpenAI Realtime server?")
        except Exception as exc:
            return failed(f"The speech server closed the connection ({type(exc).__name__})")
        latency_ms = int((time.monotonic() - started) * 1000)
        event = None
        with contextlib.suppress(Exception):
            parsed = json.loads(frame)
            if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                event = parsed["type"][:64]
        if event is None:
            return failed("The speech server answered with something other than OpenAI Realtime events.")
        if event == "error":
            return failed("The speech server answered with an error event.")
        return {"ok": True, "connected": True, "url": url, "latency_ms": latency_ms, "event": event, "reason": None}
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()


@router.websocket("/speech-server/socket")
async def speech_server_socket(ws: WebSocket) -> None:
    await _speech_server_socket(ws, lambda refuse, url, key, profile: _speech_server_relay(ws, refuse, url, key))


async def _speech_server_socket(ws: WebSocket, relay: Callable[..., Any]) -> None:
    """A relay socket to the profile's speech server: auth, settings, slot, then ``relay``."""
    authorized = await grok_live._ws_authorized(ws)
    if not authorized:
        try:
            _speech_server_auth_failure_limiter.acquire("")
        except core.TokenError:
            await ws.close(code=SPEECH_SERVER_CLOSE_UNAUTHORIZED)
            return
    await ws.accept()
    profile = (ws.query_params.get("profile") or "").strip() or None

    async def refuse(code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code=code, reason=grok_live._close_reason(reason))

    if not authorized:
        await refuse(SPEECH_SERVER_CLOSE_UNAUTHORIZED, "Dashboard sign-in required")
        return
    try:
        profile = await core._conduit_route_profile(ws, profile)
    except HTTPException as exc:
        # A 5xx is Hermes failing, which a retry may get past; the rest won't change.
        await refuse(SPEECH_SERVER_CLOSE_FAILED if exc.status_code >= 500 else SPEECH_SERVER_CLOSE_REFUSED, str(exc.detail))
        return
    try:
        url, key = await asyncio.wait_for(
            core._run_scoped(profile, lambda: _speech_server_setup(profile), _speech_server_executor),
            timeout=SPEECH_SERVER_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await refuse(SPEECH_SERVER_CLOSE_UNREACHABLE, "The Hermes host timed out reading the speech server settings")
        return
    except core.TokenError as exc:
        code = SPEECH_SERVER_CLOSE_RATE_LIMITED if exc.status == 429 else SPEECH_SERVER_CLOSE_NOT_CONFIGURED
        await refuse(code, str(exc))
        return
    except HTTPException as exc:
        await refuse(SPEECH_SERVER_CLOSE_REFUSED, str(exc.detail))
        return
    except Exception as exc:
        logger.warning("Speech server socket setup failed: %s", type(exc).__name__)
        await refuse(SPEECH_SERVER_CLOSE_FAILED, f"The speech server relay failed on the host ({type(exc).__name__})")
        return

    open_key = core._limiter_key(profile)
    if not _speech_server_take_open(open_key):
        await refuse(SPEECH_SERVER_CLOSE_RATE_LIMITED, "Too many speech server connections are open on this host")
        return
    try:
        await relay(refuse, url, key, profile)
    finally:
        _speech_server_give_open(open_key)


async def _speech_server_relay(ws: WebSocket, refuse: Callable[[int, str], Any], url: str, key: str,
                               intercept: Optional[Callable[[str], Any]] = None,
                               send_lock: Optional[asyncio.Lock] = None,
                               read_back: Optional["local_live_read_back._LocalLiveReadBack"] = None) -> None:
    """One utterance's relay, from dialing the speech server to forwarding its close.

    ``intercept`` sees each text frame from Conduit first and returns True for
    one meant for the plugin, which then isn't forwarded (Local Live's turn
    frames). ``send_lock`` is held for each frame sent to Conduit, when
    something else sends on the same socket. ``read_back`` runs a Local Live
    call's turns when the server turns out to only read text back: it sees
    the text frames both ways.
    """
    send_lock = send_lock or asyncio.Lock()
    try:
        upstream = await _connect_speech_server(url, _speech_server_headers(key))
    except grok_live.RealtimeUpstreamRefused as exc:
        logger.warning("The speech server refused the relay: HTTP %s", exc.status)
        await refuse(*_speech_server_refusal(exc.status))
        return
    except Exception as exc:
        logger.warning("Could not reach the speech server: %s", type(exc).__name__)
        await refuse(SPEECH_SERVER_CLOSE_UNREACHABLE, "Could not reach the speech server from the Hermes host")
        return
    if read_back is not None:
        read_back.attach(upstream.send, upstream.close)

    async def client_to_server() -> None:
        while True:
            message = await asyncio.wait_for(ws.receive(), timeout=SPEECH_SERVER_IDLE_S)
            if message.get("type") == "websocket.disconnect":
                return
            text, data = message.get("text"), message.get("bytes")
            size = len(text.encode("utf-8")) if text is not None else len(data or b"")
            if size > SPEECH_SERVER_MAX_CLIENT_FRAME_BYTES:
                raise ValueError("frame too large")
            if text is not None:
                if intercept is not None and await intercept(text):
                    continue
                if read_back is not None and await read_back.from_client(text):
                    continue
                await upstream.send(text)
            elif data is not None:
                await upstream.send(data)

    async def server_to_client() -> None:
        async for frame in upstream:
            if read_back is not None and isinstance(frame, str):
                frame = await read_back.from_server(frame)
                if frame is None:
                    continue
            async with send_lock:
                if isinstance(frame, str):
                    await ws.send_text(frame)
                else:
                    await ws.send_bytes(bytes(frame))

    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        ConnectionClosed = ()  # type: ignore[assignment,misc]

    outbound = asyncio.create_task(client_to_server())
    inbound = asyncio.create_task(server_to_client())
    try:
        done, _ = await asyncio.wait({outbound, inbound}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if read_back is not None:
            read_back.close()
        for task in (outbound, inbound):
            task.cancel()
        with contextlib.suppress(Exception):
            await upstream.close()
        await asyncio.gather(outbound, inbound, return_exceptions=True)
    outbound_failure = None if outbound.cancelled() else outbound.exception()
    inbound_failure = None if inbound.cancelled() else inbound.exception()
    if outbound in done and not isinstance(outbound_failure, ConnectionClosed):
        if isinstance(outbound_failure, ValueError):
            await refuse(1009, "Frame too large")
        elif isinstance(outbound_failure, asyncio.TimeoutError):
            await refuse(1000, "Idle for too long")
        return
    if inbound_failure is not None and not isinstance(inbound_failure, ConnectionClosed):
        logger.warning("Speech server relay failed: %s", type(inbound_failure).__name__)
    code = getattr(upstream, "close_code", None)
    reason = str(getattr(upstream, "close_reason", "") or "")
    if code is not None and 3000 <= code <= 4999:
        detail = f"{code}: {reason}" if reason else str(code)
        await refuse(SPEECH_SERVER_CLOSE_REFUSED, f"The speech server closed the connection ({detail})")
    elif code is None or code in (1005, 1006, 1015) or not 1000 <= code <= 1014:
        await refuse(SPEECH_SERVER_CLOSE_UNREACHABLE, "The connection to the speech server was lost")
    else:
        await refuse(code, reason)
