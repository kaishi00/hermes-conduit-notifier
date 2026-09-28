import base64
import contextlib
import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\n"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_gpt_live", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def _jwt(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{part}.s"


GOOD_TOKEN = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}})


class AuthError(RuntimeError):
    pass


@pytest.fixture
def hermes(monkeypatch):
    """Stub Hermes: config, Codex sign-in, profile scope; no tools.voice_live (pre-#108940)."""
    state = types.SimpleNamespace(config={}, token=GOOD_TOKEN, auth_error=False, refresh=[], entered=[])

    def resolve(*, refresh_if_expiring=True, **_):
        state.refresh.append(refresh_if_expiring)
        if state.auth_error:
            raise AuthError("signed out")
        return {"api_key": state.token}

    def decode(token):
        try:
            return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        except Exception:
            return {}

    @contextlib.contextmanager
    def scope(profile):
        state.entered.append(profile)
        yield

    auth_codex = types.ModuleType("hermes_cli.auth_codex")
    auth_codex.resolve_codex_runtime_credentials = resolve
    auth_constants = types.ModuleType("hermes_cli.auth_constants")
    auth_constants.AuthError = AuthError
    auth_constants._decode_jwt_claims = decode
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: state.config
    profiles = types.ModuleType("hermes_cli.web_server_profiles")
    profiles._config_profile_scope = scope
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.auth_codex", auth_codex)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth_constants", auth_constants)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", profiles)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.voice_live", None)
    monkeypatch.setattr(api, "_gpt_live_limiter", api._MintLimiter(api.GPT_LIVE_LIMIT, api.GPT_LIVE_WINDOW_S))
    return state


@pytest.fixture
def openai(monkeypatch):
    calls = []
    reply = {"status": 201, "answer": ANSWER, "location": "/v1/realtime/calls/rtc_abc123"}

    def post(url, headers, body):
        calls.append((url, headers, body))
        return reply["status"], reply["answer"], reply["location"]

    monkeypatch.setattr(api, "_post_sdp", post)
    return types.SimpleNamespace(calls=calls, reply=reply)


@pytest.fixture
def client(hermes, openai):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_session_uses_the_codex_sign_in_and_keeps_it_on_the_host(client, openai):
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "ok": True, "auth": "subscription", "source": "plugin",
        "session": {"id": "rtc_abc123"}, "transport": {"type": "webrtc", "sdp": ANSWER},
    }
    url, headers, body = openai.calls[0]
    assert url == api.GPT_LIVE_URL
    assert headers == {"Authorization": f"Bearer {GOOD_TOKEN}", "ChatGPT-Account-Id": "acct-1",
                       "OpenAI-Alpha": "quicksilver=v2"}
    assert body["sdp"] == OFFER
    assert body["session"]["model"] == "gpt-live-1-codex"
    assert body["session"]["audio"] == {"output": {"voice": "cove"}}
    assert body["session"]["delegation"] == {"type": "client"}
    assert "initial_items" not in body["session"]
    assert GOOD_TOKEN not in response.text and "acct-1" not in response.text


def test_session_reads_the_profile_config_and_seeds_history(client, hermes, openai):
    hermes.config = {"voice": {"gpt_live": {"subscription_model": "gpt-live-2-codex",
                                            "subscription_voice": "ember", "instructions": "Speak French.",
                                            "auth": "api"}}}
    history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"{i}"}]}
               for i in range(api.GPT_LIVE_MAX_HISTORY_ITEMS + 5)]
    client.post(f"{BASE}/gpt-live/session?profile=coder", json={"sdp": OFFER, "history": history})
    session = openai.calls[0][2]["session"]
    assert session["model"] == "gpt-live-2-codex"
    assert session["audio"] == {"output": {"voice": "ember"}}
    assert session["instructions"].endswith("\n\nSpeak French.")
    assert session["initial_items"] == history[-api.GPT_LIVE_MAX_HISTORY_ITEMS:]
    assert hermes.entered == ["coder"]


@pytest.mark.parametrize("body, status", [
    ({}, 400),
    ({"sdp": "not sdp"}, 400),
    ({"sdp": OFFER, "history": "nope"}, 400),
    ({"sdp": OFFER, "history": ["nope"]}, 400),
])
def test_bad_requests_never_reach_openai(client, openai, body, status):
    assert client.post(f"{BASE}/gpt-live/session", json=body).status_code == status
    assert openai.calls == []


def test_oversized_body_is_refused(client, openai):
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": "v=0" + "x" * api.GPT_LIVE_MAX_BODY_BYTES})
    assert response.status_code == 413
    assert openai.calls == []


def test_signed_out_host_is_a_503_with_no_fallback(client, hermes, openai):
    hermes.auth_error = True
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.status_code == 503
    assert "hermes auth" in response.json()["detail"]
    assert "No API fallback" in response.json()["detail"]
    assert openai.calls == []


def test_token_without_an_account_is_a_503(client, hermes, openai):
    hermes.token = _jwt({"sub": "x"})
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 503
    assert openai.calls == []


@pytest.mark.parametrize("status, answer, location", [
    (403, "", ""),
    (201, "<html>", "/v1/realtime/calls/rtc_abc"),
    (201, ANSWER, "/v1/realtime/calls/../../evil"),
])
def test_rejected_or_malformed_answers_are_a_502(client, openai, status, answer, location):
    openai.reply.update(status=status, answer=answer, location=location)
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.status_code == 502
    assert "No API fallback" in response.json()["detail"]


