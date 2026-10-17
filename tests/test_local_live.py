import abc
import asyncio
import contextlib
import dataclasses
import importlib.util
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_local_live", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """Stub Hermes: config, per-profile homes, dashboard WS auth, the auth seam."""
    state = types.SimpleNamespace(config={"voice": {"speech_server": {"url": "ws://127.0.0.1:8765/v1/realtime"}}},
                                  ws_auth=True, ws_allowed=True, profile=None, providers={}, token_routes=set())
    homes = {}

    def home():
        name = state.profile or "default"
        return homes.setdefault(name, tmp_path / name)

    @contextlib.contextmanager
    def scope(profile):
        previous, state.profile = state.profile, profile
        try:
            yield
        finally:
            state.profile = previous

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = home
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: state.config
    config.get_env_value = lambda key: None
    profiles = types.ModuleType("hermes_cli.web_server_profiles")
    profiles._config_profile_scope = scope
    chat = types.ModuleType("hermes_cli.web_server_chat")
    chat._ws_auth_ok = lambda ws: state.ws_auth
    chat._ws_request_is_allowed = lambda ws: state.ws_allowed

    base = types.ModuleType("hermes_cli.dashboard_auth.base")

    class DashboardAuthProvider(abc.ABC):
        name = ""
        display_name = ""
        supports_token = False

        @abc.abstractmethod
        def start_login(self, *, redirect_uri): ...

        @abc.abstractmethod
        def complete_login(self, **kwargs): ...

        @abc.abstractmethod
        def verify_session(self, **kwargs): ...

    @dataclasses.dataclass(frozen=True)
    class TokenPrincipal:
        principal: str
        provider: str
        scopes: tuple = ()

    base.DashboardAuthProvider = DashboardAuthProvider
    base.TokenPrincipal = TokenPrincipal
    registry = types.ModuleType("hermes_cli.dashboard_auth.registry")
    registry.get_provider = lambda name: state.providers.get(name)
    registry.list_providers = lambda: list(state.providers.values())

    def register_provider(provider):
        if provider.name in state.providers:
            raise ValueError("already registered")
        state.providers[provider.name] = provider

    registry.register_provider = register_provider
    token_auth = types.ModuleType("hermes_cli.dashboard_auth.token_auth")
    token_auth.register_token_route = state.token_routes.add
    dashboard_auth = types.ModuleType("hermes_cli.dashboard_auth")
    dashboard_auth.base, dashboard_auth.registry, dashboard_auth.token_auth = base, registry, token_auth

    for name, module in {
        "hermes_constants": constants,
        "hermes_cli": types.ModuleType("hermes_cli"),
        "hermes_cli.config": config,
        "hermes_cli.web_server_profiles": profiles,
        "hermes_cli.web_server_chat": chat,
        "hermes_cli.dashboard_auth": dashboard_auth,
        "hermes_cli.dashboard_auth.base": base,
        "hermes_cli.dashboard_auth.registry": registry,
        "hermes_cli.dashboard_auth.token_auth": token_auth,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(api, "_speech_server_limiter",
                        api._MintLimiter(api.SPEECH_SERVER_LIMIT, api.SPEECH_SERVER_WINDOW_S))
    monkeypatch.setattr(api, "_local_live_auth_state", {"ready": True, "reason": None, "started": True})
    monkeypatch.setattr(api, "_local_live_calls", {})
    monkeypatch.setattr(api, "_local_live_last_request", {})
    state.home = home
    return state


class FakeUpstream:
    """The speech server: records what Conduit's socket forwarded, then waits."""

    def __init__(self):
        self.sent = []
        self.close_code = 1000
        self.close_reason = ""
        self.release = asyncio.Event()

    async def send(self, frame):
        self.sent.append(frame)
        if frame == "finish":
            self.release.set()

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        await self.release.wait()
        if False:
            yield

    async def close(self):
        pass


@pytest.fixture
def server(monkeypatch):
    state = types.SimpleNamespace(upstream=FakeUpstream())

    async def connect(url, headers):
        return state.upstream

    monkeypatch.setattr(api, "_connect_speech_server", connect)
    return state


@pytest.fixture
def client(hermes, server):
    app = FastAPI()

    @app.middleware("http")
    async def token_seam(request: Request, call_next):
        # Hermes' token-auth seam, as far as Local Live needs it.
        if request.url.path in hermes.token_routes or request.url.path in api.LOCAL_LIVE_TOKEN_PATHS:
            auth = request.headers.get("authorization", "")
            token = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else ""
            provider = hermes.providers.get(api.LOCAL_LIVE_PROVIDER_NAME) or api._local_live_provider()
            principal = provider.verify_token(token=token)
            if principal is not None:
                request.state.token_principal = principal
        return await call_next(request)

    app.include_router(api.router, prefix=BASE)
    with TestClient(app) as test_client:
        yield test_client


