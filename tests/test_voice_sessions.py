import contextlib
import contextvars
import importlib.util
import pathlib
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_voice", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
_queue_voice_call_end = api._queue_voice_call_end

_active_profile = contextvars.ContextVar("active_profile", default=None)


# Mirrors hermes_state_errors.
class CompressionSessionClosedError(RuntimeError):
    pass


class CompressionSessionBusyError(RuntimeError):
    pass


class SessionCompressionInProgressError(CompressionSessionBusyError):
    pass


class FakeStore:
    """One profile's state.db: sessions, messages, state_meta."""

    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.meta = {}
        self.titles = {}
        self.fail_append = None


class FakeSessionDB:
    stores = {}

    def __init__(self, db_path=None):
        self.path = Path(db_path)
        self.store = FakeSessionDB.stores.setdefault(self.path.parent.name, FakeStore())
        self.closed = False

    def close(self):
        self.closed = True

    def get_meta(self, key):
        return self.store.meta.get(key)

    def set_meta(self, key, value):
        self.store.meta[key] = value

    def get_session(self, session_id):
        return self.store.sessions.get(session_id)

    def ensure_session(self, session_id, source="unknown", model=None, **kwargs):
        self.store.sessions.setdefault(session_id, {"id": session_id, "source": source, "model": model,
                                                    "end_reason": None, **kwargs})
        self.store.messages.setdefault(session_id, [])
        return session_id

    def set_session_title(self, session_id, title):
        if title in self.store.titles.values():
            raise ValueError("title in use")
        self.store.titles[session_id] = title
        return True

    def get_session_title(self, session_id):
        return self.store.titles.get(session_id)

    def get_session_title_source(self, session_id):
        return "user" if session_id in self.store.titles else None

    def delete_session(self, session_id):
        self.store.sessions.pop(session_id, None)
        self.store.messages.pop(session_id, None)
        self.store.titles.pop(session_id, None)
        return True

    def end_session(self, session_id, end_reason):
        row = self.store.sessions[session_id]
        if row["end_reason"] is None:
            row["end_reason"] = end_reason

    def _check(self, session_id):
        if self.store.fail_append:
            raise self.store.fail_append
        # Hermes' write guard: only a row that compression closed refuses appends.
        if self.store.sessions[session_id]["end_reason"] == "compression":
            raise CompressionSessionClosedError(session_id)

    def append_message(self, session_id, role, content=None, timestamp=None, **kwargs):
        self._check(session_id)
        self.store.messages[session_id].append({"role": role, "content": content, "timestamp": timestamp})

    def append_messages_batch(self, session_id, messages, **kwargs):
        self._check(session_id)
        self.store.messages[session_id].extend(dict(message) for message in messages)
        return len(messages)


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_voice_write_limiter",
                        api._MintLimiter(api.VOICE_WRITE_LIMIT, api.VOICE_WRITE_WINDOW_S))
    monkeypatch.setattr(api, "_voice_read_limiter",
                        api._MintLimiter(api.VOICE_READ_LIMIT, api.VOICE_WRITE_WINDOW_S))
    FakeSessionDB.stores = {}
    entered = []
    # A save that stores new turns ends its call; these tests record that
    # instead of running it (the call-end tests below run it).
    ends = []
    monkeypatch.setattr(api, "_queue_voice_call_end", lambda *args: ends.append(args))

    def home():
        return tmp_path / (_active_profile.get() or "default")

    @contextlib.contextmanager
    def scope(profile):
        entered.append(profile)
        token = _active_profile.set(profile)
        try:
            yield
        finally:
            _active_profile.reset(token)

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = home
    profiles = types.ModuleType("hermes_cli.web_server_profiles")
    profiles._config_profile_scope = scope
    state = types.ModuleType("hermes_state")
    state.SessionDB = FakeSessionDB
    ids = types.ModuleType("hermes_state_ids")
    counter = iter(range(1, 1000))
    ids.new_session_id = lambda: f"20260930_120000_{next(counter):012x}"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", profiles)
    monkeypatch.setitem(sys.modules, "hermes_state", state)
    monkeypatch.setitem(sys.modules, "hermes_state_ids", ids)
    return types.SimpleNamespace(stores=FakeSessionDB.stores, entered=entered, ends=ends)


@pytest.fixture
def client(hermes):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def turns(*pairs, start=0):
    return [{"index": start + i, "role": role, "text": text, "at": 1_780_000_000 + i}
            for i, (role, text) in enumerate(pairs)]


