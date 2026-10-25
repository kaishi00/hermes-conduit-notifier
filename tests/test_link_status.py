"""The dashboard's Conduit card: link_status.py, the hooks' push results and
the /link-status route."""

import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import urllib.error
import ssl
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
CONDUIT = "Conduit/212 CFNetwork/3826.500.1 Darwin/25.0.0"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _private_copy(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


link = _private_copy("conduit_link_status_under_test", ROOT / "link_status.py")
client = _private_copy("conduit_push._client_link_status_under_test", ROOT / "client.py")
api = _private_copy("conduit_plugin_api_link_status", ROOT / "dashboard" / "plugin_api.py")


class WSTransport:
    def __init__(self, headers):
        self._ws = types.SimpleNamespace(headers=headers)
        self.closed = False


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    for module in (link, client.link_status):
        module._written.clear()
        module._last_push_ok.clear()
    # The dashboard's own copy of link_status.py, loaded afresh per test.
    monkeypatch.setattr(api, "_link_status_module", None)
    api._link_app_noted.clear()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)


def _saved(home):
    return json.loads((Path(home) / link.FILE_NAME).read_text())


def test_app_seen_writes_at_most_once_a_minute(tmp_path):
    assert link.app_seen(tmp_path, now=1000.0)
    assert not link.app_seen(tmp_path, now=1030.0)
    assert link.read(tmp_path) == {"app_seen_at": 1000.0}
    assert link.app_seen(tmp_path, now=1061.0)
    assert link.read(tmp_path)["app_seen_at"] == 1061.0


def test_an_older_sighting_never_replaces_a_newer_one(tmp_path):
    link.app_seen(tmp_path, now=2000.0)
    link._written.clear()  # another process, with its own throttle
    assert not link.app_seen(tmp_path, now=1500.0)
    assert link.read(tmp_path)["app_seen_at"] == 2000.0


def test_a_push_that_starts_or_stops_failing_writes_at_once(tmp_path):
    assert link.push_result(tmp_path, True, now=100.0)
    assert not link.push_result(tmp_path, True, now=110.0)
    assert link.push_result(tmp_path, False, "Couldn't reach the relay.", now=120.0)
    assert not link.push_result(tmp_path, False, "Couldn't reach the relay.", now=130.0)
    assert link.push_result(tmp_path, True, now=140.0)
    assert link.read(tmp_path) == {"push_ok_at": 140.0, "push_failed_at": 120.0,
                                   "push_failed_reason": "Couldn't reach the relay."}


def test_both_writers_keep_each_others_fields(tmp_path):
    link.app_seen(tmp_path, now=10.0)
    link.push_result(tmp_path, True, now=20.0)
    assert _saved(tmp_path) == {"app_seen_at": 10.0, "push_ok_at": 20.0}


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"app_seen_at": "yesterday",
                                                                    "push_ok_at": float("inf")}),
                                     json.dumps({"push_failed_reason": "no time with it"})])
def test_anything_unreadable_reads_as_nothing_recorded(tmp_path, content):
    (tmp_path / link.FILE_NAME).write_text(content)
    assert link.read(tmp_path) == {}


def test_an_unwritable_home_is_tried_again_next_time(tmp_path):
    missing = tmp_path / "gone"
    assert not link.app_seen(missing, now=10.0)
    missing.mkdir()
    assert link.app_seen(missing, now=11.0)


def test_the_file_is_private(tmp_path):
    link.app_seen(tmp_path, now=10.0)
    assert (tmp_path / link.FILE_NAME).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("error, reason", [
    (client.RelayRejected(401, "unknown gateway"), "The relay no longer knows this host. Pair Conduit again."),
    (client.RelayRejected(403, "", from_relay=False), "Something in front of the relay is blocking this host (403)."),
    (client.RelayRejected(503, "busy"), "The relay answered 503."),
    (urllib.error.URLError(ssl.SSLCertVerificationError("bad cert for relay.internal")),
     "The relay's certificate didn't verify."),
    (urllib.error.URLError("Name or service not known: relay.internal"), "Couldn't reach the relay."),
    (TimeoutError("10.0.0.5 timed out"), "Couldn't reach the relay."),
    (ValueError("https://relay.internal/v1/events"), "The delivery failed."),
])
def test_failure_reasons_never_carry_the_errors_own_text(error, reason):
    assert client.push_failure_reason(error) == reason


