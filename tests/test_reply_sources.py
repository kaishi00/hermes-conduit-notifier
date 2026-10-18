"""Reply-ready pushes only for the sources the user keeps on (#115)."""
import importlib.util
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
ELECTRON = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Hermes/1.0 Chrome/130 Electron/33.0 Safari/537.36"
SAFARI = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"

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


sources = _load("conduit_push.reply_sources", ROOT / "reply_sources.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
api = _load("conduit_plugin_api_reply_sources", ROOT / "dashboard" / "plugin_api.py")
Store = sources.ReplySourcesStore


class WSTransport:
    """Named like Hermes' tui_gateway.ws.WSTransport; carries the upgrade headers."""

    def __init__(self, headers):
        self._ws = types.SimpleNamespace(headers=headers)


# --- The setting -------------------------------------------------------------


def test_every_source_notifies_until_turned_off(tmp_path):
    store = Store(tmp_path)
    assert store.settings() == {"conduit": True, "desktop": True, "messaging": True, "terminal": True}
    assert not (tmp_path / sources.FILE_NAME).exists()
    store.set_notify("desktop", False)
    store.set_notify("messaging", False)
    assert Store(tmp_path).settings() == {"conduit": True, "desktop": False, "messaging": False, "terminal": True}
    assert not Store(tmp_path).notifies("desktop") and Store(tmp_path).notifies("conduit")
    assert Store(tmp_path).set_notify("desktop", True)["desktop"] is True
    assert (tmp_path / sources.FILE_NAME).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("source, notify", [("", False), ("telegram", False), (None, True), ("desktop", "no")])
def test_bad_choices_are_refused(tmp_path, source, notify):
    with pytest.raises(ValueError):
        Store(tmp_path).set_notify(source, notify)


def test_an_unreadable_file_reads_as_everything_on(tmp_path):
    (tmp_path / sources.FILE_NAME).write_text("{not json")
    assert all(Store(tmp_path).settings().values())
    (tmp_path / sources.FILE_NAME).write_text('{"muted_reply_sources": {"desktop": "yes", "nope": 1}}')
    assert all(Store(tmp_path).settings().values())
    (tmp_path / sources.FILE_NAME).write_text('{"muted_reply_sources": {"desktop": 1' + "0" * 400 + '}}')
    assert all(Store(tmp_path).settings().values())


# --- Where a turn's message came from ----------------------------------------


@pytest.mark.parametrize("platform, transport, expected", [
    ("tui", WSTransport({"x-conduit-client": "conduit", "user-agent": "Conduit/1 CFNetwork"}), "conduit"),
    # A Telegram chat continued in Conduit is a Conduit turn.
    ("telegram", WSTransport({"x-conduit-client": "conduit"}), "conduit"),
    ("desktop", WSTransport({"user-agent": ELECTRON}), "desktop"),
    ("tui", WSTransport({"user-agent": SAFARI}), "desktop"),
    ("desktop", types.SimpleNamespace(conduit_client="conduit"), "conduit"),
    ("telegram", None, "messaging"),
    ("Discord", None, "messaging"),
    ("cli", None, "terminal"),
    # Unknown: the push goes out whatever is set.
    ("tui", WSTransport({"user-agent": "python-websockets/12"}), None),
    # Only Conduit's own value counts.
    ("tui", WSTransport({"x-conduit-client": "", "user-agent": SAFARI}), "desktop"),
    ("tui", WSTransport({"x-conduit-client": "other"}), None),
    ("desktop", None, None),
    ("tui", None, None),
    ("local", None, None),
    ("cron", None, None),
    ("", None, None),
    (None, None, None),
    ("tui", object(), None),
])
def test_turn_source(platform, transport, expected):
    assert sources.turn_source(platform, transport) == expected


def test_unreadable_socket_headers_are_unknown():
    class WSTransport:
        _ws = types.SimpleNamespace(headers=None)

    assert sources.turn_source("tui", WSTransport()) is None


def test_hermes_platform_list_decides_messaging_when_it_has_one(monkeypatch):
    class Platform:
        def __init__(self, value):
            if value not in ("buzz", "telegram"):
                raise ValueError(value)

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    config = types.ModuleType("gateway.config")
    config.Platform = Platform
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", config)
    assert sources.turn_source("buzz", None) == "messaging"
    assert sources.turn_source("telegram", None) == "messaging"
    # Not a platform this Hermes has, though the plugin's own list has it.
    assert sources.turn_source("slack", None) is None
    assert sources.turn_source("webui", None) is None
    assert sources.turn_source("local", None) is None


def test_the_turns_connection_comes_from_hermes_gateway_when_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.transport", raising=False)
    assert sources.current_transport() is None
    bound = WSTransport({"x-conduit-client": "conduit"})
    module = types.ModuleType("tui_gateway.transport")
    module.current_transport = lambda: bound
    monkeypatch.setitem(sys.modules, "tui_gateway.transport", module)
    assert sources.current_transport() is bound


# --- The reply-ready push ------------------------------------------------------


@pytest.fixture
def turn(tmp_path, monkeypatch):
    """Runs a finished turn's hook from ``transport`` and returns what was pushed."""
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    monkeypatch.setattr(plugin, "_desktop_watched", lambda session_id, started: False)
    monkeypatch.setattr(plugin, "_session_title", lambda session_id: "")
    module = types.ModuleType("tui_gateway.transport")
    monkeypatch.setitem(sys.modules, "tui_gateway.transport", module)
    pushed = []
    monkeypatch.setattr(plugin, "enqueue", pushed.append)

    def run(platform, transport=None, **kwargs):
        pushed.clear()
        module.current_transport = lambda: transport
        plugin._post_llm_call(session_id="s-1", turn_id="t-1", assistant_response="Done.", platform=platform,
                              **kwargs)
        plugin._on_session_end(session_id="s-1", turn_id="t-1", task_id=kwargs.get("task_id"), completed=True,
                               failed=False, interrupted=False, platform=platform)
        return [event["type"] for event in pushed]

    run.home = tmp_path
    return run


def test_replies_push_from_every_source_by_default(turn):
    assert turn("tui", WSTransport({"user-agent": ELECTRON})) == ["response.ready"]
    assert turn("telegram") == ["response.ready"]


def test_conduit_only_skips_desktop_messaging_and_terminal_replies(turn):
    store = Store(turn.home)
    for source in ("desktop", "messaging", "terminal"):
        store.set_notify(source, False)
    assert turn("tui", WSTransport({"x-conduit-client": "conduit"})) == ["response.ready"]
    assert turn("telegram", WSTransport({"x-conduit-client": "conduit"})) == ["response.ready"]
    assert turn("desktop", WSTransport({"user-agent": ELECTRON})) == []
    assert turn("telegram") == []
    assert turn("cli") == []
    # Unknown sources and cron runs still push.
    assert turn("tui") == ["response.ready"]
    assert turn("cron", task_id="cron:abc123:1") == ["response.ready"]


def test_an_unreadable_setting_lets_the_push_through(turn, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(sources.ReplySourcesStore, "notifies", broken)
    assert turn("telegram") == ["response.ready"]


def test_failures_still_push_from_a_muted_source(turn, monkeypatch):
    Store(turn.home).set_notify("messaging", False)
    pushed = []
    monkeypatch.setattr(plugin, "enqueue", pushed.append)
    monkeypatch.setattr(plugin.cron_runs, "quiet", lambda *args: False)
    plugin._on_session_end(session_id="s-1", turn_id="t-1", completed=False, interrupted=False, platform="telegram")
    assert [event["type"] for event in pushed] == ["turn.failed"]


# --- Routes ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_cron_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_read_and_change_a_source(client):
    on = {"conduit": True, "desktop": True, "messaging": True, "terminal": True}
    assert client.get(f"{BASE}/notifications/reply-sources").json() == {"ok": True, "sources": on}
    response = client.put(f"{BASE}/notifications/reply-sources", json={"source": "desktop", "notify": False})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "sources": {**on, "desktop": False}}
    assert client.get(f"{BASE}/notifications/reply-sources").json()["sources"]["desktop"] is False


def test_a_bad_body_is_a_400(client):
    assert client.put(f"{BASE}/notifications/reply-sources", json={"source": "x", "notify": False}).status_code == 400
    assert client.put(f"{BASE}/notifications/reply-sources", json=["desktop"]).status_code == 400


def test_watch_jobs_count_as_conduit():
    assert sources.turn_source("desktop", api._WatchJobTransport(None)) == "conduit"


def test_an_unreadable_file_is_not_overwritten(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.set_notify("messaging", False)
    store.set_notify("desktop", False)
    saved = store.path.read_text(encoding="utf-8")
    real = type(store.path).read_text

    def unreadable(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("busy")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", unreadable)
    with pytest.raises(OSError):
        store.set_notify("terminal", False)
    monkeypatch.undo()
    assert store.path.read_text(encoding="utf-8") == saved