def _key(hermes, profile=None):
    with hermes_scope(hermes, profile):
        return api.local_live_key(profile)


@contextlib.contextmanager
def hermes_scope(hermes, profile):
    previous, hermes.profile = hermes.profile, profile
    try:
        yield
    finally:
        hermes.profile = previous


def _completion(text="What's the weather?", stream=False):
    return {"model": "aria-voice", "stream": stream,
            "messages": [{"role": "system", "content": "You are a voice assistant."},
                         {"role": "user", "content": "Hi"},
                         {"role": "assistant", "content": "Hello!"},
                         {"role": "user", "content": text}]}


def _sse_text(body):
    words = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            delta = json.loads(line[6:])["choices"][0]["delta"]
            words.append(delta.get("content") or "")
    return "".join(words)


# --- Keys ----------------------------------------------------------------------

def test_key_is_made_once_kept_private_and_names_its_profile(hermes):
    key = _key(hermes, "work")
    assert key.startswith("cll1.")
    assert _key(hermes, "work") == key
    with hermes_scope(hermes, "work"):
        stored = api._local_live_key_path()
    assert json.loads(stored.read_text())["api_key"] == key
    assert stat_mode(stored) == 0o600
    assert api.local_live_profile_for_key(key) == "work"


def test_a_mixed_case_profiles_key_verifies_in_that_profile(hermes):
    key = _key(hermes, "Work")
    assert api.local_live_profile_for_key(key) == "Work"


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_default_profile_key_and_rotation(hermes):
    key = _key(hermes)
    assert api.local_live_profile_for_key(key) == ""
    with hermes_scope(hermes, None):
        rotated = api.local_live_key(None, rotate=True)
    assert rotated != key
    assert api.local_live_profile_for_key(key) is None
    assert api.local_live_profile_for_key(rotated) == ""


@pytest.mark.parametrize("token", ["", "cll1", "cll1.-.", "cll2.-.abc", "cll1.!!!.abc", "Bearer x", "cll1.d29yaw.abc"])
def test_malformed_or_unknown_keys_name_no_profile(hermes, token):
    _key(hermes, "work")
    assert api.local_live_profile_for_key(token) is None


def test_another_profiles_key_with_this_profiles_name_is_refused(hermes):
    work = _key(hermes, "work")
    _key(hermes, "home")
    forged = ".".join(["cll1", api._local_live_profile_part("home"), work.split(".")[2]])
    assert api.local_live_profile_for_key(forged) is None


# --- Auth seam -------------------------------------------------------------------

def test_provider_is_token_only(hermes):
    provider = api._local_live_provider()
    key = _key(hermes, "work")
    assert provider.supports_token and not provider.supports_session
    assert provider.verify_token(token=key).principal == "local-live:work"
    assert provider.verify_token(token="nope") is None
    with pytest.raises(NotImplementedError):
        provider.start_login(redirect_uri="https://x")


def _dashboard(monkeypatch, auth_required):
    app = types.SimpleNamespace(state=types.SimpleNamespace(auth_required=auth_required))
    web_server = types.ModuleType("hermes_cli.web_server")
    web_server.app = app
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", web_server)


def test_registers_key_and_routes_on_a_loopback_dashboard(hermes, monkeypatch):
    _dashboard(monkeypatch, auth_required=False)
    api._local_live_auth_state.update(ready=False)
    api._local_live_register_auth()
    assert api._local_live_auth_state["ready"]
    assert api.LOCAL_LIVE_PROVIDER_NAME in hermes.providers
    assert hermes.token_routes == set(api.LOCAL_LIVE_TOKEN_PATHS)


def test_stays_off_when_a_public_dashboard_has_nothing_else_to_sign_in_with(hermes, monkeypatch):
    # Registered alone, the key would pass for the sign-in a public bind needs.
    _dashboard(monkeypatch, auth_required=True)
    api._local_live_auth_state.update(ready=False)
    api._local_live_register_auth()
    assert not api._local_live_auth_state["ready"]
    assert hermes.providers == {} and hermes.token_routes == set()


def test_registers_beside_a_public_dashboards_sign_in(hermes, monkeypatch):
    _dashboard(monkeypatch, auth_required=True)
    hermes.providers["basic"] = types.SimpleNamespace(name="basic")
    api._local_live_auth_state.update(ready=False)
    api._local_live_register_auth()
    assert api._local_live_auth_state["ready"]