def test_deliveries_are_recorded_for_the_card(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(client, "load_state", lambda: {"credential": "c", "relay_url": "https://relay.example"})
    monkeypatch.setattr(client, "RETRY_DELAYS", ())
    monkeypatch.setattr(client, "_send", lambda *args, **kwargs: {"accepted": True})
    monkeypatch.setattr(client, "record_relay_accepted", lambda state: None)
    assert client._deliver({"type": "response.ready"})
    assert "push_ok_at" in link.read(tmp_path)

    def refused(*args, **kwargs):
        raise client.RelayRejected(503, "busy")

    monkeypatch.setattr(client, "_send", refused)
    assert not client._deliver({"type": "response.ready"})
    recorded = link.read(tmp_path)
    assert recorded["push_failed_reason"] == "The relay answered 503."
    assert recorded["push_failed_at"] >= recorded["push_ok_at"]


def test_a_broken_record_never_fails_a_delivery(monkeypatch, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(client.link_status, "push_result", broken)
    monkeypatch.setattr(client, "load_state", lambda: {"credential": "c", "relay_url": "https://relay.example"})
    monkeypatch.setattr(client, "_send", lambda *args, **kwargs: {"accepted": True})
    monkeypatch.setattr(client, "record_relay_accepted", lambda state: None)
    assert client._deliver({"type": "response.ready"})


# --- The route --------------------------------------------------------------


@pytest.fixture()
def route(monkeypatch, tmp_path):
    hook = api._DesktopViewHook(api._DesktopViewStore(flush_delay=3600.0))
    monkeypatch.setattr(hook, "ensure_started", lambda: None)
    monkeypatch.setattr(api, "_desktop_view_hook", hook)
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    pairing = tmp_path / "conduit-push.json"
    monkeypatch.setattr(api, "_pairing_state_path", lambda: pairing)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    http.pair = lambda **extra: pairing.write_text(json.dumps({"credential": "c", "gateway_name": "box", **extra}))
    http.hook = hook
    return http


def _status(http):
    response = http.get(f"{BASE}/link-status", headers={"user-agent": CHROME})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_a_host_conduit_never_used_says_so(route):
    body = _status(route)
    assert body["app"]["state"] == "never" and body["push"]["state"] == "not_paired"
    assert "credential" not in json.dumps(body)


def test_a_request_from_conduit_marks_the_app_connected(route, tmp_path):
    route.get(f"{BASE}/sessions/desktop-views", headers={"user-agent": CONDUIT})
    body = _status(route)
    assert body["app"]["state"] == "connected" and body["app"]["seen_at"] is not None
    # Saved for the next dashboard start.
    assert "app_seen_at" in link.read(tmp_path)


def test_a_browser_request_and_the_card_itself_dont_count(route):
    route.get(f"{BASE}/sessions/desktop-views", headers={"user-agent": CHROME})
    route.get(f"{BASE}/link-status", headers={"user-agent": CONDUIT})
    assert _status(route)["app"]["state"] == "never"


def test_sightings_stay_bounded_whatever_profiles_callers_send(monkeypatch):
    monkeypatch.setattr(api, "LIMITER_MAX_KEYS", 3)
    note = lambda profile: api._note_app_request(types.SimpleNamespace(
        url=types.SimpleNamespace(path=f"{BASE}/sessions/desktop-views"),
        headers={"user-agent": CONDUIT}, query_params={"profile": profile}))
    for profile in ("a", "b", "c", "d"):
        note(profile)
    assert len(api._link_app_noted) == 3 and api._limiter_key("d") not in api._link_app_noted
    # A sighting past the window makes room.
    api._link_app_noted[api._limiter_key("a")] -= api.LINK_APP_RECENT_S + 1
    note("d")
    assert api._limiter_key("d") in api._link_app_noted and api._limiter_key("a") not in api._link_app_noted


def test_an_old_sighting_reads_as_idle(route, tmp_path):
    link.app_seen(tmp_path, now=1.0)
    body = _status(route)
    assert body["app"]["state"] == "idle" and body["app"]["seen_at"] == 1.0


def test_an_open_conduit_socket_counts_until_it_closes(monkeypatch, route, tmp_path):
    socket = WSTransport({"user-agent": CONDUIT, "x-conduit-client": "conduit"})
    server = types.ModuleType("tui_gateway.server")
    server._sessions = {"rt-1": {"profile_home": str(tmp_path)}}
    server._session_home = lambda session: Path(session["profile_home"])

    def handler(rid, params):
        return {"jsonrpc": "2.0", "id": rid, "result": {"session_id": "rt-1", "session_key": "stored-1"}}

    server._methods = {"session.activate": handler, "session.resume": handler}
    transports = types.ModuleType("tui_gateway.transport")
    transports.current_transport = lambda: socket
    ws = types.ModuleType("tui_gateway.ws")
    ws.WSTransport = WSTransport
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    monkeypatch.setitem(sys.modules, "tui_gateway.transport", transports)
    monkeypatch.setitem(sys.modules, "tui_gateway.ws", ws)
    assert route.hook.try_install()

    server._methods["session.resume"](1, {})
    body = _status(route)
    assert body["app"] == {"state": "connected", "seen_at": body["app"]["seen_at"], "open_sockets": 1,
                           "sockets_observed": True}
    # Conduit's opens never become Desktop views.
    assert route.hook.store.read(tmp_path) == {}

    socket.closed = True
    route.hook.sweep()
    assert _status(route)["app"]["open_sockets"] == 0


@pytest.mark.parametrize("extra, recorded, state", [
    ({}, {}, "waiting"),
    ({}, {"push_ok_at": 10.0}, "ok"),
    ({}, {"push_ok_at": 10.0, "push_failed_at": 20.0, "push_failed_reason": "Couldn't reach the relay."}, "failing"),
    ({}, {"push_ok_at": 30.0, "push_failed_at": 20.0, "push_failed_reason": "x"}, "ok"),
    ({"relay_rejected_at": "2026-10-25T00:00:00Z"}, {"push_ok_at": 10.0}, "refused"),
    ({"relay_blocked_at": "2026-10-25T00:00:00Z"}, {}, "blocked"),
])
def test_the_push_state(route, tmp_path, extra, recorded, state):
    route.pair(**extra)
    (tmp_path / link.FILE_NAME).write_text(json.dumps(recorded))
    push = _status(route)["push"]
    assert push["state"] == state
    assert push["gateway_name"] == "box"
    assert push["failed_reason"] == ("Couldn't reach the relay." if state == "failing" else None)


def test_the_manifest_puts_the_card_on_the_sessions_page():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert manifest["slots"] == ["sessions:top"]
    bundle = (ROOT / "dashboard" / "dist" / "index.js").read_text()
    assert 'registerSlot("conduit_push", "sessions:top"' in bundle
    assert "/api/plugins/conduit_push/link-status" in bundle
