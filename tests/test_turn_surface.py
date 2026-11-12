"""Where in Conduit a turn's message was sent (hermes-conduit#888)."""
import os
import contextvars
import importlib.util
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
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
    # So does the phone's state with each message.
    monkeypatch.setattr(plugin.phone_tool, "turn_note", lambda: None)
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
        "Its screen is small: keep the answer short, with no tables. " + surfaces.SPOKEN_HINT + "]")
    # A voice call's turn: its reply is read aloud, so the agent makes no
    # audio of its own (a farm Local voice call read a MEDIA: path out).
    assert surfaces.line("iphone", "spoken") == (
        "[Conduit: this message came from the user's iPhone, spoken. " + surfaces.SPOKEN_HINT + "]")
    assert surfaces.SPOKEN_HINT not in surfaces.line("iphone", "typed")
    # Only audio the agent makes is ruled out: a file the user asks for in a
    # spoken message still goes.
    assert "text_to_speech" in surfaces.SPOKEN_HINT and "asks for is still fine" in surfaces.SPOKEN_HINT
    assert surfaces.line("ipad", "typed") == (
        "[Conduit: this message came from the user's iPad, typed. When you need several details only the user can "
        "give (like dates or a budget), ask them in one clarify call with open questions: Conduit shows it as a "
        "form, and they may leave some blank.]")
    assert surfaces.line("siri") == "[Conduit: this message came from Siri or a Shortcut.]"
    assert "likely driving" in surfaces.line("carplay", "spoken")
    # The Pulse widget's Do it: a tap, never typed or spoken, in Conduit's words.
    assert surfaces.line("widget") == (
        "[Conduit: this message came from a button the user tapped on a Conduit widget, in words Conduit wrote. "
        "Conduit isn't open: the user reads the answer later, in this chat.]")


def test_a_typed_message_on_a_screen_hears_that_open_questions_show_as_a_form():
    for surface in ("iphone", "ipad", "mac", "vision"):
        assert surfaces.FORM_HINT in surfaces.line(surface, "typed")
        # Spoken, it's read out one question at a time; and only a message
        # Conduit says was typed hears it.
        assert surfaces.FORM_HINT not in surfaces.line(surface, "spoken")
        assert surfaces.FORM_HINT not in surfaces.line(surface)
    for surface in ("watch", "carplay", "siri", "widget"):
        assert surfaces.FORM_HINT not in surfaces.line(surface, "typed")


def test_a_typed_message_read_aloud_hears_that_the_answer_is_heard():
    # Conduit's Read replies aloud reads the reply (notifier issue 674): no
    # sound file of the agent's own under it; the form stays a screen's.
    line = surfaces.line("iphone", "typed", heard=True)
    assert surfaces.HEARD_HINT in line and surfaces.FORM_HINT in line
    assert "text_to_speech" in surfaces.HEARD_HINT and "asks for is still fine" in surfaces.HEARD_HINT
    assert surfaces.HEARD_HINT not in surfaces.line("iphone", "typed")
    # A spoken message already says it, once.
    spoken = surfaces.line("iphone", "spoken", heard=True)
    assert surfaces.SPOKEN_HINT in spoken and surfaces.HEARD_HINT not in spoken


def test_a_report_keeps_whether_its_reply_is_heard():
    session = {}
    surfaces.record(session, surfaces.parse({**_report("iphone", entry="typed"), "heard": True}), clock=lambda: 1.0)
    assert surfaces.take(session, "hi", clock=lambda: 2.0) == ("iphone", "typed", True)
    # Left out, as from a Conduit before it: not heard.
    surfaces.record(session, surfaces.parse(_report("iphone", entry="typed")), clock=lambda: 1.0)
    assert surfaces.take(session, "hi", clock=lambda: 2.0) == ("iphone", "typed", False)


def test_the_turn_of_a_message_read_aloud_says_so(gateway, client):
    response = client.post(f"{BASE}/turn-surface", json={**_report("iphone", "Plan my day", "typed"), "heard": True})
    assert response.status_code == 200
    assert surfaces.HEARD_HINT in gateway.turn(CONDUIT, "Plan my day")