def test_without_the_seam_local_live_says_why(hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_auth", None)
    api._local_live_auth_state.update(ready=False)
    api._local_live_register_auth()
    assert api._local_live_auth_state["reason"] == api.LOCAL_LIVE_UNAVAILABLE


# --- Status ------------------------------------------------------------------------

def test_status_gives_the_model_address_and_key(client, hermes):
    body = client.get(f"{BASE}/local-live/status?profile=work").json()
    assert body["available"] is True
    assert body["model_path"] == "/api/plugins/conduit_push/local-live/v1"
    assert api.local_live_profile_for_key(body["api_key"]) == "work"
    assert body["in_call"] is False


def test_status_without_the_seam_has_no_key(client, hermes):
    api._local_live_auth_state.update(ready=False, reason=api.LOCAL_LIVE_UNAVAILABLE)
    body = client.get(f"{BASE}/local-live/status").json()
    assert body["available"] is False
    assert body["api_key"] is None
    assert body["reason"] == api.LOCAL_LIVE_UNAVAILABLE


def test_key_rotation_route(client, hermes):
    old = client.get(f"{BASE}/local-live/status").json()["api_key"]
    new = client.post(f"{BASE}/local-live/key").json()["api_key"]
    assert new != old and api.local_live_profile_for_key(old) is None


# --- Turns -----------------------------------------------------------------------

def test_model_routes_need_the_key(client):
    assert client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion()).status_code == 401
    assert client.get(f"{BASE}/local-live/v1/models",
                      headers={"Authorization": "Bearer cll1.-.wrong"}).status_code == 401


def test_models_lists_one_model(client, hermes):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    assert client.get(f"{BASE}/local-live/v1/models", headers=headers).json()["data"][0]["id"] == "conduit"


def test_without_a_call_the_server_still_gets_an_answer(client, hermes):
    # A server's warmup request comes before any call; failing it would stop
    # the server from starting.
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    body = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion(), headers=headers).json()
    assert body["choices"][0]["message"]["content"] == api.LOCAL_LIVE_NO_CALL_REPLY
    assert client.get(f"{BASE}/local-live/status").json()["last_request_at"] is not None
    streamed = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion(stream=True), headers=headers)
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert _sse_text(streamed.text) == api.LOCAL_LIVE_NO_CALL_REPLY
    assert streamed.text.rstrip().endswith("data: [DONE]")


def _ask_in_background(client, headers, body):
    result = {}

    def run():
        result["response"] = client.post(f"{BASE}/local-live/v1/chat/completions", json=body, headers=headers)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_a_turn_goes_to_the_phone_and_its_words_stream_back(client, hermes, server):
    headers = {"Authorization": f"Bearer {_key(hermes, 'work')}"}
    with client.websocket_connect(f"{BASE}/local-live/socket?profile=work") as ws:
        ws.send_text('{"type":"session.update"}')
        thread, result = _ask_in_background(client, headers, _completion("Turn on the lights", stream=True))
        turn = json.loads(ws.receive_text())
        assert turn == {"type": "conduit.turn", "turn_id": "t1", "text": "Turn on the lights"}
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "Done, "}))
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "the lights are on."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t1"}))
        thread.join(10)
        ws.send_text("finish")
    response = result["response"]
    assert response.status_code == 200
    assert _sse_text(response.text) == "Done, the lights are on."
    # Turn frames never reach the speech server.
    assert server.upstream.sent == ['{"type":"session.update"}', "finish"]


def test_a_turn_for_another_profile_isnt_sent_to_this_call(client, hermes):
    headers = {"Authorization": f"Bearer {_key(hermes, 'home')}"}
    with client.websocket_connect(f"{BASE}/local-live/socket?profile=work") as ws:
        body = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion(), headers=headers).json()
        ws.send_text("finish")
    assert body["choices"][0]["message"]["content"] == api.LOCAL_LIVE_NO_CALL_REPLY


def test_a_new_turn_cancels_the_one_before(client, hermes):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        first, first_result = _ask_in_background(client, headers, _completion("one"))
        assert json.loads(ws.receive_text())["turn_id"] == "t1"
        second, second_result = _ask_in_background(client, headers, _completion("two"))
        assert json.loads(ws.receive_text()) == {"type": "conduit.turn.cancel", "turn_id": "t1"}
        assert json.loads(ws.receive_text())["turn_id"] == "t2"
        first.join(10)
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t2", "text": "Two."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t2"}))
        second.join(10)
        ws.send_text("finish")
    assert first_result["response"].json()["choices"][0]["message"]["content"] == ""
    assert second_result["response"].json()["choices"][0]["message"]["content"] == "Two."


def test_a_phone_error_ends_the_turn(client, hermes):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        thread, result = _ask_in_background(client, headers, _completion())
        ws.receive_text()
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "Hermes is offline."}))
        ws.send_text(json.dumps({"type": "conduit.turn.error", "turn_id": "t1", "message": "offline"}))
        thread.join(10)
        ws.send_text("finish")
    assert result["response"].json()["choices"][0]["message"]["content"] == "Hermes is offline."


