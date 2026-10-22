"""A reply that lands in a chat Conduit has on screen sends no push (chat_on_screen.py)."""
import importlib.util
import pathlib
import sys
import tempfile
import time
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
if "tools.clarify_tool" not in sys.modules:
    _tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    _tools.__path__ = getattr(_tools, "__path__", [])
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        # Same semantics as test_clarify_loop's stub, whichever module loads first.
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool
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


on_screen = _load("conduit_push.chat_on_screen", ROOT / "chat_on_screen.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
api = _load("conduit_plugin_api_chat_on_screen", ROOT / "dashboard" / "plugin_api.py")


# --- The store ---------------------------------------------------------------


def test_a_held_chat_is_on_screen_by_any_of_its_ids_until_the_hold_runs_out(tmp_path):
    assert on_screen.hold(tmp_path, "mac", ["runtime-1", "stored-1"], 90, now=1000.0) is True
    assert on_screen.on_screen(tmp_path, "stored-1", now=1000.0)
    assert on_screen.on_screen(tmp_path, " runtime-1 ", now=1089.0)
    assert not on_screen.on_screen(tmp_path, "stored-2", now=1000.0)
    assert not on_screen.on_screen(tmp_path, "stored-1", now=1090.0)


def test_a_hold_of_zero_ends_only_that_devices_hold(tmp_path):
    on_screen.hold(tmp_path, "iphone", ["s-1"], 90, now=1000.0)
    on_screen.hold(tmp_path, "mac", ["s-1"], 90, now=1000.0)
    assert on_screen.hold(tmp_path, "iphone", [], 0, now=1001.0) is False
    assert on_screen.on_screen(tmp_path, "s-1", now=1001.0), "the Mac still has it on screen"
    on_screen.hold(tmp_path, "mac", ["s-1"], 0, now=1002.0)
    assert not on_screen.on_screen(tmp_path, "s-1", now=1002.0)
    assert list((tmp_path / on_screen.DIRECTORY).iterdir()) == []


def test_a_new_hold_replaces_the_devices_last_chat(tmp_path):
    on_screen.hold(tmp_path, "ipad", ["s-1"], 90, now=1000.0)
    on_screen.hold(tmp_path, "ipad", ["s-2"], 90, now=1005.0)
    assert not on_screen.on_screen(tmp_path, "s-1", now=1005.0)
    assert on_screen.on_screen(tmp_path, "s-2", now=1005.0)


@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    '{"ids": ["s-1"]}',
    '{"ids": ["s-1"], "until": true}',
    '{"ids": ["s-1"], "until": "2000"}',
    '{"ids": "s-1", "until": 1050}',
    '{"ids": ["s-1"], "until": 1e400}',
    '{"ids": ["s-1"], "until": 10' + "0" * 400 + '}',
    # Further out than one request can hold: an edited file or a clock step.
    '{"ids": ["s-1"], "until": 2000}',
    '{"ids": [' + ", ".join(['"s-1"'] * 17) + '], "until": 1050}',
])
def test_anything_hold_doesnt_write_counts_as_nothing_on_screen(tmp_path, raw):
    (tmp_path / on_screen.DIRECTORY).mkdir()
    (tmp_path / on_screen.DIRECTORY / "iphone.json").write_text(raw, encoding="utf-8")
    assert not on_screen.on_screen(tmp_path, "s-1", now=1000.0)


def test_the_file_is_private_and_no_temp_file_is_left(tmp_path):
    on_screen.hold(tmp_path, "iphone", ["s-1"], 30, now=1000.0)
    directory = tmp_path / on_screen.DIRECTORY
    assert [path.name for path in directory.iterdir()] == ["iphone.json"]
    assert (directory / "iphone.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("body", [
    [],
    {"device": "android", "session_ids": ["s-1"], "hold_s": 30},
    {"device": "iphone", "session_ids": ["s-1"], "hold_s": -1},
    {"device": "iphone", "session_ids": ["s-1"], "hold_s": 601},
    {"device": "iphone", "session_ids": ["s-1"], "hold_s": True},
    {"device": "iphone", "session_ids": ["s-1"], "hold_s": float("nan")},
    {"device": "iphone", "session_ids": ["s-1"], "hold_s": float("inf")},
    # An integer too large for a float is a 400, not an OverflowError.
    {"device": "iphone", "session_ids": ["s-1"], "hold_s": 10 ** 400},
    {"device": "iphone", "session_ids": "s-1", "hold_s": 30},
    {"device": "iphone", "session_ids": [1], "hold_s": 30},
    {"device": "iphone", "session_ids": [" "], "hold_s": 30},
    {"device": "iphone", "session_ids": ["x" * 201], "hold_s": 30},
    {"device": "iphone", "session_ids": [f"s-{n}" for n in range(17)], "hold_s": 30},
    {"device": "iphone", "session_ids": [], "hold_s": 30},
])
def test_bad_bodies_are_refused(body):
    with pytest.raises(ValueError):
        on_screen.parse(body)


