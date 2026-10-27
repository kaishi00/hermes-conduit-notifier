import abc
import asyncio
import base64
import contextlib
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
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


# Two dashboard processes on one HERMES_HOME (`hermes dashboard` and the
# desktop's `hermes serve`, say): the key is made slowly, so both read "no key
# yet" before either writes unless the file lock keeps them apart.
_FIRST_KEY_WORKER = textwrap.dedent("""
    import importlib.util, pathlib, secrets, sys, time, types
    home = pathlib.Path(sys.argv[1])
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = constants
    spec = importlib.util.spec_from_file_location("plugin_api", sys.argv[2])
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    token_urlsafe = secrets.token_urlsafe
    def slow(n):
        time.sleep(0.5)
        return token_urlsafe(n)
    api.secrets = types.SimpleNamespace(token_urlsafe=slow)
    print(api.local_live_key(None))
""")


def test_two_processes_making_the_first_key_at_once_hand_out_the_same_one(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(_FIRST_KEY_WORKER)
    home = tmp_path / "home"
    home.mkdir()
    processes = [subprocess.Popen([sys.executable, str(worker), str(home), str(ROOT / "dashboard" / "plugin_api.py")],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    keys = []
    for process in processes:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, err
        keys.append(out.strip().splitlines()[-1])
    stored = json.loads((home / api.LOCAL_LIVE_FILE).read_text())["api_key"]
    # Only the key on disk verifies, so both must have handed out that one.
    assert keys == [stored, stored]


@pytest.mark.parametrize("token", ["", "cll1", "cll1.-.", "cll2.-.abc", "cll1.!!!.abc", "Bearer x", "cll1.d29yaw.abc",
                                   "cll1.-." + "a" * 600])
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
    monkeypatch.setattr(api, "_local_live_key_check_total_limiter", api._MintLimiter(100, 60.0, message="Too many"))
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


def test_malformed_keys_spend_no_checks_and_one_profile_cant_lock_out_another(monkeypatch):
    monkeypatch.setattr(api, "_local_live_key_check_limiter", api._MintLimiter(2, 60.0, message="Too many"))
    monkeypatch.setattr(api, "_local_live_key_check_total_limiter", api._MintLimiter(100, 60.0, message="Too many"))
    checked = []
    monkeypatch.setattr(api, "_local_live_check_key", lambda token: checked.append(token))
    for n in range(5):
        assert api.local_live_profile_for_key(f"garbage{n}") is None
    assert checked == []
    for n in range(3):
        api.local_live_profile_for_key(f"cll1.-.wrong{n}")
    work = "cll1." + base64.urlsafe_b64encode(b"work").decode().rstrip("=") + ".wrong"
    api.local_live_profile_for_key(work)
    assert len(checked) == 3 and checked[-1] == work


def test_an_unforeseen_error_starting_auth_retries_with_a_true_reason(monkeypatch):
    def broken():
        raise RuntimeError("registry broke")

    monkeypatch.setattr(api, "_local_live_register_auth", broken)
    monkeypatch.setattr(api, "_local_live_auth_state", {**api._local_live_auth_state, "started": True})
    api._local_live_register_auth_thread()
    assert api._local_live_auth_state["started"] is False
    assert "RuntimeError" in api._local_live_auth_state["reason"]


def test_the_total_key_check_budget_backs_up_every_profile(monkeypatch):
    monkeypatch.setattr(api, "_local_live_key_check_limiter", api._MintLimiter(100, 60.0, message="Too many"))
    monkeypatch.setattr(api, "_local_live_key_check_total_limiter", api._MintLimiter(2, 60.0, message="Too many"))
    checked = []
    monkeypatch.setattr(api, "_local_live_check_key", lambda token: checked.append(token))
    for name in (b"a", b"b", b"c"):
        api.local_live_profile_for_key("cll1." + base64.urlsafe_b64encode(name).decode().rstrip("=") + ".wrong")
    assert len(checked) == 2
    assert api.local_live_profile_for_key(None) is None


def test_wrong_keys_spending_a_profiles_budget_dont_lock_out_its_right_key(hermes, monkeypatch):
    monkeypatch.setattr(api, "_local_live_verified", {})
    monkeypatch.setattr(api, "_local_live_known", {})
    monkeypatch.setattr(api, "_local_live_key_check_limiter", api._MintLimiter(2, 60.0, message="Too many"))
    monkeypatch.setattr(api, "_local_live_key_check_total_limiter", api._MintLimiter(100, 60.0, message="Too many"))
    key = _key(hermes, "work")
    assert api.local_live_profile_for_key(key) == "work"
    wrong = ".".join(key.split(".")[:2] + ["wrong"])
    for _ in range(5):
        assert api.local_live_profile_for_key(wrong) is None
    # The 30 s cache has expired: the right key still gets in, the wrong one doesn't.
    api._local_live_verified.clear()
    assert api.local_live_profile_for_key(key) == "work"
    api._local_live_verified.clear()
    assert api.local_live_profile_for_key(wrong) is None


def test_a_rotation_by_another_process_retires_the_remembered_key(hermes, monkeypatch):
    monkeypatch.setattr(api, "_local_live_verified", {})
    monkeypatch.setattr(api, "_local_live_known", {})
    key = _key(hermes, "work")
    assert api.local_live_profile_for_key(key) == "work"
    # Another dashboard process replaces the file; this one's counter never moves.
    with hermes_scope(hermes, "work"):
        path = hermes.home() / api.LOCAL_LIVE_FILE
    rotated = ".".join(key.split(".")[:2] + ["rotated"])
    replacement = path.with_name("replacement")
    replacement.write_text(json.dumps({"version": 1, "api_key": rotated}))
    os.replace(replacement, path)
    api._local_live_verified.clear()
    assert api.local_live_profile_for_key(key) is None
    assert api.local_live_profile_for_key(rotated) == "work"


def test_a_rotation_racing_the_remembered_key_sends_it_the_long_way(monkeypatch):
    monkeypatch.setattr(api, "_local_live_verified", {})
    calls = []

    def known_until_rotated(token, profile):
        calls.append(token)
        if len(calls) > 1:
            return False
        monkeypatch.setattr(api, "_local_live_key_generation", api._local_live_key_generation + 1)
        return True

    monkeypatch.setattr(api, "_local_live_is_known", known_until_rotated)
    monkeypatch.setattr(api, "_local_live_check_key", lambda token: None)
    assert api.local_live_profile_for_key("cll1.-.old") is None
    assert api._local_live_verified == {}


def test_a_rotation_here_retires_the_remembered_key(hermes, monkeypatch):
    monkeypatch.setattr(api, "_local_live_verified", {})
    monkeypatch.setattr(api, "_local_live_known", {})
    key = _key(hermes, "work")
    assert api.local_live_profile_for_key(key) == "work"
    with hermes_scope(hermes, "work"):
        api.local_live_key("work", rotate=True)
    assert "work" not in api._local_live_known
    assert api.local_live_profile_for_key(key) is None


# --- Read-back speech servers ---------------------------------------------------
#
# A server that only transcribes and reads text back (the farm's voice
# adapter): it refuses automatic responses, and the plugin runs the call's
# turns on the relay.

import queue as _queue

from starlette.websockets import WebSocketDisconnect

LIVE_UPDATE = {"type": "session.update", "session": {"type": "realtime", "audio": {"input": {
    "format": {"type": "audio/pcm", "rate": 24000},
    "turn_detection": {"type": "server_vad", "create_response": True, "interrupt_response": True}}}}}
PCM = base64.b64encode(b"\x10\x27" * 240).decode()


def _check(marker):
    return {"type": "response.create", "response": {"conversation": "none", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": marker}]}]}}


class ReadBackUpstream:
    """The farm voice adapter's realtime socket, as far as the relay sees it:
    automatic responses refused, each response the text it's given, read back."""

    def __init__(self, refuses=True, says=None):
        self.refuses = refuses
        # What its transcripts say instead of the text (a server with a model).
        self.says = says
        self.hold = False
        self.held = None
        # Holds a response without starting it, and drops it on a cancel
        # without a word (the adapter can, on Python 3.12+).
        self.unstarted = False
        # Answers the next response.create this many times with busy.
        self.busy = 0
        # Leaves a held response's cancel unanswered until answer_cancel().
        self.defer_cancel = False
        self.deferred = None
        # Streams a response's audio over this long before its end.
        self.stream_s = 0.0
        self.sent = []
        self.events = _queue.Queue()
        self.close_code = 1000
        self.close_reason = ""
        self.count = 0

    def push(self, event):
        self.events.put(json.dumps(event))

    def creates(self):
        return [frame for frame in self.sent if isinstance(frame, dict) and frame.get("type") == "response.create"]

    def spoken(self):
        return [api._local_live_item_words(frame["response"]["input"][0]) for frame in self.creates()]

    async def send(self, frame):
        if frame == "finish":
            self.events.put(None)
            return
        event = json.loads(frame)
        self.sent.append(event)
        kind = event.get("type")
        if kind == "session.update":
            detection = api._local_live_turn_detection(event)
            if detection is not None and self.refuses:
                self.push({"type": "error", "error": {"type": "invalid_request_error",
                                                      "code": "realtime_live_unavailable",
                                                      "param": "session.audio.input.turn_detection.create_response",
                                                      "message": "This speech server reads text aloud only."}})
            else:
                self.push({"type": "session.updated", "session": event["session"]})
        elif kind == "response.create":
            if self.busy:
                self.busy -= 1
                self.push({"type": "error", "error": {"code": "conversation_already_has_active_response"}})
                # The response it was busy with ends.
                self.push({"type": "response.done", "response": {"id": "other", "status": "completed"}})
                return
            self.count += 1
            rid = f"resp{self.count}"
            text = api._local_live_item_words((event.get("response") or {}).get("input", [None])[0]) or ""
            if self.unstarted:
                self.held = (rid, text)
                return
            self.push({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
            if self.hold:
                self.held = (rid, text)
                return
            if self.stream_s:
                threading.Thread(target=self._stream, args=(rid, text), daemon=True).start()
                return
            self._speak(rid, text)
        elif kind == "response.cancel" and self.unstarted:
            self.held = None
        elif kind == "response.cancel" and self.held is not None and self.defer_cancel:
            self.deferred, self.held = self.held, None
        elif kind == "response.cancel" and self.held is not None:
            rid, _ = self.held
            self.held = None
            # Audio already on its way when the cancel lands.
            self.push({"type": "response.output_audio.delta", "response_id": rid, "delta": PCM})
            self.push({"type": "response.done", "response": {"id": rid, "status": "cancelled"}})

    def answer_cancel(self):
        rid, _ = self.deferred
        self.deferred = None
        self.push({"type": "response.done", "response": {"id": rid, "status": "cancelled"}})

    def _stream(self, rid, text):
        self.push({"type": "response.output_audio_transcript.delta", "response_id": rid, "delta": text})
        end = time.monotonic() + self.stream_s
        while time.monotonic() < end:
            self.push({"type": "response.output_audio.delta", "response_id": rid, "delta": PCM})
            time.sleep(0.05)
        self.push({"type": "response.output_audio_transcript.done", "response_id": rid, "transcript": text})
        self.push({"type": "response.done", "response": {"id": rid, "status": "completed"}})

    def _speak(self, rid, text):
        said = self.says or text
        self.push({"type": "response.output_audio_transcript.delta", "response_id": rid, "delta": said})
        self.push({"type": "response.output_audio.delta", "response_id": rid, "delta": PCM})
        self.push({"type": "response.output_audio_transcript.done", "response_id": rid, "transcript": said})
        self.push({"type": "response.done", "response": {"id": rid, "status": "completed"}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            try:
                frame = self.events.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.005)
                continue
            if frame is None:
                raise StopAsyncIteration
            return frame

    async def close(self):
        self.events.put(None)


def _next(ws, kind):
    """Frames from the relay until one of ``kind``, which it returns with the ones before."""
    seen = []
    while True:
        frame = json.loads(ws.receive_text())
        seen.append(frame)
        if frame.get("type") == kind:
            return frame, seen


def _finish(ws):
    """Ends the server's side, then waits for the relay to close the call's."""
    ws.send_text("finish")
    with contextlib.suppress(Exception):
        while True:
            ws.receive_text()


def _ready(ws, upstream, greeting=None):
    ws.send_text(json.dumps(LIVE_UPDATE))
    updated, seen = _next(ws, "session.updated")
    assert [frame["type"] for frame in seen] == ["session.updated"]
    marker = "conduit-ready-1234"
    ws.send_text(json.dumps(_check(marker)))
    ask, _ = _next(ws, "conduit.turn")
    assert ask["text"] == marker
    if greeting:
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": ask["turn_id"], "text": greeting}))
    ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": ask["turn_id"]}))
    done, seen = _next(ws, "response.done")
    return seen


def test_a_read_back_server_gets_automatic_responses_off_and_the_call_never_sees_the_refusal(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        seen = _ready(ws, upstream)
        _finish(ws)
    # The check's ask went to the phone, which answered it with nothing: the
    # call still sees its response begin and end, and the server said nothing.
    assert [frame["type"] for frame in seen] == ["response.created", "response.done"]
    assert seen[-1]["response"]["status"] == "completed"
    updates = [frame for frame in upstream.sent if frame["type"] == "session.update"]
    assert updates[0] == LIVE_UPDATE
    detection = updates[1]["session"]["audio"]["input"]["turn_detection"]
    assert detection == {"type": "server_vad", "create_response": False, "interrupt_response": False}
    assert updates[1]["session"]["audio"]["input"]["format"] == LIVE_UPDATE["session"]["audio"]["input"]["format"]
    assert upstream.creates() == []


def test_the_greeting_is_read_back_as_the_checks_reply(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        seen = _ready(ws, upstream, greeting="Hi, I'm listening.")
        _finish(ws)
    assert upstream.spoken() == ["Hi, I'm listening."]
    assert [frame["type"] for frame in seen] == ["response.created", "response.output_audio_transcript.delta",
                                                 "response.output_audio.delta",
                                                 "response.output_audio_transcript.done", "response.done"]


def test_a_spoken_turn_runs_on_the_phone_and_its_reply_is_read_back_a_sentence_at_a_time(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "item1"})
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": "item1",
                       "transcript": " Turn on   the lights "})
        turn, seen = _next(ws, "conduit.turn")
        # The words reach the call before its turn does.
        assert [frame["type"] for frame in seen] == ["input_audio_buffer.speech_started",
                                                     "conversation.item.input_audio_transcription.completed",
                                                     "conduit.turn"]
        assert turn["text"] == "Turn on the lights"
        for delta in ("Done, the lights ", "are on. Is there anything else ", "you'd like me to do tonight?"):
            ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"], "text": delta}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))
        done, seen = _next(ws, "response.done")
        _finish(ws)
    assert upstream.spoken() == ["Done, the lights are on.", "Is there anything else you'd like me to do tonight?"]
    # One response for the whole reply, its transcript spaced between sentences.
    kinds = [frame["type"] for frame in seen]
    assert kinds.count("response.created") == 1 and kinds.count("response.done") == 1
    assert "".join(frame["delta"] for frame in seen if frame["type"] == "response.output_audio_transcript.delta") == \
        "Done, the lights are on. Is there anything else you'd like me to do tonight?"
    assert kinds.count("response.output_audio.delta") == 2
    assert done["response"]["status"] == "completed"
    # Nothing the plugin keeps reaches the server.
    assert not any(frame["type"].startswith("conduit.") for frame in upstream.sent)


def test_speaking_over_the_reply_stops_it_and_lets_the_turn_go(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        upstream.hold = True
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Tell me a story"})
        turn, _ = _next(ws, "conduit.turn")
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"],
                                 "text": "Once upon a time, there was a fox. "}))
        _next(ws, "response.created")
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "item2"})
        cancel, seen = _next(ws, "conduit.turn.cancel")
        assert [frame["type"] for frame in seen] == ["input_audio_buffer.speech_started", "conduit.turn.cancel"]
        assert cancel["turn_id"] == turn["turn_id"]
        done, seen = _next(ws, "response.done")
        assert done["response"]["status"] == "cancelled" and seen == [done]
        # The cancelled response's late audio and end stay with the plugin;
        # the next turn speaks.
        upstream.hold = False
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Stop"})
        turn, seen = _next(ws, "conduit.turn")
        assert [frame["type"] for frame in seen] == ["conversation.item.input_audio_transcription.completed",
                                                     "conduit.turn"]
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"], "text": "Okay."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))
        done, seen = _next(ws, "response.done")
        _finish(ws)
    assert {"type": "response.cancel"} in upstream.sent
    assert upstream.spoken() == ["Once upon a time, there was a fox.", "Okay."]
    assert [frame["type"] for frame in seen].count("response.output_audio.delta") == 1


def test_the_users_stop_reaches_the_server_once_whether_or_not_a_sentence_is_in_progress(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        # Mid-sentence: the plugin cancels the sentence it asked for, once.
        upstream.hold = True
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Tell me a story"})
        turn, _ = _next(ws, "conduit.turn")
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"],
                                 "text": "Once upon a time, there was a fox. "}))
        _next(ws, "response.created")
        # The server holds its answer to the cancel, so the second tap lands
        # while the first is still winding down.
        upstream.defer_cancel = True
        ws.send_text(json.dumps({"type": "response.cancel"}))
        cancel, _ = _next(ws, "conduit.turn.cancel")
        assert cancel["turn_id"] == turn["turn_id"]
        # A second tap then tells the server nothing new. The audio after it
        # is relayed in order, so once it arrives both taps were handled.
        ws.send_text(json.dumps({"type": "response.cancel"}))
        ws.send_text(json.dumps({"type": "input_audio_buffer.append", "audio": PCM}))
        deadline = time.monotonic() + 10
        while not any(frame.get("type") == "input_audio_buffer.append" for frame in upstream.sent):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert [frame["type"] for frame in upstream.sent].count("response.cancel") == 1
        upstream.defer_cancel = False
        upstream.answer_cancel()
        done, _ = _next(ws, "response.done")
        assert done["response"]["status"] == "cancelled"
        # The server already finished the sentence, which may still play on
        # the phone: the stop goes on, so a server that keeps its microphone
        # shut while its audio plays opens it (#1325).
        upstream.hold = False
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Another"})
        turn, _ = _next(ws, "conduit.turn")
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"], "text": "Okay."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))
        _next(ws, "response.done")
        ws.send_text(json.dumps({"type": "response.cancel"}))
        # A server with nothing to cancel may say so; the call never hears it.
        upstream.push({"type": "error", "error": {"code": "response_cancel_not_active", "message": "No active response"}})
        ws.send_text("finish")
        seen = []
        with contextlib.suppress(Exception):
            while True:
                seen.append(json.loads(ws.receive_text()))
        assert all(frame.get("type") != "error" for frame in seen)
    assert [frame["type"] for frame in upstream.sent].count("response.cancel") == 2
    assert upstream.sent[-1] == {"type": "response.cancel"}


def test_a_typed_turn_is_run_by_the_plugin_not_read_back(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    item = {"type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What time is it?"}]}}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        ws.send_text(json.dumps(item))
        ws.send_text(json.dumps({"type": "response.create"}))
        turn, _ = _next(ws, "conduit.turn")
        assert turn["text"] == "What time is it?"
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"], "text": "Nine."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))
        _next(ws, "response.done")
        _finish(ws)
    assert upstream.spoken() == ["Nine."]
    assert not any(frame["type"] == "conversation.item.create" for frame in upstream.sent)


