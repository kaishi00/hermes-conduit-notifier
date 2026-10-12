import contextlib
import importlib.util
import json
import os
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


focus = _load("conduit_desktop_focus_test", ROOT / "desktop_focus.py")
api = _load("conduit_plugin_api_desktop_focus", ROOT / "dashboard" / "plugin_api.py")

CLIENT = "window0001"


# --- The store ---------------------------------------------------------------


def test_a_focused_window_puts_its_chat_on_screen_until_the_hold_runs_out(tmp_path):
    assert focus.report(tmp_path, CLIENT, ["rt-1", "st-1"], 60, now=1000.0) == {"status": "focused"}
    assert focus.on_screen(tmp_path, "rt-1", now=1000.0)
    assert focus.on_screen(tmp_path, "st-1", now=1059.0)
    assert not focus.on_screen(tmp_path, "other", now=1000.0)
    assert not focus.on_screen(tmp_path, "st-1", now=1060.0)


def test_losing_focus_takes_the_chat_off_screen(tmp_path):
    focus.report(tmp_path, CLIENT, ["rt-1"], 60, now=1000.0)
    assert focus.report(tmp_path, CLIENT, [], 0, now=1001.0) == {"status": "away"}
    assert not focus.on_screen(tmp_path, "rt-1", now=1001.0)
    assert list((tmp_path / focus.DIR_NAME).iterdir()) == []


def test_each_window_reports_for_itself(tmp_path):
    focus.report(tmp_path, "window-aaaa", ["rt-1"], 60, now=1000.0)
    focus.report(tmp_path, "window-bbbb", ["rt-2"], 60, now=1000.0)
    focus.report(tmp_path, "window-aaaa", [], 0, now=1001.0)
    assert not focus.on_screen(tmp_path, "rt-1", now=1001.0)
    assert focus.on_screen(tmp_path, "rt-2", now=1001.0)


def test_a_switch_moves_the_window_to_the_new_chat(tmp_path):
    focus.report(tmp_path, CLIENT, ["rt-1"], 60, now=1000.0)
    focus.report(tmp_path, CLIENT, ["rt-2"], 60, now=1001.0)
    assert not focus.on_screen(tmp_path, "rt-1", now=1001.0)
    assert focus.on_screen(tmp_path, "rt-2", now=1001.0)


def test_holds_are_capped(tmp_path):
    focus.report(tmp_path, CLIENT, ["rt-1"], 10_000, now=1000.0)
    assert focus.on_screen(tmp_path, "rt-1", now=1000.0 + focus.MAX_HOLD_S - 1)
    assert not focus.on_screen(tmp_path, "rt-1", now=1000.0 + focus.MAX_HOLD_S)


def test_run_out_reports_are_cleared_by_the_next_report(tmp_path):
    focus.report(tmp_path, "window-aaaa", ["rt-1"], 60, now=1000.0)
    focus.report(tmp_path, "window-bbbb", ["rt-2"], 60, now=2000.0)
    assert sorted(path.name for path in (tmp_path / focus.DIR_NAME).iterdir()) == ["window-bbbb.json"]


def test_unreadable_reports_never_hold_a_push_back(tmp_path):
    folder = tmp_path / focus.DIR_NAME
    folder.mkdir()
    (folder / "window-aaaa.json").write_text("{not json")
    (folder / "window-bbbb.json").write_text(json.dumps({"session_ids": "rt-1", "until": 9e9}))
    (folder / "window-cccc.json").write_text(json.dumps({"session_ids": ["rt-1"], "until": "later"}))
    assert not focus.on_screen(tmp_path, "rt-1", now=1000.0)
    assert not focus.on_screen(tmp_path / "missing", "rt-1", now=1000.0)
    assert not focus.on_screen(tmp_path, "", now=1000.0)
    assert not focus.on_screen(tmp_path, None, now=1000.0)


def test_report_files_are_private(tmp_path):
    focus.report(tmp_path, CLIENT, ["rt-1"], 60, now=1000.0)
    if os.name == "posix":
        assert ((tmp_path / focus.DIR_NAME / f"{CLIENT}.json").stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("client, ids, hold", [
    ("short", ["rt-1"], 60),
    ("../../escape", ["rt-1"], 60),
    (None, ["rt-1"], 60),
    (CLIENT, "rt-1", 60),
    (CLIENT, ["a", "b", "c", "d", "e"], 60),
    (CLIENT, [7], 60),
    (CLIENT, ["x" * 201], 60),
    (CLIENT, ["rt-1"], -1),
    (CLIENT, ["rt-1"], True),
    (CLIENT, ["rt-1"], float("nan")),
    (CLIENT, ["rt-1"], "60"),
])
def test_malformed_reports_are_refused(tmp_path, client, ids, hold):
    with pytest.raises(ValueError):
        focus.report(tmp_path, client, ids, hold, now=1000.0)


# --- The route -----------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_profile_scope", lambda profile: contextlib.nullcontext())
    api._desktop_focus_limiter._mints.clear()
    return tmp_path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_route_records_and_clears_a_window(home):
    http = _http()
    response = http.put(f"{BASE}/desktop/focus", json={"client": CLIENT, "session_ids": ["rt-1", None], "hold_s": 60})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "status": "focused"}
    assert focus.on_screen(home, "rt-1")
    response = http.put(f"{BASE}/desktop/focus", json={"client": CLIENT, "session_ids": [], "hold_s": 0})
    assert response.json() == {"ok": True, "status": "away"}
    assert not focus.on_screen(home, "rt-1")


@pytest.mark.parametrize("body", [[], {"client": "bad", "session_ids": [], "hold_s": 0},
                                  {"client": CLIENT, "session_ids": ["rt-1"]}])
def test_the_route_refuses_malformed_reports(home, body):
    response = _http().put(f"{BASE}/desktop/focus", json=body)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
