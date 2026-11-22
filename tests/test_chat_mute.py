import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mute = _load("conduit_chat_mute_test", ROOT / "chat_mute.py")
api = _load("conduit_plugin_api_chat_mute", ROOT / "dashboard" / "plugin_api.py")
ChatMuteStore = mute.ChatMuteStore


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_timed_mute_ends_on_its_own(tmp_path):
    clock = Clock()
    store = ChatMuteStore(tmp_path, clock=clock)
    assert not store.muted(["chat-1"])
    chats = store.set_mute(["chat-1", "stored-1"], 900)
    assert chats == [{"session_ids": ["chat-1", "stored-1"], "until": 1900.0, "remaining_s": 900}]
    assert store.muted(["stored-1"]) and store.muted(["other", "chat-1"])
    assert not store.muted(["other"])
    clock.now = 1899.0
    assert ChatMuteStore(tmp_path, clock=clock).muted(["chat-1"])
    clock.now = 1900.0
    assert not store.muted(["chat-1"]) and store.chats() == []
    assert (tmp_path / mute.FILE_NAME).stat().st_mode & 0o777 == 0o600


def test_until_unmuted_lasts_and_unmuting_ends_it(tmp_path):
    clock = Clock()
    store = ChatMuteStore(tmp_path, clock=clock)
    assert store.set_mute(["chat-1"], None) == [{"session_ids": ["chat-1"], "until": None, "remaining_s": None}]
    clock.now += 10 * 366 * 86400
    assert store.muted(["chat-1"])
    assert store.set_mute(["chat-1"], 0) == []
    assert not store.muted(["chat-1"])


def test_muting_again_replaces_the_chats_entry(tmp_path):
    clock = Clock()
    store = ChatMuteStore(tmp_path, clock=clock)
    store.set_mute(["chat-1"], None)
    store.set_mute(["chat-2"], 60)
    chats = store.set_mute(["chat-1", "stored-1"], 3600)
    assert [chat["session_ids"] for chat in chats] == [["chat-2"], ["chat-1", "stored-1"]]
    assert chats[1]["until"] == 4600.0


def test_ended_mutes_are_dropped_and_the_list_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(mute, "MAX_CHATS", 2)
    clock = Clock()
    store = ChatMuteStore(tmp_path, clock=clock)
    store.set_mute(["a"], 10)
    clock.now += 1
    store.set_mute(["b"], None)
    clock.now += 1
    store.set_mute(["c"], None)
    assert [chat["session_ids"] for chat in store.chats()] == [["b"], ["c"]]
    clock.now += 100
    store.set_mute(["d"], None)
    assert [chat["session_ids"] for chat in store.chats()] == [["c"], ["d"]]


@pytest.mark.parametrize("body", [
    None, [], {"mute_s": 60}, {"session_ids": [], "mute_s": 60}, {"session_ids": ["a"]},
    {"session_ids": ["a"], "mute_s": -1}, {"session_ids": ["a"], "mute_s": True},
    {"session_ids": ["a"], "mute_s": float("inf")}, {"session_ids": ["a"], "mute_s": mute.MAX_MUTE_S + 1},
    {"session_ids": [" "], "mute_s": 60}, {"session_ids": [1], "mute_s": 60},
    {"session_ids": ["x" * 201], "mute_s": 60}, {"session_ids": ["a"] * 17, "mute_s": 60},
])
def test_bad_requests_are_refused(body):
    with pytest.raises(ValueError):
        mute.parse(body)


def test_a_request_reads_its_ids_and_length():
    assert mute.parse({"session_ids": [" a ", "b", "a"], "mute_s": 900}) == (["a", "b"], 900.0)
    assert mute.parse({"session_ids": ["a"], "mute_s": None}) == (["a"], None)
    assert mute.parse({"session_ids": ["a"], "mute_s": 0}) == (["a"], 0.0)


@pytest.mark.parametrize("content", [
    "{not json", "[]", '{"chats": {}}', '{"chats": [{"ids": ["a"], "until": "soon", "at": 1}]}',
    '{"chats": [{"ids": ["a"], "until": 99999999999, "at": 1}]}', '{"chats": [{"ids": [], "until": null, "at": 1}]}',
])
def test_a_file_that_cant_be_read_mutes_nothing(tmp_path, content):
    (tmp_path / mute.FILE_NAME).write_text(content)
    store = ChatMuteStore(tmp_path, clock=Clock())
    assert store.chats() == [] and not store.muted(["a"])


