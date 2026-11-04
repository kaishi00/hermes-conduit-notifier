"""Boards in Hermes Desktop: kept on the host by the id the agent's
::conduit-board line names, for the plugin's Desktop half to draw
(room_boards.DesktopBoards, GET /boards/desktop, POST /boards/desktop/seen,
desktop/plugin.js)."""

import contextlib
import importlib.util
import json
import pathlib
import re
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_room_boards import BOARD, Clock, api, room_boards

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _ids():
    count = iter(range(1, 10_000))
    return lambda: f"cb-{next(count):012x}"


def test_a_desktop_board_is_kept_by_its_id(tmp_path):
    store = room_boards.DesktopBoards(tmp_path, clock=Clock(), new_id=_ids())
    first = store.add(BOARD)
    assert first == "cb-000000000001" and store.get(first) == BOARD
    assert store.get("cb-0000000000ff") is None, "a board that's gone"
    for bad in ("", "cb-1", "../x", "CB-000000000001", None, 7):
        try:
            store.get(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} named a board")
    store.clock.now += room_boards.KEEP_S
    assert store.get(first) is None, "a month later it's forgotten"


def test_the_file_keeps_the_newest_boards(tmp_path):
    store = room_boards.DesktopBoards(tmp_path, clock=Clock(), new_id=_ids())
    ids = [store.add({"n": index}) for index in range(room_boards.MAX_DESKTOP_BOARDS + 2)]
    assert store.get(ids[0]) is None and store.get(ids[-1]) == {"n": room_boards.MAX_DESKTOP_BOARDS + 1}
    assert (store.path.stat().st_mode & 0o777) == 0o600 and (store.path.parent.stat().st_mode & 0o777) == 0o700


def test_the_half_counts_as_loaded_for_a_while_after_it_says_so(tmp_path):
    store = room_boards.DesktopBoards(tmp_path, clock=Clock())
    assert store.half_live() is False
    store.saw_half()
    assert store.half_live() is True
    store.clock.now += room_boards.HALF_LIVE_S
    assert store.half_live() is False, "a half that stopped saying so, turned off or gone"
    store.half_path.write_text(json.dumps({"seen": True}))
    assert store.half_live() is False
    assert not store.path.exists(), "saying it's loaded leaves the boards alone"
    store.half_path.write_text("not json")
    assert store.half_live() is False


def test_the_routes_read_a_board_and_note_the_half(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    identity = api.core._plugin_package_module("room_identity")
    monkeypatch.setattr(identity, "root_home", lambda home=None: tmp_path)
    kept = api.core._plugin_package_module("room_boards").DesktopBoards(tmp_path)
    board_id = kept.add(BOARD)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    response = http.get(f"{BASE}/boards/desktop", params={"id": board_id})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"board": BOARD}
    assert http.get(f"{BASE}/boards/desktop", params={"id": "cb-0000000000ff"}).status_code == 404
    assert http.get(f"{BASE}/boards/desktop", params={"id": "nope"}).status_code == 400
    assert http.get(f"{BASE}/boards/desktop").status_code == 400
    assert kept.half_live() is False
    assert http.post(f"{BASE}/boards/desktop/seen", json={}).json() == {"ok": True}
    assert kept.half_live() is True
    assert "desktop-boards" in api.capabilities.ROUTE_CAPABILITIES


def _plugin():
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
    return plugin


def test_the_plugin_keeps_a_desktop_board_only_while_the_half_is_loaded(tmp_path, monkeypatch):
    plugin = _plugin()
    from conduit_push import room_boards as kept_boards, room_identity
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path / "profiles" / "work")
    monkeypatch.setattr(room_identity, "root_home", lambda home=None: tmp_path)
    assert plugin._save_desktop_board(BOARD) is None and plugin._desktop_half_live() is False
    store = kept_boards.DesktopBoards(tmp_path)
    store.saw_half()
    board_id = plugin._save_desktop_board(BOARD)
    assert re.fullmatch(r"cb-[0-9a-f]{12}", board_id) and store.get(board_id) == BOARD, "kept in the root home"
    assert plugin._desktop_half_live() is True


def test_the_desktop_half_claims_the_line_the_tool_writes():
    source = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "id: ID," in source and "const ID = 'conduit_push'" in source, "the half's id is the package's folder"
    assert "name: 'conduit-board'" in source
    assert "const BOARD_ID = /^cb-[0-9a-f]{12}$/" in source
    assert room_boards.DESKTOP_BOARD_ID.pattern == "cb-[0-9a-f]{12}"
    imports = re.findall(r"^import .* from '([^']+)'", source, flags=re.MULTILINE)
    assert set(imports) <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}, "Desktop resolves only these"
    for route in ("/boards/desktop?id=", "/boards/desktop/seen", "/boards/checklists"):
        assert route in source
