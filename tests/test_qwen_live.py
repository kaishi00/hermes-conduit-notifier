"""Direct Qwen credentials: profile-scoped, short-lived, never audio relay."""
import importlib.util
import json
import time
import urllib.error
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("qwen_test_api", Path(__file__).parents[1] / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def env(**values):
    return {"DASHSCOPE_API_KEY": "sk-permanent", "CONDUIT_QWEN_LIVE_WORKSPACE": "workspace1", **values}.get


def test_defaults_and_regions():
    settings = api.qwen_live_settings(env())
    assert settings["model"] == "qwen3.8-omni-flash-realtime"
    assert settings["websocket_url"].startswith("wss://workspace1.cn-beijing.maas.aliyuncs.com/")
    assert "expire_in_seconds=120" in settings["token_url"]
    singapore = api.qwen_live_settings(env(CONDUIT_QWEN_LIVE_REGION="ap-southeast-1"))
    assert "dashscope-intl.aliyuncs.com" in singapore["token_url"]
    assert "workspace1.ap-southeast-1.maas.aliyuncs.com" in singapore["websocket_url"]


@pytest.mark.parametrize("values", [
    {"CONDUIT_QWEN_LIVE_WORKSPACE": ""}, {"CONDUIT_QWEN_LIVE_WORKSPACE": "evil.com/path"},
    {"CONDUIT_QWEN_LIVE_REGION": "https://evil.com"}, {"CONDUIT_QWEN_LIVE_MODEL": "gemini"},
])
def test_invalid_settings_are_not_available(values):
    assert api.qwen_live_status(env(**values))["available"] is False


def test_mint_uses_bearer_but_returns_only_temporary_key():
    requests = []
    def post(request):
        requests.append(request)
        return {"token": "st-temporary", "expires_at": time.time() + 120}
    result = api.mint_qwen_live_token(env(), post=post)
    assert requests[0].get_header("Authorization") == "Bearer sk-permanent"
    assert result["token"] == "st-temporary"
    assert result["provider"] == "qwen"
    assert "sk-permanent" not in json.dumps(result)
    assert "access_token" not in result["websocket_url"]


@pytest.mark.parametrize("result", [{}, {"token": "sk-permanent", "expires_at": time.time()+120},
                                     {"token": "st-t", "expires_at": 1},
                                     {"token": "st-t", "expires_at": float("nan")}])
def test_bad_mint_never_falls_back_to_permanent_key(result):
    with pytest.raises(api.TokenError):
        api.mint_qwen_live_token(env(), post=lambda _: result)


def test_provider_failure_does_not_echo_body_or_secret():
    def post(_):
        raise urllib.error.HTTPError("https://example.com?sk-secret", 401, "sk-secret", {}, None)
    with pytest.raises(api.TokenError) as failure:
        api.mint_qwen_live_token(env(), post=post)
    assert "sk-secret" not in str(failure.value)


def test_routes_are_profile_scoped_and_not_cached(monkeypatch):
    scopes = []
    async def scoped(profile, fn, executor=None):
        scopes.append(profile)
        return fn()
    monkeypatch.setattr(api, "_run_scoped", scoped)
    monkeypatch.setattr(api, "qwen_live_status", lambda: {"available": True, "provider": "qwen"})
    monkeypatch.setattr(api, "mint_qwen_live_token", lambda **_: {"token": "st-test"})
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    with TestClient(app) as client:
        for method, path in [(client.get, "status"), (client.post, "token")]:
            response = method(f"/api/plugins/conduit_push/qwen-live/{path}?profile=work")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
    assert scopes == ["work", "work"]


def test_rate_limit_is_applied_before_network(monkeypatch):
    class Refused:
        def acquire(self, key):
            assert key == "work"
            raise api.TokenError(429, "Too many requests")
    monkeypatch.setattr(api, "_qwen_mint_limiter", Refused())
    with pytest.raises(api.TokenError) as exc:
        api.mint_qwen_live_token(env(), post=lambda _: pytest.fail("network called"), limiter_key="work")
    assert exc.value.status == 429


def test_gemini_and_qwen_routes_are_independent(monkeypatch):
    async def scoped(profile, fn, executor=None):
        assert profile == "work"
        return fn()
    monkeypatch.setattr(api, "_run_scoped", scoped)
    monkeypatch.setattr(api, "gemini_live_status", lambda: {"available": True, "model": "gemini-test"})
    monkeypatch.setattr(api, "qwen_live_status", lambda: {"available": True, "model": "qwen-test"})
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        assert client.get("/gemini-live/status?profile=work").json()["model"] == "gemini-test"
        assert client.get("/qwen-live/status?profile=work").json()["model"] == "qwen-test"
        assert client.get("/direct-live/status").status_code == 404


def test_watch_token_tools_cannot_switch_provider(monkeypatch):
    from types import SimpleNamespace
    import threading
    monkeypatch.setattr(api, "_scoped_call", lambda profile, fn: fn())
    monkeypatch.setattr(api, "mint_gemini_live_token", lambda **kwargs: {"token": "google-only"})
    monkeypatch.setattr(api, "mint_qwen_live_token", lambda **kwargs: {"token": "st-qwen-only"})
    def grant(tool):
        return SimpleNamespace(tools=(tool,), live_tokens=0, lock=threading.Lock(), profile="work", limiter_key="work")
    gemini = grant(api.WATCH_LIVE_TOKEN)
    qwen = grant(api.WATCH_QWEN_LIVE_TOKEN)
    assert api.run_watch_live_token(gemini)["token"] == "google-only"
    assert api.run_watch_live_token(qwen, provider="qwen")["token"] == "st-qwen-only"
    assert api.run_watch_live_token(gemini, provider="qwen")["status"] == 403
    assert api.run_watch_live_token(qwen, provider="gemini")["status"] == 403
