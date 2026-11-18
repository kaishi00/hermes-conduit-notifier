"""One chat's tokens and cost (hermes-conduit#2699): its row, its compression
continuations and its subagent runs, read from Hermes' sessions table."""

import importlib.util
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


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_session_usage",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def _add(conn, sid, *, parent=None, started=1.0, ended=None, end_reason=None, model="m", model_config=None,
         calls=1, input_tokens=100, output_tokens=10, cache_read=0, reasoning=0, tools=0,
         estimated=None, actual=None, status=None):
    conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason, model, "
        "model_config, api_call_count, input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, "
        "tool_call_count, estimated_cost_usd, actual_cost_usd, cost_status) "
        "VALUES (?, 'conduit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, parent, started, ended, end_reason, model, model_config, calls, input_tokens, output_tokens,
         cache_read, reasoning, tools, estimated, actual, status))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path, raising=False)
    # No per-profile plugin gate: a real Hermes' would refuse made-up profiles.
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_dashboard", None)
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _get(query):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app).get(f"{BASE}/sessions/usage{query}")


def test_a_compressed_chat_counts_every_row_it_continued_into(db):
    _add(db, "a", started=1, ended=5, end_reason="compression", input_tokens=1000, output_tokens=100,
         estimated=0.10, status="estimated")
    _add(db, "b", parent="a", started=6, ended=9, end_reason="compression", input_tokens=500, output_tokens=50,
         estimated=0.05, status="estimated")
    _add(db, "c", parent="b", started=10, model="newest", input_tokens=200, output_tokens=20,
         estimated=0.02, status="estimated")
    db.commit()
    # Asked by any row of the chain, the answer is the whole chat's.
    for sid in ("a", "b", "c"):
        body = _get(f"?session_id={sid}").json()
        assert body["input_tokens"] == 1700 and body["output_tokens"] == 170
        assert body["compressions"] == 2
        assert body["cost_usd"] == pytest.approx(0.17)
        assert body["cost_status"] == "estimated"
        assert body["model"] == "newest" and body["newest_session_id"] == "c"
        assert body["started_at"] == 1


def test_subagents_count_and_branches_do_not(db):
    _add(db, "chat", started=1, ended=20, end_reason="branched", input_tokens=100, actual=1.0, status="actual")
    _add(db, "sub", parent="chat", started=2, input_tokens=40, actual=0.5, status="actual")
    _add(db, "subsub", parent="sub", started=3, input_tokens=10, actual=0.25, status="actual")
    _add(db, "branch", parent="chat", started=21, input_tokens=9999, actual=9.0, status="actual")
    _add(db, "marked", parent="chat", started=4, model_config='{"_branched_from": "chat"}', input_tokens=8888)
    db.commit()
    body = _get("?session_id=chat").json()
    assert body["input_tokens"] == 150
    assert body["subagent_runs"] == 2
    assert body["cost_usd"] == pytest.approx(1.75)
    assert body["cost_status"] == "actual"


def test_an_unpriced_chat_has_no_cost_rather_than_zero(db):
    # Hermes stores an unknown price as 0; the status says it wasn't priced.
    _add(db, "local", estimated=0.0, status="unknown")
    db.commit()
    body = _get("?session_id=local").json()
    assert body["cost_usd"] is None and body["cost_status"] == "unknown"


def test_a_plan_without_per_token_billing_is_included(db):
    _add(db, "plan", estimated=0.0, status="included")
    db.commit()
    body = _get("?session_id=plan").json()
    assert body["cost_usd"] == 0 and body["cost_status"] == "included"


def test_some_calls_unpriced_make_the_cost_a_floor(db):
    _add(db, "mixed", started=1, ended=5, end_reason="compression", estimated=0.3, status="estimated")
    _add(db, "tail", parent="mixed", started=6, estimated=0.0, status="unknown")
    db.commit()
    body = _get("?session_id=mixed").json()
    assert body["cost_usd"] == pytest.approx(0.3) and body["cost_status"] == "partial"


def test_a_row_that_never_ran_says_nothing_about_cost(db):
    _add(db, "fresh", calls=0, input_tokens=0, output_tokens=0)
    db.commit()
    body = _get("?session_id=fresh").json()
    assert body["input_tokens"] == 0 and body["cost_usd"] is None and body["cost_status"] == "unknown"


def test_refusals(db):
    assert _get("?session_id=nope").status_code == 404
    response = _get("")
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_the_capability_is_listed():
    assert "session-usage" in api.capabilities.ROUTE_CAPABILITIES
