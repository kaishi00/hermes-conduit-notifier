import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

ELECTRON = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Hermes/1.4.0 Chrome/138.0.0.0 Electron/37.2.0 Safari/537.36")
CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
CONDUIT = "Conduit/212 CFNetwork/3826.500.1 Darwin/25.0.0"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_desktop_views", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


class WSTransport:
    """Named like Hermes' tui_gateway.ws.WSTransport; carries the upgrade headers."""

    def __init__(self, headers):
        self._ws = types.SimpleNamespace(headers=headers)
        self.closed = False


class StdioTransport:
    pass


class FakeGateway:
    """tui_gateway.server and tui_gateway.transport as far as the hook reads them."""

    def __init__(self, home, transport=None):
        self.home = Path(home)
        self.transport = transport
        self.calls = []
        self.server = types.ModuleType("tui_gateway.server")
        self.server._sessions = {"rt-1": {"profile_home": str(self.home)}}
        self.server._session_home = lambda session: Path(session.get("profile_home"))
        self.server._hermes_home = self.home
        self.server._methods = {
            "session.activate": self._handler("session.activate"),
            "session.resume": self._handler("session.resume"),
            "session.list": self._handler("session.list"),
        }
        self.transports = types.ModuleType("tui_gateway.transport")
        self.transports.current_transport = lambda: self.transport
        self.ws = types.ModuleType("tui_gateway.ws")
        self.ws.WSTransport = WSTransport

    def _handler(self, name):
        def handler(rid, params):
            self.calls.append((name, rid, params))
            if params.get("fail"):
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": 4004, "message": "no such session"}}
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"session_id": "rt-1", "session_key": params.get("key", "stored-1")}}
        return handler

    def install(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "tui_gateway.server", self.server)
        monkeypatch.setitem(sys.modules, "tui_gateway.transport", self.transports)
        monkeypatch.setitem(sys.modules, "tui_gateway.ws", self.ws)

    def call(self, method, **params):
        return self.server._methods[method](7, params)


def _hook(flush_delay=3600.0):
    return api._DesktopViewHook(api._DesktopViewStore(flush_delay=flush_delay))


@pytest.mark.parametrize("headers, expected", [
    ({"user-agent": ELECTRON}, "desktop"),
    ({"user-agent": CHROME}, "browser"),
    ({"user-agent": CONDUIT}, None),
    ({"user-agent": ELECTRON, "x-conduit-client": "ios"}, None),
    ({}, None),
])
def test_only_browser_engine_sockets_count(headers, expected):
    assert api.desktop_view_client(WSTransport(headers)) == expected


def test_non_websocket_transports_never_count():
    assert api.desktop_view_client(StdioTransport()) is None
    assert api.desktop_view_client(None) is None


def test_a_desktop_open_is_recorded_and_the_response_is_untouched(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path, WSTransport({"user-agent": ELECTRON}))
    gateway.install(monkeypatch)
    hook = _hook()
    assert hook.try_install()
    assert hook.observing and hook.reason is None

    response = gateway.call("session.activate", key="stored-1")
    assert response == {"jsonrpc": "2.0", "id": 7, "result": {"session_id": "rt-1", "session_key": "stored-1"}}
    gateway.call("session.resume", key="stored-2")
    views = hook.store.read(tmp_path)
    assert set(views) == {"stored-1", "stored-2"}
    assert views["stored-1"]["client"] == "desktop"
    # Methods it doesn't watch stay as they were.
    assert not hasattr(gateway.server._methods["session.list"], api._DESKTOP_VIEW_MARKER)


def test_conduit_and_failed_opens_are_not_recorded(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path, WSTransport({"user-agent": CONDUIT}))
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()
    gateway.call("session.activate")
    gateway.transport = WSTransport({"user-agent": ELECTRON})
    gateway.call("session.activate", fail=True)
    assert hook.store.read(tmp_path) == {}


def test_the_wrapper_never_fails_the_call(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path, WSTransport({"user-agent": ELECTRON}))
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()

    class Broken(dict):
        def get(self, *args):
            raise RuntimeError("registry moved")

    gateway.server._sessions = Broken()
    response = gateway.call("session.activate")
    assert response["result"]["session_key"] == "stored-1"
    assert gateway.calls == [("session.activate", 7, {})]