def test_turn_text_is_the_newest_user_message():
    assert api._local_live_user_text(_completion("  hello\n  there ")) == "hello there"
    parts = {"messages": [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"},
                                                        {"type": "input_text", "text": "b"}]}]}
    assert api._local_live_user_text(parts) == "a b"
    assert api._local_live_user_text({"messages": [{"role": "system", "content": "x"}]}) == ""


def test_capability_is_advertised():
    assert "local-live" in api.ROUTE_CAPABILITIES


def test_a_speak_socket_has_its_text_read_back_unchanged(client, hermes, server):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket?mode=speak") as ws:
        streamed = client.post(f"{BASE}/local-live/v1/chat/completions",
                               json=_completion("It's sunny, 21 degrees.", stream=True), headers=headers)
        ws.send_text("finish")
    assert _sse_text(streamed.text) == "It's sunny, 21 degrees."
    # Nothing went to the phone: there's no turn to run.
    assert server.upstream.sent == ["finish"]


def _wait_for(condition):
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_speak_and_live_sockets_on_one_profile_each_get_their_own_requests(client, hermes, server):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    item = {"type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Read  me."}]}}
    with client.websocket_connect(f"{BASE}/local-live/socket") as live:
        with client.websocket_connect(f"{BASE}/local-live/socket?mode=speak") as speak:
            speak.send_text(json.dumps(item))
            _wait_for(lambda: any("Read  me." in sent for sent in server.upstream.sent))
            read = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion("Read me."), headers=headers)
            assert read.json()["choices"][0]["message"]["content"] == "Read me."
            # A retry (the server lost the answer) still reads back.
            again = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion("Read me."), headers=headers)
            assert again.json()["choices"][0]["message"]["content"] == "Read me."
            # The newer speak socket doesn't take the live call's turn.
            thread, result = _ask_in_background(client, headers, _completion("What's the time?"))
            turn = json.loads(live.receive_text())
            assert turn["text"] == "What's the time?"
            live.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))
            thread.join(10)
            speak.send_text("finish")
        live.send_text("finish")
    assert result["response"].status_code == 200


def test_a_turns_reply_is_capped(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_MAX_REPLY_CHARS", 10)
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        thread, result = _ask_in_background(client, headers, _completion())
        ws.receive_text()
        for _ in range(3):
            ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "abcdefg"}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t1"}))
        thread.join(10)
        ws.send_text("finish")
    assert result["response"].json()["choices"][0]["message"]["content"] == "abcdefgabc"


def test_auth_start_retries_once_the_dashboard_is_imported(monkeypatch):
    monkeypatch.setitem(api._local_live_auth_state, "started", False)
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server", raising=False)
    api._local_live_start_auth()
    assert api._local_live_auth_state["started"] is False


def test_a_runaway_voice_server_is_rate_limited(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_local_live_limiter", api._MintLimiter(2, 60.0, message="Too many"))
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    codes = [client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion(), headers=headers).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_models_route_counts_against_the_rate_limit(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_local_live_limiter", api._MintLimiter(1, 60.0, message="Too many"))
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    codes = [client.get(f"{BASE}/local-live/v1/models", headers=headers).status_code for _ in range(2)]
    assert codes == [200, 429]


def test_non_text_message_content_is_no_turn():
    body = {"messages": [{"role": "user", "content": {"text": "hi"}}]}
    assert api._local_live_user_text(body) == ""
    parts = {"messages": [{"role": "user", "content": [{"type": "text", "text": 5}, {"type": "text", "text": "hi"}]}]}
    assert api._local_live_user_text(parts) == "hi"


def test_a_flood_of_wrong_keys_stops_reading_files(monkeypatch):
    monkeypatch.setattr(api, "_local_live_key_check_limiter", api._MintLimiter(2, 60.0, message="Too many"))
    checked = []
    monkeypatch.setattr(api, "_local_live_check_key", lambda token: checked.append(token))
    for n in range(4):
        assert api.local_live_profile_for_key(f"cll1.-.wrong{n}") is None
    assert len(checked) == 2


def test_a_verify_racing_a_rotation_does_not_cache_the_old_key(monkeypatch):
    monkeypatch.setattr(api, "_local_live_verified", {})

    def check_during_rotation(token):
        monkeypatch.setattr(api, "_local_live_key_generation", api._local_live_key_generation + 1)
        return "work"

    monkeypatch.setattr(api, "_local_live_check_key", check_during_rotation)
    assert api.local_live_profile_for_key("cll1.d29yaw.old") == "work"
    assert "cll1.d29yaw.old" not in api._local_live_verified