@pytest.mark.parametrize("body", [
    None, [], {}, {"session_id": "rt-1", "surface": "iphone"},
    {**_report("iphone"), "session_id": ""}, {**_report("iphone"), "session_id": "x" * 201},
    {**_report("iphone"), "session_id": 3}, {**_report("iphone"), "surface": "toaster"},
    {**_report("iphone"), "surface": ["iphone"]}, {**_report("iphone"), "surface": {}},
    {**_report("iphone"), "input": "sung"}, {**_report("iphone"), "input": ["typed"]},
    {**_report("iphone"), "input": {}}, {**_report("iphone"), "text_sha256": "ABC"},
    {**_report("iphone"), "text_sha256": surfaces.text_sha256("hi").upper()},
    {**_report("iphone"), "heard": "yes"}, {**_report("iphone"), "heard": 1},
])
def test_a_body_that_isnt_one_is_refused(body):
    with pytest.raises(ValueError):
        surfaces.parse(body)


def test_a_kept_report_goes_stale():
    session = {}
    surfaces.record(session, surfaces.parse(_report("carplay", entry="spoken")), clock=lambda: 100.0)
    assert surfaces.take({surfaces.KEY: dict(session[surfaces.KEY])}, "hi",
                         clock=lambda: 100.0 + surfaces.FRESH_S) == ("carplay", "spoken", False)
    assert surfaces.take(session, "hi", clock=lambda: 101.0 + surfaces.FRESH_S) is None
    assert session[surfaces.KEY] == {}


def test_a_chat_keeps_a_few_reports_and_drops_the_oldest():
    session = {}
    for index in range(surfaces.MAX_KEPT + 1):
        surfaces.record(session, surfaces.parse(_report("ipad", text=f"m{index}")), clock=lambda: 1.0)
    assert len(session[surfaces.KEY]) == surfaces.MAX_KEPT
    assert surfaces.take(session, "m0", clock=lambda: 2.0) is None
    assert surfaces.take(session, f"m{surfaces.MAX_KEPT}", clock=lambda: 2.0) == ("ipad", None, False)


