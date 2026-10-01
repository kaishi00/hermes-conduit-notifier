import asyncio
import contextlib
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_grok_live", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


@pytest.fixture
def hermes(monkeypatch):
    """Stub Hermes: config, .env, xAI credential resolver, profile scope, dashboard WS auth."""
    state = types.SimpleNamespace(config={}, env={}, credentials={"provider": "xai-oauth", "api_key": "oauth-token"},
                                  resolver_error=None, ws_auth=True, ws_allowed=True, entered=[])

    def resolve():
        if state.resolver_error:
            raise state.resolver_error
        return state.credentials

    @contextlib.contextmanager
    def scope(profile):
        state.entered.append(profile)
        yield

    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: state.config
    config.get_env_value = lambda key: state.env.get(key)
    profiles = types.ModuleType("hermes_cli.web_server_profiles")
    profiles._config_profile_scope = scope
    chat = types.ModuleType("hermes_cli.web_server_chat")
    chat._ws_auth_ok = lambda ws: state.ws_auth
    chat._ws_request_is_allowed = lambda ws: state.ws_allowed
    xai_http = types.ModuleType("tools.xai_http")
    xai_http.resolve_xai_http_credentials = resolve
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", profiles)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", chat)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.xai_http", xai_http)
    monkeypatch.setattr(api, "_grok_live_limiter", api._MintLimiter(api.GROK_LIVE_LIMIT, api.GROK_LIVE_WINDOW_S))
    return state


class FakeUpstream:
    """xAI's side of the relay: records what Conduit sent, replays scripted frames, then closes."""

    def __init__(self, frames=(), close_code=1000, close_reason=""):
        self.sent = []
        self.frames = list(frames)
        self.close_code = close_code
        self.close_reason = close_reason
        self.closed = False
        self.release = asyncio.Event()

    async def send(self, frame):
        self.sent.append(frame)
        if frame == "finish":
            self.release.set()

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self.frames:
            yield frame
        await self.release.wait()

    async def close(self):
        self.closed = True


@pytest.fixture
def xai(monkeypatch):
    state = types.SimpleNamespace(connects=[], upstream=FakeUpstream(), error=None)

    async def connect(url, headers):
        state.connects.append((url, headers))
        if state.error:
            raise state.error
        return state.upstream

    monkeypatch.setattr(api, "_connect_xai", connect)
    return state


@pytest.fixture
def client(hermes, xai):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _wait_close(ws):
    """The close frame the relay sent, skipping any data frames before it."""
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return types.SimpleNamespace(code=message.get("code"), reason=message.get("reason") or "")


def _close_of(client, path=f"{BASE}/grok-live/socket"):
    with client.websocket_connect(path) as ws:
        return _wait_close(ws)


# --- status -----------------------------------------------------------------

def test_status_reports_subscription(client, hermes):
    body = client.get(f"{BASE}/grok-live/status?profile=work").json()
    assert body == {"ok": True, "available": True, "auth": "subscription", "reason": None,
                    "model": "grok-voice-latest", "voice": "eve", "transport": "relay"}
    assert hermes.entered == ["work"]


def test_status_reports_api_key_and_config(client, hermes):
    hermes.credentials = {"provider": "xai", "api_key": "xai-key"}
    hermes.config = {"voice": {"grok_live": {"model": "grok-voice-2", "voice": "Ara"}}}
    body = client.get(f"{BASE}/grok-live/status").json()
    assert (body["auth"], body["model"], body["voice"]) == ("api_key", "grok-voice-2", "Ara")


def test_status_model_env_override_and_bad_names_fall_back(client, hermes):
    hermes.env[api.GROK_LIVE_MODEL_ENV_VAR] = "grok-voice-beta"
    hermes.config = {"voice": {"grok_live": {"voice": "eve; drop"}}}
    body = client.get(f"{BASE}/grok-live/status").json()
    assert (body["model"], body["voice"]) == ("grok-voice-beta", "eve")


def test_status_without_credentials(client, hermes):
    hermes.credentials = {"provider": "xai-oauth", "api_key": ""}
    body = client.get(f"{BASE}/grok-live/status").json()
    assert body["available"] is False
    assert "SuperGrok" in body["reason"] and "XAI_API_KEY" in body["reason"]


def test_status_never_swaps_a_failed_sign_in_for_the_billed_key(client, hermes):
    hermes.resolver_error = RuntimeError("refresh failed")
    hermes.env["XAI_API_KEY"] = "env-key"
    body = client.get(f"{BASE}/grok-live/status").json()
    assert (body["available"], body["reason"]) == (False, api.GROK_LIVE_SIGN_IN_FAILED)
    assert "refresh failed" not in json.dumps(body)


def test_socket_refuses_when_the_sign_in_fails(client, hermes, xai):
    hermes.resolver_error = RuntimeError("refresh failed")
    hermes.env["XAI_API_KEY"] = "env-key"
    closed = _close_of(client)
    assert closed.code == api.GROK_CLOSE_NO_CREDENTIAL
    assert xai.connects == []


def test_socket_setup_that_hangs_closes_as_retryable(client, hermes, xai, monkeypatch):
    released = threading.Event()

    def stuck():
        released.wait(5)
        return hermes.credentials

    sys.modules["tools.xai_http"].resolve_xai_http_credentials = stuck
    monkeypatch.setattr(api, "GROK_LIVE_SETUP_TIMEOUT_S", 0.2)
    try:
        closed = _close_of(client)
    finally:
        released.set()
    assert closed.code == api.GROK_CLOSE_UNREACHABLE
    assert xai.connects == []


