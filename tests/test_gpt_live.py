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
        "ok": True, "auth": "subscription", "source": "plugin", "voice": "cove", "briefing_applied": False, "greeting_applied": False,
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
    assert session["instructions"].endswith("\n\nSpeak French.\n\n" + api.GPT_LIVE_WAIT_FOR_USER)
    assert session["initial_items"] == history[-api.GPT_LIVE_MAX_HISTORY_ITEMS:]
    assert hermes.entered == ["coder"]


def test_session_voice_from_conduit_overrides_the_configured_voice(client, hermes, openai):
    hermes.config = {"voice": {"gpt_live": {"subscription_voice": "ember"}}}
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "voice": " Sol "}).json()
    assert body["voice"] == "sol", "The applied voice is echoed for Conduit"
    assert openai.calls[0][2]["session"]["audio"] == {"output": {"voice": "sol"}}
    # No voice (or an empty one) keeps the profile's.
    client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "voice": "  "})
    assert openai.calls[1][2]["session"]["audio"] == {"output": {"voice": "ember"}}


def test_session_briefing_joins_the_instructions_instead_of_context_appends(client, hermes, openai):
    hermes.config = {"voice": {"gpt_live": {"instructions": "Speak French."}}}
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "briefing": "  [Conduit rules] "}).json()
    assert body["briefing_applied"] is True
    assert openai.calls[0][2]["session"]["instructions"].endswith("Speak French.\n\n[Conduit rules]\n\n" + api.GPT_LIVE_WAIT_FOR_USER)


def test_session_greets_first_only_when_conduit_asks(client, hermes, openai):
    hermes.config = {"voice": {"gpt_live": {"instructions": "Speak French."}}}
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).json()
    assert body["greeting_applied"] is False
    assert openai.calls[0][2]["session"]["instructions"].endswith(api.GPT_LIVE_WAIT_FOR_USER)

    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "greeting": ""}).json()
    assert body["greeting_applied"] is True
    instructions = openai.calls[1][2]["session"]["instructions"]
    assert api.GPT_LIVE_WAIT_FOR_USER not in instructions
    assert instructions.endswith(api._greet_first("")) and "Say:" not in instructions

    client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "greeting": ' Hey Neal,\n I\'m "here". '})
    instructions = openai.calls[2][2]["session"]["instructions"]
    assert instructions.endswith(api._greet_first("Hey Neal, I'm 'here'."))
    assert 'Say: "Hey Neal, I\'m \'here\'."' in instructions, "the user's text stays on one line, inside the quotes"


def test_session_opens_a_call_hermes_placed_by_speaking_first(client, hermes, openai):
    body = client.post(f"{BASE}/gpt-live/session",
                       json={"sdp": OFFER, "briefing": "About this call", "call_opening": True}).json()
    assert body["greeting_applied"] is True, "Conduit must not send its own opening turn as well"
    instructions = openai.calls[0][2]["session"]["instructions"]
    assert instructions.endswith("About this call\n\n" + api.GPT_LIVE_CALL_OPENING)
    assert api.GPT_LIVE_WAIT_FOR_USER not in instructions

    # It wins over a greeting, and false keeps the usual policy.
    assert client.post(f"{BASE}/gpt-live/session",
                       json={"sdp": OFFER, "briefing": "About this call", "greeting": "Hi", "call_opening": True}).status_code == 200
    assert openai.calls[1][2]["session"]["instructions"].endswith(api.GPT_LIVE_CALL_OPENING)
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "call_opening": False}).json()
    assert body["greeting_applied"] is False
    assert openai.calls[2][2]["session"]["instructions"].endswith(api.GPT_LIVE_WAIT_FOR_USER)


def test_call_opening_without_a_briefing_never_guesses_why(client, hermes, openai):
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "call_opening": True}).json()
    assert body["greeting_applied"] is True
    instructions = openai.calls[0][2]["session"]["instructions"]
    assert instructions.endswith(api.GPT_LIVE_CALL_OPENING_UNBRIEFED)
    assert api.GPT_LIVE_CALL_OPENING not in instructions


@pytest.mark.parametrize("call_opening", [1, "true", []])
def test_session_rejects_a_bad_call_opening(client, openai, call_opening):
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "call_opening": call_opening}).status_code == 400
    assert openai.calls == []


