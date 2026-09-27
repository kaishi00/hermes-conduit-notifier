import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_api():
    # The dashboard imports plugin_api.py by file path, not as a package.
    spec = importlib.util.spec_from_file_location("conduit_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def env(**values):
    return lambda key: values.get(key)


def test_manifest_mounts_the_api_under_the_plugin_name():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "conduit_push"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["tab"]["hidden"] is True
    assert (ROOT / "dashboard" / manifest["entry"]).exists()


def test_status_reports_missing_key_without_calling_google():
    assert api.gemini_live_status(env()) == {"available": False, "reason": "no_api_key", "model": "gemini-3.8-live"}


def test_status_accepts_google_api_key_and_model_override():
    status = api.gemini_live_status(env(GOOGLE_API_KEY="g-key", CONDUIT_GEMINI_LIVE_MODEL="models/gemini-4-live"))
    assert status == {"available": True, "model": "gemini-4-live"}


def test_gemini_key_wins_over_google_key():
    assert api.resolve_api_key(env(GEMINI_API_KEY=" gem ", GOOGLE_API_KEY="goog")) == "gem"


def test_token_is_single_use_model_locked_and_short_lived():
    calls = []

    def post(url, key, body):
        calls.append((url, key, body))
        return {"name": "auth_tokens/abc123"}

    result = api.mint_gemini_live_token(env(GEMINI_API_KEY="secret"), post, now=NOW)

    url, key, body = calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
    assert key == "secret"
    assert body == {
        "uses": 1,
        "expireTime": "2026-09-27T12:30:00Z",
        "newSessionExpireTime": "2026-09-27T12:01:00Z",
        "bidiGenerateContentSetup": {"model": "models/gemini-3.8-live"},
        "fieldMask": "model",
    }
    assert result == {
        "token": "auth_tokens/abc123",
        "expires_at": "2026-09-27T12:30:00Z",
        "new_session_expires_at": "2026-09-27T12:01:00Z",
        "model": "gemini-3.8-live",
        "websocket_url": "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained",
    }
    assert "secret" not in json.dumps(result)


def test_token_without_key_is_a_503():
    with pytest.raises(api.TokenError) as raised:
        api.mint_gemini_live_token(env(), lambda *_: pytest.fail("must not call Google"), now=NOW)
    assert raised.value.status == 503


def test_token_response_without_a_name_is_a_502():
    with pytest.raises(api.TokenError) as raised:
        api.mint_gemini_live_token(env(GEMINI_API_KEY="secret"), lambda *_: {"error": "nope"}, now=NOW)
    assert raised.value.status == 502


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "_env_value", lambda key: {"GEMINI_API_KEY": "secret"}.get(key))
    monkeypatch.setattr(api, "_post_json", lambda url, key, body: {"name": "auth_tokens/xyz"})
    monkeypatch.setattr(api, "_mint_limiter", api._MintLimiter(api.MINT_LIMIT, api.MINT_WINDOW_S))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    return TestClient(app)


def test_routes_return_status_and_token(client):
    status = client.get("/api/plugins/conduit_push/gemini-live/status").json()
    assert status == {"ok": True, "available": True, "model": "gemini-3.8-live"}

    token = client.post("/api/plugins/conduit_push/gemini-live/token").json()
    assert token["ok"] is True
    assert token["token"] == "auth_tokens/xyz"
    assert "secret" not in json.dumps(token)


def test_token_response_is_not_cacheable(client):
    response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert response.headers["cache-control"] == "no-store"


def test_requested_profile_fails_closed_when_it_cannot_be_scoped(client):
    # No hermes_cli in the test environment, so profile scoping is unavailable:
    # the request must not fall back to the default profile's key.
    for method, path in (("post", "token"), ("get", "status")):
        response = getattr(client, method)(f"/api/plugins/conduit_push/gemini-live/{path}?profile=coder")
        assert response.status_code == 503


