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


class FanoutTransport:
    """Named like Hermes' tui_gateway.transport.FanoutTransport: one chat open on several clients."""

    def __init__(self, *peers):
        self._peers = list(peers)

    def transports(self):
        return list(self._peers)


def _closed(transport):
    transport._closed = True
    return transport


PHONE = WSTransport({"x-conduit-client": "conduit"})
IPAD = WSTransport({"x-conduit-client": "conduit", "user-agent": "Conduit/1 CFNetwork"})
DESKTOP = WSTransport({"user-agent": ELECTRON})


@pytest.mark.parametrize("transport, expected, desktop", [
    # Eric's .52 (2026-11-07): his Bot Chat open on two Conduits got no board rule.
    (FanoutTransport(PHONE, IPAD), "conduit", False),
    (FanoutTransport(PHONE, types.SimpleNamespace(conduit_client="conduit")), "conduit", False),
    (FanoutTransport(DESKTOP, WSTransport({"user-agent": ELECTRON})), "desktop", True),
    (FanoutTransport(PHONE, DESKTOP), None, False),  # either could have sent it
    (FanoutTransport(DESKTOP, WSTransport({"user-agent": SAFARI})), "desktop", False),  # the web dashboard isn't Desktop
    (FanoutTransport(PHONE, object()), None, False),
    (FanoutTransport(), None, False),
    (FanoutTransport(FanoutTransport(PHONE)), None, False),
    # A client whose socket closed isn't counted (DeepSeek's review of #611).
    (FanoutTransport(PHONE, _closed(WSTransport({"user-agent": ELECTRON}))), "conduit", False),
    (FanoutTransport(_closed(WSTransport({"x-conduit-client": "conduit"})), DESKTOP), "desktop", True),
    (FanoutTransport(_closed(WSTransport({"x-conduit-client": "conduit"}))), None, False),
])
def test_a_chat_open_on_several_clients_is_theirs_when_they_agree(transport, expected, desktop):
    assert sources.turn_source("tui", transport) == expected
    assert sources.is_desktop_app(transport) is desktop


def test_a_chat_open_on_conduit_and_desktop_is_conduits_while_conduit_has_it_on_screen(monkeypatch):
    # Eric, 2026-11-09: Desktop open beside the phone kept the board rule
    # and cards from the phone.
    mixed = FanoutTransport(PHONE, DESKTOP)
    assert sources.conduit_among(mixed) and not sources.conduit_among(FanoutTransport(DESKTOP))
    assert not sources.conduit_among(PHONE), "one client isn't a fanout"
    monkeypatch.setattr(plugin.reply_sources, "current_transport", lambda: mixed)
    on_screen = []
    monkeypatch.setattr(plugin.chat_on_screen, "on_screen", lambda home, session: session in on_screen)
    assert plugin._turn_source("tui", "bot-chat") is None, "either could have sent it"
    on_screen.append("bot-chat")
    assert plugin._turn_source("tui", "bot-chat") == "conduit"
    monkeypatch.setattr(plugin.reply_sources, "current_transport", lambda: FanoutTransport(DESKTOP))
    assert plugin._turn_source("tui", "bot-chat") == "desktop", "Desktop alone stays Desktop's"
    # The plugin's own work update was sent by no client.
    plugin.turn_surface.expect_wake("Work update from your workstreams. ...")
    plugin.turn_surface.start_turn("bot-chat", "Work update from your workstreams. ...")
    try:
        assert plugin._turn_source("tui", "bot-chat") is None
        assert plugin._source_notifies("tui", "bot-chat") is True
    finally:
        plugin.turn_surface.start_turn("bot-chat")


def test_unreadable_fanout_clients_are_unknown():
    class Broken:
        def transports(self):
            raise RuntimeError("closed")

    assert sources.turn_source("tui", Broken()) is None and sources.is_desktop_app(Broken()) is False


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


def test_the_turns_platform_comes_from_hermes_gateway_when_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "gateway.session_context", raising=False)
    assert sources.current_platform() is None
    context = types.ModuleType("gateway.session_context")
    context.get_session_env = lambda name, default="": "telegram" if name == "HERMES_SESSION_PLATFORM" else default
    monkeypatch.setitem(sys.modules, "gateway.session_context", context)
    assert sources.current_platform() == "telegram"

    def broken(*args):
        raise RuntimeError("no session")

    context.get_session_env = broken
    assert sources.current_platform() is None


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


# --- Sessions that aren't conversations (notifier#660) ------------------------


def _no_hermes_sources(monkeypatch):
    for name in ("agent.session_source", "gateway.session_finite"):
        monkeypatch.setitem(sys.modules, name, None)