def test_greeting_control_characters_become_spaces():
    assert api._clean_greeting("Hi\x00there\u200b!") == "Hi there !"


@pytest.mark.parametrize("greeting", [5, True, False, "x" * (api.GPT_LIVE_MAX_GREETING_CHARS + 1)])
def test_session_rejects_a_bad_greeting(client, openai, greeting):
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "greeting": greeting}).status_code == 400
    assert openai.calls == []


@pytest.mark.parametrize("briefing", [5, "x" * (api.GPT_LIVE_MAX_BRIEFING_CHARS + 1)])
def test_session_rejects_a_bad_briefing(client, openai, briefing):
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "briefing": briefing}).status_code == 400
    assert openai.calls == []


@pytest.mark.parametrize("voice", ["../x", "a b", 5, "x" * 40])
def test_session_rejects_a_malformed_voice(client, openai, voice):
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "voice": voice})
    assert response.status_code == 400
    assert openai.calls == []


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
                    "source": "plugin", "available": True, "reason": None,
                    "api": {"auth": "api", "model": "gpt-live-1", "voice": "marin", "enabled": False,
                            "daily_limit": api.GPT_LIVE_API_DAILY_CALLS, "available": False,
                            "reason": api.GPT_LIVE_API_OFF}}
    assert "conduit_api_billing" in body["api"]["reason"]
    assert hermes.refresh == [False]

    hermes.auth_error = True
    body = client.get(f"{BASE}/gpt-live/status").json()
    assert body["available"] is False and "hermes auth" in body["reason"]


def test_session_rate_limit(client, openai):
    for _ in range(api.GPT_LIVE_LIMIT):
        assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 200
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 429


# --- Stepping aside once Hermes ships hermes-agent#108940 ---------------------


def _upstream(monkeypatch, calls, error=None, built=None):
    voice_live = types.ModuleType("tools.voice_live")
    voice_live.LIVE_PERSONA = "Hermes persona."

    def build_session_config(history=None, *, live=None):
        calls.append(("build", history, live))
        return built(live) if built else {"model": "upstream-model"}

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
    assert body == {"ok": True, "auth": "subscription", "source": "hermes", "voice": "ember", "briefing_applied": False, "greeting_applied": False,
                    "session": {"id": "rtc_up"}, "transport": {"type": "webrtc", "sdp": ANSWER}}
    # Subscription is forced even when the desktop's own setting says api.
    assert calls[0] == ("build", [{"type": "message"}], {"auth": "subscription", "subscription_voice": "ember",
                                                             "instructions": api.GPT_LIVE_WAIT_FOR_USER})
    assert calls[1] == ("exchange", OFFER, {"model": "upstream-model"})
    assert openai.calls == []
    calls.clear()
    client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "voice": "sol"})
    assert calls[0][2]["subscription_voice"] == "sol"
    assert client.get(f"{BASE}/gpt-live/status").json()["source"] == "hermes"


def test_hermes_path_reports_the_briefing_only_when_its_config_carries_it(client, openai, monkeypatch):
    _upstream(monkeypatch, [])
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "briefing": "[Conduit rules]"}).json()
    assert body["briefing_applied"] is False, "Hermes dropped it, so Conduit must still send it"
    _upstream(monkeypatch, [], built=lambda live: {"session": {"instructions": live["instructions"],
                                                               "audio": {"output": {"voice": "Marin"}}}})
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "briefing": "[Conduit rules]"}).json()
    assert body["briefing_applied"] is True
    assert body["voice"] == "marin"


def test_hermes_path_reports_the_greeting_only_when_its_config_carries_it(client, openai, monkeypatch):
    _upstream(monkeypatch, [])
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "greeting": ""}).json()
    assert body["greeting_applied"] is False, "Hermes dropped it, so Conduit must still ask for it"
    calls = []
    _upstream(monkeypatch, calls, built=lambda live: {"session": {"instructions": live["instructions"]}})
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "greeting": "Hi"}).json()
    assert body["greeting_applied"] is True
    assert calls[0][2]["instructions"].endswith(api._greet_first("Hi"))
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).json()
    assert body["greeting_applied"] is False
    calls.clear()
    body = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "briefing": "About this call", "call_opening": True}).json()
    assert body["greeting_applied"] is True
    assert calls[0][0] == "build" and calls[0][2]["instructions"].endswith(api.GPT_LIVE_CALL_OPENING)