def test_anything_but_a_positive_hold_releases(tmp_path):
    on_screen.hold(tmp_path, "mac", ["s-1"], 90, now=1000.0)
    assert not on_screen.hold(tmp_path, "mac", ["s-1"], -5, now=1000.0)
    on_screen.hold(tmp_path, "mac", ["s-1"], 90, now=1000.0)
    assert not on_screen.hold(tmp_path, "mac", ["s-1"], float("nan"), now=1000.0)
    assert not on_screen.on_screen(tmp_path, "s-1", now=1001.0)


def test_a_huge_hold_is_capped(tmp_path):
    assert on_screen.hold(tmp_path, "mac", ["s-1"], 10 ** 400, now=1000.0)
    assert on_screen.on_screen(tmp_path, "s-1", now=1000.0 + on_screen.MAX_HOLD_S - 1)


def test_only_a_turn_within_the_host_cap_counts_as_attended():
    assert on_screen.attended(100.0, now=100.0 + on_screen.MAX_TURN_S)
    assert not on_screen.attended(100.0, now=100.0 + on_screen.MAX_TURN_S + 1)
    assert not on_screen.attended(None)


def test_a_release_needs_no_ids():
    assert on_screen.parse({"device": "mac", "hold_s": 0}) == ("mac", [], 0.0)


# --- The reply-ready push ------------------------------------------------------


@pytest.fixture
def turn(tmp_path, monkeypatch):
    """Runs a finished turn's hooks for chat ``s-1`` and returns what was pushed."""
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    monkeypatch.setattr(plugin.calls, "failed_turn", lambda *args, **kwargs: False)
    monkeypatch.setattr(plugin.cron_runs, "quiet", lambda *args: False)
    monkeypatch.setattr(plugin, "_desktop_watched", lambda session_id, started: False)
    monkeypatch.setattr(plugin, "_source_notifies", lambda platform: True)
    monkeypatch.setattr(plugin, "_session_title", lambda session_id: "")
    monkeypatch.setattr(plugin, "_titled", lambda title, session_id: title)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    pushed = []
    monkeypatch.setattr(plugin, "enqueue", pushed.append)

    def run(failed=False, ran_s=5.0):
        pushed.clear()
        plugin._turn_started["t-1"] = time.monotonic() - ran_s
        plugin._post_llm_call(session_id="s-1", turn_id="t-1", assistant_response="Done.", platform="tui")
        plugin._on_session_end(session_id="s-1", turn_id="t-1", completed=not failed, failed=failed,
                               interrupted=False, platform="tui")
        return [event["type"] for event in pushed]

    run.home = tmp_path
    return run


def test_a_reply_in_a_chat_conduit_has_on_screen_sends_no_push(turn):
    assert turn() == ["response.ready"]
    on_screen.hold(turn.home, "mac", ["runtime-1", "s-1"], 90)
    assert turn() == []
    on_screen.hold(turn.home, "mac", [], 0)
    assert turn() == ["response.ready"]


def test_a_turn_past_the_host_cap_pushes_even_while_held(turn):
    on_screen.hold(turn.home, "mac", ["s-1"], 90)
    assert turn(ran_s=on_screen.MAX_TURN_S + 60) == ["response.ready"]


def test_another_chat_on_screen_doesnt_quiet_this_one(turn):
    on_screen.hold(turn.home, "iphone", ["s-2"], 90)
    assert turn() == ["response.ready"]


def test_a_failed_turn_still_pushes_in_a_chat_on_screen(turn):
    on_screen.hold(turn.home, "iphone", ["s-1"], 90)
    assert turn(failed=True) == ["turn.failed"]


def test_an_unreadable_hold_lets_the_push_through(turn, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(plugin.chat_on_screen, "on_screen", broken)
    assert turn() == ["response.ready"]


# --- Route -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_cron_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_on_screen_limiter", api._MintLimiter(api.ON_SCREEN_LIMIT, api.ON_SCREEN_WINDOW_S))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    test_client = TestClient(app)
    test_client.home = tmp_path
    return test_client


def test_the_route_holds_and_releases_a_chat(client):
    response = client.put(f"{BASE}/notifications/on-screen",
                          json={"device": "iphone", "session_ids": ["r-1", "s-1"], "hold_s": 90})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "on_screen": True}
    assert on_screen.on_screen(client.home, "s-1")
    response = client.put(f"{BASE}/notifications/on-screen", json={"device": "iphone", "hold_s": 0})
    assert response.json() == {"ok": True, "on_screen": False}
    assert not on_screen.on_screen(client.home, "s-1")


def test_a_bad_body_is_a_400(client):
    response = client.put(f"{BASE}/notifications/on-screen", json={"device": "pager", "hold_s": 0})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert client.put(f"{BASE}/notifications/on-screen", json=["iphone"]).status_code == 400
    response = client.put(f"{BASE}/notifications/on-screen", content=b"{not json",
                          headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
