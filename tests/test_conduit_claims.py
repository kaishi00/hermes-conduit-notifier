"""The dashboard lets go of Conduit's claim on a chat once it sits idle, so
Hermes Desktop can open it again (hermes-conduit#1123)."""
import importlib
import importlib.util
import pathlib
import sys
import threading
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg

claims = importlib.import_module("conduit_push.conduit_claims")

NOW = 10_000.0
IDLE = NOW - claims.IDLE_S - 1
DETACHED = object()


class Lease:
    def __init__(self, enabled=True):
        self.session_id = "stored-1"
        self.enabled = enabled
        self.released = False


class Socket:
    def __init__(self, headers):
        self.headers = headers


class WSTransport:
    """Hermes' socket transport: the upgrade's headers on ``_ws``, ``_closed``."""

    def __init__(self, headers, closed=False):
        self._ws = Socket(headers)
        self._closed = closed


class FanoutTransport:
    def __init__(self, *peers):
        self.peers = list(peers)

    def transports(self):
        return list(self.peers)


class JobTransport:
    conduit_client = "conduit"


def conduit(closed=False):
    return WSTransport({"X-Conduit-Client": "conduit", "User-Agent": "Conduit/2.0"}, closed=closed)


def desktop(closed=False):
    return WSTransport({"User-Agent": "Mozilla/5.0 Electron/38"}, closed=closed)


class Server:
    def __init__(self):
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._detached_ws_transport = DETACHED
        self.pending = {}
        self.delegating = set()
        self.released = []

    def _session_pending_kind(self, sid):
        return self.pending.get(sid, "")

    def _session_has_active_delegations(self, sid, session=None):
        return sid in self.delegating

    def _release_active_session_slot(self, session):
        lease = session.get("active_session_lease")
        lease.released = True
        session.pop("active_session_lease")
        self.released.append(lease.session_id)
        return True


def chat(server, sid="live-1", transport=None, **fields):
    ready = threading.Event()
    ready.set()
    session = {"active_session_lease": Lease(), "running": False, "last_active": IDLE, "agent_ready": ready,
               "history_lock": threading.Lock(), "session_key": "stored-1", "source": "desktop",
               "transport": conduit() if transport is None else transport, **fields}
    server._sessions[sid] = session
    return session


def sweep(server, now=NOW):
    return claims.sweep(server, lambda: ROOT, now=now)