def test_a_server_that_answers_by_itself_is_relayed_as_before(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream(refuses=False)
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        ws.send_text(json.dumps(LIVE_UPDATE))
        _next(ws, "session.updated")
        ws.send_text(json.dumps(_check("conduit-ready-5678")))
        # The server's own response to the check: the plugin isn't in it.
        _next(ws, "response.done")
        _finish(ws)
    assert upstream.sent[0] == LIVE_UPDATE and upstream.sent[1] == _check("conduit-ready-5678")
    assert len(upstream.sent) == 2


def test_a_server_that_doesnt_read_back_ends_the_call(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream(says="I am a model and I say what I like")
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hello"})
        turn, _ = _next(ws, "conduit.turn")
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"],
                                 "text": "Hello there, how are you doing today? I hope the weather is kind. "}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_text()
    assert closed.value.code == api.SPEECH_SERVER_CLOSE_REFUSED
    assert closed.value.reason == api.LOCAL_LIVE_NOT_READ_BACK
    assert len(upstream.creates()) == 2


def test_read_back_sentences_start_early_and_join_short_ones():
    clock = [0.0]
    sentences = api._ReadBackSentences(lambda: clock[0])
    assert sentences.feed("Yes. ") == ["Yes."]
    assert sentences.feed("It is. Short. ") == []
    assert sentences.feed("And this one is long enough to go on its own. ") == \
        ["It is. Short. And this one is long enough to go on its own."]
    assert sentences.feed("Waiting for the rest") == []
    clock[0] = api.LOCAL_LIVE_READ_BACK_FLUSH_S + 1
    assert sentences.tick() == ["Waiting for the"]
    assert sentences.finish() == ["rest"]
    long = api._ReadBackSentences()
    out = long.feed("word " * 120)
    assert out and all(len(sentence) <= api.LOCAL_LIVE_READ_BACK_MAX_SENTENCE_CHARS for sentence in out)
    assert api._ReadBackSentences().feed("It costs 3.50 today. ") == ["It costs 3.50 today."]


def test_read_back_is_checked_only_on_sentences_that_can_tell():
    assert api._local_live_read_back_matches("Hello there, how are you today?", "Hello there, how are you today?")
    assert api._local_live_read_back_matches("Something else entirely here now", "Hello there, how are you today?") \
        is False
    assert api._local_live_read_back_matches("Hi.", "Hi there.") is None
    assert api._local_live_read_back_matches("twenty one degrees and sunny out", "21 degrees and sunny out") is None


def test_only_a_refusal_of_automatic_responses_turns_read_back_on():
    assert api._local_live_refuses_live({"type": "error", "error": {"code": "realtime_live_unavailable"}})
    assert api._local_live_refuses_live({"type": "error", "error": {
        "param": "session.audio.input.turn_detection.create_response"}})
    assert not api._local_live_refuses_live({"type": "error", "error": {"code": "invalid_api_key"}})
    legacy = {"type": "session.update", "session": {"turn_detection": {"type": "server_vad"}}}
    changed, interrupts = api._local_live_read_back_update(legacy)
    assert changed["session"]["turn_detection"] == {"type": "server_vad", "create_response": False,
                                                    "interrupt_response": False}
    assert interrupts and legacy["session"]["turn_detection"] == {"type": "server_vad"}
    off = {"type": "session.update", "session": {"turn_detection": {"create_response": False}}}
    assert api._local_live_turn_detection(off) is None


def _say(ws, turn, *deltas, done=True):
    for delta in deltas:
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": turn["turn_id"], "text": delta}))
    if done:
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": turn["turn_id"]}))


def _heard(upstream, words):
    upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": words})


def test_a_cough_while_hermes_works_doesnt_cost_the_reply(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        _heard(upstream, "What's on today?")
        turn, _ = _next(ws, "conduit.turn")
        # Nothing said yet: the noise is shown, the turn stays.
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "cough"})
        _heard(upstream, "")
        _, seen = _next(ws, "conversation.item.input_audio_transcription.completed")
        assert [frame["type"] for frame in seen] == ["input_audio_buffer.speech_started",
                                                     "conversation.item.input_audio_transcription.completed"]
        _say(ws, turn, "Two meetings and a dentist appointment at four.")
        done, seen = _next(ws, "response.done")
        _finish(ws)
    assert done["response"]["status"] == "completed"
    assert not any(frame["type"] == "conduit.turn.cancel" for frame in seen)
    assert upstream.spoken() == ["Two meetings and a dentist appointment at four."]


