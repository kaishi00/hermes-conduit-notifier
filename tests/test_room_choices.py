"""Each owner's choices for their agent in a Linked Room (designs/cross-owner-agents, decision 9)."""
import argparse
import importlib.util
import json
import logging
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


choices_module = _load("conduit_push.room_choices", ROOT / "room_choices.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_choices", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

ChoicesStore = choices_module.ChoicesStore
ChoicesError = choices_module.ChoicesError
RoomChoices = choices_module.RoomChoices

ROOM = "room-1"
OTHER = "room-2"


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_room_without_choices_gets_the_defaults(tmp_path):
    choices = ChoicesStore(tmp_path).get(ROOM)
    assert choices == RoomChoices()
    assert choices.as_dict() == {
        "tools": "talk", "custom_tools": [], "share": "ask", "search_chats": False, "takes_part": "joins",
        "daily_limit": 40, "note": ""}
    # Reading writes nothing.
    assert not (tmp_path / choices_module.FILE_NAME).exists()


def test_choices_set_for_a_room_are_kept_for_that_room_only(tmp_path):
    store = ChoicesStore(tmp_path)
    saved = store.set(ROOM, {"tools": "look", "search_chats": True, "note": "Don't mention the party"})
    assert saved.tools == "look" and saved.search_chats and saved.note == "Don't mention the party"
    assert ChoicesStore(tmp_path).get(ROOM) == saved
    assert ChoicesStore(tmp_path).get(OTHER) == RoomChoices()


def test_a_partial_change_keeps_the_other_choices(tmp_path):
    store = ChoicesStore(tmp_path)
    store.set(ROOM, {"tools": "custom", "custom_tools": ["terminal", "read_file", "terminal"]})
    after = store.set(ROOM, {"daily_limit": 10})
    assert after.tools == "custom"
    # Kept once each, in the order given.
    assert after.custom_tools == ("terminal", "read_file")
    assert after.daily_limit == 10


def test_saved_defaults_fill_rooms_that_have_none(tmp_path):
    store = ChoicesStore(tmp_path)
    store.set(ROOM, {"tools": "look"})
    store.set_defaults({"share": "nothing", "daily_limit": 5})
    assert store.defaults() == RoomChoices(share="nothing", daily_limit=5)
    assert store.get(OTHER) == RoomChoices(share="nothing", daily_limit=5)
    # A room with its own choices keeps them.
    assert store.get(ROOM).tools == "look" and store.get(ROOM).share == "ask"


def test_view_lists_the_defaults_and_each_room(tmp_path):
    store = ChoicesStore(tmp_path, clock=Clock(2_000.0))
    store.set(ROOM, {"tools": "look"})
    view = store.view()
    assert view["defaults"] == RoomChoices().as_dict()
    assert view["rooms"] == {ROOM: {"choices": {**RoomChoices().as_dict(), "tools": "look"}, "at": 2_000.0}}


@pytest.mark.parametrize("change", [
    {"tools": "admin"}, {"tools": None}, {"custom_tools": "terminal"}, {"custom_tools": [3]},
    {"custom_tools": ["bad name"]}, {"custom_tools": ["x" * 129]}, {"custom_tools": [f"t{n}" for n in range(65)]},
    {"custom_tools": ["memory"]}, {"custom_tools": ["skill_manage"]}, {"custom_tools": ["delegate_task"]},
    {"custom_tools": ["execute_code"]}, {"custom_tools": ["message_agent"]}, {"custom_tools": ["react_to_message"]},
    {"custom_tools": ["kanban_create"]},
    {"share": "everything"}, {"search_chats": "yes"}, {"search_chats": 1}, {"takes_part": "sometimes"},
    {"daily_limit": 0}, {"daily_limit": 501}, {"daily_limit": True}, {"daily_limit": "5"}, {"daily_limit": 2.5},
    {"note": 3}, {"note": "x" * 1001}, {"approve": True}, [], "talk",
])
def test_bad_choices_are_refused_and_nothing_is_kept(tmp_path, change):
    store = ChoicesStore(tmp_path)
    with pytest.raises(ChoicesError):
        store.set(ROOM, change)
    with pytest.raises(ChoicesError):
        store.set_defaults(change)
    assert not (tmp_path / choices_module.FILE_NAME).exists()


@pytest.mark.parametrize("room_id", ["", " ", "../etc", "-room", "a" * 129, "room\n", 3, None])
def test_bad_room_ids_are_refused(tmp_path, room_id):
    store = ChoicesStore(tmp_path)
    with pytest.raises(ChoicesError):
        store.set(room_id, {"tools": "look"})
    with pytest.raises(ChoicesError):
        store.get(room_id)


def test_a_note_keeps_its_lines_but_not_control_characters(tmp_path):
    saved = ChoicesStore(tmp_path).set(ROOM, {"note": "  Line one\nLine\x00 two\x1b[31m  "})
    assert saved.note == "Line one\nLine two[31m"


def test_a_corrupt_file_reads_as_defaults(tmp_path):
    (tmp_path / choices_module.FILE_NAME).write_text("{not json", encoding="utf-8")
    store = ChoicesStore(tmp_path)
    assert store.get(ROOM) == RoomChoices()
    assert store.defaults() == RoomChoices()
    # A room entry a hand edit broke falls back to the defaults on its own.
    (tmp_path / choices_module.FILE_NAME).write_text(json.dumps({
        "version": 1, "defaults": {"tools": "nonsense"},
        "rooms": {ROOM: {"choices": {"tools": "look", "daily_limit": -3}, "at": 1.0},
                  OTHER: {"choices": {"tools": "look"}, "at": 1.0}}}), encoding="utf-8")
    assert store.defaults() == RoomChoices()
    assert store.get(ROOM) == RoomChoices()
    assert store.get(OTHER).tools == "look"


def test_a_room_entry_that_cant_be_used_gets_the_careful_choices_not_the_defaults(tmp_path, caplog):
    (tmp_path / choices_module.FILE_NAME).write_text(json.dumps({
        "version": 1, "defaults": {"tools": "look"},
        "rooms": {ROOM: {"choices": {"tools": "look", "daily_limit": -3}, "at": 1.0},
                  OTHER: {"choices": "talk", "at": "yesterday"}, "room-3": "custom"}}), encoding="utf-8")
    store = ChoicesStore(tmp_path)
    with caplog.at_level(logging.WARNING):
        for room_id in (ROOM, OTHER, "room-3"):
            assert store.get(room_id) == RoomChoices() and store.saved(room_id), room_id
    assert "couldn't read the room choices" in caplog.text
    # Rooms with no choices of their own still get the owner's defaults.
    assert store.get("room-4").tools == "look"
    # Setting the room's choices again mends its entry.
    assert store.set(OTHER, {"share": "nothing"}) == RoomChoices(share="nothing")
    assert ChoicesStore(tmp_path).get(OTHER) == RoomChoices(share="nothing")


def test_defaults_that_cant_be_used_are_the_careful_ones_and_logged(tmp_path, caplog):
    (tmp_path / choices_module.FILE_NAME).write_text(json.dumps({
        "version": 1, "defaults": {"tools": "custom", "custom_tools": "terminal"}, "rooms": {}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert ChoicesStore(tmp_path).get(ROOM) == RoomChoices()
    assert "couldn't read the room choices" in caplog.text and "defaults" in caplog.text


def test_a_save_that_fails_leaves_no_temporary_file(tmp_path, monkeypatch):
    store = ChoicesStore(tmp_path)
    store.set(ROOM, {"tools": "look"})

    def refused(self, target):
        raise PermissionError("read-only")

    monkeypatch.setattr(choices_module.Path, "replace", refused)
    with pytest.raises(PermissionError):
        store.set(ROOM, {"tools": "talk"})
    assert sorted(path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")) == []
    monkeypatch.undo()
    assert ChoicesStore(tmp_path).get(ROOM).tools == "look"


def test_a_save_that_fails_while_writing_leaves_no_temporary_file(tmp_path, monkeypatch):
    store = ChoicesStore(tmp_path)
    store.set(ROOM, {"tools": "look"})
    real_fdopen = choices_module.os.fdopen

    class Full:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, text):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(choices_module.os, "fdopen", lambda *args, **kwargs: Full(real_fdopen(*args, **kwargs)))
    with pytest.raises(OSError):
        store.set(ROOM, {"tools": "talk"})
    assert sorted(path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")) == []
    monkeypatch.undo()
    assert ChoicesStore(tmp_path).get(ROOM).tools == "look"


def test_a_room_named_by_an_id_no_room_has_is_left_out_and_logged(tmp_path, caplog):
    # Only a hand edit writes one: Conduit and the CLI refuse such an id.
    (tmp_path / choices_module.FILE_NAME).write_text(json.dumps({"version": 1, "defaults": None, "rooms": {
        "my room": {"choices": {"tools": "look"}, "at": 1.0},
        ROOM: {"choices": {"tools": "look"}, "at": 1.0}}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert ChoicesStore(tmp_path).get(ROOM).tools == "look"
    assert "an id no room has" in caplog.text
    assert set(ChoicesStore(tmp_path).view()["rooms"]) == {ROOM}


def test_the_oldest_rooms_go_past_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(choices_module, "MAX_ROOMS", 3)
    clock = Clock()
    store = ChoicesStore(tmp_path, clock=clock)
    for number in range(4):
        clock.now += 1
        store.set(f"room-{number}", {"tools": "look"})
    assert set(store.view()["rooms"]) == {"room-1", "room-2", "room-3"}


def test_the_file_is_private(tmp_path):
    store = ChoicesStore(tmp_path)
    store.set(ROOM, {"tools": "look"})
    assert store.path.stat().st_mode & 0o777 == 0o600


# --- Routes ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_read_and_change_a_rooms_choices(client, tmp_path):
    fresh = client.get(f"{BASE}/rooms/choices", params={"room_id": ROOM})
    assert fresh.status_code == 200
    assert fresh.headers["cache-control"] == "no-store"
    assert fresh.json() == {"ok": True, "room_id": ROOM, "choices": RoomChoices().as_dict(), "saved": False}

    put = client.put(f"{BASE}/rooms/choices", json={"room_id": ROOM, "choices": {"tools": "look"}})
    assert put.status_code == 200
    assert put.json()["choices"]["tools"] == "look"
    assert client.get(f"{BASE}/rooms/choices", params={"room_id": ROOM}).json()["saved"] is True
    assert ChoicesStore(tmp_path).get(ROOM).tools == "look"

    listed = client.get(f"{BASE}/rooms/choices").json()
    assert set(listed["rooms"]) == {ROOM}


def test_routes_read_and_change_the_defaults(client, tmp_path):
    assert client.get(f"{BASE}/rooms/defaults").json() == {"ok": True, "choices": RoomChoices().as_dict()}
    put = client.put(f"{BASE}/rooms/defaults", json={"choices": {"share": "nothing"}})
    assert put.status_code == 200 and put.json()["choices"]["share"] == "nothing"
    assert ChoicesStore(tmp_path).defaults().share == "nothing"


@pytest.mark.parametrize("body", [
    {}, {"room_id": ROOM}, {"room_id": ROOM, "choices": "look"}, {"room_id": "../x", "choices": {}},
    {"room_id": ROOM, "choices": {"tools": "admin"}}, {"room_id": ROOM, "choices": {"custom_tools": ["memory"]}},
    [],
])
def test_routes_refuse_bad_choices(client, body):
    response = client.put(f"{BASE}/rooms/choices", json=body)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_routes_refuse_a_bad_room_id_on_read(client):
    assert client.get(f"{BASE}/rooms/choices", params={"room_id": "../x"}).status_code == 400


def test_room_rules_is_a_listed_capability(client):
    assert "room-rules" in client.get(f"{BASE}/capabilities").json()["capabilities"]


# --- CLI ------------------------------------------------------------------------


def _rooms_args(action, room="", **changes):
    values = {"tools": None, "tool": None, "share": None, "search": None, "takes_part": None, "limit": None,
              "note": None, **changes}
    return argparse.Namespace(conduit_push_action="rooms", rooms_action=action, room=room, **values)


def test_cli_shows_and_sets_room_choices(tmp_path, monkeypatch, capsys):
    cli = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    assert cli.dispatch(_rooms_args("set", ROOM, tools="custom", tool=["terminal", "read_file"], search="on",
                                    limit=10, note="No work talk")) == 0
    saved = ChoicesStore(tmp_path).get(ROOM)
    assert (saved.tools, saved.custom_tools, saved.search_chats, saved.daily_limit, saved.note) == (
        "custom", ("terminal", "read_file"), True, 10, "No work talk")
    assert cli.dispatch(_rooms_args("set", share="nothing", takes_part="asked")) == 0
    assert ChoicesStore(tmp_path).defaults() == RoomChoices(share="nothing", takes_part="asked")
    capsys.readouterr()

    assert cli.dispatch(_rooms_args("show")) == 0
    shown = capsys.readouterr().out
    assert "Defaults" in shown and f"Room {ROOM}" in shown and "terminal, read_file" in shown
    assert cli.dispatch(_rooms_args("show", OTHER)) == 0
    assert "the defaults" in capsys.readouterr().out


def test_cli_refuses_choices_the_store_wont_keep(tmp_path, monkeypatch, capsys):
    cli = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    assert cli.dispatch(_rooms_args("set", ROOM, tools="custom", tool=["memory"])) == 1
    assert "never allowed" in capsys.readouterr().out
    assert cli.dispatch(_rooms_args("set", ROOM, limit=0)) == 1
    assert cli.dispatch(_rooms_args("set", ROOM)) == 2
    assert cli.dispatch(_rooms_args("show", "../x")) == 1
    assert not (tmp_path / choices_module.FILE_NAME).exists()


def test_cli_says_when_the_choices_cant_be_saved(tmp_path, monkeypatch, capsys):
    cli = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    (tmp_path / choices_module.FILE_NAME).mkdir()
    assert cli.dispatch(_rooms_args("set", ROOM, tools="look")) == 1
    assert capsys.readouterr().out.startswith("Couldn't set that:")