@pytest.mark.parametrize("audio", ["loud", {"output": "x"}, {"output": {"voice": "bad voice!"}}])
def test_an_oddly_shaped_hermes_voice_keeps_the_requested_one(client, openai, monkeypatch, audio):
    _upstream(monkeypatch, [], built=lambda live: {"audio": audio})
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "voice": "sol"})
    assert response.status_code == 200
    assert response.json()["voice"] == "sol"


@pytest.mark.parametrize("error, status", [(ValueError("SECRET-DETAIL bad history"), 502), (RuntimeError("SECRET-DETAIL rejected"), 502),
                                          (KeyError("SECRET-DETAIL"), 502), (TimeoutError("SECRET-DETAIL"), 502)])
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


def test_hermes_result_is_whitelisted_and_validated(client, hermes, openai, monkeypatch):
    voice_live = _upstream(monkeypatch, [])
    good = {"auth": "subscription", "session": {"id": "rtc_up", "token": "LEAK"},
            "transport": {"type": "webrtc", "sdp": ANSWER, "bearer": "LEAK"}, "debug": "LEAK"}
    voice_live._create_subscription_session = lambda sdp, config: good
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert "LEAK" not in response.text
    assert response.json()["session"] == {"id": "rtc_up"}
    for bad in (None, {}, {"session": {}, "transport": {"sdp": ANSWER}}, {"session": {"id": "x"}, "transport": {}}):
        voice_live._create_subscription_session = (lambda value: lambda sdp, config: value)(bad)
        assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 502


def test_a_raising_decoder_is_a_503(client, hermes, openai, monkeypatch):
    import hermes_cli.auth_constants as constants

    def boom(token):
        raise ValueError("malformed")

    monkeypatch.setattr(constants, "_decode_jwt_claims", boom)
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 503


def test_a_header_injecting_token_is_refused(client, hermes, openai):
    hermes.token = GOOD_TOKEN + "\r\nX-Evil: 1"
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 503
    assert openai.calls == []


def test_the_exchange_never_follows_redirects():
    request = api.urllib.request.Request("https://example.com")
    assert api._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.example") is None


@pytest.mark.parametrize("sdp, call_id", [("not-an-sdp", "rtc_up"), (ANSWER, "../../evil"), (ANSWER, "https://evil.example/x")])
def test_hermes_result_gets_the_same_sdp_and_call_id_checks(client, monkeypatch, sdp, call_id):
    voice_live = _upstream(monkeypatch, [])
    result = {"session": {"id": call_id}, "transport": {"type": "webrtc", "sdp": sdp}}
    voice_live._create_subscription_session = lambda offer, config: result
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 502


def test_an_unexpected_credentials_failure_keeps_the_no_fallback_message(client, hermes, openai, monkeypatch):
    import hermes_cli.auth_codex as auth_codex

    def boom(**kwargs):
        raise OSError("SECRET-DETAIL refresh failed")

    monkeypatch.setattr(auth_codex, "resolve_codex_runtime_credentials", boom)
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.status_code == 503
    assert "No API fallback" in response.json()["detail"] and "SECRET-DETAIL" not in response.text
    status = client.get(f"{BASE}/gpt-live/status").json()
    assert status["available"] is False and "SECRET-DETAIL" not in json.dumps(status)
    assert openai.calls == []


def test_a_config_read_failure_uses_the_defaults(client, hermes, monkeypatch):
    import hermes_cli.config as config

    def boom():
        raise OSError("disk")

    monkeypatch.setattr(config, "load_config", boom)
    assert client.get(f"{BASE}/gpt-live/status").json()["model"] == "gpt-live-1-codex"


def test_status_reason_omits_the_call_failure_sentence(client, hermes):
    hermes.auth_error = True
    reason = client.get(f"{BASE}/gpt-live/status").json()["reason"]
    assert "hermes auth" in reason and "No API fallback" not in reason


