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

    def __init__(self, home: Path, dead_pids=(), unknown_pids=(), unreadable=False, starts=None):
        self.home = home
        self.starts = starts or {}  # pid -> the running process's start time (default START)
        self.dead_pids = set(dead_pids)
        self.unknown_pids = set(unknown_pids)
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
        if pid in self.unknown_pids:
            return None
        if process_start_time is not None and process_start_time != self.starts.get(pid, START):
            return False  # the pid was recycled by another process
        return pid not in self.dead_pids


class FakeTurnMarker:
    def __init__(self, markers=None, states=None):
        self.markers = markers or {}
        self.states = states or {}

    def read_turn_marker(self, home, session_key):
        return self.markers.get(session_key)

    def marker_writer_state(self, entry):
        return self.states.get(entry.get("writer_pid"), "alive")


START = 100.0


def _entry(session_id, pid=DESKTOP_PID, surface="desktop", lease="lease-1", start=START):
    return {"lease_id": lease, "session_id": session_id, "surface": surface, "pid": pid,
            "process_start_time": start, "started_at": 1.0, "updated_at": 1.0}


def _seed(home: Path, entries):
    path = home / "runtime" / "active_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}))
    return path


def _entries(home: Path):
    return json.loads((home / "runtime" / "active_sessions.json").read_text())["entries"]


def _take(home, ids, registry=None, marker=None):
    return api.take_over_session(ids, registry=registry or FakeRegistry(home), turn_marker=marker or FakeTurnMarker(),
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


def test_a_child_process_marker_still_blocks(tmp_path):
    # An isolated turn runs in a compute-host child, so its marker's writer
    # isn't the owner's pid.
    _seed(tmp_path, [_entry("chat")])
    marker = FakeTurnMarker({"chat": {"writer_pid": 4242}})
    assert _take(tmp_path, ["chat"], marker=marker)["status"] == "busy"


def test_a_marker_under_another_alias_still_blocks(tmp_path):
    _seed(tmp_path, [_entry("stored-id")])
    marker = FakeTurnMarker({"runtime-id": {"writer_pid": DESKTOP_PID}})
    assert _take(tmp_path, ["runtime-id", "stored-id"], marker=marker)["status"] == "busy"


def test_an_owner_without_a_pid_is_never_dropped_even_with_a_marker(tmp_path):
    entry = {**_entry("chat"), "pid": None}
    _seed(tmp_path, [entry])
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}})
    assert _take(tmp_path, ["chat"], marker=marker)["status"] == "same_host"
    assert _entries(tmp_path) == [entry]


def test_never_touches_this_dashboards_own_claim(tmp_path):
    _seed(tmp_path, [_entry("chat", pid=OWN_PID, surface="tui")])
    assert _take(tmp_path, ["chat"]) == {"status": "same_host", "surface": "tui"}
    assert _entries(tmp_path) == [_entry("chat", pid=OWN_PID, surface="tui")]


def test_drops_the_desktop_claim_but_reports_this_dashboards_own(tmp_path):
    own = _entry("runtime-id", pid=OWN_PID, surface="tui", lease="lease-own")
    _seed(tmp_path, [own, _entry("chat")])
    assert _take(tmp_path, ["runtime-id", "chat"]) == {"status": "same_host", "surface": "tui"}
    assert _entries(tmp_path) == [own]


def test_a_blank_lease_id_never_matches_other_claims(tmp_path):
    other = {**_entry("other", pid=OWN_PID), "lease_id": ""}
    _seed(tmp_path, [{**_entry("chat"), "lease_id": ""}, other])
    assert _take(tmp_path, ["chat"])["status"] == "taken_over"
    assert _entries(tmp_path) == [other]


def test_an_unreadable_marker_counts_as_running(tmp_path):
    class BrokenMarker(FakeTurnMarker):
        def read_turn_marker(self, home, session_key):
            raise OSError("permission denied")

    _seed(tmp_path, [_entry("chat")])
    assert _take(tmp_path, ["chat"], marker=BrokenMarker())["status"] == "busy"


def test_an_owner_of_unknown_liveness_still_counts_as_live(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, unknown_pids={DESKTOP_PID})
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "busy"


@pytest.mark.parametrize("writer_state", ["dead", "unknown"])
def test_a_dead_owner_is_dropped_without_waiting(tmp_path, writer_state):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, dead_pids={DESKTOP_PID})
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}}, {DESKTOP_PID: writer_state})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "taken_over"
    assert _entries(tmp_path) == []


def test_a_dead_owner_whose_isolated_child_still_runs_a_turn_is_busy(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, dead_pids={DESKTOP_PID})
    marker = FakeTurnMarker({"chat": {"writer_pid": 4242}}, {4242: "alive"})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "busy"
    assert _entries(tmp_path) == [_entry("chat")]


def test_a_recycled_pid_is_not_the_live_owner(tmp_path):
    # Desktop's pid now belongs to another process started later.
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, starts={DESKTOP_PID: START + 50})
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}}, {DESKTOP_PID: "unknown"})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "taken_over"
    assert _entries(tmp_path) == []


def test_the_same_pid_and_start_time_is_the_live_owner(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}}, {DESKTOP_PID: "unknown"})
    assert _take(tmp_path, ["chat"], marker=marker)["status"] == "busy"


def test_a_lock_that_is_not_a_context_manager_is_unsupported(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path)
    registry._FileLock = lambda path: None
    with pytest.raises(api.TokenError) as raised:
        _take(tmp_path, ["chat"], registry=registry)
    assert raised.value.status == 501
    assert _entries(tmp_path) == [_entry("chat")]


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


# --- Capability probe -----------------------------------------------------------


