import asyncio
import contextlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_speech_server", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


@pytest.fixture
def hermes(monkeypatch):
    """Stub Hermes: config, profile scope, dashboard WS auth."""
    state = types.SimpleNamespace(config={"voice": {"speech_server": {"url": "ws://127.0.0.1:8765/v1/realtime"}}},
                                  ws_auth=True, ws_allowed=True, entered=[])

    @contextlib.contextmanager
    def scope(profile):
        state.entered.append(profile)
        yield

    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: state.config
    config.get_env_value = lambda key: None
    profiles = types.ModuleType("hermes_cli.web_server_profiles")
    profiles._config_profile_scope = scope
    chat = types.ModuleType("hermes_cli.web_server_chat")
    chat._ws_auth_ok = lambda ws: state.ws_auth
    chat._ws_request_is_allowed = lambda ws: state.ws_allowed
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", profiles)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", chat)
    monkeypatch.setattr(api, "_speech_server_limiter",
                        api._MintLimiter(api.SPEECH_SERVER_LIMIT, api.SPEECH_SERVER_WINDOW_S))
    monkeypatch.setattr(api, "_speech_server_auth_failure_limiter",
                        api._MintLimiter(api.SPEECH_SERVER_AUTH_FAILURE_LIMIT, api.SPEECH_SERVER_WINDOW_S))
    return state


def _settings(hermes, **values):
    hermes.config = {"voice": {"speech_server": values}}


class FakeUpstream:
    """The speech server's side: records what Conduit sent, replays scripted frames, then closes."""

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

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3600)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self.frames:
            yield frame
        await self.release.wait()

    async def close(self):
        self.closed = True


@pytest.fixture
def server(monkeypatch):
    state = types.SimpleNamespace(connects=[], upstream=FakeUpstream(), error=None)

    async def connect(url, headers):
        state.connects.append((url, headers))
        if state.error:
            raise state.error
        return state.upstream

    monkeypatch.setattr(api, "_connect_speech_server", connect)
    return state


@pytest.fixture
def client(hermes, server):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _wait_close(ws):
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return types.SimpleNamespace(code=message.get("code"), reason=message.get("reason") or "")


# --- URL policy ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("ws://127.0.0.1:8765/v1/realtime", "ws://127.0.0.1:8765/v1/realtime"),
    ("http://192.168.1.20:8765", "ws://192.168.1.20:8765/v1/realtime"),
    ("10.0.0.5:8765", "ws://10.0.0.5:8765/v1/realtime"),
    ("ws://speech:8765/", "ws://speech:8765/v1/realtime"),
    ("ws://voice.lan:8765/realtime", "ws://voice.lan:8765/realtime"),
    ("ws://gpu-box.tailnet.ts.net:8765", "ws://gpu-box.tailnet.ts.net:8765/v1/realtime"),
    ("ws://100.101.102.103:8765", "ws://100.101.102.103:8765/v1/realtime"),
    ("ws://[fd00::5]:8765", "ws://[fd00::5]:8765/v1/realtime"),
    ("https://voice.example.com", "wss://voice.example.com/v1/realtime"),
    ("wss://voice.example.com:443/v1/realtime?model=x", "wss://voice.example.com:443/v1/realtime?model=x"),
])
def test_url_policy_accepts(raw, expected):
    assert api.speech_server_url(raw) == expected


@pytest.mark.parametrize("raw,fragment", [
    ("", "No speech server is set"),
    ("ws://voice.example.com:8765", "needs wss://"),
    ("ws://8.8.8.8:8765", "needs wss://"),
    ("ws://134744072:8765", "needs wss://"),
    ("ws://0x08080808:8765", "needs wss://"),
    ("ftp://10.0.0.5", "must look like"),
    ("ws://user:pw@10.0.0.5:8765", "must look like"),
    ("ws://10.0.0.5:99999", "not valid"),
    ("ws://10.0.0.5/a b", "must look like"),
    ("ws://10.0.0.5/a\tb", "must look like"),
    ("ws://10.0.0.5/a\x00b", "must look like"),
    ("ws://10.0.0.5/a\x7fb", "must look like"),
])
def test_url_policy_refuses(raw, fragment):
    with pytest.raises(api.TokenError) as caught:
        api.speech_server_url(raw)
    assert fragment in str(caught.value)
    assert caught.value.status == 503


