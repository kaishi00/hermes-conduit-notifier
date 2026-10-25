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


@pytest.mark.parametrize("marker_entry", [{"writer_pid": DESKTOP_PID}, {}])
def test_a_dead_owner_is_dropped_without_waiting(tmp_path, marker_entry):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, dead_pids={DESKTOP_PID})
    # A provably dead writer, or a legacy marker naming no writer.
    marker = FakeTurnMarker({"chat": marker_entry}, {DESKTOP_PID: "dead", None: "unknown"})
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
    # Upstream's writer check uses the same pid + start-time probe, so it reads dead too.
    marker = FakeTurnMarker({"chat": {"writer_pid": DESKTOP_PID}}, {DESKTOP_PID: "dead"})
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


def test_a_dead_owner_whose_child_has_unknown_liveness_is_busy(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    registry = FakeRegistry(tmp_path, dead_pids={DESKTOP_PID})
    marker = FakeTurnMarker({"chat": {"writer_pid": 4242}}, {4242: "unknown"})
    assert _take(tmp_path, ["chat"], registry=registry, marker=marker)["status"] == "busy"


def test_an_overflowing_pid_is_treated_as_unknown(tmp_path):
    entry = _entry("chat", pid=float("inf"))
    _seed(tmp_path, [entry])
    assert _take(tmp_path, ["chat"])["status"] == "same_host"


def test_a_live_worker_claim_without_turn_markers_is_never_dropped(tmp_path):
    # CLI/kanban workers and gateways write no turn marker, so idle can't be told apart from mid-turn.
    for surface in ("cli", "kanban", "gateway", "", None):
        _seed(tmp_path, [_entry("chat", surface=surface)])
        assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": surface or ""}
        assert _entries(tmp_path) == [_entry("chat", surface=surface)]


def test_a_dead_worker_claim_is_still_dropped(tmp_path):
    worker_pid = 3000
    _seed(tmp_path, [_entry("chat", pid=worker_pid, surface="kanban")])
    registry = FakeRegistry(tmp_path, dead_pids={worker_pid})
    assert _take(tmp_path, ["chat"], registry=registry)["status"] == "taken_over"
    assert _entries(tmp_path) == []


def test_a_worker_claim_blocks_alongside_a_desktop_claim(tmp_path):
    entries = [_entry("chat"), _entry("runtime-id", pid=3000, surface="cli", lease="lease-2")]
    _seed(tmp_path, entries)
    assert _take(tmp_path, ["runtime-id", "chat"]) == {"status": "busy", "surface": "cli"}
    assert _entries(tmp_path) == entries


def test_surface_names_match_regardless_of_case(tmp_path):
    _seed(tmp_path, [_entry("chat", surface=" Desktop ")])
    assert _take(tmp_path, ["chat"])["status"] == "taken_over"


def test_this_dashboards_own_claim_beside_a_live_worker_claim_reports_same_host(tmp_path):
    entries = [_entry("runtime-id", pid=OWN_PID, surface="tui", lease="lease-own"),
               _entry("chat", pid=3000, surface="cli")]
    _seed(tmp_path, entries)
    assert _take(tmp_path, ["runtime-id", "chat"]) == {"status": "same_host", "surface": "tui"}
    assert _entries(tmp_path) == entries


def _ledger(home: Path, admissions=(), workers=(), columns=True):
    import sqlite3
    db = sqlite3.connect(home / "state.db")
    if columns:
        db.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT, request_id TEXT,"
                   " principal_id TEXT)")
        db.executemany("INSERT INTO session_admissions VALUES (?, ?, ?, ?)", admissions)
    else:
        db.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT)")
        db.executemany("INSERT INTO session_admissions VALUES (?, ?)", [row[:2] for row in admissions])
    db.execute("CREATE TABLE worker_executions (session_id TEXT, status TEXT)")
    db.executemany("INSERT INTO worker_executions VALUES (?, ?)", workers)
    db.commit()
    db.close()