def test_an_unencodable_header_is_a_502_without_the_value(monkeypatch):
    class Boom:
        def open(self, request, timeout=None):
            raise UnicodeEncodeError("latin-1", "SECRET-DETAIL", 0, 1, "nope")

    monkeypatch.setattr(api, "_opener", Boom())
    with pytest.raises(api.TokenError) as raised:
        api._post_sdp(api.GPT_LIVE_URL, {}, {"sdp": OFFER})
    assert raised.value.status == 502
    assert "No API fallback" in str(raised.value) and "SECRET-DETAIL" not in str(raised.value)


# --- GPT-Live billed to the host's OpenAI API key ----------------------------

API_ANSWER = {"session": {"id": "sess_live1"}, "transport": {"type": "webrtc", "sdp": ANSWER}}


@pytest.fixture
def api_key(hermes, openai, monkeypatch):
    """The profile allows API billing, a key is set, and OpenAI answers like /v1/live/sessions."""
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-test")
    hermes.config = {"voice": {"gpt_live": {"conduit_api_billing": True}}}
    openai.reply.update(status=200, answer=json.dumps(API_ANSWER), location="")
    monkeypatch.setattr(api, "_gpt_live_api_daily", api._MintLimiter(api.GPT_LIVE_API_DAILY_CALLS, api.GPT_LIVE_DAY_S,
                                                               message=api._gpt_live_api_daily.message))
    return hermes


def test_a_request_without_billing_is_never_billed_to_the_api_key(client, api_key, openai):
    openai.reply.update(status=201, answer=ANSWER, location="/v1/realtime/calls/rtc_abc123")
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.json()["auth"] == "subscription"
    assert openai.calls[0][0] == api.GPT_LIVE_URL


def test_api_billing_uses_the_public_live_api_on_the_hosts_key(client, api_key, openai):
    history = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api", "history": history,
                                                           "voice": "cedar", "briefing": "Be brief."})
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "auth": "api", "source": "plugin", "model": "gpt-live-1", "voice": "cedar",
        "briefing_applied": True, "greeting_applied": False,
        "session": {"id": "sess_live1"}, "transport": {"type": "webrtc", "sdp": ANSWER},
    }
    url, headers, body = openai.calls[0]
    assert url == "https://api.openai.com/v1/live/sessions"
    assert headers == {"Authorization": "Bearer sk-live-test"}
    # The offer goes byte-exact: the vendor's parser needs its trailing CRLF.
    assert body["transport"] == {"type": "webrtc", "sdp": OFFER}
    session = body["session"]
    assert session["model"] == "gpt-live-1"
    assert session["audio"] == {"output": {"voice": "cedar"}}
    assert session["delegation"] == {"type": "client"}
    assert session["input"] == history and "initial_items" not in session
    assert "Be brief." in session["instructions"]
    assert "sk-live-test" not in response.text


def test_api_billing_reads_the_profiles_api_model_voice_and_base_url(client, api_key, openai):
    api_key.config["voice"]["gpt_live"].update(model="gpt-live-1-mini", voice="quartz", base_url="https://example.test/openai/v1/",
                                               api_key="sk-profile", subscription_voice="ember")
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.json()["voice"] == "quartz"
    url, headers, body = openai.calls[0]
    assert url == "https://example.test/openai/v1/live/sessions"
    assert headers["Authorization"] == "Bearer sk-profile"
    assert body["session"]["model"] == "gpt-live-1-mini"


@pytest.mark.parametrize("switch", [None, False, "true", 1, "yes"])
def test_api_billing_needs_the_profiles_own_switch(client, api_key, openai, switch):
    live = api_key.config["voice"]["gpt_live"]
    if switch is None:
        del live["conduit_api_billing"]
    else:
        live["conduit_api_billing"] = switch
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 403
    assert "conduit_api_billing" in response.json()["detail"]
    assert "subscription was not used" in response.json()["detail"]
    assert openai.calls == []


def test_api_billing_without_a_key_never_falls_back_to_the_subscription(client, api_key, openai, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]
    assert openai.calls == []


def test_a_failed_subscription_never_falls_back_to_the_api_key(client, api_key, openai):
    api_key.auth_error = True
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER})
    assert response.status_code == 503
    assert openai.calls == []