# --- status -------------------------------------------------------------------

def test_status_reports_configured_server(client, hermes):
    _settings(hermes, url="http://10.0.0.5:8765", api_key="secret")
    body = client.get(f"{BASE}/speech-server/status?profile=work").json()
    assert body == {"ok": True, "configured": True, "available": True,
                    "url": "ws://10.0.0.5:8765/v1/realtime", "has_key": True, "reason": None}
    assert hermes.entered == ["work"]


def test_status_without_a_server(client, hermes):
    hermes.config = {}
    body = client.get(f"{BASE}/speech-server/status").json()
    assert body["configured"] is False and body["available"] is False
    assert "No speech server is set" in body["reason"]


def test_status_explains_an_invalid_url(client, hermes):
    _settings(hermes, url="ws://voice.example.com")
    body = client.get(f"{BASE}/speech-server/status").json()
    assert body["configured"] is True and body["available"] is False and body["url"] is None
    assert "needs wss://" in body["reason"]


def test_status_says_when_the_config_cannot_be_read(client, hermes):
    def broken():
        raise PermissionError("config.yaml")
    sys.modules["hermes_cli.config"].load_config = broken
    body = client.get(f"{BASE}/speech-server/status").json()
    assert body["available"] is False
    assert "could not read its config (PermissionError)" in body["reason"]


def test_key_with_a_control_character_is_refused(hermes):
    _settings(hermes, url="ws://10.0.0.5:8765", api_key="sec\x00ret")
    with pytest.raises(api.TokenError) as caught:
        api.speech_server_target()
    assert "control character" in str(caught.value)


# --- test ---------------------------------------------------------------------

def test_test_reports_the_first_event_and_latency(client, server):
    server.upstream = FakeUpstream(frames=['{"type":"session.created","session":{}}'])
    body = client.post(f"{BASE}/speech-server/test").json()
    assert body["connected"] is True and body["event"] == "session.created"
    assert isinstance(body["latency_ms"], int) and body["reason"] is None
    assert server.connects == [("ws://127.0.0.1:8765/v1/realtime", {})]
    assert server.upstream.closed


def test_test_sends_the_key_as_a_bearer(client, hermes, server):
    _settings(hermes, url="ws://10.0.0.5:8765", api_key="secret")
    server.upstream = FakeUpstream(frames=['{"type":"session.created"}'])
    client.post(f"{BASE}/speech-server/test")
    assert server.connects[0][1] == {"Authorization": "Bearer secret"}


def test_test_without_a_server_says_why(client, hermes, server):
    hermes.config = {}
    body = client.post(f"{BASE}/speech-server/test").json()
    assert body["connected"] is False and "No speech server is set" in body["reason"]
    assert server.connects == []