def test_new_words_while_the_reply_speaks_start_a_new_turn(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        upstream.hold = True
        _heard(upstream, "Tell me a story")
        first, _ = _next(ws, "conduit.turn")
        _say(ws, first, "Once upon a time, there was a fox. ", done=False)
        _next(ws, "response.created")
        upstream.hold = False
        # No speech_started (a server that doesn't send it): the words alone.
        _heard(upstream, "Actually, what time is it?")
        second, seen = _next(ws, "conduit.turn")
        kinds = [frame["type"] for frame in seen]
        # The words show, the reply ends, and its turn gives way to theirs.
        assert kinds == ["conversation.item.input_audio_transcription.completed", "response.done",
                         "conduit.turn.cancel", "conduit.turn"]
        assert seen[1]["response"]["status"] == "cancelled" and seen[2]["turn_id"] == first["turn_id"]
        _say(ws, second, "Nine.")
        _next(ws, "response.done")
        _finish(ws)
    assert upstream.spoken() == ["Once upon a time, there was a fox.", "Nine."]


def test_a_cancel_the_server_drops_silently_holds_the_next_reply_only_briefly(client, hermes, server, monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_READ_BACK_CANCEL_TIMEOUT_S", 0.2)
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        upstream.unstarted = True
        _heard(upstream, "Tell me a story")
        first, _ = _next(ws, "conduit.turn")
        _say(ws, first, "Once upon a time. ", done=False)
        _wait_for(lambda: len(upstream.creates()) == 1)
        upstream.push({"type": "input_audio_buffer.speech_started"})
        _next(ws, "conduit.turn.cancel")
        upstream.unstarted = False
        _heard(upstream, "Stop")
        second, _ = _next(ws, "conduit.turn")
        started = time.monotonic()
        _say(ws, second, "Okay.")
        _next(ws, "response.done")
        waited = time.monotonic() - started
        _finish(ws)
    assert upstream.spoken() == ["Once upon a time.", "Okay."]
    assert waited < 5


def test_a_busy_server_gets_the_sentence_again_once_free(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        upstream.busy = 1
        _heard(upstream, "Hi")
        turn, _ = _next(ws, "conduit.turn")
        _say(ws, turn, "Hello there.")
        _, seen = _next(ws, "response.done")
        _finish(ws)
    # Asked twice: refused while busy, then read back.
    assert upstream.spoken() == ["Hello there.", "Hello there."]
    deltas = [frame["delta"] for frame in seen if frame["type"] == "response.output_audio_transcript.delta"]
    assert deltas == ["Hello there."]
    assert not any(frame["type"] == "error" for frame in seen)


def test_a_long_sentence_still_playing_isnt_given_up_on(client, hermes, server, monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_READ_BACK_RESPONSE_TIMEOUT_S", 0.3)
    server.upstream = upstream = ReadBackUpstream()
    upstream.stream_s = 1.0
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        _heard(upstream, "Read me something long")
        turn, _ = _next(ws, "conduit.turn")
        _say(ws, turn, "This sentence takes a whole second to say. ", "And this one follows it.")
        _, seen = _next(ws, "response.done")
        _finish(ws)
    # The second went only once the first was over: nothing of the first was cut.
    assert upstream.spoken() == ["This sentence takes a whole second to say.", "And this one follows it."]
    assert sum(1 for frame in seen if frame["type"] == "response.output_audio.delta") >= 20
    assert not any(frame["type"] == "error" for frame in seen)


def test_a_sentence_never_passes_the_cap():
    out = api._ReadBackSentences().feed("Ok. " + "word " * 200 + "end. ")
    assert out[0] == "Ok." and all(len(sentence) <= api.LOCAL_LIVE_READ_BACK_MAX_SENTENCE_CHARS for sentence in out)
    assert " ".join(out) == " ".join(("Ok. " + "word " * 200 + "end.").split())


def test_both_turn_detection_blocks_turn_automatic_responses_off():
    both = {"type": "session.update", "session": {"turn_detection": {"type": "server_vad"},
                                                  "audio": {"input": {"turn_detection": {"type": "server_vad",
                                                                                         "interrupt_response": False}}}}}
    changed, interrupts = api._local_live_read_back_update(both)
    assert api._local_live_turn_detections(changed) == [] and interrupts is False


def test_speech_before_the_reply_says_anything_holds_it_until_the_words_are_in(client, hermes, server):
    server.upstream = upstream = ReadBackUpstream()
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _ready(ws, upstream)
        _heard(upstream, "Book a table")
        first, _ = _next(ws, "conduit.turn")
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "more"})
        _next(ws, "input_audio_buffer.speech_started")
        # The reply's first sentence waits while the user may be speaking.
        _say(ws, first, "Sure, for how many people? ", done=False)
        time.sleep(0.3)
        assert upstream.creates() == []
        # Their words come: a new turn, and the held sentence is never said.
        _heard(upstream, "for two at eight")
        second, _ = _next(ws, "conduit.turn")
        _say(ws, second, "Done, two at eight.")
        _next(ws, "response.done")
        # A sound that comes to nothing lets a held reply go on.
        _heard(upstream, "Thanks")
        third, _ = _next(ws, "conduit.turn")
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "noise"})
        _next(ws, "input_audio_buffer.speech_started")
        _say(ws, third, "You're welcome.")
        time.sleep(0.3)
        assert upstream.spoken() == ["Done, two at eight."]
        upstream.push({"type": "conversation.item.input_audio_transcription.failed", "item_id": "noise",
                       "error": {"code": "server_error"}})
        _next(ws, "response.done")
        _finish(ws)
    assert upstream.spoken() == ["Done, two at eight.", "You're welcome."]


