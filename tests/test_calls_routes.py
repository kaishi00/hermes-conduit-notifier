import contextlib
import importlib.util
import json
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

_spec = importlib.util.spec_from_file_location("conduit_plugin_api_calls", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_pairing_state_path", lambda: tmp_path / "conduit-push.json")
    monkeypatch.setattr(api, "_profile_scope", lambda profile: contextlib.nullcontext())
    api._calls_limiter._mints.clear()
    return tmp_path


@pytest.fixture()
def paired(home):
    (home / "conduit-push.json").write_text(json.dumps({"credential": "c", "installation_id": "i", "gateway_id": "g"}))
    return home


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _turn_calls_on(http):
    assert http.put(f"{BASE}/calls", json={"settings": {"enabled": True}}).status_code == 200


def test_status_reports_the_defaults_and_their_bounds(paired):
    response = _http().get(f"{BASE}/calls")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "ok": True,
        "paired": True,
        "settings": {"enabled": False, "when_asked": True, "decides": False, "alerts": False,
                     "min_gap_s": 120, "per_hour": 6, "per_day": 20},
        "bounds": {
            "min_gap_s": {"min": 30, "max": 3600},
            "per_hour": {"min": 1, "max": 30},
            "per_day": {"min": 1, "max": 60},
        },
        "watches": 0,
    }


def test_status_says_when_the_profile_is_not_paired(home):
    assert _http().get(f"{BASE}/calls").json()["paired"] is False


def test_settings_change_and_persist(paired):
    http = _http()
    response = http.put(f"{BASE}/calls", json={"settings": {"enabled": True, "per_day": 40}})
    assert response.status_code == 200
    assert response.json()["settings"]["per_day"] == 40
    assert http.get(f"{BASE}/calls").json()["settings"]["enabled"] is True


@pytest.mark.parametrize("body", [
    {"settings": {"per_day": 61}},
    {"settings": {"min_gap_s": "120"}},
    {"settings": {"ring_tone": "loud"}},
    {"enabled": True},
    [],
])
def test_bad_settings_are_refused(paired, body):
    response = _http().put(f"{BASE}/calls", json=body)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_a_watch_needs_a_paired_profile(home):
    response = _http().post(f"{BASE}/calls/watches", json={"session_ids": ["rt-1"], "title": "Job"})
    assert response.status_code == 409
    assert "paired" in response.json()["detail"]


