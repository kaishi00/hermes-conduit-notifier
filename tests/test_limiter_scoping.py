import contextlib
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from test_gemini_live import api

BASE = "/api/plugins/conduit_push"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


@pytest.fixture()
def hermes_scope(monkeypatch):
    """Hermes' profile scope: any profile named ghost* doesn't exist."""

    @contextlib.contextmanager
    def scope(profile):
        if profile and profile.startswith("ghost"):
            raise HTTPException(status_code=404, detail="Profile not found")
        yield

    fake = types.ModuleType("hermes_cli.web_server_profiles")
    fake._config_profile_scope = scope
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", fake)


def test_a_full_limiter_turns_new_keys_away():
    clock = [0.0]
    limiter = api._MintLimiter(5, 60, clock=lambda: clock[0], max_keys=2)
    limiter.acquire("coder")
    limiter.acquire("work")
    with pytest.raises(api.TokenError) as refused:
        limiter.acquire("third")
    assert refused.value.status == 429
    # Known keys keep their budget, and the dashboard's own profile always fits.
    limiter.acquire("coder")
    limiter.acquire("")
    assert set(limiter._mints) == {"coder", "work", ""}
    # Once a window passes the idle keys go, and new ones fit again.
    clock[0] = 61
    limiter.acquire("third")
    assert set(limiter._mints) == {"third"}


@pytest.mark.parametrize("method, path, limiter", [
    ("put", "/conduit-settings", "_conduit_settings_limiter"),
    ("get", "/cron/notifications", "_cron_limiter"),
    ("get", "/calls", "_calls_limiter"),
    ("post", "/health/key", "_health_key_limiter"),
    ("post", "/phone/key", "_phone_key_limiter"),
    ("post", "/location/key", "_location_key_limiter"),
    ("get", "/voice-tools", "_voice_tools_limiter"),
    ("post", "/e2e", "_e2e_limiter"),
    ("post", "/e2e/rotate", "_e2e_limiter"),
])
def test_an_unknown_profile_gets_no_rate_limit_bucket(client, hermes_scope, monkeypatch, method, path, limiter):
    fresh = api._MintLimiter(1000, 60)
    monkeypatch.setattr(api, limiter, fresh)
    for n in range(5):
        response = getattr(client, method)(f"{BASE}{path}?profile=ghost{n}", **({} if method == "get" else {"json": {}}))
        assert response.status_code == 404, response.text
        assert response.headers["cache-control"] == "no-store"
    assert fresh._mints == {}


def test_a_hung_gpt_live_status_never_wedges_later_checks(client, hermes_scope, monkeypatch):
    release = threading.Event()
    inside = threading.Semaphore(0)
    hung = {"left": 2}
    hung_lock = threading.Lock()

    def status():
        with hung_lock:
            hangs = hung["left"] > 0
            hung["left"] -= hangs
        if hangs:
            inside.release()
            release.wait(10)
        return {"available": True}

    monkeypatch.setattr(api, "gpt_live_status", status)
    # Long enough that each read is running (holding its slot) when it times out.
    monkeypatch.setattr(api, "GPT_LIVE_REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(api, "_gpt_live_status_executor", ThreadPoolExecutor(max_workers=2))
    monkeypatch.setattr(api, "_gpt_live_status_slots", threading.BoundedSemaphore(2))
    try:
        assert client.get(f"{BASE}/gpt-live/status").status_code == 504
        assert client.get(f"{BASE}/gpt-live/status").status_code == 504
        assert inside.acquire(timeout=5) and inside.acquire(timeout=5), "both reads hold a worker"
        # Both workers are still held: turned away at once, not queued.
        busy = client.get(f"{BASE}/gpt-live/status")
        assert busy.status_code == 503 and busy.headers["cache-control"] == "no-store"
    finally:
        release.set()
        api._gpt_live_status_executor.shutdown(wait=True)
    recovered = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(api, "_gpt_live_status_executor", recovered)
    try:
        assert client.get(f"{BASE}/gpt-live/status").json() == {"ok": True, "available": True}
    finally:
        recovered.shutdown(wait=True)


def test_a_hung_local_live_key_rotation_times_out(client, hermes_scope, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(api, "_local_live_start_auth", lambda: None)
    monkeypatch.setattr(api, "_local_live_auth_state", {"ready": True})
    monkeypatch.setattr(api, "_local_live_profile_can_carry_key", lambda profile: True)
    monkeypatch.setattr(api, "local_live_key", lambda profile, rotate=False: release.wait(10) and "key")
    monkeypatch.setattr(api, "SPEECH_SERVER_SETUP_TIMEOUT_S", 0.2)
    pool = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(api, "_speech_server_executor", pool)
    try:
        response = client.post(f"{BASE}/local-live/key")
        assert response.status_code == 504 and response.headers["cache-control"] == "no-store"
        release.set()
        # An unknown profile is Hermes' own 404, not a host failure.
        assert client.post(f"{BASE}/local-live/key?profile=ghost").status_code == 404
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_made_up_local_live_key_profiles_never_fill_the_check_limiter():
    # The total budget bounds its live keys, so the key cap never turns a
    # real profile's first check away.
    assert api._local_live_key_check_limiter.max_keys > api.LOCAL_LIVE_KEY_CHECK_TOTAL_LIMIT


def test_a_broken_scope_is_a_named_host_failure(client, monkeypatch):
    def broken(profile):
        raise RuntimeError("scope exploded")

    monkeypatch.setattr(api, "_profile_scope", broken)
    response = client.post(f"{BASE}/health/key?profile=coder", json={})
    assert response.status_code == 500 and response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == "Conduit profile check failed on the host (RuntimeError)"


def test_voice_shared_reads_the_calling_location_device_alone():
    from test_location import tool

    asked = []

    class Store:
        def snapshot(self, device_id=None):
            asked.append(device_id)
            return {"device": "iphone"} if device_id in (None, "iphone") else None

    assert tool.voice_shared(Store()) is True
    assert tool.voice_shared(Store(), "iphone") is True
    assert tool.voice_shared(Store(), "ipad") is False
    assert asked == [None, "iphone", "ipad"]