def test_an_invalid_url_does_not_spend_a_rate_limit_slot(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_speech_server_limiter", api._MintLimiter(1, api.SPEECH_SERVER_WINDOW_S))
    _settings(hermes, url="ws://voice.example.com")
    for _ in range(3):
        assert "needs wss://" in client.post(f"{BASE}/speech-server/test").json()["reason"]
    _settings(hermes, url="ws://10.0.0.5:8765")
    assert client.post(f"{BASE}/speech-server/test").status_code == 200
    assert client.post(f"{BASE}/speech-server/test").status_code == 429


def test_test_reports_an_unreachable_server_without_echoing_the_error(client, server):
    server.error = OSError("connect to 127.0.0.1 secret detail")
    body = client.post(f"{BASE}/speech-server/test").json()
    assert body["connected"] is False
    assert "Could not reach the speech server" in body["reason"] and "secret" not in body["reason"]


def test_test_reports_a_refused_key(client, server):
    server.error = api.RealtimeUpstreamRefused(401)
    body = client.post(f"{BASE}/speech-server/test").json()
    assert body["connected"] is False and "refused the key" in body["reason"]


def test_test_refuses_a_server_that_is_not_realtime(client, server):
    server.upstream = FakeUpstream(frames=["hello"])
    body = client.post(f"{BASE}/speech-server/test").json()
    assert body["connected"] is False and "OpenAI Realtime" in body["reason"]


def test_test_times_out_on_a_silent_server(client, server, monkeypatch):
    monkeypatch.setattr(api, "SPEECH_SERVER_TEST_FIRST_EVENT_S", 0.05)
    body = client.post(f"{BASE}/speech-server/test").json()
    assert body["connected"] is False and "sent nothing" in body["reason"]
    assert server.upstream.closed


# --- socket -------------------------------------------------------------------

def test_socket_relays_both_ways(client, server):
    server.upstream = FakeUpstream(frames=['{"type":"session.created"}'])
    with client.websocket_connect(f"{BASE}/speech-server/socket?profile=work") as ws:
        assert ws.receive_text() == '{"type":"session.created"}'
        ws.send_text('{"type":"session.update"}')
        ws.send_text("finish")
        close = _wait_close(ws)
    assert server.upstream.sent == ['{"type":"session.update"}', "finish"]
    assert close.code == 1000
    assert server.connects == [("ws://127.0.0.1:8765/v1/realtime", {})]


def test_socket_without_a_server_closes_with_the_reason(client, hermes, server):
    hermes.config = {}
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_NOT_CONFIGURED
    assert "No speech server is set" in close.reason
    assert server.connects == []


def test_socket_refuses_without_dashboard_auth(client, hermes, server):
    hermes.ws_auth = False
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_UNAUTHORIZED
    assert server.connects == []


def test_socket_maps_an_unreachable_server_to_retryable(client, server):
    server.error = OSError("boom")
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_UNREACHABLE


def test_socket_maps_a_dropped_server_to_retryable(client, server):
    server.upstream = FakeUpstream(close_code=1006)
    server.upstream.release.set()
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_UNREACHABLE


def test_socket_keeps_the_servers_own_close_reason(client, server):
    server.upstream = FakeUpstream(close_code=4000, close_reason="model not loaded")
    server.upstream.release.set()
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_REFUSED
    assert close.reason == "The speech server closed the connection (4000: model not loaded)"


def test_socket_sends_the_key_as_a_bearer(client, hermes, server):
    _settings(hermes, url="ws://10.0.0.5:8765", api_key="secret")
    server.upstream.release.set()
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        _wait_close(ws)
    assert server.connects == [("ws://10.0.0.5:8765/v1/realtime", {"Authorization": "Bearer secret"})]


def test_socket_closes_an_idle_client(client, server, monkeypatch):
    monkeypatch.setattr(api, "SPEECH_SERVER_IDLE_S", 0.05)
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == 1000 and close.reason == "Idle for too long"
    assert server.upstream.closed


def test_socket_caps_open_relays_on_the_host(client, server, monkeypatch):
    monkeypatch.setattr(api, "_speech_server_open", {"a": 3, "b": 3, "c": 2})
    with client.websocket_connect(f"{BASE}/speech-server/socket?profile=work") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_RATE_LIMITED
    assert server.connects == []


def test_socket_rate_limit_closes_with_4429(client, server, monkeypatch):
    monkeypatch.setattr(api, "_speech_server_limiter", api._MintLimiter(1, api.SPEECH_SERVER_WINDOW_S))
    server.upstream.release.set()
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        _wait_close(ws)
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_RATE_LIMITED


def test_socket_refuses_oversized_frames(client, server):
    with client.websocket_connect(f"{BASE}/speech-server/socket") as ws:
        ws.send_text("x" * (api.SPEECH_SERVER_MAX_CLIENT_FRAME_BYTES + 1))
        close = _wait_close(ws)
    assert close.code == 1009


def test_socket_caps_open_relays_per_profile(client, server, monkeypatch):
    monkeypatch.setattr(api, "_speech_server_open", {api._limiter_key("work"): api.SPEECH_SERVER_MAX_OPEN_PER_PROFILE})
    with client.websocket_connect(f"{BASE}/speech-server/socket?profile=work") as ws:
        close = _wait_close(ws)
    assert close.code == api.SPEECH_SERVER_CLOSE_RATE_LIMITED
    assert server.connects == []


def test_capability_is_advertised():
    assert "speech-server" in api.ROUTE_CAPABILITIES