def test_token_route_rate_limits_per_profile(client, monkeypatch):
    monkeypatch.setattr(api, "_mint_limiter", api._MintLimiter(2, 60.0))
    codes = [client.post("/api/plugins/conduit_push/gemini-live/token").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_mint_limiter_frees_slots_after_the_window():
    now = [0.0]
    limiter = api._MintLimiter(1, 60.0, clock=lambda: now[0])
    limiter.acquire("default")
    with pytest.raises(api.TokenError) as raised:
        limiter.acquire("default")
    assert raised.value.status == 429
    limiter.acquire("coder")
    now[0] = 60.0
    limiter.acquire("default")
    assert list(limiter._mints) == ["default"]


def test_requests_that_never_reach_google_do_not_use_the_mint_budget(client, monkeypatch):
    monkeypatch.setattr(api, "_mint_limiter", api._MintLimiter(1, 60.0))
    for _ in range(3):
        assert client.post("/api/plugins/conduit_push/gemini-live/token?profile=nope").status_code == 503
    assert api._mint_limiter._mints == {}
    assert client.post("/api/plugins/conduit_push/gemini-live/token").status_code == 200


def test_google_http_error_becomes_a_502_without_the_key(monkeypatch):
    import io
    import urllib.error

    def fail(request, timeout):
        body = io.BytesIO(json.dumps({"error": {"message": "API key not valid"}}).encode())
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(api._opener, "open", fail)
    with pytest.raises(api.TokenError) as raised:
        api._post_json(api.token_url("v1alpha"), "secret", {})
    assert raised.value.status == 502
    assert "API key not valid" in str(raised.value)
    assert "secret" not in str(raised.value)


def test_redirects_are_not_followed():
    assert api._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example") is None


def test_token_route_maps_failures_to_http_status(client, monkeypatch):
    monkeypatch.setattr(api, "_env_value", lambda key: None)
    response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_api_version_override_moves_both_urls():
    calls = []
    result = api.mint_gemini_live_token(
        env(GEMINI_API_KEY="secret", CONDUIT_GEMINI_LIVE_API_VERSION="v1beta"),
        lambda url, key, body: calls.append(url) or {"name": "auth_tokens/t"}, now=NOW)
    assert calls == ["https://generativelanguage.googleapis.com/v1beta/auth_tokens"]
    assert ".v1beta.GenerativeService." in result["websocket_url"]


def test_unknown_api_version_falls_back_to_v1alpha():
    assert api.resolve_api_version(env(CONDUIT_GEMINI_LIVE_API_VERSION="../evil")) == "v1alpha"


def test_token_reports_the_window_google_granted():
    granted = {"name": "auth_tokens/t", "expireTime": "2026-09-27T12:20:00Z",
               "newSessionExpireTime": "2026-09-27T12:00:30Z"}
    result = api.mint_gemini_live_token(env(GEMINI_API_KEY="secret"), lambda *_: granted, now=NOW)
    assert result["expires_at"] == "2026-09-27T12:20:00Z"
    assert result["new_session_expires_at"] == "2026-09-27T12:00:30Z"


def test_network_errors_return_a_generic_message(monkeypatch):
    import urllib.error

    def fail(request, timeout):
        raise urllib.error.URLError("proxy.internal:3128 refused")

    monkeypatch.setattr(api._opener, "open", fail)
    with pytest.raises(api.TokenError) as raised:
        api._post_json(api.token_url("v1alpha"), "secret", {})
    assert raised.value.status == 502
    assert str(raised.value) == "Could not reach Google"


def test_requests_without_a_profile_still_enter_hermes_scope(client, monkeypatch):
    # Once the dashboard hosts several profiles, an unscoped secret read raises
    # (UnscopedSecretError), so the no-profile path must enter the scope too.
    import contextlib
    import sys
    import types

    entered = []

    @contextlib.contextmanager
    def scope(profile):
        entered.append(profile)
        yield

    fake = types.ModuleType("hermes_cli.web_server_profiles")
    fake._config_profile_scope = scope
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", fake)

    assert client.get("/api/plugins/conduit_push/gemini-live/status").status_code == 200
    assert client.post("/api/plugins/conduit_push/gemini-live/token").status_code == 200
    assert client.get("/api/plugins/conduit_push/gemini-live/status?profile=coder").status_code == 200
    assert entered == [None, None, "coder"]


def test_unexpected_host_errors_name_the_failure(client, monkeypatch):
    def boom(key):
        raise RuntimeError("secret scope exploded at /home/eric")

    monkeypatch.setattr(api, "_env_value", boom)
    response = client.get("/api/plugins/conduit_push/gemini-live/status")
    assert response.status_code == 500
    assert response.json()["detail"] == "Gemini Live status failed on the host (RuntimeError)"
