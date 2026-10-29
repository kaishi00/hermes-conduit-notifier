"""Locked mode (CONDUIT_LOCKED): the routes that change the host refuse."""

import importlib.util
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants

spec = importlib.util.spec_from_file_location("conduit_plugin_api_locked_mode", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

LOCKED_ROUTES = ["/self/update", "/self/restart", "/rooms/setup", "/pair", "/e2e", "/watch-audio/prepare",
                 "/workspace/save"]
# What those routes run, by module: none of it may run while locked.
WORK = {"self_update": ("start_self_update", "self_restart"), "linked_rooms": ("room_setup",),
        "pairing": ("pair_profile", "provision_e2e")}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


@pytest.fixture
def env(monkeypatch):
    dotenv = {}
    monkeypatch.delenv(api.core.LOCKED_ENV_VAR, raising=False)
    monkeypatch.setattr(api.gemini_qwen_live, "_env_value", dotenv.get)
    return dotenv


@pytest.mark.parametrize("where", ["environment", ".env"])
@pytest.mark.parametrize("route", LOCKED_ROUTES)
def test_a_locked_host_refuses_before_doing_anything(client, env, monkeypatch, route, where):
    called = []
    for module, names in WORK.items():
        for name in names:
            monkeypatch.setattr(getattr(api, module), name, lambda *a, _name=name, **k: called.append(_name))
    monkeypatch.setattr(api.watch_audio._watch_audio_runtime, "prepare", lambda *a, **k: called.append("prepare"))
    monkeypatch.setattr(api.core, "_read_json_body", lambda *a, **k: called.append("body"))
    if where == "environment":
        monkeypatch.setenv(api.core.LOCKED_ENV_VAR, "1")
    else:
        env[api.core.LOCKED_ENV_VAR] = "true"
    response = client.post(f"{BASE}{route}", json={})
    assert response.status_code == 403
    assert response.json() == {"detail": api.core.LOCKED_MESSAGE}
    assert response.json()["detail"].startswith("Locked mode is on for this host")
    assert response.headers["cache-control"] == "no-store"
    assert called == []


@pytest.mark.parametrize("value", [None, "", "0", "off", "no"])
def test_locked_mode_is_off_unless_turned_on(env, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(api.core.LOCKED_ENV_VAR, value)
        env[api.core.LOCKED_ENV_VAR] = value
    assert api.core.conduit_locked() is False


def test_an_unlocked_host_still_answers_those_routes(client, env, monkeypatch, tmp_path):
    monkeypatch.setattr(api.core, "_pairing_state_path", lambda: tmp_path / "s.json")
    # A pairing code that isn't one is turned away by the route itself.
    response = client.post(f"{BASE}/pair", json={"pairing_code": "nope"})
    assert response.status_code == 400


def test_reads_and_sealed_rotation_stay_open_when_locked(client, env, monkeypatch):
    monkeypatch.setenv(api.core.LOCKED_ENV_VAR, "1")
    monkeypatch.setattr(api.self_update, "self_update_status", lambda: None)
    assert client.get(f"{BASE}/self/update").status_code == 200
    # Rotation needs a request sealed with the current key, so it stays
    # open: Conduit rotates every 7 days on its own.
    assert client.post(f"{BASE}/e2e/rotate", json={}).status_code != 403