@pytest.mark.parametrize("status, answer", [
    (401, ""), (200, "not json"), (200, json.dumps({"session": {"id": "sess_1"}})),
    (200, json.dumps({"session": {"id": "bad\nid"}, "transport": {"type": "webrtc", "sdp": ANSWER}})),
    (200, json.dumps({"session": {"id": "x" * 257}, "transport": {"type": "webrtc", "sdp": ANSWER}})),
    (200, json.dumps({"session": {"id": "sess_1"}, "transport": {"type": "webrtc", "sdp": "nope"}})),
])
def test_api_billing_rejects_a_bad_answer(client, api_key, openai, status, answer):
    openai.reply.update(status=status, answer=answer)
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 502
    assert "subscription was not used" in response.json()["detail"]


def test_api_transport_failures_name_the_api_path(client, api_key, monkeypatch):
    def post(url, headers, body):
        raise api.TokenError(502, "GPT-Live connection failed. " + api.GPT_LIVE_NO_FALLBACK)
    monkeypatch.setattr(api, "_post_sdp", post)
    detail = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).json()["detail"]
    assert "No API fallback" not in detail and "subscription was not used" in detail


@pytest.mark.parametrize("billing", ["API", "both", 1, True, ""])
def test_an_unknown_billing_is_refused(client, api_key, openai, billing):
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": billing})
    assert response.status_code == 400
    assert openai.calls == []


def test_api_billing_has_a_daily_cap_that_a_missing_key_does_not_use_up(client, api_key, openai, monkeypatch):
    api_key.config["voice"]["gpt_live"]["conduit_api_daily_calls"] = 2
    monkeypatch.delenv("OPENAI_API_KEY")
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 503
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-test")
    for _ in range(2):
        assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 200
    capped = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert capped.status_code == 429 and "conduit_api_daily_calls" in capped.json()["detail"]
    assert len(openai.calls) == 2
    # The subscription path has no daily cap.
    openai.reply.update(status=201, answer=ANSWER, location="/v1/realtime/calls/rtc_abc123")
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER}).status_code == 200


def test_status_reports_the_api_path_on_its_own(client, api_key):
    body = client.get(f"{BASE}/gpt-live/status").json()
    assert body["auth"] == "subscription" and body["available"] is True
    assert body["api"] == {"auth": "api", "model": "gpt-live-1", "voice": "marin", "enabled": True,
                           "daily_limit": api.GPT_LIVE_API_DAILY_CALLS, "available": True, "reason": None,
                           "host": "api.openai.com"}
    assert "sk-live-test" not in json.dumps(body)