def test_status_checks_credentials_without_refreshing(client, hermes):
    body = client.get(f"{BASE}/gpt-live/status").json()
    assert body == {"ok": True, "auth": "subscription", "model": "gpt-live-1-codex", "voice": "cove",
                    "source": "plugin", "available": True, "reason": None}
    assert hermes.refresh == [False]

    hermes.auth_error = True
    body = client.get(f"{BASE}/gpt-live/status").json()
    assert body["available"] is False and "hermes auth" in body["reason"]


def test_session_rate_limit(client, openai):
    for _ in range(api.GPT_LIVE_LIMIT):
        assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 200
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 429


# --- Stepping aside once Hermes ships hermes-agent#108940 ---------------------


def _upstream(monkeypatch, calls, error=None):
    voice_live = types.ModuleType("tools.voice_live")
    voice_live.LIVE_PERSONA = "Hermes persona."

    def build_session_config(history=None, *, live=None):
        calls.append(("build", history, live))
        return {"model": "upstream-model"}

    def _create_subscription_session(sdp_offer, config):
        calls.append(("exchange", sdp_offer, config))
        if error:
            raise error
        return {"auth": "subscription", "session": {"id": "rtc_up"}, "transport": {"type": "webrtc", "sdp": ANSWER}}

    voice_live.build_session_config = build_session_config
    voice_live._create_subscription_session = _create_subscription_session
    monkeypatch.setitem(sys.modules, "tools.voice_live", voice_live)
    sys.modules["tools"].voice_live = voice_live
    return voice_live


def test_hands_off_to_hermes_when_it_ships_the_exchange(client, hermes, openai, monkeypatch):
    calls = []
    _upstream(monkeypatch, calls)
    hermes.config = {"voice": {"gpt_live": {"auth": "api", "subscription_voice": "ember"}}}
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "history": [{"type": "message"}]}).json()
    assert body == {"ok": True, "auth": "subscription", "source": "hermes",
                    "session": {"id": "rtc_up"}, "transport": {"type": "webrtc", "sdp": ANSWER}}
    # Subscription is forced even when the desktop's own setting says api.
    assert calls[0] == ("build", [{"type": "message"}], {"auth": "subscription", "subscription_voice": "ember"})
    assert calls[1] == ("exchange", OFFER, {"model": "upstream-model"})
    assert openai.calls == []
    assert client.get(f"{BASE}/gpt-live/status").json()["source"] == "hermes"


@pytest.mark.parametrize("error, status", [(ValueError("SECRET-DETAIL sign in"), 503), (RuntimeError("SECRET-DETAIL rejected"), 502)])
def test_hermes_failures_keep_their_words_and_never_fall_back(client, openai, monkeypatch, error, status):
    _upstream(monkeypatch, [], error=error)
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.status_code == status
    # Hermes' wording (which could quote a provider response) stays in the log.
    assert "SECRET-DETAIL" not in response.text
    assert "No API fallback" in response.json()["detail"]
    assert openai.calls == []


def test_a_differently_shaped_hermes_exchange_is_not_used(client, openai, monkeypatch):
    voice_live = _upstream(monkeypatch, [])
    voice_live.build_session_config = lambda history=None: {}
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).json()["source"] == "plugin"
    assert len(openai.calls) == 1


def test_persona_matches_hermes(monkeypatch):
    # Kept in step with tools/voice_live.py so the plugin path sounds like Hermes' own.
    assert api.GPT_LIVE_PERSONA.startswith("You are Hermes, a calm and friendly voice assistant.")
    assert "Delegation policy:" in api.GPT_LIVE_PERSONA


def test_whitespace_config_falls_back_to_the_defaults(client, hermes, openai):
    hermes.config = {"voice": {"gpt_live": {"subscription_model": "   ", "subscription_voice": " "}}}
    client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    session = openai.calls[0][2]["session"]
    assert session["model"] == "gpt-live-1-codex"
    assert session["audio"] == {"output": {"voice": "cove"}}


@pytest.mark.parametrize("config", [None, [], {"voice": None}, {"voice": {"gpt_live": "x"}}])
def test_malformed_config_uses_the_defaults(client, hermes, config):
    hermes.config = config
    assert client.get(f"{BASE}/gpt-live/status").json()["model"] == "gpt-live-1-codex"


def test_a_stuck_exchange_is_a_504(client, monkeypatch):
    import time
    monkeypatch.setattr(api, "GPT_LIVE_REQUEST_TIMEOUT_S", 0.05)
    monkeypatch.setattr(api, "_plugin_gpt_live_session", lambda *a, **k: time.sleep(0.5))
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 504
    monkeypatch.setattr(api, "gpt_live_status", lambda: time.sleep(0.5))
    assert client.get(f"{BASE}/gpt-live/status").status_code == 504


def test_oversized_answer_is_refused():
    class Big:
        status = 201
        headers = {"Location": "/v1/realtime/calls/rtc_abc"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return b"v=0" + b"x" * size

    class Opener:
        def open(self, request, timeout=None):
            return Big()

    original = api._opener
    api._opener = Opener()
    try:
        with pytest.raises(api.TokenError) as raised:
            api._post_sdp(api.GPT_LIVE_URL, {}, {"sdp": OFFER})
    finally:
        api._opener = original
    assert raised.value.status == 502


@pytest.mark.parametrize("claims", [None, [], "x"])
def test_a_decoder_returning_a_non_dict_is_a_503(client, hermes, openai, monkeypatch, claims):
    import hermes_cli.auth_constants as constants
    monkeypatch.setattr(constants, "_decode_jwt_claims", lambda token: claims)
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 503
    assert openai.calls == []