def test_installing_twice_never_wraps_a_wrapper(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path, WSTransport({"user-agent": ELECTRON}))
    gateway.install(monkeypatch)
    first = _hook()
    first.try_install()
    wrapped = gateway.server._methods["session.activate"]
    second = _hook()
    assert second.try_install()
    assert gateway.server._methods["session.activate"] is wrapped

    # The newer load observes, so the route it serves sees the live selection.
    gateway.call("session.activate", key="a")
    assert second.read(tmp_path)["a"]["open"] is True
    assert first.read(tmp_path) == {}


def test_a_subclass_of_the_gateway_socket_still_counts(monkeypatch, tmp_path):
    class TLSSocket(WSTransport):
        pass

    gateway = FakeGateway(tmp_path, TLSSocket({"user-agent": ELECTRON}))
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()
    gateway.call("session.activate", key="a")
    assert hook.store.read(tmp_path)["a"]["client"] == "desktop"


def test_header_names_are_matched_in_any_case():
    assert api.desktop_view_client(WSTransport({"User-Agent": ELECTRON})) == "desktop"
    assert api.desktop_view_client(WSTransport({"User-Agent": ELECTRON, "X-Conduit-Client": "conduit"})) is None


def test_a_socket_type_that_isnt_a_class_falls_back_to_the_name():
    assert api.desktop_view_client(WSTransport({"user-agent": CHROME}), socket_type=(WSTransport,)) == "browser"


def test_an_open_at_the_same_moment_takes_the_newer_client():
    merged = api._merge_desktop_view({"opened_at": 5.0, "seen_through": 5.0, "client": "browser"},
                                     {"opened_at": 5.0, "seen_through": 5.0, "client": "desktop"})
    assert merged["client"] == "desktop"


def test_a_blank_conduit_header_still_marks_conduit():
    assert api.desktop_view_client(WSTransport({"user-agent": ELECTRON, "x-conduit-client": ""})) is None


def test_an_absurdly_nested_file_reads_as_empty(tmp_path):
    (tmp_path / api.DESKTOP_VIEWS_FILE).write_text("[" * 100000)
    assert api._DesktopViewStore(flush_delay=3600.0).read(tmp_path) == {}


def test_reports_why_it_is_not_observing(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    hook = _hook()
    assert not hook.try_install()
    assert hook.reason == "gateway-not-in-process"

    gateway = FakeGateway(tmp_path)
    del gateway.server._methods["session.resume"]
    gateway.install(monkeypatch)
    assert not hook.try_install()
    assert hook.reason == "gateway-unsupported" and not hook.observing


def test_flush_merges_newest_wins_with_the_file_and_keeps_it_private(tmp_path):
    path = tmp_path / api.DESKTOP_VIEWS_FILE
    path.write_text(json.dumps({"version": 1, "views": {
        "a": {"opened_at": 100.0, "seen_through": 150.0, "client": "browser"},
        "b": {"opened_at": 300.0, "seen_through": 400.0, "client": "browser"},
        "junk": {"opened_at": "soon", "seen_through": 1.0},
        "flag": {"opened_at": True, "seen_through": 1.0},
        "huge": {"opened_at": 10**400, "seen_through": 1.0},
        "negative": {"opened_at": -5, "seen_through": 1.0},
        "odd": {"opened_at": 1.0, "seen_through": 2.0, "client": 7},
    }}))
    store = api._DesktopViewStore(flush_delay=3600.0)
    store.record(tmp_path, "a", "desktop", 210.0, opened_at=200.0)
    store.record(tmp_path, "b", "desktop", 350.0, opened_at=250.0)
    store.flush()

    views = json.loads(path.read_text())["views"]
    assert views == {"a": {"opened_at": 200.0, "seen_through": 210.0, "client": "desktop"},
                     "b": {"opened_at": 300.0, "seen_through": 400.0, "client": "browser"},
                     "odd": {"opened_at": 1.0, "seen_through": 2.0, "client": "desktop"}}
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]