# --- Agent mode: a fast front model ------------------------------------------------

class FrontModel:
    """A stand-in OpenAI-compatible server: each request gets the next scripted
    answer, a list of SSE chunk deltas (or an int HTTP status)."""

    def __init__(self):
        import http.server
        self.answers = []
        self.requests = []
        model = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                answer = model.answers.pop(0) if model.answers else [{"content": "ok"}]
                if isinstance(answer, int):
                    self.send_response(answer)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta in answer:
                    chunk = {"choices": [{"index": 0, "delta": delta}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def front(hermes):
    model = FrontModel()
    hermes.config["voice"]["local_live"] = {"front_model": {"base_url": model.base_url, "model": "fast-1",
                                                            "api_key": "sk-front"}}
    yield model
    model.close()


AGENT_TOOLS = [{"type": "function", "name": "start_job", "description": "Start a Hermes job.",
                "parameters": {"type": "object", "properties": {"instructions": {"type": "string"}}}},
               {"type": "function", "function": {"name": "list_jobs", "parameters": {"type": "object"}}},
               {"type": "function", "name": "bad name!"}]


def _agent(ws, instructions="You are Hermes' voice."):
    ws.send_text(json.dumps({"type": "conduit.agent", "instructions": instructions, "tools": AGENT_TOOLS}))
    assert json.loads(ws.receive_text()) == {"type": "conduit.agent.ready", "tools": 2}


def test_status_reports_the_fast_model(client, hermes, front):
    body = client.get(f"{BASE}/local-live/status").json()
    assert body["front_model"] == {"configured": True, "available": True, "base_url": front.base_url,
                                   "model": "fast-1", "has_key": True, "reason": None}
    del hermes.config["voice"]["local_live"]
    assert client.get(f"{BASE}/local-live/status").json()["front_model"]["configured"] is False


def test_the_fast_models_address():
    assert api.local_live_front_model_url("http://10.1.10.52:8080/v1") == "http://10.1.10.52:8080/v1/chat/completions"
    assert api.local_live_front_model_url("https://api.example.com/v1/") == \
        "https://api.example.com/v1/chat/completions"
    assert api.local_live_front_model_url("box.lan:11434/v1/chat/completions") == \
        "http://box.lan:11434/v1/chat/completions"
    for bad in ("", "http://api.example.com/v1", "ftp://box/v1", "http://user:pw@box/v1"):
        with pytest.raises(api.TokenError):
            api.local_live_front_model_url(bad)
    with pytest.raises(api.TokenError, match="model name"):
        api.local_live_front_model_target({"base_url": "http://box.lan/v1"})


def test_agent_mode_answers_with_the_fast_model_not_a_hermes_turn(client, hermes, server, front):
    front.answers = [[{"role": "assistant"}, {"content": "<think>hmm</think>Hi! "}, {"content": "It's noon."}]]
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        streamed = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion("What time is it?", stream=True),
                               headers=headers)
        ws.send_text("finish")
    assert _sse_text(streamed.text) == "Hi! It's noon."
    request = front.requests[0]
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer sk-front"
    assert request["body"]["model"] == "fast-1" and request["body"]["stream"] is True
    messages = request["body"]["messages"]
    assert messages[0]["role"] == "system" and messages[0]["content"].startswith("You are Hermes' voice.")
    # The speech server's own history and system prompt aren't used: the call keeps its own.
    assert messages[1:] == [{"role": "user", "content": "What time is it?"}]
    assert [tool["function"]["name"] for tool in request["body"]["tools"]] == ["start_job", "list_jobs"]
    # Agent frames never reach the speech server.
    assert server.upstream.sent == ["finish"]


def test_a_fast_model_that_never_stops_is_cut_off(client, hermes, front, monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_MAX_REPLY_CHARS", 10)
    front.answers = [[{"content": "abcdef"}, {"content": "ghijkl"}, {"content": "mnop"}]]
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        streamed = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion("Talk", stream=True),
                               headers=headers)
        ws.send_text("finish")
    assert _sse_text(streamed.text) == "abcdefghij"


def test_plain_http_fast_models_skip_proxies():
    # An empty ProxyHandler opens nothing, so the opener has no proxy route at all.
    assert not any(isinstance(handler, api.urllib.request.ProxyHandler)
                   for handler in api._local_live_front_direct_opener.handlers)


def test_a_tool_call_runs_on_the_phone_and_the_model_goes_on(client, hermes, front):
    front.answers = [
        [{"content": "On it. "},
         {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "start_job", "arguments": '{"instr'}}]},
         {"tool_calls": [{"index": 0, "function": {"arguments": 'uctions": "check mail"}'}}]}],
        [{"content": "Hermes is checking your mail."}],
        [{"content": "You said thanks."}],
    ]
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        thread, result = _ask_in_background(client, headers, _completion("Check my mail", stream=True))
        call = json.loads(ws.receive_text())
        assert call == {"type": "conduit.tool.call", "call_id": "ll1", "name": "start_job",
                        "arguments": '{"instructions": "check mail"}'}
        ws.send_text(json.dumps({"type": "conduit.tool.result", "call_id": "ll1",
                                 "output": '{"status":"started","job":"1"}'}))
        thread.join(10)
        client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion("Thanks"), headers=headers)
        ws.send_text("finish")
    assert _sse_text(result["response"].text) == "On it. Hermes is checking your mail."
    second = front.requests[1]["body"]["messages"][1:]
    assert second == [
        {"role": "user", "content": "Check my mail"},
        {"role": "assistant", "content": "On it. ", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "start_job", "arguments": '{"instructions": "check mail"}'}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": '{"status":"started","job":"1"}'},
    ]
    # The next utterance carries the whole exchange.
    assert front.requests[2]["body"]["messages"][-2:] == [
        {"role": "assistant", "content": "Hermes is checking your mail."}, {"role": "user", "content": "Thanks"}]


