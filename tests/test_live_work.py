"""A chat's turn running in the gateway (Eric, 2026-11-27): whether one
runs, its step and delegate agents from the live steps file, and the messages
queued behind it from the gateway's admissions, for the chat Conduit has open
on ``hermes dashboard``, which keeps no queue of its own."""

import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
SCHEMA = (ROOT / "tests" / "hermes_state_schema.sql").read_text(encoding="utf-8")

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load("conduit_plugin_api_live_work", ROOT / "dashboard" / "plugin_api.py")
steps = _load("conduit_live_work_steps", ROOT / "live_steps.py")


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_dashboard", None)
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    api.live_work._live_work_limiter._mints.clear()
    steps.forget()
    state = sqlite3.connect(tmp_path / "state.db")
    state.executescript(SCHEMA)
    yield state, tmp_path
    state.close()
    steps.forget()


def _session(conn, sid, *, parent=None, started=1.0, ended=None, end_reason=None):
    conn.execute("INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason) "
                 "VALUES (?, 'api_server', ?, ?, ?, ?)", (sid, parent, started, ended, end_reason))
    conn.commit()


def _admission(conn, admission_id, session, status, payload, *, request_id=None):
    conn.execute(
        "INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, lineage_json, "
        "payload_json, payload_digest, intent, status, owner_epoch, generation) "
        "VALUES (?, ?, 'p', ?, '[]', ?, 'd', 'queue', ?, ?, ?)",
        (admission_id, request_id or admission_id, session, json.dumps(payload), status,
         1 if status == "started" else None, 1 if status == "started" else None))
    conn.commit()


def _get(query):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app).get(f"{BASE}/sessions/live-work{query}")


def test_a_turn_running_in_the_gateway_shows_its_step_agents_and_the_messages_behind_it(host):
    state, home = host
    _session(state, "api-1")
    _admission(state, "adm-1", "api-1", "started", {"text": "make the picture"})
    _admission(state, "adm-2", "api-1", "queued", {"text": "did you actually start subagents?"}, request_id="sub-2")
    _admission(state, "adm-3", "api-1", "queued",
               {"native_text_v1": {"event": {"text": "from the phone"}, "route": {"secret": "x"}}})
    _admission(state, "adm-0", "api-1", "terminal", {"text": "an old one"})
    steps.started(home, "api-1", now=10.0)
    steps.step("api-1", "tool", "agents", now=11.0)
    steps.agent_started("api-1", "child-1", "Generate the office still", now=12.0)
    steps.step("child-1", "tool", "terminal", now=13.0)
    response = _get("?session_id=api-1")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["supported"] is True and body["running"] is True
    assert body["step"] == {"phase": "tool", "tool": "agents", "since": 11.0}
    assert body["agents"] == [{"id": "child-1", "goal": "Generate the office still", "phase": "tool",
                               "tool": "terminal", "since": 13.0, "started": 12.0}]
    assert body["queued"] == [
        {"admission_id": "adm-2", "input_id": "sub-2", "text": "did you actually start subagents?",
         "status": "queued", "sequence": 2},
        # Only the words: never the envelope's routing.
        {"admission_id": "adm-3", "input_id": "adm-3", "text": "from the phone", "status": "queued", "sequence": 3}]
    assert body["queued_count"] == 2
    # The turn running now: a message that left the queue for it started.
    assert body["started"] == ["adm-1"]


def test_an_idle_chat_says_nothing_runs(host):
    state, _ = host
    _session(state, "s-1")
    _admission(state, "adm-1", "s-1", "terminal", {"text": "done"})
    assert _get("?session_id=s-1").json() == {
        "session_id": "s-1", "supported": True, "running": False, "step": None, "agents": [],
        "queued": [], "queued_count": 0, "started": []}


def test_a_turn_a_restart_cut_off_is_listed_with_the_ones_it_holds(host):
    state, _ = host
    _session(state, "s-1")
    _admission(state, "adm-1", "s-1", "unknown", {"text": "cut off"})
    _admission(state, "adm-2", "s-1", "queued", {"text": "behind it"})
    body = _get("?session_id=s-1").json()
    assert body["running"] is False
    assert [(row["admission_id"], row["status"]) for row in body["queued"]] == [("adm-1", "unknown"), ("adm-2", "queued")]


def test_the_queue_follows_the_chat_through_its_compressions(host):
    state, home = host
    _session(state, "a", started=1, ended=5, end_reason="compression")
    _session(state, "b", parent="a", started=6)
    _admission(state, "adm-1", "b", "queued", {"text": "after the compression"})
    steps.started(home, "b", now=10.0)
    body = _get("?session_id=a").json()
    assert body["running"] is True and body["step"]["phase"] == "thinking"
    assert [row["text"] for row in body["queued"]] == ["after the compression"]


def test_a_live_step_alone_says_a_turn_runs(host):
    state, home = host
    _session(state, "s-1")
    steps.started(home, "s-1", now=10.0)
    body = _get("?session_id=s-1").json()
    assert body["running"] is True and body["queued"] == []


def test_a_hermes_without_a_queue_says_so(host):
    state, _ = host
    _session(state, "s-1")
    state.execute("DROP TABLE session_admissions")
    state.commit()
    body = _get("?session_id=s-1").json()
    assert body["supported"] is False and body["running"] is False and body["queued"] == []


def test_a_long_message_is_cut(host):
    state, _ = host
    _session(state, "s-1")
    _admission(state, "adm-1", "s-1", "queued", {"text": "x" * 5000})
    text = _get("?session_id=s-1").json()["queued"][0]["text"]
    assert len(text) == api.live_work.TEXT_MAX and text.endswith("…")


def test_an_unknown_chat_or_none_named(host):
    assert _get("?session_id=nope").status_code == 404
    assert _get("").status_code == 400


def test_a_turn_a_restart_cut_off_is_not_running_whatever_step_it_left(host):
    state, home = host
    _session(state, "api-1")
    _admission(state, "adm-1", "api-1", "unknown", {"text": "cut off"})
    steps.started(home, "api-1", now=10.0)
    steps.agent_started("api-1", "child-1", "goal", now=11.0)
    body = _get("?session_id=api-1").json()
    assert body["running"] is False and body["step"] is None and body["agents"] == []
    assert [row["status"] for row in body["queued"]] == ["unknown"]