def test_the_store_keeps_only_the_newest_chats(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DESKTOP_VIEWS_MAX", 2)
    store = api._DesktopViewStore(flush_delay=3600.0)
    for index, stored_id in enumerate(["old", "mid", "new"]):
        store.record(tmp_path, stored_id, "desktop", 100.0 + index, opened_at=100.0 + index)
    store.flush()
    assert set(store.read(tmp_path)) == {"mid", "new"}


def test_route_returns_views_for_the_profile_home(monkeypatch, tmp_path):
    hook = _hook()
    monkeypatch.setattr(hook, "ensure_started", lambda: None)
    monkeypatch.setattr(api, "_desktop_view_hook", hook)
    monkeypatch.setattr(api, "_desktop_view_store", hook.store)
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    hook.store.record(tmp_path, "a", "desktop", 120.0, opened_at=100.0)
    hook.store.record(tmp_path, "b", "browser", 200.0, opened_at=180.0)

    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    response = client.get(f"{BASE}/sessions/desktop-views")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ok"] is True and body["observing"] is False and body["reason"] == "gateway-not-in-process"
    assert set(body["views"]) == {"a", "b"}

    newer = client.get(f"{BASE}/sessions/desktop-views", params={"since": 150}).json()
    assert newer["views"] == {"b": {"opened_at": 180.0, "seen_through": 200.0, "client": "browser"}}


def test_a_half_loaded_gateway_never_fails_the_route(monkeypatch, tmp_path):
    hook = _hook()
    monkeypatch.setattr(hook, "ensure_started", lambda: None)

    def broken():
        raise ValueError("wrapper loop")

    monkeypatch.setattr(hook, "try_install", broken)
    monkeypatch.setattr(api, "_desktop_view_hook", hook)
    monkeypatch.setattr(api, "_desktop_view_store", hook.store)
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    hook.store.record(tmp_path, "a", "desktop", 120.0, opened_at=100.0)

    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).get(f"{BASE}/sessions/desktop-views")
    assert response.status_code == 200
    assert set(response.json()["views"]) == {"a"}


def test_a_watcher_that_cant_start_is_retried(monkeypatch):
    hook = _hook()

    class Unstartable:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(api.threading, "Thread", Unstartable)
    hook.ensure_started()
    assert hook._thread is None


class Clock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


def test_a_chat_stays_seen_while_its_desktop_has_it_selected(monkeypatch, tmp_path):
    clock = Clock(1000.0)
    monkeypatch.setattr(api, "time", types.SimpleNamespace(time=clock.time, monotonic=clock.time, sleep=lambda _: None))
    desktop = WSTransport({"user-agent": ELECTRON})
    gateway = FakeGateway(tmp_path, desktop)
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()

    gateway.call("session.activate", key="a")
    clock.now = 1060.0
    views = hook.read(tmp_path)
    assert views["a"] == {"opened_at": 1000.0, "seen_through": 1060.0, "client": "desktop", "open": True}

    # Switching to b ends a's selection there.
    clock.now = 1100.0
    gateway.call("session.activate", key="b")
    clock.now = 1200.0
    views = hook.read(tmp_path)
    assert views["a"] == {"opened_at": 1000.0, "seen_through": 1100.0, "client": "desktop"}
    assert views["b"]["seen_through"] == 1200.0 and views["b"]["open"] is True

    # Closing Desktop ends b's at the last check that found it connected.
    desktop.closed = True
    clock.now = 1300.0
    views = hook.read(tmp_path)
    assert views["b"] == {"opened_at": 1100.0, "seen_through": 1200.0, "client": "desktop"}


def test_each_desktop_connection_keeps_its_own_selection(monkeypatch, tmp_path):
    clock = Clock(1000.0)
    monkeypatch.setattr(api, "time", types.SimpleNamespace(time=clock.time, monotonic=clock.time, sleep=lambda _: None))
    laptop, browser = WSTransport({"user-agent": ELECTRON}), WSTransport({"user-agent": CHROME})
    gateway = FakeGateway(tmp_path, laptop)
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()

    gateway.call("session.activate", key="a")
    gateway.transport = browser
    clock.now = 1010.0
    gateway.call("session.activate", key="b")
    clock.now = 1050.0
    views = hook.read(tmp_path)
    assert views["a"]["open"] and views["a"]["seen_through"] == 1050.0
    assert views["b"]["open"] and views["b"]["client"] == "browser"


def test_a_bad_profile_keeps_hermes_own_status(monkeypatch):
    from fastapi import HTTPException

    def unknown_profile(profile):
        raise HTTPException(status_code=404, detail="Unknown profile")

    hook = _hook()
    monkeypatch.setattr(hook, "ensure_started", lambda: None)
    monkeypatch.setattr(api, "_desktop_view_hook", hook)
    monkeypatch.setattr(api, "_profile_scope", unknown_profile)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).get(f"{BASE}/sessions/desktop-views", params={"profile": "nope"})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_replaced_handlers_are_wrapped_again(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path, WSTransport({"user-agent": ELECTRON}))
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()
    gateway.server._methods["session.activate"] = gateway._handler("session.activate")  # a gateway reload
    hook.verify()
    assert hook.observing
    gateway.call("session.activate", key="after-reload")
    assert "after-reload" in hook.store.read(tmp_path)


