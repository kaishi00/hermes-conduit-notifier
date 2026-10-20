"""Where in Conduit a turn's message was sent (hermes-conduit#888)."""
import contextvars
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


surfaces = _load("conduit_push.turn_surface", ROOT / "turn_surface.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
api = _load("conduit_plugin_api_turn_surface", ROOT / "dashboard" / "plugin_api.py")


class WSTransport:
    """Named like Hermes' tui_gateway.ws.WSTransport; carries the upgrade headers."""

    def __init__(self, headers):
        self._ws = types.SimpleNamespace(headers=headers)


CONDUIT = WSTransport({"X-Conduit-Client": "conduit", "User-Agent": "Conduit/1.0"})
DESKTOP = WSTransport({"User-Agent": "Mozilla/5.0 (Macintosh) Hermes/1.0 Electron/33.0"})


class FakeGateway:
    """tui_gateway.server and tui_gateway.transport as far as the route and hook read them."""

    def __init__(self):
        self.session = {}
        self.server = types.ModuleType("tui_gateway.server")
        self.server.dispatch = lambda req, transport=None: None
        self.server._sessions = {"rt-1": self.session}
        self.server._current_runtime_session_record = contextvars.ContextVar("record", default=None)
        self.transport = None
        self.transports = types.ModuleType("tui_gateway.transport")
        self.transports.current_transport = lambda: self.transport
        self.ws = types.ModuleType("tui_gateway.ws")
        self.ws.WSTransport = WSTransport

    def install(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "tui_gateway.server", self.server)
        monkeypatch.setitem(sys.modules, "tui_gateway.transport", self.transports)
        monkeypatch.setitem(sys.modules, "tui_gateway.ws", self.ws)

    def turn(self, transport, message="hi", **kwargs):
        """pre_llm_call's context for a turn of rt-1 answering ``message``, sent over ``transport``."""
        self.transport = transport
        token = self.server._current_runtime_session_record.set(self.session)
        try:
            result = plugin._pre_llm_call(session_id="stored-1", turn_id="t", platform="tui",
                                          user_message=message, **kwargs)
        finally:
            self.server._current_runtime_session_record.reset(token)
        return (result or {}).get("context", "")


def _report(surface, text="hi", entry=None, session_id="rt-1"):
    body = {"session_id": session_id, "text_sha256": surfaces.text_sha256(text), "surface": surface}
    if entry:
        body["input"] = entry
    return body


@pytest.fixture
def gateway(monkeypatch):
    gateway = FakeGateway()
    gateway.install(monkeypatch)
    # The data-sharing line reads stores this test doesn't set up.
    monkeypatch.setattr(plugin.conduit_first, "note", lambda: None)
    return gateway


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


# --- The line -----------------------------------------------------------------


def test_the_line_names_the_surface_and_how_it_was_entered():
    assert surfaces.line("watch", "spoken") == (
        "[Conduit: this message came from the user's Apple Watch, spoken. "
        "Its screen is small: keep the answer short, with no tables.]")
    assert surfaces.line("ipad", "typed") == "[Conduit: this message came from the user's iPad, typed.]"
    assert surfaces.line("siri") == "[Conduit: this message came from Siri or a Shortcut.]"
    assert "likely driving" in surfaces.line("carplay", "spoken")


@pytest.mark.parametrize("body", [
    None, [], {}, {"session_id": "rt-1", "surface": "iphone"},
    {**_report("iphone"), "session_id": ""}, {**_report("iphone"), "session_id": "x" * 201},
    {**_report("iphone"), "session_id": 3}, {**_report("iphone"), "surface": "toaster"},
    {**_report("iphone"), "input": "sung"}, {**_report("iphone"), "text_sha256": "ABC"},
    {**_report("iphone"), "text_sha256": surfaces.text_sha256("hi").upper()},
])
def test_a_body_that_isnt_one_is_refused(body):
    with pytest.raises(ValueError):
        surfaces.parse(body)


def test_a_kept_report_goes_stale():
    session = {}
    surfaces.record(session, surfaces.parse(_report("carplay", entry="spoken")), clock=lambda: 100.0)
    assert surfaces.take({surfaces.KEY: dict(session[surfaces.KEY])}, "hi",
                         clock=lambda: 100.0 + surfaces.FRESH_S) == ("carplay", "spoken")
    assert surfaces.take(session, "hi", clock=lambda: 101.0 + surfaces.FRESH_S) is None
    assert session[surfaces.KEY] == {}


def test_a_chat_keeps_a_few_reports_and_drops_the_oldest():
    session = {}
    for index in range(surfaces.MAX_KEPT + 1):
        surfaces.record(session, surfaces.parse(_report("ipad", text=f"m{index}")), clock=lambda: 1.0)
    assert len(session[surfaces.KEY]) == surfaces.MAX_KEPT
    assert surfaces.take(session, "m0", clock=lambda: 2.0) is None
    assert surfaces.take(session, f"m{surfaces.MAX_KEPT}", clock=lambda: 2.0) == ("ipad", None)


def test_a_message_sent_with_images_matches_its_text_parts():
    session = {}
    surfaces.record(session, surfaces.parse(_report("iphone", text="what is this?", entry="typed")))
    parts = [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:"}}]
    assert surfaces.take(session, parts) == ("iphone", "typed")


# --- Route and hook -------------------------------------------------------------


def test_the_turn_of_the_reported_message_says_where_it_came_from_once(gateway, client):
    response = client.post(f"{BASE}/turn-surface", json=_report("carplay", "Find a charger", "spoken"))
    assert response.status_code == 200 and response.json() == {"ok": True, "recorded": True}
    assert response.headers["cache-control"] == "no-store"
    assert gateway.turn(CONDUIT, "Find a charger") == surfaces.line("carplay", "spoken")
    # Taken by that turn: the same text sent again says nothing until Conduit reports again.
    assert gateway.turn(CONDUIT, "Find a charger") == ""


def test_a_report_never_labels_another_messages_turn(gateway, client):
    # Say a spoken correction steered a running turn: no turn of its own took it.
    client.post(f"{BASE}/turn-surface", json=_report("carplay", "Actually, the closer one", "spoken"))
    # The goal's next step, or a background result, runs on Conduit's socket.
    assert gateway.turn(CONDUIT, "Continue working toward the goal.") == ""


def test_queued_messages_each_keep_their_own_surface(gateway, client):
    client.post(f"{BASE}/turn-surface", json=_report("ipad", "first", "typed"))
    client.post(f"{BASE}/turn-surface", json=_report("siri", "second"))
    assert gateway.turn(CONDUIT, "first") == surfaces.line("ipad", "typed")
    assert gateway.turn(CONDUIT, "second") == surfaces.line("siri")


def test_a_turn_sent_from_elsewhere_gets_no_line(gateway, client):
    client.post(f"{BASE}/turn-surface", json=_report("watch", "same words", "spoken"))
    assert gateway.turn(DESKTOP, "same words") == ""


def test_a_chat_this_process_doesnt_run_keeps_nothing(gateway, client, monkeypatch):
    assert client.post(f"{BASE}/turn-surface", json=_report("iphone", session_id="rt-other")).json() == {
        "ok": True, "recorded": False}
    monkeypatch.delitem(sys.modules, "tui_gateway.server")
    assert client.post(f"{BASE}/turn-surface", json=_report("iphone")).json() == {"ok": True, "recorded": False}
    assert gateway.session == {}


def test_a_bad_report_is_refused(gateway, client):
    response = client.post(f"{BASE}/turn-surface", json={**_report("iphone"), "surface": "toaster"})
    assert response.status_code == 400 and "surface must be one of" in response.json()["detail"]
    assert client.post(f"{BASE}/turn-surface", content=b"x" * 2048).status_code == 413
    assert gateway.session == {}


def test_the_plugins_watch_turns_name_the_watch(gateway):
    jobs = api._WatchJobTransport(jobs=None)
    assert gateway.turn(jobs) == surfaces.line("watch", "spoken")
    chat = api._WatchChatTransport(chat=None)
    assert gateway.turn(chat) == surfaces.line("watch", "spoken")


def test_the_line_sits_with_the_turns_other_context(gateway, client):
    client.post(f"{BASE}/turn-surface", json=_report("iphone", "hi", "typed"))
    voice = plugin._voice_note_prefix() + " hi"
    context = gateway.turn(CONDUIT, "hi", conversation_history=[{"role": "user", "content": voice}])
    assert context.split("\n\n")[0] == surfaces.line("iphone", "typed")
    assert plugin.PERSONA_VOICE_NOTE in context


def test_a_subagent_turn_leaves_the_report_alone(gateway, client):
    client.post(f"{BASE}/turn-surface", json=_report("ipad", "hi", "typed"))
    with plugin._children_lock:
        plugin._child_sessions.add("stored-1")
    try:
        assert gateway.turn(CONDUIT, "hi") == ""
    finally:
        with plugin._children_lock:
            plugin._child_sessions.discard("stored-1")
    assert gateway.turn(CONDUIT, "hi") == surfaces.line("ipad", "typed")


def test_no_gateway_in_this_process_means_no_line(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    assert surfaces.note(True, "hi", CONDUIT) is None