def save(client, **body):
    body.setdefault("engine", "gemini-live")
    body.setdefault("call_id", "call-1")
    return client.post(f"{BASE}/voice/sessions", json=body)


# --- Saving -------------------------------------------------------------------


def test_first_save_creates_a_desktop_session_without_a_model(client, hermes):
    response = save(client, title="Voice call", turns=turns(("user", "hi"), ("assistant", "hello")))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ok"] and body["created"] and body["written"] == 2 and body["appended"] == 2
    store = hermes.stores["default"]
    row = store.sessions[body["session_id"]]
    # The source is the agent's platform and the model is restored on resume:
    # a saved call must look exactly like a Conduit chat.
    assert row["source"] == "desktop" and row["model"] is None
    assert row["end_reason"] == api.VOICE_END_REASON
    assert [m["content"] for m in store.messages[body["session_id"]]] == ["hi", "hello"]
    assert store.messages[body["session_id"]][0]["timestamp"] == 1_780_000_000
    assert store.titles[body["session_id"]] == "Voice call"


@pytest.mark.parametrize("engine", ["gpt-live", "grok-live"])
def test_first_save_tags_the_session_as_a_call(client, hermes, engine):
    session_id = save(client, engine=engine, turns=turns(("user", "hi"))).json()["session_id"]
    tags = client.get(f"{BASE}/voice/tags").json()["tags"]
    assert tags == {session_id: {"kind": "call", "engine": engine}}


def test_nothing_to_save_creates_no_session(client, hermes):
    body = save(client, turns=[]).json()
    assert body["session_id"] is None
    assert not hermes.stores.get("default", api) or not hermes.stores["default"].sessions


def test_appends_skip_turns_already_written_for_the_call(client, hermes):
    first = save(client, turns=turns(("user", "one"), ("assistant", "two"))).json()
    # A retried flush resends turn 1 alongside the new turn 2.
    again = save(client, session_id=first["session_id"],
                 turns=turns(("assistant", "two"), ("user", "three"), start=1)).json()
    assert again["appended"] == 1 and again["written"] == 3 and not again["created"]
    contents = [m["content"] for m in hermes.stores["default"].messages[first["session_id"]]]
    assert contents == ["one", "two", "three"]


def test_replays_are_skipped_and_new_turns_continue_in_order(client, hermes):
    first = save(client, turns=turns(("user", "zero"), ("assistant", "one"))).json()
    again = save(client, session_id=first["session_id"],
                 turns=turns(("assistant", "one"), ("user", "two"), start=1)).json()
    assert again["appended"] == 1 and again["skipped"] == 1 and again["written"] == 3
    contents = [m["content"] for m in hermes.stores["default"].messages[first["session_id"]]]
    assert contents == ["zero", "one", "two"]


def test_a_gap_in_indices_is_refused_not_stored_or_lost(client, hermes):
    first = save(client, turns=turns(("user", "zero"))).json()
    gap = save(client, session_id=first["session_id"], turns=turns(("user", "two"), start=2))
    assert gap.status_code == 400 and "index 1" in gap.json()["detail"]
    assert save(client, call_id="call-2", turns=turns(("user", "late"), start=1)).status_code == 400
    assert len(hermes.stores["default"].sessions) == 1


def test_a_turn_that_isnt_final_is_refused(client, hermes):
    # An empty turn could still gain text; a sent index must never change.
    assert save(client, turns=turns(("user", "hi"), ("assistant", "  "))).status_code == 400
    assert not hermes.stores.get("default") or not hermes.stores["default"].sessions


def test_bad_timestamps_are_dropped(client, hermes):
    body = save(client, turns=[{"index": 0, "role": "user", "text": "a", "at": 1e300},
                               {"index": 1, "role": "user", "text": "b", "at": -5}]).json()
    stored = hermes.stores["default"].messages[body["session_id"]]
    assert all("timestamp" not in message for message in stored)


def test_a_retried_create_continues_the_row_it_made(client, hermes):
    # The first create landed but its response was lost: Conduit resends it.
    first = save(client, turns=turns(("user", "a"))).json()
    again = save(client, turns=turns(("user", "a"), ("assistant", "b"))).json()
    assert again["session_id"] == first["session_id"] and not again["created"]
    assert again["appended"] == 1 and again["skipped"] == 1
    assert len(hermes.stores["default"].sessions) == 1


