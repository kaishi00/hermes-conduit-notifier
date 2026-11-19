"""Done chats, kept on the host (chat_marks_store, dashboard/api/chat_marks.py, chat_done_tool)."""

import contextlib
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants
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


marks = _load("conduit_push.chat_marks_store", ROOT / "chat_marks_store.py")
tool = _load("conduit_push.chat_done_tool", ROOT / "chat_done_tool.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_chat_marks", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def store(tmp_path):
    return marks.ChatMarksStore(tmp_path, clock=Clock())


def test_a_mark_is_found_by_any_of_the_chats_ids(store):
    entry = store.mark(["root", "tip"], True)
    assert entry == {"ids": ["root", "tip"], "at": 1_000_000.0, "by": "user"}
    assert store.is_done(["tip"]) and store.is_done(["x", "root"]) and not store.is_done(["other"])
    assert store.done() == [entry]


def test_marking_again_replaces_the_chats_mark_and_opening_drops_it(store):
    store.mark(["root"], True)
    store.clock.now += 5
    again = store.mark(["root", "tip"], True, by="agent")
    assert store.done() == [again], "one mark per chat, the newest"
    assert store.mark(["tip"], False) is None
    assert store.done() == [] and not store.is_done(["root"])


def test_the_newest_marks_come_first_and_the_oldest_go_past_the_cap(store, monkeypatch):
    monkeypatch.setattr(marks, "MAX_DONE", 3)
    for index in range(5):
        store.clock.now += 1
        store.mark([f"c{index}"], True)
    assert [entry["ids"] for entry in store.done()] == [["c4"], ["c3"], ["c2"]]


def test_bad_marks_are_refused(store):
    for ids in (None, [], [""], "root", [1]):
        with pytest.raises(marks.ChatMarksError):
            store.mark(ids, True)
    with pytest.raises(marks.ChatMarksError):
        store.mark(["root"], "yes")
    with pytest.raises(marks.ChatMarksError):
        store.mark(["root"], True, by="pulse")
    assert len(marks.clean_ids([f"id{n}" for n in range(20)])) == marks.MAX_IDS


def test_a_hand_edited_file_is_read_safely(store):
    store.path.write_text(json.dumps({"done": [{"ids": ["a"], "at": 1.0, "by": "user"},
                                               {"ids": "b", "at": 1.0, "by": "user"},
                                               {"ids": ["c"], "at": float("inf"), "by": "user"},
                                               {"ids": ["d"], "at": 1.0, "by": "someone"}]}))
    assert [entry["ids"] for entry in store.done()] == [["a"]]
    store.path.write_text("not json")
    assert store.done() == []


# --- The tool ------------------------------------------------------------------------


def test_the_tool_marks_the_chat_under_its_compaction_chain(tmp_path):
    store = marks.ChatMarksStore(tmp_path)
    answer = json.loads(tool.handle({}, session_id="tip", store=store, chain=lambda s: [s, "middle", "root"]))
    assert answer["ok"] and answer["done"]
    assert store.done()[0]["ids"] == ["tip", "middle", "root"] and store.done()[0]["by"] == "agent"
    answer = json.loads(tool.handle({"done": False}, session_id="tip", store=store, chain=lambda s: [s]))
    assert answer == {"ok": True, "done": False, "note": "Open again in Conduit."}
    assert store.done() == []


def test_a_long_chain_keeps_its_root(tmp_path):
    store = marks.ChatMarksStore(tmp_path)
    tool.handle({}, session_id="s0", store=store, chain=lambda s: [f"s{n}" for n in range(12)])
    assert store.done()[0]["ids"] == ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s11"]


def test_the_tool_refuses_subagents_and_no_chat(tmp_path):
    store = marks.ChatMarksStore(tmp_path)
    assert json.loads(tool.handle({}, session_id="", store=store))["error"] == "no_chat"
    assert json.loads(tool.handle({}, session_id="kid", store=store, is_child=lambda s: True))["error"] == "subagent"
    assert json.loads(tool.handle({"done": "yes"}, session_id="s", store=store, chain=lambda s: [s]))["error"] == "bad_done"
    assert store.done() == []


def test_the_tool_registers():
    registered = []
    ctx = types.SimpleNamespace(register_tool=lambda **kwargs: registered.append(kwargs))
    tool.register(ctx, is_child=lambda s: False)
    assert registered[0]["name"] == "conduit_chat_done" and registered[0]["toolset"] == "conduit"
    assert "conduit_chat_done" in (ROOT / "plugin.yaml").read_text()


# --- Routes ----------------------------------------------------------------------------


@pytest.fixture()
def route_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    api.chat_marks._chat_marks_limiter._mints.clear()
    return tmp_path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_routes_mark_and_read_done_chats(route_home):
    http = _http()
    response = http.post(f"{BASE}/chats/done?profile=work", json={"ids": ["root", "tip"], "done": True})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["mark"]["ids"] == ["root", "tip"] and response.json()["mark"]["by"] == "user"
    read = http.get(f"{BASE}/chats/done")
    assert read.status_code == 200 and [entry["ids"] for entry in read.json()["done"]] == [["root", "tip"]]
    assert http.post(f"{BASE}/chats/done", json={"ids": ["tip"], "done": False}).json() == {"ok": True, "mark": None}
    assert http.get(f"{BASE}/chats/done").json() == {"ok": True, "done": []}


def test_the_routes_refuse_bad_requests(route_home):
    http = _http()
    assert http.post(f"{BASE}/chats/done", json={"done": True}).status_code == 400
    assert http.post(f"{BASE}/chats/done", json={"ids": ["a"], "done": 1}).status_code == 400
    assert http.post(f"{BASE}/chats/done", json=[1]).status_code == 400


def test_done_chats_are_a_capability():
    assert "chat-done" in api.capabilities.ROUTE_CAPABILITIES
