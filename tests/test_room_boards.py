"""Boards in group rooms: kept on the host for Conduit to draw under the
agent's message (room_boards, GET /boards/rooms)."""

import contextlib
import importlib.util
import json
import pathlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

_spec = importlib.util.spec_from_file_location("conduit_push_test_room_boards", ROOT / "room_boards.py")
room_boards = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = room_boards
_spec.loader.exec_module(room_boards)

_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_boards", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
sys.modules[_api_spec.name] = api
_api_spec.loader.exec_module(api)

BOARD = {"cards": [{"type": "callout", "callout": {"text": "Book early", "tone": "tip"}}], "markdown": "Book early"}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_a_rooms_boards_are_kept_by_room_and_agent(tmp_path):
    store = room_boards.RoomBoards(tmp_path, clock=Clock())
    store.add("room-1", "researcher", BOARD)
    store.clock.now += 5
    store.add("room-2", "default", BOARD)
    assert store.room("room-1") == [{"profile": "researcher", "at": 1_000_000.0, "board": BOARD}]
    assert [entry["profile"] for entry in store.room("room-2")] == ["default"]


def test_a_room_keeps_its_newest_boards_and_old_ones_go(tmp_path):
    store = room_boards.RoomBoards(tmp_path, clock=Clock())
    for index in range(room_boards.MAX_PER_ROOM + 3):
        store.clock.now += 1
        store.add("room-1", "a", {"n": index, **BOARD})
    kept = store.room("room-1")
    assert len(kept) == room_boards.MAX_PER_ROOM and kept[-1]["board"]["n"] == room_boards.MAX_PER_ROOM + 2
    store.clock.now += room_boards.KEEP_S
    assert store.room("room-1") == []


def test_a_hand_edited_file_is_read_safely(tmp_path):
    store = room_boards.RoomBoards(tmp_path, clock=Clock())
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"boards": ["x", {"room": "room-1"}, {"room": "room-1", "profile": "a",
                                                                           "at": 1_000_000.0, "board": BOARD}]}))
    assert len(store.room("room-1")) == 1
    store.path.write_text("not json")
    assert store.room("room-1") == []


def test_the_route_reads_a_rooms_boards(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    identity = api.core._plugin_package_module("room_identity")
    monkeypatch.setattr(identity, "root_home", lambda home=None: tmp_path)
    api.core._plugin_package_module("room_boards").RoomBoards(tmp_path).add("room-1", "a", BOARD)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    response = http.get(f"{BASE}/boards/rooms", params={"room_id": "room-1"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert [entry["board"] for entry in response.json()["boards"]] == [BOARD]
    assert http.get(f"{BASE}/boards/rooms").status_code == 400
    assert "room-boards" in api.capabilities.ROUTE_CAPABILITIES


def test_the_plugin_keeps_a_board_only_for_a_room_it_runs(tmp_path, monkeypatch):
    import tempfile
    import types
    if "hermes_constants" not in sys.modules:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
        sys.modules["hermes_constants"] = constants
    if "conduit_push" not in sys.modules:
        package = types.ModuleType("conduit_push")
        package.__path__ = [str(ROOT)]
        sys.modules["conduit_push"] = package
    plugin = sys.modules.get("conduit_push.__init__")
    if plugin is None:
        spec = importlib.util.spec_from_file_location("conduit_push.__init__", ROOT / "__init__.py")
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
    from conduit_push import room_boards as kept_boards, room_identity, room_setup
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(room_identity, "root_home", lambda home=None: tmp_path)
    monkeypatch.setattr(room_setup, "profile_name", lambda home: "researcher")
    turns = {"here": plugin.room_rules.RoomTurn(room_id="room-1", side="home", with_others=False, session_id="here"),
             "away": plugin.room_rules.RoomTurn(room_id="room-2", side="member", with_others=True, session_id="away")}
    monkeypatch.setattr(plugin.room_rules, "room_turn", lambda session, home=None: turns.get(session))
    assert plugin._save_room_board("here", BOARD) is True
    assert plugin._save_room_board("away", BOARD) is False, "a room another host runs"
    assert plugin._save_room_board("chat", BOARD) is False
    assert [entry["profile"] for entry in kept_boards.RoomBoards(tmp_path).room("room-1")] == ["researcher"]
