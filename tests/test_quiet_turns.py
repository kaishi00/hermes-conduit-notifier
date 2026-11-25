"""Each reply's push decision, kept for Conduit for desktop (quiet_turns.py):
a reply the phone hears nothing about stays quiet on the computer too."""
import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_turn_end_pushes import plugin, pushed  # noqa: F401 — the fixture

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quiet_turns = _load("conduit_quiet_turns_test", ROOT / "quiet_turns.py")
api = _load("conduit_plugin_api_quiet_turns", ROOT / "dashboard" / "plugin_api.py")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_the_newest_decision_of_a_chat_answers(tmp_path):
    clock = Clock()
    store = quiet_turns.QuietTurnStore(tmp_path, clock)
    assert store.newest(["a"]) is None
    store.note("a", True)
    clock.now += 5
    assert store.newest(["a"]) == {"session_id": "a", "quiet": True, "age_s": 5.0}
    store.note("a", False)
    assert store.newest(["a"]) == {"session_id": "a", "quiet": False, "age_s": 0.0}
    assert len(store._load()["turns"]) == 1, "one entry per chat"


def test_a_chat_is_found_under_any_of_its_sessions_newest_first(tmp_path):
    clock = Clock()
    store = quiet_turns.QuietTurnStore(tmp_path, clock)
    store.note("root", False)
    clock.now += 3
    store.note("tip", True)
    clock.now += 1
    assert store.newest(["root", "tip"]) == {"session_id": "tip", "quiet": True, "age_s": 1.0}
    assert store.newest(["other"]) is None


def test_old_decisions_answer_nothing_and_the_file_stays_small(tmp_path):
    clock = Clock()
    store = quiet_turns.QuietTurnStore(tmp_path, clock)
    store.note("old", True)
    clock.now += quiet_turns.MAX_AGE_S + 1
    assert store.newest(["old"]) is None
    for n in range(quiet_turns.MAX_TURNS + 5):
        store.note(f"s{n}", True)
    turns = store._load()["turns"]
    assert len(turns) == quiet_turns.MAX_TURNS and turns[-1]["id"] == f"s{quiet_turns.MAX_TURNS + 4}"
    assert all(entry["id"] != "old" for entry in turns)


@pytest.mark.parametrize("content", ["not json", "[]", '{"turns": [{"id": "a", "quiet": "yes", "at": 1000}]}'])
def test_a_file_that_cant_be_read_answers_nothing(tmp_path, content):
    (tmp_path / quiet_turns.FILE_NAME).write_text(content, encoding="utf-8")
    assert quiet_turns.QuietTurnStore(tmp_path, Clock()).newest(["a"]) is None


def test_a_silent_reply_and_a_waits_only_answer_are_kept_quiet_and_a_reply_is_not(pushed, tmp_path, monkeypatch):  # noqa: F811
    store = quiet_turns.QuietTurnStore(tmp_path)
    plugin._post_llm_call(session_id="bc", turn_id="t1", assistant_response="[SILENT]")
    assert store.newest(["bc"])["quiet"] is True
    plugin._post_llm_call(session_id="bc", turn_id="t2", assistant_response="W2 is done.")
    assert store.newest(["bc"])["quiet"] is False
    monkeypatch.setattr(plugin, "_waits_only_turn", lambda turn_id, end=False: turn_id == "t3")
    plugin._post_llm_call(session_id="bc", turn_id="t3", assistant_response="W3 needs your OK above.")
    assert store.newest(["bc"])["quiet"] is True


def test_a_store_that_cant_be_written_never_breaks_the_turn(pushed, monkeypatch):  # noqa: F811
    def broken(self, session_id, quiet):
        raise OSError("read-only")

    monkeypatch.setattr(plugin.quiet_turns.QuietTurnStore, "note", broken)
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    assert [event["type"] for event in pushed] == ["response.ready"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.notifications, "_cron_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_route_answers_a_chats_newest_decision(client, tmp_path):
    assert client.get(f"{BASE}/notifications/quiet-turns", params={"session_id": "a"}).json() == {"ok": True, "turn": None}
    api.notifications._quiet_turns().QuietTurnStore(tmp_path).note("a", True)
    response = client.get(f"{BASE}/notifications/quiet-turns", params=[("session_id", "x"), ("session_id", "a")])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    turn = response.json()["turn"]
    assert turn["session_id"] == "a" and turn["quiet"] is True and turn["age_s"] < 5


def test_the_route_refuses_a_request_without_ids(client):
    assert client.get(f"{BASE}/notifications/quiet-turns").status_code == 400
    many = [("session_id", f"s{n}") for n in range(quiet_turns.MAX_IDS + 1)]
    assert client.get(f"{BASE}/notifications/quiet-turns", params=many).status_code == 400