def test_status_api_path_without_a_key_says_so(client, api_key, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    body = client.get(f"{BASE}/gpt-live/status").json()["api"]
    assert body["available"] is False and "OPENAI_API_KEY" in body["reason"]
    assert "subscription was not used" not in body["reason"]


def test_a_custom_base_url_never_gets_the_hosts_general_key(client, api_key, openai):
    api_key.config["voice"]["gpt_live"]["base_url"] = "https://gateway.example.test/v1"
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 503
    assert "voice.gpt_live.api_key" in response.json()["detail"]
    assert openai.calls == []
    status = client.get(f"{BASE}/gpt-live/status").json()["api"]
    assert status["available"] is False and "gateway.example.test" in status["reason"]


@pytest.mark.parametrize("base_url", ["https://", "http://api.openai.com/v1", "https://api.openai.com/v1\r\nX: y", "ftp://x",
                                      "https://api.openai.com/v1?x=1", "https://api.openai.com/v1#f",
                                      "https://api.openai.com:99999/v1"])
def test_a_base_url_without_https_and_a_host_is_refused(client, api_key, openai, base_url):
    api_key.config["voice"]["gpt_live"].update(base_url=base_url, api_key="sk-profile")
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 503
    assert openai.calls == []
    status = client.get(f"{BASE}/gpt-live/status")
    assert status.status_code == 200 and status.json()["api"]["available"] is False


def test_a_call_openai_did_not_start_gives_its_daily_slot_back(client, api_key, openai):
    api_key.config["voice"]["gpt_live"]["conduit_api_daily_calls"] = 1
    openai.reply.update(status=401, answer="")
    for _ in range(3):
        assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 502
    openai.reply.update(status=200, answer=json.dumps(API_ANSWER))
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 200
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 429


def test_a_daily_limit_of_zero_says_api_calls_are_off(client, api_key, openai):
    api_key.config["voice"]["gpt_live"]["conduit_api_daily_calls"] = 0
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 403 and "is 0" in response.json()["detail"]
    assert "daily limit" not in response.json()["detail"]
    assert client.get(f"{BASE}/gpt-live/status").json()["api"]["available"] is False
    assert openai.calls == []


@pytest.mark.parametrize("status, code, words", [(401, 502, "rejected the API key"), (429, 429, "rate-limiting")])
def test_openai_key_and_quota_errors_are_told_apart(client, api_key, openai, status, code, words):
    openai.reply.update(status=status, answer="")
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == code
    assert words in response.json()["detail"] and "subscription was not used" in response.json()["detail"]


def test_an_answer_on_another_transport_is_refused(client, api_key, openai):
    openai.reply.update(answer=json.dumps({"session": {"id": "sess_1"}, "transport": {"type": "websocket", "sdp": ANSWER}}))
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 502


def test_a_broken_hermes_key_lookup_is_a_failure_not_the_environment(client, api_key, openai, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "tools.tool_backend_helpers":
            raise ImportError("no module named 'httpx'", name="httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 503 and "could not read the OpenAI key" in response.json()["detail"]
    assert openai.calls == []


def test_release_gives_back_the_failed_calls_own_slot():
    now = [0.0]
    limiter = api._MintLimiter(2, 100.0, clock=lambda: now[0], message="capped")
    early = limiter.acquire("p")
    now[0] = 90.0
    limiter.acquire("p")
    limiter.release("p", early)
    # The later call's slot is still counted: it keeps the window busy until 190, not 100.
    now[0] = 120.0
    limiter.acquire("p")
    with pytest.raises(api.TokenError):
        limiter.acquire("p")


def test_a_zero_limit_leaves_no_empty_bucket_behind():
    limiter = api._MintLimiter(2, 100.0, clock=lambda: 0.0, message="capped")
    with pytest.raises(api.TokenError):
        limiter.acquire("p", limit=0)
    limiter.acquire("other")
    only = limiter.acquire("p")
    limiter.release("p", only)
    limiter.acquire("other")  # an emptied bucket never trips the next acquire
    limiter.release("p", 12345.0)  # an unknown or aged-out mint is ignored


def test_an_unfamiliar_session_id_format_is_accepted(client, api_key, openai):
    openai.reply.update(answer=json.dumps({"session": {"id": "sess.2026:abc/1"}, "transport": {"type": "webrtc", "sdp": ANSWER}}))
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 200 and response.json()["session"] == {"id": "sess.2026:abc/1"}


def test_an_invalid_answer_after_a_2xx_keeps_its_daily_slot(client, api_key, openai):
    api_key.config["voice"]["gpt_live"]["conduit_api_daily_calls"] = 1
    openai.reply.update(status=200, answer="not json")
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 502
    openai.reply.update(answer=json.dumps(API_ANSWER))
    # OpenAI may have started (and billed) that session: the day's one call is used.
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 429


def test_the_shared_key_never_goes_to_a_non_standard_port(client, api_key, openai):
    api_key.config["voice"]["gpt_live"]["base_url"] = "https://api.openai.com:8443/v1"
    response = client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"})
    assert response.status_code == 503 and "voice.gpt_live.api_key" in response.json()["detail"]
    api_key.config["voice"]["gpt_live"]["base_url"] = "https://api.openai.com:443/v1"
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 200


def test_a_failing_env_lookup_is_a_reason_not_a_500(client, api_key, openai, monkeypatch):
    def boom(_key):
        raise RuntimeError("unscoped secret")
    monkeypatch.setattr(api, "_env_value", boom)
    status = client.get(f"{BASE}/gpt-live/status")
    assert status.status_code == 200 and status.json()["api"]["available"] is False
    assert client.post(f"{BASE}/gpt-live/session", json={"sdp": OFFER, "billing": "api"}).status_code == 503