def test_without_a_fast_model_hermes_answers_each_turn(client, hermes):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        thread, result = _ask_in_background(client, headers, _completion("Hello"))
        assert json.loads(ws.receive_text())["type"] == "conduit.turn"
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "Hi."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t1"}))
        thread.join(10)
        ws.send_text("finish")
    assert result["response"].json()["choices"][0]["message"]["content"] == "Hi."


def test_the_readiness_check_is_still_the_phones_turn(client, hermes, front):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        thread, result = _ask_in_background(client, headers, _completion("conduit-ready-1234"))
        assert json.loads(ws.receive_text())["text"] == "conduit-ready-1234"
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t1"}))
        thread.join(10)
        ws.send_text("finish")
    assert front.requests == []


@pytest.mark.parametrize("answer", [500, [], ["x", {"tool_calls": "x"}]], ids=["refused", "empty", "misshapen"])
def test_a_fast_model_that_is_down_hands_the_utterance_to_hermes(client, hermes, front, answer):
    front.answers = [answer]
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        thread, result = _ask_in_background(client, headers, _completion("Hello"))
        assert json.loads(ws.receive_text())["text"] == "Hello"
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "Hi from Hermes."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t1"}))
        thread.join(10)
        ws.send_text("finish")
    assert result["response"].json()["choices"][0]["message"]["content"] == \
        api.LOCAL_LIVE_FRONT_UNREACHABLE_REPLY + " Hi from Hermes."