def test_coroutine_handlers_are_not_wrapped(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path)

    async def resume(rid, params):
        return {}

    gateway.server._methods["session.resume"] = resume
    gateway.install(monkeypatch)
    hook = _hook()
    assert not hook.try_install()
    assert hook.reason == "gateway-unsupported"
    assert gateway.server._methods["session.resume"] is resume


def test_a_watcher_that_gave_up_can_start_again(monkeypatch):
    monkeypatch.setattr(api, "DESKTOP_VIEWS_INSTALL_WINDOW_S", 0.0)
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    hook = _hook()
    hook._thread = threading.current_thread()
    hook._run()
    assert hook._thread is None


def test_a_failing_install_attempt_keeps_the_watcher_restartable(monkeypatch):
    monkeypatch.setattr(api, "DESKTOP_VIEWS_INSTALL_WINDOW_S", 0.0)
    hook = _hook()
    attempts = []

    def broken():
        attempts.append(1)
        raise RuntimeError("gateway half-loaded")

    monkeypatch.setattr(hook, "try_install", broken)
    hook._thread = threading.current_thread()
    hook._run()
    assert attempts and hook._thread is None


def test_an_open_whose_profile_is_unknown_is_not_recorded(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path, WSTransport({"user-agent": ELECTRON}))
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()
    # The live session is gone by the time the open is seen: the default
    # profile is not assumed.
    gateway.server._sessions = {}
    gateway.call("session.activate")
    assert hook.store.read(tmp_path) == {}


def test_a_gateway_that_cant_name_a_sessions_profile_is_unsupported(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path)
    del gateway.server._session_home
    gateway.install(monkeypatch)
    hook = _hook()
    assert not hook.try_install()
    assert hook.reason == "gateway-unsupported"


def test_a_socket_whose_state_cant_be_read_ends_its_selection(monkeypatch, tmp_path):
    clock = Clock(1000.0)
    monkeypatch.setattr(api, "time", types.SimpleNamespace(time=clock.time, monotonic=clock.time, sleep=lambda _: None))
    desktop = WSTransport({"user-agent": ELECTRON})
    del desktop.closed
    gateway = FakeGateway(tmp_path, desktop)
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()
    gateway.call("session.activate", key="a")
    clock.now = 1100.0
    assert "open" not in hook.read(tmp_path)["a"]


def test_unreadable_socket_headers_are_reported_not_ignored(monkeypatch, tmp_path):
    desktop = WSTransport({"user-agent": ELECTRON})
    desktop._ws = types.SimpleNamespace()
    gateway = FakeGateway(tmp_path, desktop)
    gateway.install(monkeypatch)
    hook = _hook()
    hook.try_install()
    gateway.call("session.activate")
    assert hook.status() == (False, "gateway-unsupported")
    assert hook.store.read(tmp_path) == {}

    gateway.transport = WSTransport({"user-agent": ELECTRON})
    gateway.call("session.activate")
    assert hook.status() == (True, None)


def test_a_gateway_without_its_websocket_transport_is_unsupported(monkeypatch, tmp_path):
    gateway = FakeGateway(tmp_path)
    gateway.install(monkeypatch)
    monkeypatch.setitem(sys.modules, "tui_gateway.ws", types.ModuleType("tui_gateway.ws"))
    hook = _hook()
    assert not hook.try_install()
    assert hook.reason == "gateway-unsupported"


def test_a_selection_end_merges_into_any_stored_entry():
    merged = api._merge_desktop_view({"opened_at": 0.0, "seen_through": 5.0, "client": "browser"},
                                     {"seen_through": 9.0, "client": "desktop"})
    assert merged == {"opened_at": 0.0, "seen_through": 9.0, "client": "browser"}


def test_reads_are_rate_limited(monkeypatch, tmp_path):
    hook = _hook()
    monkeypatch.setattr(hook, "ensure_started", lambda: None)
    monkeypatch.setattr(api, "_desktop_view_hook", hook)
    monkeypatch.setattr(api, "_desktop_views_limiter", api._MintLimiter(1, 60.0, message="slow down"))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert client.get(f"{BASE}/sessions/desktop-views").status_code == 200
    assert client.get(f"{BASE}/sessions/desktop-views").status_code == 429
