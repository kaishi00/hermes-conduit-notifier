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


@pytest.mark.parametrize("voice", ["", "  "])
def test_blank_voice_uses_the_default(voice):
    assert api.qwen_live_settings(env(CONDUIT_QWEN_LIVE_VOICE=voice))["voice"] == "Tina"


@pytest.mark.parametrize("voice", ["Tina&x=1", "a" * 65, "<script>", "Ti na"])
def test_unbounded_voice_is_refused(voice):
    status = api.qwen_live_status(env(CONDUIT_QWEN_LIVE_VOICE=voice))
    assert status["available"] is False
    assert "CONDUIT_QWEN_LIVE_VOICE" in status["reason"]


def test_missing_key_uses_the_gemini_reason_code():
    status = api.qwen_live_status({"CONDUIT_QWEN_LIVE_WORKSPACE": "workspace1"}.get)
    assert status == {"provider": "qwen", "available": False, "reason": "no_api_key",
                      "model": "qwen3.8-omni-flash-realtime"}


def test_routes_name_unexpected_failures_and_stay_uncached(monkeypatch):
    async def scoped(profile, fn, executor=None):
        return fn()
    monkeypatch.setattr(api, "_run_scoped", scoped)
    def broken(**_):
        raise RuntimeError("host detail")
    monkeypatch.setattr(api, "qwen_live_status", lambda: broken())
    monkeypatch.setattr(api, "mint_qwen_live_token", broken)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        for method, path, route in [(client.get, "status", "status"), (client.post, "token", "token")]:
            response = method(f"/qwen-live/{path}")
            assert response.status_code == 500
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["detail"] == f"Qwen Live {route} failed on the host (RuntimeError)"
            assert "host detail" not in response.text


def test_route_keeps_no_store_on_a_profile_refusal(monkeypatch):
    async def scoped(profile, fn, executor=None):
        raise api.HTTPException(status_code=404, detail="Unknown profile")
    monkeypatch.setattr(api, "_run_scoped", scoped)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        for method, path in [(client.get, "status"), (client.post, "token")]:
            response = method(f"/qwen-live/{path}?profile=nope")
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"


def test_watch_qwen_answers_name_qwen(monkeypatch):
    from types import SimpleNamespace
    import threading
    monkeypatch.setattr(api, "_scoped_call", lambda profile, fn: fn())
    grant = SimpleNamespace(tools=(api.WATCH_QWEN_LIVE_TOKEN,), live_tokens=api.WATCH_LIVE_TOKENS_PER_GRANT,
                            lock=threading.Lock(), profile="work", limiter_key="work")
    assert api.run_watch_live_token(grant, provider="qwen")["detail"] == "This call has used all its Qwen Live tokens"
    grant.live_tokens = 0
    def broken(**_):
        raise RuntimeError("boom")
    failed = api.run_watch_live_token(grant, mint=broken, provider="qwen")
    assert failed["detail"] == "Minting a Qwen Live token failed on the host (RuntimeError)"
    assert grant.live_tokens == 0