def test_a_server_without_tool_support_is_asked_again_without_tools(client, hermes, front):
    front.answers = [400, [{"content": "Hello."}]]
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        body = client.post(f"{BASE}/local-live/v1/chat/completions", json=_completion("Hi"), headers=headers).json()
        ws.send_text("finish")
    assert body["choices"][0]["message"]["content"] == "Hello."
    assert "tools" in front.requests[0]["body"] and "tools" not in front.requests[1]["body"]


def test_an_engine_with_its_own_front_model_uses_the_calls_tools(client, hermes):
    headers = {"Authorization": f"Bearer {_key(hermes)}"}
    assert client.post(f"{BASE}/local-live/v1/tools/call", json={"name": "start_job"},
                       headers=headers).status_code == 409
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        listed = client.get(f"{BASE}/local-live/v1/tools", headers=headers).json()
        assert listed["in_call"] is True
        assert [tool["function"]["name"] for tool in listed["tools"]] == ["start_job", "list_jobs"]
        assert client.post(f"{BASE}/local-live/v1/tools/call", json={"name": "nope"},
                           headers=headers).status_code == 404
        result = {}

        def run():
            result["response"] = client.post(f"{BASE}/local-live/v1/tools/call",
                                             json={"name": "start_job", "arguments": {"instructions": "x"}},
                                             headers=headers)

        thread = threading.Thread(target=run)
        thread.start()
        call = json.loads(ws.receive_text())
        assert call["name"] == "start_job" and json.loads(call["arguments"]) == {"instructions": "x"}
        ws.send_text(json.dumps({"type": "conduit.tool.result", "call_id": call["call_id"], "output": "started"}))
        thread.join(10)
        ws.send_text("finish")
    assert result["response"].json() == {"output": "started"}
    assert client.get(f"{BASE}/local-live/v1/tools").status_code == 401