def test_another_bots_message_in_the_ledger_answers_busy(tmp_path):
    # Another bot's message (message_agent) runs in this bot's Bot Chat
    # through the gateway's ledger, with no claim in the registry.
    _ledger(tmp_path, [("chat", "started", "bot:5bd49571", "automation:agent:nahida:local:dm:chat")])
    assert _take(tmp_path, ["runtime-id", "chat"]) == {"status": "busy", "surface": "bot"}


def test_ledger_work_keeps_any_claim(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    _ledger(tmp_path, [("chat", "queued", "req-1", "uid:0")])
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "gateway"}
    assert _entries(tmp_path) == [_entry("chat")]


def test_an_open_worker_execution_answers_busy(tmp_path):
    _ledger(tmp_path, workers=[("chat", "running")])
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "gateway"}


def test_settled_ledger_rows_and_other_chats_do_not_block(tmp_path):
    _seed(tmp_path, [_entry("chat")])
    _ledger(tmp_path, [("chat", "terminal", "bot:1", "automation:agent:x"), ("other", "started", "bot:2", "")],
            [("chat", "terminal")])
    assert _take(tmp_path, ["chat"])["status"] == "taken_over"


def test_a_ledger_without_the_bot_columns_still_answers_busy(tmp_path):
    _ledger(tmp_path, [("chat", "started", "", "")], columns=False)
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "gateway"}


def test_a_state_db_without_a_ledger_is_ignored(tmp_path):
    import sqlite3
    sqlite3.connect(tmp_path / "state.db").close()
    assert _take(tmp_path, ["chat"]) == {"status": "free"}


def test_another_bots_message_is_named_beside_other_queued_work(tmp_path):
    _ledger(tmp_path, [("chat", "queued", "req-1", "uid:0"),
                       ("chat", "started", "bot:5bd49571", "automation:agent:nahida:local:dm:chat")])
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "bot"}


def test_a_ledger_with_only_the_request_id_still_names_a_bot(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT, request_id TEXT)")
    db.execute("INSERT INTO session_admissions VALUES ('chat', 'started', 'bot:1')")
    db.commit()
    db.close()
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "bot"}


def test_a_ledger_with_only_the_principal_still_names_a_bot(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT, principal_id TEXT)")
    db.executemany("INSERT INTO session_admissions VALUES (?, ?, ?)",
                   [("chat", "queued", "uid:0"), ("chat", "started", "automation:agent:nahida:local:dm:chat")])
    db.commit()
    db.close()
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "bot"}


def test_an_unreadable_state_db_falls_through_to_the_claims(tmp_path):
    (tmp_path / "state.db").write_bytes(b"not a database" * 100)
    _seed(tmp_path, [_entry("chat")])
    assert _take(tmp_path, ["chat"])["status"] == "taken_over"


def test_hermes_real_ledger_schema(tmp_path):
    # The schema copy test_pulse keeps in sync with Hermes: the names and the
    # settled word the probe reads are Hermes' own.
    import sqlite3
    db = sqlite3.connect(tmp_path / "state.db")
    db.executescript((ROOT / "tests" / "hermes_state_schema.sql").read_text())
    db.execute("INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id,"
               " lineage_json, payload_json, payload_digest, intent, status, owner_epoch, generation)"
               " VALUES ('a1', 'bot:5bd4', 'automation:agent:nahida:local:dm:chat', 'chat', '[]', '{}', 'd',"
               " 'queue', 'started', 39, 1)")
    db.commit()
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "bot"}
    db.execute("UPDATE session_admissions SET status = 'terminal'")
    db.execute("INSERT INTO worker_executions (execution_id, session_id, kind, owner_epoch, generation, status,"
               " adoption_digest) VALUES ('w1', 'chat', 'child', 39, 1, 'running', 'd')")
    db.commit()
    assert _take(tmp_path, ["chat"]) == {"status": "busy", "surface": "gateway"}
    db.execute("UPDATE worker_executions SET status = 'terminal'")
    db.commit()
    db.close()
    assert _take(tmp_path, ["chat"]) == {"status": "free"}