def test_a_retried_create_finishes_a_row_the_first_attempt_left_untagged(client, hermes):
    first = save(client, turns=turns(("user", "a"))).json()["session_id"]
    store = hermes.stores["default"]
    # As if the first attempt died after writing turns, before end and tag.
    store.sessions[first]["end_reason"] = None
    store.meta[api.VOICE_TAGS_KEY] = "{}"
    again = save(client, turns=turns(("user", "a"))).json()
    assert again["session_id"] == first
    assert store.sessions[first]["end_reason"] == api.VOICE_END_REASON
    assert client.get(f"{BASE}/voice/tags").json()["tags"][first]["kind"] == "call"


def test_a_title_failure_never_costs_the_transcript(client, hermes, monkeypatch):
    def broken(self, session_id, title):
        raise RuntimeError("constraint")

    monkeypatch.setattr(FakeSessionDB, "set_session_title", broken)
    body = save(client, title="Voice call", turns=turns(("user", "a")))
    assert body.status_code == 200
    assert len(hermes.stores["default"].messages[body.json()["session_id"]]) == 1


def test_pruning_a_deleted_rows_tag_clears_its_meta(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "VOICE_MAX_TAGS", 1)
    call = save(client, turns=turns(("user", "a"))).json()["session_id"]
    store = hermes.stores["default"]
    del store.sessions[call]
    client.post(f"{BASE}/voice/tags", json={"session_id": "s_new", "kind": "job"})
    assert not store.meta.get(api.VOICE_CALLS_KEY.format(session_id=call))


def test_a_saved_calls_tag_cant_be_replaced(client, hermes):
    call = save(client, turns=turns(("user", "a"))).json()["session_id"]
    assert client.post(f"{BASE}/voice/tags", json={"session_id": call, "kind": "classic"}).status_code == 422
    assert client.get(f"{BASE}/voice/tags").json()["tags"][call]["kind"] == "call"