def test_a_watch_needs_calls_on(paired):
    response = _http().post(f"{BASE}/calls/watches", json={"session_ids": ["rt-1"], "title": "Job"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Calls are off for this Hermes profile"


def test_a_watch_is_registered_and_removed(paired):
    http = _http()
    _turn_calls_on(http)
    response = http.post(f"{BASE}/calls/watches", json={"session_ids": ["rt-1", "st-1"], "title": "Check the server"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "watching"
    assert http.get(f"{BASE}/calls").json()["watches"] == 1
    removed = http.delete(f"{BASE}/calls/watches/{body['id']}")
    assert removed.json() == {"ok": True, "removed": True}
    assert http.delete(f"{BASE}/calls/watches/{body['id']}").json() == {"ok": True, "removed": False}
    assert http.get(f"{BASE}/calls").json()["watches"] == 0


def test_a_job_that_already_ended_answers_ended(paired):
    http = _http()
    _turn_calls_on(http)
    # The job's turn ended in the agent process just before Conduit hung up.
    api._calls_store().CallStore(paired).fire("rt-1", "done")
    response = http.post(f"{BASE}/calls/watches", json={"session_ids": ["rt-1"], "title": "Job", "ended_within_s": 300})
    assert response.json() == {"ok": True, "status": "ended", "outcome": "done"}


def test_a_watch_held_during_the_call_is_renewed_and_released(paired):
    http = _http()
    _turn_calls_on(http)
    watch_id = http.post(f"{BASE}/calls/watches", json={"session_ids": ["rt-1"], "title": "Job", "hold_s": 180}).json()["id"]
    assert http.put(f"{BASE}/calls/watches/{watch_id}", json={"hold_s": 180}).json() == {"ok": True, "status": "watching"}
    # The job ends during the call, then the user hangs up.
    assert api._calls_store().CallStore(paired).fire("rt-1", "failed")["status"] == "held"
    released = http.put(f"{BASE}/calls/watches/{watch_id}", json={"hold_s": 0})
    assert released.json() == {"ok": True, "status": "ended", "outcome": "failed"}
    assert released.headers["cache-control"] == "no-store"
    assert http.put(f"{BASE}/calls/watches/{watch_id}", json={"hold_s": 0}).json() == {"ok": True, "status": "gone"}


@pytest.mark.parametrize("body", [{"hold_s": 601}, {"hold_s": "60"}, {}, []])
def test_a_bad_hold_is_refused(paired, body):
    http = _http()
    _turn_calls_on(http)
    watch_id = http.post(f"{BASE}/calls/watches", json={"session_ids": ["rt-1"], "title": "Job"}).json()["id"]
    assert http.put(f"{BASE}/calls/watches/{watch_id}", json=body).status_code == 400


def test_holding_a_malformed_watch_id_is_refused(paired):
    assert _http().put(f"{BASE}/calls/watches/not-a-watch", json={"hold_s": 0}).status_code == 400


@pytest.mark.parametrize("body", [{"session_ids": []}, {"session_ids": "rt-1"}, {"session_ids": ["bad id"]}, {},
                                  {"session_ids": ["rt-1"], "hold_s": 601}, {"session_ids": ["rt-1"], "ended_within_s": -1}])
def test_a_watch_needs_session_ids(paired, body):
    http = _http()
    _turn_calls_on(http)
    assert http.post(f"{BASE}/calls/watches", json=body).status_code == 400


def test_too_many_watches_is_429(paired):
    http = _http()
    _turn_calls_on(http)
    store = api._calls_store().CallStore(paired)
    for index in range(20):
        store.add_watch([f"s{index}"], "Job")
    response = http.post(f"{BASE}/calls/watches", json={"session_ids": ["s20"], "title": "Job"})
    assert response.status_code == 429


def test_an_unknown_watch_id_is_refused(paired):
    assert _http().delete(f"{BASE}/calls/watches/not-a-watch").status_code == 400


def test_the_routes_are_rate_limited(paired):
    http = _http()
    for _ in range(api.CALLS_LIMIT):
        assert http.get(f"{BASE}/calls").status_code == 200
    response = http.get(f"{BASE}/calls")
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"


def test_an_oversized_body_is_refused(paired):
    response = _http().post(f"{BASE}/calls/watches", content=b"{" + b" " * 5000 + b"}",
                            headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_new_settings_for_hermes_decides_and_alerts_change(paired):
    http = _http()
    response = http.put(f"{BASE}/calls", json={"settings": {"enabled": True, "decides": True, "alerts": True}})
    assert response.status_code == 200
    settings = http.get(f"{BASE}/calls").json()["settings"]
    assert (settings["decides"], settings["alerts"]) == (True, True)
    assert http.put(f"{BASE}/calls", json={"settings": {"alerts": "yes"}}).status_code == 400


def test_presence_holds_calls_during_a_live_voice_call(paired):
    http = _http()
    _turn_calls_on(http)
    present = http.put(f"{BASE}/calls/presence", json={"hold_s": 90})
    assert present.json() == {"ok": True, "status": "present"}
    assert present.headers["cache-control"] == "no-store"
    store = api._calls_store().CallStore(paired)
    store.add_watch(["rt-1"], "Job")
    assert store.fire("rt-1", "done")["status"] == "busy"
    assert http.put(f"{BASE}/calls/presence", json={"hold_s": 0}).json() == {"ok": True, "status": "away"}


@pytest.mark.parametrize("body", [{"hold_s": 601}, {"hold_s": -1}, {}, []])
def test_a_bad_presence_is_refused(paired, body):
    assert _http().put(f"{BASE}/calls/presence", json=body).status_code == 400


def test_presence_needs_a_paired_profile(home):
    response = _http().put(f"{BASE}/calls/presence", json={"hold_s": 90})
    assert response.status_code == 409
    assert not (home / "conduit-calls.json").exists()


# --- Outcomes ----------------------------------------------------------------

def test_an_outcome_needs_a_paired_profile(home):
    body = {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1"], "outcome": "missed"}
    response = _http().post(f"{BASE}/calls/outcomes", json=body)
    assert response.status_code == 409
    assert not (home / "conduit-calls.json").exists()


def test_a_declined_call_is_recorded_for_its_chat(paired):
    home = paired
    http = _http()
    body = {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1", "st-1"], "outcome": "declined",
            "kind": "done", "title": "Deploy", "reason": "The deploy finished.", "age_s": 4}
    response = http.post(f"{BASE}/calls/outcomes", json=body)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "status": "recorded"}
    assert http.post(f"{BASE}/calls/outcomes", json=body).json() == {"ok": True, "status": "recorded"}
    taken = api._calls_store().CallStore(home).take_outcomes("st-1")
    assert [(entry["outcome"], entry["reason"]) for entry in taken] == [("declined", "The deploy finished.")]


@pytest.mark.parametrize("body", [
    {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1"], "outcome": "answered"},
    {"call_id": "", "session_ids": ["rt-1"], "outcome": "missed"},
    {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "outcome": "missed"},
    {"call_id": "a1b2c3d4e5f6a1b2c3d4e5f6", "session_ids": ["rt-1"], "outcome": "missed", "age_s": 1.5},
    [],
])
def test_a_malformed_outcome_is_refused(paired, body):
    response = _http().post(f"{BASE}/calls/outcomes", json=body)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