def test_status_has_its_own_workers():
    # A stuck socket setup holds the socket pool, never the readiness check.
    assert api._grok_live_status_executor is not api._grok_live_executor


def test_status_without_hermes_resolver_uses_env_key(client, hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.xai_http", None)
    assert client.get(f"{BASE}/grok-live/status").json()["available"] is False
    hermes.env["XAI_API_KEY"] = "env-key"
    assert client.get(f"{BASE}/grok-live/status").json()["auth"] == "api_key"


# --- relay ------------------------------------------------------------------

def test_socket_relays_both_ways_with_host_bearer(client, xai, hermes):
    xai.upstream = FakeUpstream(frames=['{"type":"session.created"}', b"\x01\x02"], close_code=1000)
    with client.websocket_connect(f"{BASE}/grok-live/socket?profile=work") as ws:
        assert ws.receive_text() == '{"type":"session.created"}'
        assert ws.receive_bytes() == b"\x01\x02"
        ws.send_text('{"type":"session.update"}')
        ws.send_bytes(b"\x03")
        ws.send_text("finish")
        closed = _wait_close(ws)
    assert closed.code == 1000
    url, headers = xai.connects[0]
    assert url == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert headers == {"Authorization": "Bearer oauth-token"}
    assert xai.upstream.sent == ['{"type":"session.update"}', b"\x03", "finish"]
    assert xai.upstream.closed
    assert hermes.entered == ["work"]


def test_socket_forwards_xai_close_reason(client, xai):
    xai.upstream = FakeUpstream(close_code=1008, close_reason="bad session")
    xai.upstream.release.set()
    closed = _close_of(client)
    assert (closed.code, closed.reason) == (1008, "bad session")


def test_socket_maps_a_dropped_xai_connection_to_retryable(client, xai):
    xai.upstream = FakeUpstream(close_code=1006)
    xai.upstream.release.set()
    assert _close_of(client).code == api.GROK_CLOSE_UNREACHABLE


def test_socket_refuses_without_dashboard_auth(client, hermes, xai):
    hermes.ws_auth = False
    # Accepted first, so Conduit reads 4401 rather than a bare HTTP 403.
    assert _close_of(client).code == api.GROK_CLOSE_UNAUTHORIZED
    assert xai.connects == []
    assert hermes.entered == [], "no host work before auth"


def test_socket_refuses_a_disallowed_origin(client, hermes, xai):
    hermes.ws_allowed = False
    assert _close_of(client).code == api.GROK_CLOSE_UNAUTHORIZED
    assert xai.connects == []


def test_socket_fails_closed_without_hermes_ws_auth(client, xai, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", None)
    assert _close_of(client).code == api.GROK_CLOSE_UNAUTHORIZED
    assert xai.connects == []


def test_socket_without_credentials_says_why(client, hermes, xai):
    hermes.credentials = {"provider": "xai-oauth", "api_key": ""}
    closed = _close_of(client)
    assert closed.code == api.GROK_CLOSE_NO_CREDENTIAL
    assert "SuperGrok" in closed.reason
    assert len(closed.reason.encode()) <= 123
    assert xai.connects == []


@pytest.mark.parametrize("status,code,text", [
    (403, api.GROK_CLOSE_REFUSED, "xAI refused the SuperGrok sign-in (HTTP 403)"),
    (429, api.GROK_CLOSE_RATE_LIMITED, "xAI is rate limiting voice (HTTP 429)"),
    (404, api.GROK_CLOSE_REFUSED, "xAI refused the call (HTTP 404)"),
    (503, api.GROK_CLOSE_UNREACHABLE, "xAI voice is unavailable (HTTP 503)"),
])
def test_socket_reports_xai_refusal(client, xai, status, code, text):
    xai.error = api.GrokUpstreamRefused(status)
    closed = _close_of(client)
    assert (closed.code, closed.reason) == (code, text)


def test_socket_names_the_api_key_when_it_was_refused(client, hermes, xai):
    hermes.credentials = {"provider": "xai", "api_key": "k"}
    xai.error = api.GrokUpstreamRefused(401)
    assert _close_of(client).reason == "xAI refused XAI_API_KEY (HTTP 401)"


def test_socket_unreachable_xai_never_echoes_the_error(client, xai):
    xai.error = OSError("connect to wss://api.x.ai?token=secret failed")
    closed = _close_of(client)
    assert (closed.code, closed.reason) == (api.GROK_CLOSE_UNREACHABLE, "Could not reach xAI")


def test_socket_rate_limits_per_profile(client, xai, monkeypatch):
    monkeypatch.setattr(api, "_grok_live_limiter", api._MintLimiter(1, 60))
    xai.upstream.release.set()
    _close_of(client)
    assert _close_of(client).code == api.GROK_CLOSE_RATE_LIMITED
    assert len(xai.connects) == 1


def test_socket_refuses_oversized_frames(client, xai):
    with client.websocket_connect(f"{BASE}/grok-live/socket") as ws:
        ws.send_text("x" * (api.GROK_LIVE_MAX_CLIENT_FRAME_BYTES + 1))
        closed = _wait_close(ws)
    assert closed.code == 1009
    assert xai.upstream.sent == []


def test_session_update_text_is_never_rewritten(client, xai):
    update = json.dumps({"type": "session.update", "session": {"instructions": "é" * 1000}})
    with client.websocket_connect(f"{BASE}/grok-live/socket") as ws:
        ws.send_text(update)
        ws.send_text("finish")
        _wait_close(ws)
    assert xai.upstream.sent[0] == update
