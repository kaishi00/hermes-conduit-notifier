"""Each reply's speed (reply_speed.py): the plugin adds up a turn's model calls
from Hermes' post_api_request hook, and the route ties each turn to the reply
it ended on."""

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
if "tools" not in sys.modules:
    _tools = types.ModuleType("tools")
    _tools.__path__ = []
    sys.modules["tools"] = _tools
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


speeds = _load("conduit_push.reply_speed", ROOT / "reply_speed.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
api = _load("conduit_plugin_api_reply_speed", ROOT / "dashboard" / "plugin_api.py")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_dashboard", None)
    return tmp_path


@pytest.fixture
def db(home):
    conn = sqlite3.connect(home / "state.db")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _session(conn, sid, *, parent=None, started=1.0, ended=None, end_reason=None):
    conn.execute("INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason) "
                 "VALUES (?, 'conduit', ?, ?, ?, ?)", (sid, parent, started, ended, end_reason))


def _message(conn, sid, role, content, at):
    return conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                        (sid, role, content, at)).lastrowid


def _get(query):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app).get(f"{BASE}/sessions/reply-speeds{query}")


def test_a_call_is_timed_from_its_first_chunk():
    assert speeds.call_measure(100.0, 104.0, 102.0, {"output_tokens": 100}) == (100, 2.0)


def test_hidden_thinking_and_unstreamed_calls_are_timed_from_the_request():
    # Reasoning tokens may have been written before the first chunk: timed from
    # the request, a speed reads low but never high.
    assert speeds.call_measure(100.0, 104.0, 102.0, {"output_tokens": 100, "reasoning_tokens": 60}) == (100, 4.0)
    assert speeds.call_measure(100.0, 104.0, None, {"output_tokens": 100}) == (100, 4.0)
    # A first chunk outside the call is no first chunk.
    assert speeds.call_measure(100.0, 104.0, 99.0, {"output_tokens": 100}) == (100, 4.0)


def test_a_call_without_tokens_or_times_gives_no_speed():
    assert speeds.call_measure(100.0, 104.0, 102.0, None) is None
    assert speeds.call_measure(100.0, 104.0, 102.0, {"output_tokens": 0}) is None
    assert speeds.call_measure(100.0, 104.0, 102.0, {"output_tokens": float("nan")}) is None
    assert speeds.call_measure(104.0, 100.0, None, {"output_tokens": 10}) is None
    assert speeds.call_measure("x", 100.0, None, {"output_tokens": 10}) is None


def test_the_hook_adds_a_turns_calls_together(home):
    # A tool step, then the answer: one turn, its tokens over its writing time.
    plugin._post_api_request(session_id="s", turn_id="t", started_at=10.0, ended_at=12.0, first_chunk_at=11.0,
                             usage={"output_tokens": 20, "prompt_tokens": 5}, context_length=1000)
    plugin._post_api_request(session_id="s", turn_id="t", started_at=13.0, ended_at=17.0, first_chunk_at=14.0,
                             usage={"output_tokens": 280}, context_length=1000)
    [turn] = speeds.ReplySpeedStore(home).turns(["s"])
    assert (turn["started_at"], turn["ended_at"], turn["output_tokens"], turn["calls"]) == (10.0, 17.0, 300, 2)
    assert speeds.speed(turn) == 75.0


def test_a_subagents_or_forks_calls_are_not_the_chats(home, monkeypatch):
    monkeypatch.setattr(plugin, "_is_child", lambda session_id: True)
    plugin._post_api_request(session_id="s", turn_id="t", started_at=10.0, ended_at=12.0,
                             usage={"output_tokens": 20})
    assert speeds.ReplySpeedStore(home).turns(["s"]) == []


def test_the_oldest_turns_go_first(home, monkeypatch):
    monkeypatch.setattr(speeds, "MAX_TURNS", 3)
    store = speeds.ReplySpeedStore(home)
    for n in range(5):
        store.add_call("s", f"t{n}", 10.0 * n, 10.0 * n + 2, None, {"output_tokens": 10})
    assert [turn["started_at"] for turn in store.turns(["s"])] == [20.0, 30.0, 40.0]


def test_each_turn_lands_on_the_reply_it_ended_on(db, home):
    _session(db, "a", started=1, ended=50, end_reason="compression")
    _session(db, "b", parent="a", started=51)
    _message(db, "a", "user", "hi", 9.0)
    _message(db, "a", "assistant", "", 12.0)  # the tool call, no words
    first = _message(db, "a", "assistant", "Here you go.", 17.5)
    _message(db, "b", "user", "more", 60.0)
    second = _message(db, "b", "assistant", "More.", 63.0)
    db.commit()
    store = speeds.ReplySpeedStore(home)
    store.add_call("a", "t1", 10.0, 17.0, 11.0, {"output_tokens": 300})
    store.add_call("b", "t2", 60.0, 62.5, 61.0, {"output_tokens": 30})
    # A turn still running: its reply isn't saved yet.
    store.add_call("b", "t3", 70.0, 71.0, None, {"output_tokens": 50})
    for asked in ("a", "b"):
        body = _get(f"?session_id={asked}")
        assert body.status_code == 200 and body.headers["cache-control"] == "no-store"
        replies = body.json()["replies"]
        assert [(reply["row_id"], reply["tokens_per_s"]) for reply in replies] == [
            (first, 50.0), (second, 20.0), (None, 50.0)]
        assert replies[0]["output_tokens"] == 300 and replies[0]["seconds"] == 6.0


def test_a_chat_without_kept_speeds_has_none(db):
    _session(db, "a")
    db.commit()
    assert _get("?session_id=a").json() == {"session_id": "a", "replies": []}


def test_refusals(db):
    assert _get("?session_id=nope").status_code == 404
    response = _get("")
    assert response.status_code == 400 and response.headers["cache-control"] == "no-store"


def test_the_capability_is_listed():
    assert "reply-speeds" in api.capabilities.ROUTE_CAPABILITIES