def test_reads_are_rate_limited_before_anything_runs(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_voice_read_limiter", api._MintLimiter(1, 60.0))
    assert client.get(f"{BASE}/voice/tags").status_code == 200
    assert client.get(f"{BASE}/voice/tags").status_code == 429


def test_made_up_profile_names_share_one_voice_budget(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_voice_read_limiter", api._MintLimiter(2, 60.0))
    assert client.get(f"{BASE}/voice/tags?profile=x1").status_code != 429
    assert client.get(f"{BASE}/voice/tags?profile=x2").status_code != 429
    assert client.get(f"{BASE}/voice/tags?profile=x3").status_code == 429


def test_a_discarded_create_forgets_its_call(client, hermes, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api, "_set_voice_tag", broken)
    save(client, turns=turns(("user", "a")))
    assert __import__("json").loads(hermes.stores["default"].meta.get(api.VOICE_CREATED_KEY) or "{}") == {}


def test_summaries_need_a_voice_row_and_string_text(client, hermes):
    store = FakeSessionDB.stores.setdefault("default", FakeStore())
    store.sessions["chat_1"] = {"id": "chat_1", "source": "desktop", "end_reason": None}
    assert client.post(f"{BASE}/voice/summary", json={"session_id": "chat_1", "text": "x", "covers": 1}).status_code == 422
    call = save(client, turns=turns(("user", "a"))).json()["session_id"]
    assert client.post(f"{BASE}/voice/summary", json={"session_id": call, "text": {"a": 1}, "covers": 1}).status_code == 400


def test_a_gone_row_leaves_no_voice_meta(client, hermes):
    call = save(client, turns=turns(("user", "a"))).json()["session_id"]
    client.post(f"{BASE}/voice/summary", json={"session_id": call, "text": "s", "covers": 1})
    store = hermes.stores["default"]
    del store.sessions[call]
    assert save(client, session_id=call, turns=turns(("user", "b"), start=1)).status_code == 422
    assert not store.meta.get(api.VOICE_CALLS_KEY.format(session_id=call))
    assert not store.meta.get(api.VOICE_SUMMARY_KEY.format(session_id=call))


def test_a_repeated_index_in_one_save_is_written_once(client, hermes):
    body = save(client, turns=[{"index": 0, "role": "user", "text": "hi"},
                               {"index": 0, "role": "user", "text": "hi"}]).json()
    assert body["appended"] == 1 and body["skipped"] == 1


def test_only_the_most_recent_calls_are_remembered(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "VOICE_MAX_CALLS", 2)
    session_id = save(client, turns=turns(("user", "a"))).json()["session_id"]
    for call in ("call-2", "call-3"):
        save(client, session_id=session_id, call_id=call, turns=turns(("user", call)))
    calls = hermes.stores["default"].meta[api.VOICE_CALLS_KEY.format(session_id=session_id)]
    assert __import__("json").loads(calls) == {"call-2": 0, "call-3": 0}


def test_an_ordinary_chat_never_takes_voice_turns(client, hermes):
    store = FakeSessionDB.stores.setdefault("default", FakeStore())
    store.sessions["chat_1"] = {"id": "chat_1", "source": "desktop", "end_reason": None}
    store.messages["chat_1"] = []
    assert save(client, session_id="chat_1", turns=turns(("user", "a"))).status_code == 422
    assert store.messages["chat_1"] == [] and store.sessions["chat_1"]["end_reason"] is None
    # A classic voice chat is a real agent chat too: it never takes them.
    client.post(f"{BASE}/voice/tags", json={"session_id": "chat_1", "kind": "classic"})
    assert save(client, session_id="chat_1", turns=turns(("user", "a"))).status_code == 422


def test_a_store_with_only_the_batch_writer_can_save(client, hermes, monkeypatch):
    monkeypatch.delattr(FakeSessionDB, "append_message")
    assert save(client, turns=turns(("user", "a"))).status_code == 200


def test_a_failed_create_leaves_no_row_behind(client, hermes, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api, "_set_voice_tag", broken)
    assert save(client, title="Voice call", turns=turns(("user", "a"))).status_code == 500
    store = hermes.stores["default"]
    assert store.sessions == {} and store.messages == {} and store.titles == {}


def test_titles_are_skipped_on_a_store_without_them_or_when_not_a_string(client, hermes, monkeypatch):
    body = save(client, title={"a": 1}, turns=turns(("user", "a"))).json()
    assert body["session_id"] not in hermes.stores["default"].titles
    monkeypatch.delattr(FakeSessionDB, "set_session_title")
    assert save(client, title="Voice call", call_id="call-2", turns=turns(("user", "b"))).status_code == 200


def test_a_resumed_call_appends_to_the_same_row(client, hermes):
    first = save(client, turns=turns(("user", "one"))).json()
    resumed = save(client, session_id=first["session_id"], call_id="call-2",
                   turns=turns(("user", "back again"))).json()
    assert resumed["session_id"] == first["session_id"] and resumed["appended"] == 1
    assert len(hermes.stores["default"].messages[first["session_id"]]) == 2


def test_older_hermes_without_the_batch_writer_appends_one_by_one(client, hermes, monkeypatch):
    monkeypatch.delattr(FakeSessionDB, "append_messages_batch")
    calls = []
    original = FakeSessionDB.append_message
    monkeypatch.setattr(FakeSessionDB, "append_message",
                        lambda self, *args, **kwargs: calls.append(1) or original(self, *args, **kwargs))
    body = save(client, turns=turns(("user", "a"), ("assistant", "b"))).json()
    assert len(calls) == 2
    assert [m["content"] for m in hermes.stores["default"].messages[body["session_id"]]] == ["a", "b"]


def test_title_clash_gets_the_id_tail(client, hermes):
    first = save(client, title="Voice call", turns=turns(("user", "a"))).json()["session_id"]
    second = save(client, title="Voice call", call_id="call-2", turns=turns(("user", "b"))).json()["session_id"]
    titles = hermes.stores["default"].titles
    assert titles[first] == "Voice call"
    assert titles[second] == f"Voice call ({second[-6:]})"


def test_unknown_session_is_422(client, hermes):
    # Not 404: Conduit reads a 404 as "the plugin has no such route".
    assert save(client, session_id="gone_123", turns=turns(("user", "a"))).status_code == 422


@pytest.mark.parametrize("body", [
    {"engine": "gemini-live", "turns": []},  # no call id
    {"engine": "nope", "call_id": "c", "turns": []},
    {"engine": "gemini-live", "call_id": "c", "turns": "x"},
    {"engine": "gemini-live", "call_id": "c", "turns": [{"role": "system", "text": "x", "index": 0}]},
    {"engine": "gemini-live", "call_id": "c", "turns": [{"role": "user", "text": "x"}]},
    {"engine": "gemini-live", "call_id": "c", "session_id": "../etc", "turns": []},
    {"engine": "gemini-live", "call_id": "c", "turns": [{"role": "user", "text": "x", "index": 100_001}]},
    {"engine": "gemini-live", "call_id": "c", "turns": [{"role": "user", "text": {"a": 1}, "index": 0}]},
])
def test_invalid_bodies_are_400(client, hermes, body):
    assert client.post(f"{BASE}/voice/sessions", json=body).status_code == 400


def test_compression_in_flight_is_409_and_a_compacted_row_is_422(client, hermes):
    session_id = save(client, turns=turns(("user", "a"))).json()["session_id"]
    store = hermes.stores["default"]
    store.fail_append = SessionCompressionInProgressError("busy")
    assert save(client, session_id=session_id, turns=turns(("user", "b"), start=1)).status_code == 409
    store.fail_append = CompressionSessionBusyError("compression owns the row")
    assert save(client, session_id=session_id, turns=turns(("user", "b"), start=1)).status_code == 409
    store.fail_append = None
    store.sessions[session_id]["end_reason"] = "compression"
    assert save(client, session_id=session_id, turns=turns(("user", "b"), start=1)).status_code == 422


def test_saved_rows_are_ended_and_still_take_appends(client, hermes):
    session_id = save(client, turns=turns(("user", "a"))).json()["session_id"]
    store = hermes.stores["default"]
    assert store.sessions[session_id]["end_reason"] == api.VOICE_END_REASON
    later = save(client, session_id=session_id, turns=turns(("assistant", "b"), start=1))
    assert later.status_code == 200 and later.json()["appended"] == 1
    assert store.sessions[session_id]["end_reason"] == api.VOICE_END_REASON


def test_writes_are_rate_limited_per_profile(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "_voice_write_limiter", api._MintLimiter(2, 60.0))
    assert save(client, turns=turns(("user", "a"))).status_code == 200
    assert save(client, call_id="call-2", turns=turns(("user", "b"))).status_code == 200
    assert save(client, call_id="call-3", turns=turns(("user", "c"))).status_code == 429
    assert client.get(f"{BASE}/voice/tags").status_code == 200  # reads have their own limit


def test_hermes_without_a_session_store_is_501(client, hermes, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_state", None)
    assert save(client, turns=turns(("user", "a"))).status_code == 501


def test_saves_land_in_the_requested_profile(client, hermes):
    session_id = client.post(f"{BASE}/voice/sessions?profile=coder",
                             json={"engine": "gemini-live", "call_id": "c", "turns": turns(("user", "a"))}
                             ).json()["session_id"]
    assert session_id in hermes.stores["coder"].sessions
    assert "coder" in hermes.entered


# --- Tags -----------------------------------------------------------------------


def test_classic_and_job_tags_round_trip(client, hermes):
    call = save(client, turns=turns(("user", "a"))).json()["session_id"]
    assert client.post(f"{BASE}/voice/tags", json={"session_id": "s_classic", "kind": "classic"}).status_code == 200
    assert client.post(f"{BASE}/voice/tags", json={"session_id": "s_job", "kind": "job", "parent_id": call,
                                                    "parent_title": "Groceries"}).status_code == 200
    tags = client.get(f"{BASE}/voice/tags").json()["tags"]
    assert tags["s_classic"] == {"kind": "classic"}
    assert tags["s_job"] == {"kind": "job", "parent_id": call, "parent_title": "Groceries"}


@pytest.mark.parametrize("kind", ["call", "other", None])
def test_only_classic_and_job_tags_can_be_set_directly(client, hermes, kind):
    assert client.post(f"{BASE}/voice/tags", json={"session_id": "s", "kind": kind}).status_code == 400


def test_tags_prune_deleted_sessions_when_over_the_cap(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "VOICE_MAX_TAGS", 2)
    live = save(client, turns=turns(("user", "a"))).json()["session_id"]
    client.post(f"{BASE}/voice/tags", json={"session_id": "deleted_1", "kind": "job"})
    client.post(f"{BASE}/voice/tags", json={"session_id": "deleted_2", "kind": "job"})
    tags = client.get(f"{BASE}/voice/tags").json()["tags"]
    assert live in tags and len(tags) <= 2


def test_the_tag_being_written_survives_the_cap(client, hermes, monkeypatch):
    monkeypatch.setattr(api, "VOICE_MAX_TAGS", 1)
    save(client, turns=turns(("user", "a")))
    assert client.post(f"{BASE}/voice/tags", json={"session_id": "not_saved_yet", "kind": "classic"}).status_code == 200
    assert client.get(f"{BASE}/voice/tags").json()["tags"] == {"not_saved_yet": {"kind": "classic"}}


def test_reads_work_on_a_store_without_the_write_helpers(client, hermes, monkeypatch):
    class ReadOnlyDB:
        def __init__(self, db_path=None):
            pass

        def get_meta(self, key):
            return None

        def close(self):
            pass

    sys.modules["hermes_state"].SessionDB = ReadOnlyDB
    assert client.get(f"{BASE}/voice/tags").status_code == 200
    assert client.get(f"{BASE}/voice/summary", params={"session_id": "s_1"}).status_code == 200
    assert save(client, turns=turns(("user", "a"))).status_code == 501


def test_unreadable_tags_start_over(client, hermes):
    save(client, turns=turns(("user", "a")))
    hermes.stores["default"].meta[api.VOICE_TAGS_KEY] = "{not json"
    assert client.get(f"{BASE}/voice/tags").json()["tags"] == {}


# --- Summary --------------------------------------------------------------------


def test_summary_round_trip(client, hermes):
    session_id = save(client, turns=turns(("user", "a"))).json()["session_id"]
    assert client.get(f"{BASE}/voice/summary", params={"session_id": session_id}).json() == {
        "ok": True, "available": False, "text": "", "covers": 0}
    assert client.post(f"{BASE}/voice/summary", json={"session_id": session_id, "text": "We talked.",
                                                       "covers": 12}).status_code == 200
    assert client.get(f"{BASE}/voice/summary", params={"session_id": session_id}).json() == {
        "ok": True, "available": True, "text": "We talked.", "covers": 12}


def test_summary_for_a_deleted_session_is_422(client, hermes):
    save(client, turns=turns(("user", "a")))
    response = client.post(f"{BASE}/voice/summary", json={"session_id": "gone_1", "text": "x", "covers": 1})
    assert response.status_code == 422


@pytest.mark.parametrize("body", [{"text": "", "covers": 1}, {"text": "x", "covers": -1}, {"text": "x"}])
def test_summary_needs_text_and_covers(client, hermes, body):
    session_id = save(client, turns=turns(("user", "a"))).json()["session_id"]
    assert client.post(f"{BASE}/voice/summary", json={"session_id": session_id, **body}).status_code == 400


# --- Call end -------------------------------------------------------------------


def test_a_save_with_new_turns_ends_the_call(client, hermes):
    session_id = save(client, engine="gpt-live", turns=turns(("user", "hi"), ("assistant", "hello"))).json()["session_id"]
    assert hermes.ends == [(None, session_id, "gpt-live", [
        {"role": "user", "content": "hi", "timestamp": 1_780_000_000},
        {"role": "assistant", "content": "hello", "timestamp": 1_780_000_001}])]


def test_a_replayed_save_ends_nothing_twice(client, hermes):
    first = save(client, turns=turns(("user", "a"))).json()
    # The response was lost and Conduit resends the same turns.
    save(client, turns=turns(("user", "a")))
    save(client, session_id=first["session_id"], turns=turns(("user", "a")))
    assert len(hermes.ends) == 1


def test_nothing_saved_ends_nothing(client, hermes):
    save(client, turns=[])
    assert save(client, turns=turns(("user", "a"), ("assistant", " "))).status_code == 400
    assert hermes.ends == []


def test_a_resumed_call_ends_with_only_its_own_turns(client, hermes):
    session_id = save(client, turns=turns(("user", "one"), ("assistant", "two"))).json()["session_id"]
    save(client, session_id=session_id, call_id="call-2", turns=turns(("user", "back"), ("assistant", "hi again")))
    assert [m["content"] for m in hermes.ends[-1][3]] == ["back", "hi again"]
    assert hermes.ends[-1][1] == session_id


def test_a_call_ends_in_the_profile_it_was_saved_to(client, hermes):
    client.post(f"{BASE}/voice/sessions?profile=coder", json={"engine": "gemini-live", "call_id": "c",
                                                               "turns": turns(("user", "a"))})
    assert hermes.ends[0][0] == "coder"


def test_exchanges_pair_what_the_user_said_with_the_reply():
    messages = [{"role": role, "content": text} for role, text in (
        ("assistant", "Hi, what's up?"),  # a greeting before the user spoke
        ("user", "What's the weather"), ("user", "in Paris?"),
        ("assistant", "Sunny."), ("assistant", "Started a background job: forecast."),
        ("user", "Thanks"), ("assistant", "Anytime."),
        ("user", "One more thing"),  # hung up before the answer
    )]
    assert api._voice_exchanges(messages) == [
        ("What's the weather\nin Paris?", "Sunny.\nStarted a background job: forecast."),
        ("Thanks", "Anytime."),
    ]


class EndProvider:
    """A memory provider that logs each call with the profile it ran in."""

    name = "honcho"

    def __init__(self, events, available=True):
        self.events = events
        self.available = available

    def is_available(self):
        return self.available

    def initialize(self, session_id, **kwargs):
        self.events.append(("initialize", _active_profile.get(), session_id, kwargs))

    def sync_turn(self, user, assistant, *, session_id=""):
        self.events.append(("sync_turn", user, assistant, session_id))

    def on_session_end(self, messages):
        self.events.append(("provider on_session_end", messages))

    def shutdown(self):
        self.events.append(("shutdown",))


class EndMemoryManager:
    """Hermes' MemoryManager, cut down to the calls a call's end makes."""

    events = None

    def __init__(self):
        self.providers = []

    def add_provider(self, provider):
        self.providers.append(provider)

    def initialize_all(self, session_id, **kwargs):
        for provider in self.providers:
            provider.initialize(session_id=session_id, **kwargs)

    def sync_all(self, user, assistant, *, session_id=""):
        for provider in self.providers:
            provider.sync_turn(user, assistant, session_id=session_id)

    def flush_pending(self, timeout=None):
        self.events.append(("flush_pending", timeout))
        return True

    def on_session_end(self, messages):
        for provider in self.providers:
            provider.on_session_end(messages)

    def shutdown_all(self):
        for provider in self.providers[::-1]:
            provider.shutdown()


@pytest.fixture
def lifecycle(hermes, monkeypatch):
    """Hermes' side of a call's end: lifecycle hooks, memory config and manager."""
    events = []
    module = types.ModuleType("hermes_cli.lifecycle")
    module.invoke_hook = lambda name, **kwargs: events.append((name, _active_profile.get(), kwargs))
    module.finalize_session = lambda **kwargs: events.append(("on_session_finalize", _active_profile.get(), kwargs))
    monkeypatch.setattr(sys.modules["hermes_cli"], "lifecycle", module, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.lifecycle", module)
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.is_core_memory_provider = lambda name: str(name or "").strip().lower() in {
        "", "default", "builtin", "built-in", "none"}
    manager = types.ModuleType("agent.memory_manager")
    EndMemoryManager.events = events
    manager.MemoryManager = EndMemoryManager
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    monkeypatch.setitem(sys.modules, "agent.memory_manager", manager)
    state = types.SimpleNamespace(events=events, config={"memory": {"provider": "honcho"}},
                                  provider=EndProvider(events))
    monkeypatch.setattr(api, "_hermes_memory_config", lambda: state.config)
    monkeypatch.setattr(api, "_hermes_load_memory_provider", lambda name: state.provider)
    return state


CALL = [{"role": "user", "content": "remind me to call mum", "timestamp": 1.0},
        {"role": "assistant", "content": "Will do.", "timestamp": 2.0}]


def test_a_call_ends_like_a_desktop_chat(hermes, lifecycle):
    api._run_voice_call_end("coder", "s_call", "gemini-live", CALL)
    hook = {"session_id": "s_call", "platform": "desktop", "reason": "voice_call_ended"}
    events = lifecycle.events
    assert events[0] == ("on_session_end", "coder",
                         {**hook, "completed": True, "interrupted": False, "model": "gemini-live"})
    name, profile, session_id, kwargs = events[1]
    assert (name, profile, session_id) == ("initialize", "coder", "s_call")
    # Written as the row's own platform and as the primary agent, so the
    # provider writes; the recall provider's conduit_voice platform isn't used.
    assert kwargs["platform"] == "desktop" and kwargs["agent_context"] == "primary"
    assert kwargs["hermes_home"].endswith("coder")
    assert events[2:] == [
        ("sync_turn", "remind me to call mum", "Will do.", "s_call"),
        ("flush_pending", api.VOICE_END_FLUSH_TIMEOUT_S),
        ("provider on_session_end", [{"role": "user", "content": "remind me to call mum"},
                                     {"role": "assistant", "content": "Will do."}]),
        ("shutdown",),
        ("on_session_finalize", "coder", hook),
    ]


def test_the_provider_gets_the_rows_title(client, hermes, lifecycle):
    session_id = save(client, title="Groceries", turns=turns(("user", "a"), ("assistant", "b"))).json()["session_id"]
    api._run_voice_call_end(None, session_id, "gemini-live", CALL)
    kwargs = next(event for event in lifecycle.events if event[0] == "initialize")[3]
    assert kwargs["session_title"] == "Groceries" and kwargs["session_title_source"] == "user"


def test_without_an_external_provider_only_the_hooks_fire(hermes, lifecycle):
    lifecycle.config = {"memory": {"provider": "builtin"}}
    api._run_voice_call_end(None, "s_call", "gpt-live", CALL)
    assert [event[0] for event in lifecycle.events] == ["on_session_end", "on_session_finalize"]


def test_an_unavailable_provider_is_left_alone(hermes, lifecycle):
    lifecycle.provider.available = False
    api._run_voice_call_end(None, "s_call", "gpt-live", CALL)
    assert [event[0] for event in lifecycle.events] == ["on_session_end", "on_session_finalize"]


def test_a_failing_step_doesnt_stop_the_others(hermes, lifecycle, monkeypatch):
    def hook(name, **kwargs):
        raise RuntimeError("plugin bug")

    monkeypatch.setattr(sys.modules["hermes_cli.lifecycle"], "invoke_hook", hook)

    def broken_sync(*args, **kwargs):
        raise RuntimeError("honcho down")

    monkeypatch.setattr(lifecycle.provider, "sync_turn", broken_sync)
    api._run_voice_call_end(None, "s_call", "gpt-live", CALL)
    # The memory write stopped at sync_turn but still shut its provider down.
    assert [event[0] for event in lifecycle.events] == ["initialize", "shutdown", "on_session_finalize"]


def test_an_older_hermes_calls_the_plugin_hooks_directly(hermes, monkeypatch):
    events = []
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.invoke_hook = lambda name, **kwargs: events.append(name)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(api, "_hermes_memory_config", lambda: {})
    api._run_voice_call_end(None, "s_call", "gpt-live", CALL)
    assert events == ["on_session_end", "on_session_finalize"]


def test_a_hermes_without_any_of_it_ends_quietly(hermes, caplog):
    # No lifecycle, plugin or memory modules: nothing to call, nothing to warn about.
    with caplog.at_level("DEBUG"):
        api._run_voice_call_end(None, "s_call", "gpt-live", CALL)
    assert [r.message for r in caplog.records if r.levelname == "WARNING"] == []
    assert any("this Hermes has no" in r.message for r in caplog.records)


def test_a_wedged_end_is_abandoned(hermes, lifecycle, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(sys.modules["hermes_cli.lifecycle"], "invoke_hook", lambda name, **kwargs: release.wait(5))
    monkeypatch.setattr(api, "VOICE_END_TIMEOUT_S", 0.05)
    started = time.monotonic()
    api._run_voice_call_end(None, "s_call", "gpt-live", CALL)
    assert time.monotonic() - started < 2
    release.set()


def test_a_saved_call_ends_in_the_background(client, hermes, lifecycle, monkeypatch):
    finalized = threading.Event()
    finalize = sys.modules["hermes_cli.lifecycle"].finalize_session

    def finalize_and_signal(**kwargs):
        finalize(**kwargs)
        finalized.set()

    monkeypatch.setattr(sys.modules["hermes_cli.lifecycle"], "finalize_session", finalize_and_signal)
    monkeypatch.setattr(api, "_queue_voice_call_end", _queue_voice_call_end)
    session_id = client.post(f"{BASE}/voice/sessions?profile=coder", json={
        "engine": "grok-live", "call_id": "c", "turns": turns(("user", "hi"), ("assistant", "hello"))}).json()["session_id"]
    assert finalized.wait(5)
    assert ("on_session_finalize", "coder",
            {"session_id": session_id, "platform": "desktop", "reason": "voice_call_ended"}) in lifecycle.events
    assert ("sync_turn", "hi", "hello", session_id) in lifecycle.events


def test_ends_past_the_backlog_are_skipped(hermes, monkeypatch):
    monkeypatch.setattr(api, "VOICE_END_MAX_PENDING", 0)
    ran = []
    monkeypatch.setattr(api, "_run_voice_call_end", lambda *args: ran.append(args))
    _queue_voice_call_end(None, "s_call", "gpt-live", CALL)
    assert ran == [] and api._voice_end_pending == 0 and api._voice_end_jobs.empty()