def test_an_answer_nobody_waits_for_is_handed_back_as_late(client, hermes):
    with client.websocket_connect(f"{BASE}/local-live/socket") as ws:
        _agent(ws)
        ws.send_text(json.dumps({"type": "conduit.tool.result", "call_id": "ll9", "output": "x", "speak": True}))
        assert json.loads(ws.receive_text()) == {"type": "conduit.tool.late", "call_id": "ll9"}
        ws.send_text("finish")


def test_tool_calls_the_user_talked_over_get_stand_in_results():
    agent = api._LocalLiveAgent("Be brief.", [])
    agent.history = [{"role": "user", "content": "a"},
                     {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
                     {"role": "tool", "tool_call_id": "c1", "content": "done"},
                     {"role": "user", "content": "b"},
                     {"role": "tool", "tool_call_id": "c2", "content": "late"}]
    messages = agent.messages()
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert messages[4]["tool_call_id"] == "c2" and "interrupted" in messages[4]["content"]


def test_history_stays_bounded_and_starts_with_the_user(monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_AGENT_MAX_HISTORY", 3)
    agent = api._LocalLiveAgent("", [])
    agent._append({"role": "user", "content": "1"})
    agent._append({"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]})
    agent._append({"role": "tool", "tool_call_id": "c", "content": "x"})
    agent._append({"role": "user", "content": "2"})
    assert agent.history == [{"role": "user", "content": "2"}]


def test_a_reconnect_mid_request_leaves_no_unanswered_user_message(monkeypatch):
    agent = api._LocalLiveAgent("", [])

    async def stream(target, messages, tools):
        agent.close()  # the phone reconnected while the model was thinking
        yield "text", "too late"

    monkeypatch.setattr(api, "_local_live_front_stream", stream)

    async def run():
        return [piece async for piece in agent.reply(None, "Hello", ("http://box/v1", "m", ""))]

    assert asyncio.run(run()) == []
    assert agent.history == []


def test_thinking_is_never_spoken():
    think = api._ThinkFilter()
    out = "".join(think.feed(piece) for piece in ["Hel", "lo <th", "ink>secret", " stuff</thi", "nk> there", "<"])
    assert out + think.flush() == "Hello  there<"


# --- Another profile, served by this dashboard (hermes-conduit#1336) ----------------
#
# Hermes 0.22.3+ sends ?profile=<another profile> to that profile's plugin host,
# which has no sockets and none of this process's calls; ?conduit_profile= keeps
# the request here, scoped to that profile.

def test_conduit_profile_status_and_key_are_that_profiles(client, hermes):
    body = client.get(f"{BASE}/local-live/status?conduit_profile=work").json()
    assert api.local_live_profile_for_key(body["api_key"]) == "work"
    new = client.post(f"{BASE}/local-live/key?conduit_profile=work").json()["api_key"]
    assert api.local_live_profile_for_key(new) == "work"
    assert api.local_live_profile_for_key(body["api_key"]) is None


def test_conduit_profile_call_takes_that_profiles_turns(client, hermes, server):
    headers = {"Authorization": f"Bearer {_key(hermes, 'work')}"}
    with client.websocket_connect(f"{BASE}/local-live/socket?conduit_profile=work") as ws:
        thread, result = _ask_in_background(client, headers, _completion("Turn on the lights"))
        assert json.loads(ws.receive_text())["text"] == "Turn on the lights"
        ws.send_text(json.dumps({"type": "conduit.turn.delta", "turn_id": "t1", "text": "Done."}))
        ws.send_text(json.dumps({"type": "conduit.turn.done", "turn_id": "t1"}))
        thread.join(10)
        ws.send_text("finish")
    assert result["response"].json()["choices"][0]["message"]["content"] == "Done."


def test_conduit_profile_is_refused_where_the_profile_turned_the_plugin_off(client, hermes, monkeypatch):
    dashboard = types.ModuleType("hermes_cli.web_server_dashboard")

    async def refused(name):
        assert name == api.PLUGIN_NAME
        return hermes.profile == "off"

    dashboard._plugin_api_request_refused = refused
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_dashboard", dashboard)
    assert client.get(f"{BASE}/local-live/status?conduit_profile=off").status_code == 404
    assert client.get(f"{BASE}/local-live/status?conduit_profile=work").status_code == 200
    with client.websocket_connect(f"{BASE}/speech-server/socket?conduit_profile=off") as ws:
        while (message := ws.receive())["type"] != "websocket.close":
            pass
    assert message["code"] == api.SPEECH_SERVER_CLOSE_REFUSED


def test_a_failing_consent_check_closes_the_socket_as_retryable(client, hermes, monkeypatch):
    async def failing(name):
        raise api.HTTPException(status_code=503, detail="Hermes can't read this profile")

    monkeypatch.setattr(api, "_plugin_refused_on", failing)
    with client.websocket_connect(f"{BASE}/speech-server/socket?conduit_profile=work") as ws:
        while (message := ws.receive())["type"] != "websocket.close":
            pass
    assert message["code"] == api.SPEECH_SERVER_CLOSE_FAILED


def test_conduit_profile_must_match_profile(client, hermes):
    response = client.get(f"{BASE}/local-live/status?profile=home&conduit_profile=work")
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"{BASE}/local-live/status?profile=work&conduit_profile=work").status_code == 200


def test_conduit_profile_takes_a_desktop_ticket_only_for_its_own_profile(hermes, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_profile_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(api, "_plugin_refused_on", lambda name: asyncio.sleep(0, False))
    state = types.SimpleNamespace(native_http_principal={"profile_id": str(tmp_path / "work")})
    own = types.SimpleNamespace(query_params={"conduit_profile": "work"}, state=state)
    assert asyncio.run(api._conduit_route_profile(own, None)) == "work"
    for name, principal in (("home", state.native_http_principal), ("work", {})):
        conn = types.SimpleNamespace(query_params={"conduit_profile": name},
                                     state=types.SimpleNamespace(native_http_principal=principal))
        with pytest.raises(api.HTTPException) as refused:
            asyncio.run(api._conduit_route_profile(conn, None))
        assert refused.value.status_code == 403
    conn = own
    without = types.SimpleNamespace(query_params={}, state=conn.state)
    assert asyncio.run(api._conduit_route_profile(without, "home")) == "home"


def test_dashboard_profiles_is_a_capability():
    assert "dashboard-profiles" in api.ROUTE_CAPABILITIES


def test_in_a_plugin_host_local_live_says_so(hermes, monkeypatch):
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_host_child", types.ModuleType("hermes_cli.plugin_host_child"))
    api._local_live_auth_state.update(ready=False, started=False)
    api._local_live_start_auth()
    assert api._local_live_auth_state["reason"] == api.LOCAL_LIVE_PLUGIN_HOST_REASON


@pytest.mark.parametrize("method,route", [
    ("get", "/local-live/status"), ("post", "/local-live/key"), ("post", "/watch-tools/grant"),
    ("post", "/watch-tools/revoke"), ("get", "/watch-audio/status"), ("post", "/sessions/takeover"),
    ("get", "/sessions/desktop-views"), ("get", "/predict"), ("post", "/predict"),
])
def test_every_dashboard_route_takes_conduit_profile(client, hermes, method, route):
    # Each route that needs this process reads ?conduit_profile=, so a
    # mismatch with ?profile= is refused before anything else runs.
    response = getattr(client, method)(f"{BASE}{route}?profile=home&conduit_profile=work",
                                       **({"json": {}} if method == "post" else {}))
    assert response.status_code == 400
    assert response.json()["detail"] == "profile and conduit_profile name different profiles"