@pytest.fixture(autouse=True)
def no_turn_markers(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.turn_marker", raising=False)


def test_an_idle_conduit_chat_lets_go_of_its_claim():
    server = Server()
    session = chat(server)
    assert sweep(server) == 1
    assert server.released == ["stored-1"]
    assert "active_session_lease" not in session
    assert sweep(server) == 0  # nothing left to let go


def test_a_chat_used_within_the_last_minute_keeps_it():
    server = Server()
    chat(server, last_active=NOW - claims.IDLE_S + 5)
    assert sweep(server) == 0


@pytest.mark.parametrize("fields", [
    {"running": True},
    {"_closing": True},
    {"follow_profile_config": True},  # Bot Chat
    {"room_plumbing": True},
    {"source": "bot_room"},
    {"active_session_lease": Lease(enabled=False)},  # an isolated child's borrowed token
    {"active_session_lease": None},
])
def test_chats_that_keep_their_claim(fields):
    server = Server()
    chat(server, **fields)
    assert sweep(server) == 0


def test_a_chat_still_building_keeps_it():
    server = Server()
    chat(server, agent_ready=threading.Event())
    assert sweep(server) == 0


def test_waiting_on_an_answer_or_delegated_work_keeps_it():
    server = Server()
    chat(server, sid="asking")
    chat(server, sid="delegating")
    server.pending["asking"] = "clarify.request"
    server.delegating.add("delegating")
    assert sweep(server) == 0


def test_a_chat_desktop_has_open_in_this_process_keeps_it():
    server = Server()
    chat(server, sid="desktop", transport=desktop())
    chat(server, sid="both", transport=FanoutTransport(conduit(), desktop()))
    assert sweep(server) == 0


def test_a_viewer_that_left_does_not_count():
    server = Server()
    chat(server, transport=FanoutTransport(conduit(), desktop(closed=True)))
    assert sweep(server) == 1


def test_the_plugins_own_jobs_count_as_conduit():
    server = Server()
    chat(server, transport=JobTransport())
    assert sweep(server) == 1


def test_a_chat_conduit_left_counts_once_seen_with_conduit():
    server = Server()
    session = chat(server, last_active=NOW)
    assert sweep(server) == 0  # still in use, but now known to be Conduit's
    session["transport"] = DETACHED
    assert sweep(server, now=NOW + claims.IDLE_S + 1) == 1


def test_a_parked_chat_never_seen_with_conduit_keeps_it():
    server = Server()
    chat(server, transport=DETACHED)
    assert sweep(server) == 0


def test_desktop_joining_clears_the_mark():
    server = Server()
    session = chat(server, last_active=NOW)
    sweep(server)
    session["transport"] = desktop()
    sweep(server)
    session["transport"] = DETACHED
    assert sweep(server, now=NOW + claims.IDLE_S + 1) == 0


def test_a_stdio_tui_keeps_it():
    server = Server()
    chat(server, transport=object())
    assert sweep(server) == 0


def test_a_turn_marker_keeps_it(monkeypatch):
    marker = types.ModuleType("tui_gateway.turn_marker")
    marker.read_turn_marker = lambda home, key: {"writer_pid": 4321} if key == "stored-1" else None
    marker.marker_writer_state = lambda entry: "alive"
    monkeypatch.setitem(sys.modules, "tui_gateway.turn_marker", marker)
    server = Server()
    chat(server)
    assert sweep(server) == 0
    marker.marker_writer_state = lambda entry: "dead"  # a crash leftover, not a turn
    assert sweep(server) == 1


def test_an_unreadable_turn_marker_keeps_it(monkeypatch):
    marker = types.ModuleType("tui_gateway.turn_marker")

    def unreadable(home, key):
        raise OSError("disk")

    marker.read_turn_marker = unreadable
    marker.marker_writer_state = lambda entry: "dead"
    monkeypatch.setitem(sys.modules, "tui_gateway.turn_marker", marker)
    server = Server()
    chat(server)
    assert sweep(server) == 0


def test_a_turn_admitted_during_the_check_keeps_it():
    server = Server()
    session = chat(server)
    assert claims.releasable(server, "live-1", session, NOW, lambda: ROOT)
    session["running"] = True  # admission marks it under the same lock
    assert not claims.release(server, "live-1", session, NOW)
    assert server.released == []


def test_a_hermes_without_the_helpers_lets_nothing_go():
    server = Server()
    chat(server)
    server._session_pending_kind = None
    assert sweep(server) == 0
    assert sweep(None) == 0


def test_one_odd_runtime_never_stops_the_pass():
    server = Server()
    chat(server, sid="odd")
    server._sessions["odd"]["history_lock"] = "not a lock"
    chat(server, sid="fine")
    assert sweep(server) == 1


def test_a_transport_whose_closed_is_a_method_or_flag_object_reads_it():
    left = desktop()
    del left._closed
    left.closed = lambda: True
    server = Server()
    chat(server, transport=FanoutTransport(conduit(), left))
    assert sweep(server) == 1


def test_byte_headers_are_read():
    server = Server()
    chat(server, transport=WSTransport({b"x-conduit-client": b"conduit"}))
    assert sweep(server) == 1


def test_a_fanout_that_cannot_list_its_clients_keeps_it():
    class Broken(FanoutTransport):
        def transports(self):
            raise RuntimeError("closing")

    server = Server()
    chat(server, transport=Broken())
    assert sweep(server) == 0


def test_a_chat_without_a_stored_id_keeps_it_when_markers_exist(monkeypatch):
    marker = types.ModuleType("tui_gateway.turn_marker")
    marker.read_turn_marker = lambda home, key: None
    marker.marker_writer_state = lambda entry: "dead"
    monkeypatch.setitem(sys.modules, "tui_gateway.turn_marker", marker)
    server = Server()
    chat(server, session_key="")
    assert sweep(server) == 0


def test_a_replaced_runtime_is_not_released():
    server = Server()
    session = chat(server)
    assert claims.releasable(server, "live-1", session, NOW, lambda: ROOT)
    chat(server)  # the same live id now names a fresh runtime
    assert not claims.release(server, "live-1", session, NOW)
    assert server.released == []


def test_the_sweeper_stops_once_a_newer_one_takes_over(monkeypatch):
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_claims", ROOT / "dashboard" / "plugin_api.py")
    if "hermes_constants" not in sys.modules:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: ROOT
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(claims, "SWEEP_S", 0.01)
    monkeypatch.setattr(api.core, "_plugin_package_module", lambda name: claims)
    passes = []
    monkeypatch.setattr(claims, "sweep", lambda server, home: passes.append(server) or 0)
    monkeypatch.delitem(sys.__dict__, api.chats._CLAIMS_SWEEPER, raising=False)
    api.chats._ensure_claims_sweeper()
    token = sys.__dict__[api.chats._CLAIMS_SWEEPER]
    api.chats._ensure_claims_sweeper()  # already running: no second one
    assert sys.__dict__[api.chats._CLAIMS_SWEEPER] is token
    worker = next(t for t in threading.enumerate() if t.name == "conduit-idle-claims" and t.is_alive())
    deadline = time.monotonic() + 5
    while not passes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert passes
    sys.__dict__[api.chats._CLAIMS_SWEEPER] = object()  # a reloaded copy took over
    worker.join(5)
    assert not worker.is_alive()


def test_a_viewer_joining_during_the_check_keeps_it():
    server = Server()
    session = chat(server)
    assert claims.releasable(server, "live-1", session, NOW, lambda: ROOT)
    session["transport"] = FanoutTransport(conduit(), desktop())
    assert not claims.release(server, "live-1", session, NOW)
    assert server.released == []