def _install(monkeypatch, registry_attrs, marker_attrs):
    hermes_cli = types.ModuleType("hermes_cli")
    registry = types.ModuleType("hermes_cli.active_sessions")
    for name, value in registry_attrs.items():
        setattr(registry, name, value)
    hermes_cli.active_sessions = registry
    tui_gateway = types.ModuleType("tui_gateway")
    marker = types.ModuleType("tui_gateway.turn_marker")
    for name, value in marker_attrs.items():
        setattr(marker, name, value)
    tui_gateway.turn_marker = marker
    for name, module in (("hermes_cli", hermes_cli), ("hermes_cli.active_sessions", registry),
                         ("tui_gateway", tui_gateway), ("tui_gateway.turn_marker", marker)):
        monkeypatch.setitem(sys.modules, name, module)
    return registry, marker


_REGISTRY = {
    "_FileLock": lambda path: None,
    "_lease_paths": lambda lease=None, registry_home=None: (None, None),
    "_read_entries": lambda path, *, strict=False: [],
    "_write_entries": lambda path, entries: None,
    "_pid_liveness": lambda pid, start=None: True,
}
_MARKER = {"read_turn_marker": lambda home, key: None, "marker_writer_state": lambda entry: "dead"}


def test_probe_accepts_the_expected_shape(monkeypatch):
    registry, marker = _install(monkeypatch, _REGISTRY, _MARKER)
    assert api._takeover_modules() == (registry, marker)


@pytest.mark.parametrize("registry_attrs, marker_attrs", [
    ({**_REGISTRY, "_read_entries": lambda path: []}, _MARKER),
    ({**_REGISTRY, "_lease_paths": lambda registry_home=None, /: (None, None)}, _MARKER),
    ({**_REGISTRY, "_write_entries": lambda path: None}, _MARKER),
    (_REGISTRY, {**_MARKER, "read_turn_marker": lambda home, key, extra: None}),
    ({k: v for k, v in _REGISTRY.items() if k != "_pid_liveness"}, _MARKER),
    (_REGISTRY, {"read_turn_marker": _MARKER["read_turn_marker"]}),
])
def test_probe_refuses_a_different_shape(monkeypatch, registry_attrs, marker_attrs):
    _install(monkeypatch, registry_attrs, marker_attrs)
    with pytest.raises(api.TokenError) as raised:
        api._takeover_modules()
    assert raised.value.status == 501


# --- Review hardening -------------------------------------------------------------


@pytest.mark.parametrize("pid", [None, "", "abc", 0, -5, True])
def test_a_claim_without_a_valid_pid_is_kept(tmp_path, pid):
    entry = _entry("chat", pid=pid)
    _seed(tmp_path, [entry])
    assert _take(tmp_path, ["chat"])["status"] == "same_host"
    assert _entries(tmp_path) == [entry]


def test_a_pidless_claim_is_kept_while_another_process_claim_is_dropped(tmp_path):
    kept = _entry("chat", pid=None, lease="kept")
    _seed(tmp_path, [kept, _entry("chat-live", lease="desktop")])
    assert _take(tmp_path, ["chat", "chat-live"])["status"] == "same_host"
    assert _entries(tmp_path) == [kept]


def test_a_numeric_session_id_still_matches(tmp_path):
    _seed(tmp_path, [_entry(0)])
    assert _take(tmp_path, ["0"])["status"] == "taken_over"
    assert _entries(tmp_path) == []


def test_an_abandoned_takeover_writes_nothing(tmp_path):
    import threading

    _seed(tmp_path, [_entry("chat")])
    abandoned = threading.Event()
    abandoned.set()
    with pytest.raises(api.TokenError) as raised:
        api.take_over_session(["chat"], registry=FakeRegistry(tmp_path), turn_marker=FakeTurnMarker(),
                              home=tmp_path, own_pid=OWN_PID, abandoned=abandoned)
    assert raised.value.status == 504
    assert _entries(tmp_path) == [_entry("chat")]


def test_a_takeover_abandoned_while_deciding_writes_nothing(tmp_path):
    import threading

    _seed(tmp_path, [_entry("chat")])
    abandoned = threading.Event()

    class AbandonDuringRead(FakeTurnMarker):
        def read_turn_marker(self, home, session_key):
            abandoned.set()
            return None

    with pytest.raises(api.TokenError):
        api.take_over_session(["chat"], registry=FakeRegistry(tmp_path), turn_marker=AbandonDuringRead(),
                              home=tmp_path, own_pid=OWN_PID, abandoned=abandoned)
    assert _entries(tmp_path) == [_entry("chat")]


def test_a_hermes_without_its_home_helper_is_unsupported(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", types.ModuleType("hermes_constants"))
    with pytest.raises(api.TokenError) as raised:
        api._takeover_home()
    assert raised.value.status == 501


def test_a_marker_whose_writer_state_fails_counts_as_running_even_for_a_dead_owner(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, dead_pids={DESKTOP_PID})

    class BrokenState(FakeTurnMarker):
        def marker_writer_state(self, entry):
            raise OSError("permission denied")

    marker = BrokenState({"chat": {"writer_pid": 4242}})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "busy"
    assert _entries(tmp_path) == [_entry("chat")]


def test_a_marker_of_an_unexpected_shape_counts_as_running(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    assert _take(tmp_path, ["chat"], marker=FakeTurnMarker({"chat": "legacy"}))["status"] == "busy"


def test_liveness_gets_the_normalized_pid(tmp_path):
    _seed(tmp_path, [_entry("chat", pid=str(DESKTOP_PID))])
    seen = []
    registry = FakeRegistry(tmp_path)
    registry._pid_liveness = lambda pid, start=None: seen.append(pid) or True
    _take(tmp_path, ["chat"], registry=registry)
    assert seen == [DESKTOP_PID]