def test_an_unreadable_file_is_not_overwritten(tmp_path, monkeypatch):
    store = ChatMuteStore(tmp_path, clock=Clock())
    store.set_mute(["a"], None)
    saved = store.path.read_text(encoding="utf-8")
    real = type(store.path).read_text

    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", unreadable)
    with pytest.raises(OSError):
        store.set_mute(["b"], None)
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == saved


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.notifications, "_cron_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_mute_and_unmute_a_chat(client):
    assert client.get(f"{BASE}/notifications/muted-chats").json() == {"ok": True, "chats": []}
    response = client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["chat-1"], "mute_s": 3600})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    [chat] = response.json()["chats"]
    assert chat["session_ids"] == ["chat-1"] and 3599 <= chat["remaining_s"] <= 3600
    assert client.get(f"{BASE}/notifications/muted-chats").json()["chats"][0]["session_ids"] == ["chat-1"]
    assert client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["chat-1"], "mute_s": None}) \
        .json()["chats"] == [{"session_ids": ["chat-1"], "until": None, "remaining_s": None}]
    assert client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["chat-1"], "mute_s": 0}).json() \
        == {"ok": True, "chats": []}


def test_a_bad_body_is_a_400(client):
    assert client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["a"], "mute_s": -5}).status_code == 400
    assert client.put(f"{BASE}/notifications/muted-chats", json=["a"]).status_code == 400


def test_a_busy_cron_tab_cant_refuse_a_mute(client, monkeypatch):
    def full(limiter):
        def acquire(key, limit=None):
            raise api.core.TokenError(429, limiter.message)
        return acquire

    monkeypatch.setattr(api.notifications._cron_limiter, "acquire", full(api.notifications._cron_limiter))
    response = client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["chat-1"], "mute_s": 900})
    assert response.status_code == 200, "muted chats have their own bucket"
    monkeypatch.setattr(api.notifications._chat_mute_limiter, "acquire", full(api.notifications._chat_mute_limiter))
    refused = client.get(f"{BASE}/notifications/muted-chats")
    assert refused.status_code == 429 and "cron" not in refused.text.lower()


def _sessions(path, rows):
    """state.db with chat usage's columns: (id, parent, end_reason, markers, started, ended)."""
    import json
    import sqlite3

    columns = api.session_usage._COLUMNS
    db = sqlite3.connect(path / "state.db")
    db.execute(f"CREATE TABLE sessions ({', '.join(columns)})")
    for sid, parent, reason, markers, started, ended in rows:
        row = {"id": sid, "source": "cli", "parent_session_id": parent, "started_at": started, "ended_at": ended,
               "end_reason": reason, "model_config": json.dumps(markers)}
        db.execute(f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                   [row.get(column) for column in columns])
    db.commit()
    db.close()


def test_a_mute_names_the_whole_chat_through_compression_and_never_a_branch(client, tmp_path):
    _sessions(tmp_path, [
        ("root", None, "compression", {}, 1, 10), ("tip", "root", None, {}, 10, None),
        ("branch", "tip", None, {"_branched_from": "tip"}, 12, None),
    ])
    muted = client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["root"], "mute_s": None}).json()
    assert muted["chats"][0]["session_ids"] == ["root", "tip"], "the client finds it under the newest id"
    # Unmuted from the newest id the client has: the same chat.
    assert client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["tip"], "mute_s": 0}).json() \
        == {"ok": True, "chats": []}
    # A branch muted alone leaves the chat it came from as it was.
    branch = client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["branch"], "mute_s": 900}).json()
    assert [chat["session_ids"] for chat in branch["chats"]] == [["branch"]]
    assert not plugin_muted(tmp_path, "tip")


def test_a_chat_compacted_many_times_since_its_mute_still_unmutes(client, tmp_path):
    # 40 sessions, each compacted into the next: more than the plugin keeps per chat.
    ids = [f"s{n}" for n in range(40)]
    _sessions(tmp_path, [(sid, ids[n - 1] if n else None, None if n == 39 else "compression", {}, n * 10,
                          None if n == 39 else (n + 1) * 10) for n, sid in enumerate(ids)])
    stored = api.notifications._chat_mute().ChatMuteStore(tmp_path)
    stored.set_mute(api.notifications._same_chats([["s2"]])[0], None)
    assert plugin_muted(tmp_path, "s0"), "the chat's first session is kept"
    muted = client.get(f"{BASE}/notifications/muted-chats").json()
    assert "s39" in muted["chats"][0]["session_ids"], "the client finds it under its newest id"
    assert client.put(f"{BASE}/notifications/muted-chats", json={"session_ids": ["s39"], "mute_s": 0}).json() \
        == {"ok": True, "chats": []}


def test_an_entry_without_an_end_is_no_mute(tmp_path):
    import json

    store = api.notifications._chat_mute().ChatMuteStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 1, "chats": [{"ids": ["a"], "at": store.clock()}]}))
    assert not store.muted(["a"])


def plugin_muted(home, session_id):
    return api.notifications._chat_mute().ChatMuteStore(home).muted([session_id])