def test_a_scripts_tool_run_sends_no_push_or_lock_screen_progress(turn, monkeypatch):
    # `hermes chat --oneshot --source tool` on a Hermes whose helpers can't be read: its variable says.
    _no_hermes_sources(monkeypatch)
    started = []
    monkeypatch.setattr(plugin.live_activity.reporter, "turn_started", started.append)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    plugin._pre_llm_call(session_id="s-1", turn_id="t-1", user_message="step 3", platform="cli")
    assert turn("cli") == [] and started == []
    # An ordinary terminal chat still notifies and shows its progress.
    monkeypatch.delenv("HERMES_SESSION_SOURCE")
    plugin._pre_llm_call(session_id="s-1", turn_id="t-1", user_message="hi", platform="cli")
    assert turn("cli") == ["response.ready"] and started == ["s-1"]


def test_a_scripts_failed_tool_run_sends_no_failure_push(turn, monkeypatch):
    _no_hermes_sources(monkeypatch)
    pushed = []
    monkeypatch.setattr(plugin, "enqueue", pushed.append)
    monkeypatch.setattr(plugin.cron_runs, "quiet", lambda *args: False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    plugin._on_session_end(session_id="s-1", turn_id="t-1", completed=False, interrupted=False, platform="cli")
    assert pushed == []
    monkeypatch.delenv("HERMES_SESSION_SOURCE")
    plugin._on_session_end(session_id="s-1", turn_id="t-2", completed=False, interrupted=False, platform="cli")
    assert [event["type"] for event in pushed] == ["turn.failed"]


def test_a_scripts_run_ends_no_lock_screen_progress(turn, monkeypatch):
    _no_hermes_sources(monkeypatch)
    ended = []
    monkeypatch.setattr(plugin.live_activity.reporter, "turn_ended", lambda *args: ended.append(args))
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    assert turn("cli") == [] and ended == []
    monkeypatch.delenv("HERMES_SESSION_SOURCE")
    assert turn("cli") == ["response.ready"] and ended == [("s-1", "done")]


def test_a_push_a_scripts_run_wrote_with_conduit_notify_still_goes(turn, monkeypatch):
    _no_hermes_sources(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    written = plugin.notify_tool.Headline("Backup done", "All 3 disks copied.")
    monkeypatch.setattr(plugin.notify_tool, "take", lambda *args, **kwargs: written)
    assert turn("cli") == ["response.ready"]
    monkeypatch.setattr(plugin.notify_tool, "take", lambda *args, **kwargs: None)
    assert turn("cli") == []


def test_a_scripts_background_task_sends_no_push(turn, monkeypatch):
    _no_hermes_sources(monkeypatch)
    pushed = []
    monkeypatch.setattr(plugin, "enqueue", pushed.append)
    monkeypatch.setattr(plugin.cron_runs, "quiet", lambda *args: False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    plugin._pre_llm_call(session_id="script-1", turn_id="t-1", user_message="step 3", platform="cli")
    # The task finishes after the turn, where Hermes may describe the child: the parent decides.
    monkeypatch.delenv("HERMES_SESSION_SOURCE")
    plugin._subagent_stop(child_session_id="child-1", parent_session_id="script-1", child_status="completed")
    assert pushed == []
    plugin._pre_llm_call(session_id="chat-1", turn_id="t-2", user_message="hi", platform="cli")
    plugin._subagent_stop(child_session_id="child-2", parent_session_id="chat-1", child_status="completed")
    assert [event["type"] for event in pushed] == ["background_task.finished"]


@pytest.mark.parametrize("source, expected", [
    ("tool", []), ("oneshot", []), ("kanban", []), ("cli", ["response.ready"]), ("tui", ["response.ready"]),
])
def test_hermes_session_source_decides(turn, monkeypatch, source, expected):
    monkeypatch.setitem(sys.modules, "gateway.session_finite", None)
    module = types.ModuleType("agent.session_source")
    seen = []
    module.session_source_for = lambda platform: seen.append(platform) or source
    monkeypatch.setitem(sys.modules, "agent.session_source", module)
    assert turn("cli") == expected
    assert seen and set(seen) == {"cli"}


def test_a_one_shot_turn_the_gateway_runs_is_quiet(turn, monkeypatch):
    # The gateway's worker for `chat -q` binds the fact on the turn; the session's source reads as a chat's.
    monkeypatch.setitem(sys.modules, "agent.session_source", None)
    module = types.ModuleType("gateway.session_finite")
    module.finite_turn_required = lambda: True
    monkeypatch.setitem(sys.modules, "gateway.session_finite", module)
    assert turn("local") == []
    module.finite_turn_required = lambda: None
    assert turn("local") == ["response.ready"]


# --- Routes ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.notifications, "_cron_home", lambda: tmp_path)
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
    assert sources.turn_source("desktop", api.watch_jobs._WatchJobTransport(None)) == "conduit"


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
