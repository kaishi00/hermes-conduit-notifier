"""Checklist cards' ticks, kept on the host (checklist_store, dashboard/api/boards.py)."""

import os
import contextlib
import importlib.util
import json
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

_spec = importlib.util.spec_from_file_location("conduit_push_test_checklist_store", ROOT / "checklist_store.py")
checklist_store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checklist_store)
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_checklists", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

ChecklistStore = checklist_store.ChecklistStore
ITEMS = ["Rain jacket", "Swimsuit", "Passport"]


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def store(tmp_path):
    return ChecklistStore(tmp_path, clock=Clock())


def _save(store, ticked, sessions=("s1",), key="packing", items=ITEMS, title="Iceland in June"):
    return store.save({"sessions": list(sessions), "key": key, "title": title, "items": list(items),
                       "ticked": list(ticked)})


def test_the_key_is_the_live_name_then_the_title():
    assert checklist_store.list_key({"live": "Packing", "title": "Iceland"}) == "packing"
    assert checklist_store.list_key({"title": "Iceland in June"}) == "iceland in june"
    assert checklist_store.list_key({}) == "checklist"


def test_ticks_are_kept_for_every_id_of_the_chat(store):
    assert _save(store, ["swimsuit", "Socks"], sessions=("s1", "runtime-1")) == {"packing": ["Swimsuit"]}, \
        "ticks are of items it has, written as the item"
    assert store.ticks(["runtime-1"]) == {"packing": ["Swimsuit"]}
    assert store.ticks(["other"]) == {}


def test_the_newest_write_wins_across_a_chats_ids(store):
    _save(store, ["Swimsuit"], sessions=("s1",))
    store.clock.now += 5
    _save(store, ["Passport"], sessions=("runtime-1",))
    assert store.ticks(["s1", "runtime-1"]) == {"packing": ["Passport"]}


def test_hermes_hears_a_change_once(store):
    assert store.note("s1") is None
    _save(store, ["Swimsuit"])
    assert store.note("s1") == ('[Conduit: the user changed the checklist "Iceland in June" in this chat. Done: '
                                'Swimsuit; still to do: Rain jacket, Passport.]')
    assert store.note("s1") is None, "once"
    _save(store, ITEMS)
    assert store.note("s1").endswith("Done: Rain jacket, Swimsuit, Passport; everything is done.]")
    _save(store, [])
    assert "Nothing is ticked" in store.note("s1")


def test_a_change_is_heard_once_under_every_id_of_the_chat(store):
    _save(store, ["Swimsuit"], sessions=("st-1", "rt-1"))
    assert store.note("st-1") is not None
    assert store.note("rt-1") is None, "the same write, heard already"


def test_a_repeated_save_isnt_heard_twice(store):
    _save(store, ["Swimsuit"])
    assert store.note("s1") is not None
    _save(store, ["Swimsuit"])
    assert store.note("s1") is None, "nothing changed since Hermes heard it"


def test_two_chats_saving_at_once_are_each_heard(store):
    _save(store, ["Swimsuit"], sessions=("a",))
    _save(store, ["Passport"], sessions=("b",))
    assert store.note("a") is not None
    assert "Passport" in store.note("b"), "the same key and time, another chat's write"


def test_hermes_waits_for_a_first_save_still_writing(store):
    lock = store.path.with_name(f".{store.path.name}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    assert store.note("s1") is None
    assert not store.path.exists(), "nothing written when nothing changed"


def test_a_tick_is_applied_to_the_newest_ticks_any_device_saved(store):
    _save(store, ["Passport"], sessions=("s1",))
    tick = {"sessions": ["s1", "r1"], "key": "packing", "title": "Iceland in June", "items": ITEMS}
    store.clock.now += 1
    assert store.save({**tick, "change": {"item": "swimsuit", "ticked": True}}) == {"packing": ["Swimsuit", "Passport"]}
    store.clock.now += 1
    assert store.save({**tick, "change": {"item": "Passport", "ticked": False}}) == {"packing": ["Swimsuit"]}
    with pytest.raises(checklist_store.ChecklistError):
        store.save({**tick, "change": {"item": "Passport"}})


def test_bad_writes_are_refused(store):
    for body in ([], {"sessions": [], "key": "k", "items": [], "ticked": []},
                 {"sessions": ["s1"], "key": " ", "items": [], "ticked": []},
                 {"sessions": ["s1"], "key": "k", "items": "a", "ticked": []}):
        with pytest.raises(checklist_store.ChecklistError):
            store.save(body)


def test_the_file_is_bounded_and_old_lists_go(store):
    for index in range(checklist_store.MAX_LISTS + 10):
        store.clock.now += 1
        _save(store, [], sessions=(f"s{index}",))
    assert len(json.loads(store.path.read_text())["lists"]) == checklist_store.MAX_LISTS
    assert store.ticks(["s0"]) == {}, "the oldest went first"
    store.clock.now += checklist_store.KEEP_S
    assert store.ticks([f"s{checklist_store.MAX_LISTS + 9}"]) == {}


def test_a_hand_edited_file_is_read_safely(store):
    store.path.write_text(json.dumps({"lists": [{"session": "s1"}, "x", {
        "session": "s1", "key": "k", "title": "", "items": ["A"], "ticked": ["A"],
        "updated_at": store.clock.now, "heard": True}]}))
    assert store.ticks(["s1"]) == {"k": ["A"]}
    store.path.write_text("not json")
    assert store.ticks(["s1"]) == {}


# --- Route ---------------------------------------------------------------------


@pytest.fixture()
def route_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    api.boards._checklist_limiter._mints.clear()
    return tmp_path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_routes_save_and_read_ticks(route_home):
    http = _http()
    body = {"sessions": ["s1", "r1"], "key": "packing", "title": "Iceland", "items": ITEMS, "ticked": ["Passport"]}
    response = http.post(f"{BASE}/boards/checklists?profile=work", json=body)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"ticks": {"packing": ["Passport"]}}
    read = http.get(f"{BASE}/boards/checklists", params=[("session", "r1"), ("session", "x")])
    assert read.status_code == 200 and read.json() == {"ticks": {"packing": ["Passport"]}}
    assert ChecklistStore(route_home).note("s1").startswith('[Conduit: the user changed the checklist "Iceland"'), "Hermes hears it"


def test_the_routes_refuse_bad_requests(route_home):
    http = _http()
    assert http.get(f"{BASE}/boards/checklists").status_code == 400
    assert http.post(f"{BASE}/boards/checklists", json={"key": "k"}).status_code == 400
    assert http.post(f"{BASE}/boards/checklists", json=[1]).status_code == 400


def test_checklist_ticks_are_a_capability():
    assert "board-checklists" in api.capabilities.ROUTE_CAPABILITIES