def test_a_message_sent_with_images_matches_its_text_parts():
    session = {}
    surfaces.record(session, surfaces.parse(_report("iphone", text="what is this?", entry="typed")))
    parts = [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:"}}]
    assert surfaces.take(session, parts) == ("iphone", "typed", False)


# --- Route and hook -------------------------------------------------------------


def test_the_turn_of_the_reported_message_says_where_it_came_from_once(gateway, client):
    response = client.post(f"{BASE}/turn-surface", json=_report("carplay", "Find a charger", "spoken"))
    assert response.status_code == 200 and response.json() == {"ok": True, "recorded": True}
    assert response.headers["cache-control"] == "no-store"
    assert gateway.turn(CONDUIT, "Find a charger") == surfaces.line("carplay", "spoken")
    # Taken by that turn: the same text sent again says nothing until Conduit reports again.
    assert gateway.turn(CONDUIT, "Find a charger") == ""


def test_the_phones_state_follows_the_line_only_on_a_turn_conduit_sent(gateway, client, monkeypatch):
    monkeypatch.setattr(plugin.phone_tool, "turn_note", lambda: "[phone state]")
    client.post(f"{BASE}/turn-surface", json=_report("carplay", "Find a charger", "spoken"))
    assert gateway.turn(CONDUIT, "Find a charger") == surfaces.line("carplay", "spoken") + "\n[phone state]"
    # No report, no line: nothing says this turn came from Conduit.
    assert gateway.turn(CONDUIT, "Find a charger") == ""
    client.post(f"{BASE}/turn-surface", json=_report("watch", "same words", "spoken"))
    assert gateway.turn(DESKTOP, "same words") == ""


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
    jobs = api.watch_jobs._WatchJobTransport(jobs=None)
    assert gateway.turn(jobs) == surfaces.line("watch", "spoken")
    chat = api.watch_local_live._WatchChatTransport(chat=None)
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


def test_the_turns_tools_hear_how_its_message_was_entered(gateway, client):
    # conduit_show reads it: a typed message's chat has no call screen (hermes-conduit#1808).
    client.post(f"{BASE}/turn-surface", json=_report("iphone", "hi", "typed"))
    gateway.turn(CONDUIT, "hi")
    assert surfaces.turn_entry("stored-1") == "typed"
    client.post(f"{BASE}/turn-surface", json=_report("iphone", "again", "spoken"))
    gateway.turn(CONDUIT, "again")
    assert surfaces.turn_entry("stored-1") == "spoken"
    gateway.turn(CONDUIT, "unreported")
    assert surfaces.turn_entry("stored-1") is None, "a turn with no report says nothing of the last one"
    client.post(f"{BASE}/turn-surface", json=_report("iphone", "typed again", "typed"))
    gateway.turn(CONDUIT, "typed again")
    gateway.turn(DESKTOP, "typed again")
    assert surfaces.turn_entry("stored-1") is None, "a turn sent from elsewhere starts unknown"


def test_a_forks_hook_leaves_the_running_turns_entry(gateway, client, monkeypatch):
    client.post(f"{BASE}/turn-surface", json=_report("iphone", "hi", "typed"))
    gateway.turn(CONDUIT, "hi")
    monkeypatch.setattr(plugin.forks, "inside", lambda: True)
    gateway.turn(CONDUIT, "a side question")
    assert surfaces.turn_entry("stored-1") == "typed"


def test_no_gateway_in_this_process_means_no_line(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    assert surfaces.note(True, "hi", CONDUIT) is None


# --- On the gateway's own listener (hermes-conduit#1618) -----------------------

def test_on_the_listener_a_report_is_kept_for_the_authoritys_turn(gateway, client, monkeypatch):
    authority = types.SimpleNamespace(sessions={"auth-1": types.SimpleNamespace(source="src")})
    run_api = types.ModuleType("gateway.run_api")
    run_api._ACTIVE_RUNNER = types.SimpleNamespace(session_authority=authority)
    authorities = types.ModuleType("gateway.session_authorities")
    authorities.all_authorities = lambda runner: [authority]
    monkeypatch.setitem(sys.modules, "gateway.run_api", run_api)
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", authorities)
    monkeypatch.delitem(sys.__dict__, surfaces._AUTHORITY_STORE, raising=False)

    response = client.post(f"{BASE}/turn-surface", json=_report("watch", "Call Mum", "spoken", session_id="auth-1"))
    assert response.json() == {"ok": True, "recorded": True}
    # Not on the TUI gateway's record: the authority runs this chat.
    assert surfaces.KEY not in gateway.session
    line = surfaces.note(True, "Call Mum", session_id="auth-1")
    assert line and "Apple Watch" in line
    # Taken once.
    assert surfaces.note(True, "Call Mum", session_id="auth-1") is None
    # A chat the authority doesn't have open keeps nothing.
    assert client.post(f"{BASE}/turn-surface", json=_report("watch", session_id="auth-2")).json() == {
        "ok": True, "recorded": False}


def test_a_failed_send_discards_its_report(gateway, client):
    client.post(f"{BASE}/turn-surface", json=_report("carplay", "yes", "spoken"))
    body = {"session_id": "rt-1", "text_sha256": surfaces.text_sha256("yes")}
    assert client.post(f"{BASE}/turn-surface/discard", json=body).json() == {"ok": True, "discarded": True}
    # The turn of those words then takes no line.
    assert gateway.turn(CONDUIT, "yes") == ""
    assert client.post(f"{BASE}/turn-surface/discard", json=body).json() == {"ok": True, "discarded": False}
    assert client.post(f"{BASE}/turn-surface/discard", json={"session_id": "rt-1"}).status_code == 400


def test_a_report_landing_after_its_discard_is_dropped(gateway, client):
    # Issue 610: the report was still on its way when its send failed and
    # the discard came first. It must not wait for another turn of the text.
    body = {"session_id": "rt-1", "text_sha256": surfaces.text_sha256("yes")}
    assert client.post(f"{BASE}/turn-surface/discard", json=body).json() == {"ok": True, "discarded": False}
    client.post(f"{BASE}/turn-surface", json=_report("carplay", "yes", "spoken"))
    assert gateway.turn(CONDUIT, "yes") == ""
    # Other words in the chat still take their own.
    client.post(f"{BASE}/turn-surface", json=_report("carplay", "no", "spoken"))
    assert "CarPlay" in gateway.turn(CONDUIT, "no")


def test_a_discard_holds_its_text_for_a_while_only():
    clock = [100.0]
    session = {}
    report = surfaces.Report("rt-1", surfaces.text_sha256("yes"), "iphone", "typed")
    assert surfaces.discard(session, report.text_sha256, lambda: clock[0]) is False
    surfaces.record(session, report, lambda: clock[0] + 1.0)
    assert surfaces.take(session, "yes", lambda: clock[0] + 2.0) is None
    # Past the hold, a report of the same words counts again.
    later = clock[0] + surfaces.DISCARD_HOLD_S + 1.0
    surfaces.record(session, report, lambda: later)
    assert surfaces.take(session, "yes", lambda: later) == ("iphone", "typed", False)


def test_on_the_listener_a_report_landing_after_its_discard_is_dropped(gateway, client, monkeypatch):
    authority = types.SimpleNamespace(sessions={"auth-1": types.SimpleNamespace(source="src")})
    run_api = types.ModuleType("gateway.run_api")
    run_api._ACTIVE_RUNNER = types.SimpleNamespace(session_authority=authority)
    authorities = types.ModuleType("gateway.session_authorities")
    authorities.all_authorities = lambda runner: [authority]
    monkeypatch.setitem(sys.modules, "gateway.run_api", run_api)
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", authorities)
    monkeypatch.delitem(sys.__dict__, surfaces._AUTHORITY_STORE, raising=False)

    body = {"session_id": "auth-1", "text_sha256": surfaces.text_sha256("yes")}
    assert client.post(f"{BASE}/turn-surface/discard", json=body).json() == {"ok": True, "discarded": False}
    # A turn of other words in the chat leaves the hold in place.
    assert surfaces.note(False, "something else", session_id="auth-1") is None
    client.post(f"{BASE}/turn-surface", json=_report("iphone", "yes", "typed", session_id="auth-1"))
    # Not even another client's turn of those words takes it.
    assert surfaces.note(False, "yes", session_id="auth-1") is None


def test_on_the_listener_a_failed_send_discards_its_report(gateway, client, monkeypatch):
    authority = types.SimpleNamespace(sessions={"auth-1": types.SimpleNamespace(source="src")})
    run_api = types.ModuleType("gateway.run_api")
    run_api._ACTIVE_RUNNER = types.SimpleNamespace(session_authority=authority)
    authorities = types.ModuleType("gateway.session_authorities")
    authorities.all_authorities = lambda runner: [authority]
    monkeypatch.setitem(sys.modules, "gateway.run_api", run_api)
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", authorities)
    monkeypatch.delitem(sys.__dict__, surfaces._AUTHORITY_STORE, raising=False)

    client.post(f"{BASE}/turn-surface", json=_report("iphone", "yes", "typed", session_id="auth-1"))
    body = {"session_id": "auth-1", "text_sha256": surfaces.text_sha256("yes")}
    assert client.post(f"{BASE}/turn-surface/discard", json=body).json() == {"ok": True, "discarded": True}
    assert surfaces.note(False, "yes", session_id="auth-1") is None


def test_a_watch_turns_listener_requests_keep_their_own_budget():
    # The gateway's own listener answers in the call: each Watch request
    # hands it the time it was given, not the listener's own wait.
    class Listener:
        takes_timeout = True

        def __init__(self):
            self.waits = []

        def dispatch(self, frame, transport, timeout=None):
            self.waits.append(timeout)
            return {"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}}

    listener = Listener()
    chat = api.watch_local_live._WatchCallChat(listener, None)
    assert chat.rpc("session.steer", {"session_id": "s1"}, timeout=7) == {"ok": True}
    assert listener.waits == [7]
