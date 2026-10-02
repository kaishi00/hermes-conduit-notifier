import contextlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
OWN_PID = 1000
DESKTOP_PID = 2000

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_takeover", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


class FakeRegistry:
    """hermes_cli.active_sessions' private helpers, over a real JSON file."""

    def __init__(self, home: Path, dead_pids=(), unreadable=False):
        self.home = home
        self.dead_pids = set(dead_pids)
        self.unreadable = unreadable
        self.locked = False

    def _lease_paths(self, lease=None, registry_home=None):
        home = Path(registry_home)
        return home / "runtime" / "active_sessions.json", home / "runtime" / "active_sessions.lock"

    @contextlib.contextmanager
    def _FileLock(self, path):
        assert not self.locked
        self.locked = True
        try:
            yield self
        finally:
            self.locked = False

    def _read_entries(self, path, *, strict=False):
        assert self.locked
        if self.unreadable:
            raise RuntimeError("active session registry unreadable")
        if not path.exists():
            return []
        return json.loads(path.read_text())["entries"]

    def _write_entries(self, path, entries):
        assert self.locked
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entries": entries}))

    def _pid_liveness(self, pid, process_start_time=None, *, lenient=False):
        return pid not in self.dead_pids


class FakeTurnMarker:
    def __init__(self, markers=None, states=None):
        self.markers = markers or {}
        self.states = states or {}

    def read_turn_marker(self, home, session_key):
        return self.markers.get(session_key)

    def marker_writer_state(self, entry):
        return self.states.get(entry.get("writer_pid"), "alive")


def _entry(session_id, pid=DESKTOP_PID, surface="desktop", lease="lease-1"):
    return {"lease_id": lease, "session_id": session_id, "surface": surface, "pid": pid,
            "started_at": 1.0, "updated_at": 1.0}


def _seed(home: Path, entries):
    path = home / "runtime" / "active_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}))
    return path


def _entries(home: Path):
    return json.loads((home / "runtime" / "active_sessions.json").read_text())["entries"]


def _take(home, ids, registry=None, marker=None):
    return api.take_over_session(ids, registry=registry or FakeRegistry(home), turn_marker=marker,
                                 home=home, own_pid=OWN_PID)


def test_free_when_nobody_holds_the_chat(tmp_path):
    _seed(tmp_path, [_entry("other")])
    assert _take(tmp_path, ["chat"]) == {"status": "free"}
    assert _entries(tmp_path) == [_entry("other")]


def test_free_without_a_registry_file(tmp_path):
    assert _take(tmp_path, ["chat"]) == {"status": "free"}


def test_drops_only_the_desktop_claim(tmp_path):
    _seed(tmp_path, [_entry("chat"), _entry("other", lease="lease-2")])
    assert _take(tmp_path, ["runtime-id", "chat"]) == {"status": "taken_over", "surface": "desktop"}
    assert _entries(tmp_path) == [_entry("other", lease="lease-2")]


def test_waits_while_the_owner_runs_a_turn(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID, "started_at": 1.0}})
    assert _take(tmp_path, ["chat"], marker=marker) == {"status": "busy", "surface": "desktop"}
    assert _entries(tmp_path) == [_entry("chat")]


def test_unknown_marker_writer_still_counts_as_running(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}}, {DESKTOP_PID: "unknown"})
    assert _take(tmp_path, ["chat"], marker=marker)["status"] == "busy"


def test_a_crashed_turn_marker_does_not_block(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}}, {DESKTOP_PID: "dead"})
    assert _take(tmp_path, ["chat"], marker=marker)["status"] == "taken_over"
    assert _entries(tmp_path) == []


def test_another_writers_marker_does_not_block(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    marker = FakeTurnMarker({"chat": {"writer_pid": 4242}})
    assert _take(tmp_path, ["chat"], marker=marker)["status"] == "taken_over"


def test_never_touches_this_dashboards_own_claim(tmp_path):
    _seed(tmp_path, [_entry("chat", pid=OWN_PID, surface="tui")])
    assert _take(tmp_path, ["chat"]) == {"status": "same_host", "surface": "tui"}
    assert _entries(tmp_path) == [_entry("chat", pid=OWN_PID, surface="tui")]


def test_a_dead_owner_is_dropped_without_waiting(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, dead_pids={DESKTOP_PID})
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "taken_over"
    assert _entries(tmp_path) == []


def test_an_unreadable_registry_is_never_guessed(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    with pytest.raises(api.TokenError) as raised:
        _take(tmp_path, ["chat"], registry=FakeRegistry(tmp_path, unreadable=True))
    assert raised.value.status == 503
    assert _entries(tmp_path) == [_entry("chat")]


# --- Route ---------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    api._takeover_limiter = api._MintLimiter(api.TAKEOVER_LIMIT, api.TAKEOVER_WINDOW_S)
    monkeypatch.setattr(api, "_profile_scope", lambda profile: contextlib.nullcontext())
    monkeypatch.setattr(api, "_takeover_modules", lambda: (FakeRegistry(tmp_path), FakeTurnMarker()))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_route_takes_the_chat_over(client, tmp_path):
    _seed(tmp_path, [_entry("chat")])
    response = client.post(f"{BASE}/sessions/takeover", json={"session_ids": ["chat"]})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "taken_over", "surface": "desktop"}
    assert response.headers["cache-control"] == "no-store"
    assert _entries(tmp_path) == []


@pytest.mark.parametrize("body", [{}, {"session_ids": []}, {"session_ids": [""]}, {"session_ids": [1]},
                                  {"session_ids": ["a", "b", "c", "d", "e"]}, {"session_ids": ["x" * 201]}])
def test_route_rejects_bad_ids(client, body):
    assert client.post(f"{BASE}/sessions/takeover", json=body).status_code == 400


def test_route_reports_an_unsupported_hermes(monkeypatch, client):
    def missing():
        raise api.TokenError(501, "This Hermes version has no chat ownership registry")

    monkeypatch.setattr(api, "_takeover_modules", missing)
    assert client.post(f"{BASE}/sessions/takeover", json={"session_ids": ["chat"]}).status_code == 501
