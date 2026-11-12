"""The /side-question route: Conduit's step summaries, answered as Hermes'
/btw answers them but only to the asking device (no "[btw …]" note in
Hermes Desktop)."""
import os
import contextlib
import importlib.util
import pathlib
import sys
import threading
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


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = _load("conduit_plugin_api_side_question", ROOT / "dashboard" / "plugin_api.py")
QUESTION = "Look back at the tool calls you made just before your last reply."


class FakeAgent:
    model, provider, base_url, api_key, api_mode, session_id = "m", "p", "u", "k", "chat", "s1"

    def __init__(self, history):
        self._session_messages = history


@pytest.fixture
def hermes(monkeypatch):
    """Hermes' /btw answer and the TUI gateway's chats."""
    server = types.ModuleType("tui_gateway.server")
    server.dispatch = lambda *a, **k: None
    server._sessions = {}
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    side = types.ModuleType("agent.side_question")
    side.asked = []
    side.reply = "I first wanted to know whether the relay was current."

    def answer_side_question(question, history, *, parent_agent=None, main_runtime=None, **_):
        side.asked.append((question, history, parent_agent, main_runtime))
        if side.release is not None:
            side.release.wait(5)
        return side.reply

    side.answer_side_question = answer_side_question
    side.release = None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.side_question", side)
    monkeypatch.setattr(api.chats, "_side_question_limiter", api.core._MintLimiter(100, 60.0))
    api.chats._side_question_running.clear()
    server.side = side
    return server


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _ask(client, **body):
    return client.post(f"{BASE}/side-question", json={"session_id": "s1", "question": QUESTION, **body})


def test_a_side_question_answers_the_asking_device_from_the_live_chat(hermes, client):
    agent = FakeAgent([{"role": "user", "content": "deploy"}, {"role": "assistant", "content": "Done"}])
    hermes._sessions["s1"] = {"agent": agent}
    response = _ask(client)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "text": hermes.side.reply}
    assert response.headers["cache-control"] == "no-store"
    question, history, parent, runtime = hermes.side.asked[0]
    # Hermes' own /btw answer: the fork of this agent, its runtime as prompt.btw passes it.
    assert question == QUESTION and parent is agent and len(history) == 2
    assert runtime == {"model": "m", "provider": "p", "base_url": "u", "api_key": "k",
                       "api_mode": "chat", "session_id": "s1"}


def test_a_chat_whose_agent_is_gone_answers_from_its_history(hermes, client):
    hermes._sessions["s1"] = {"agent": None, "history": [{"role": "user", "content": "hi"}]}
    assert _ask(client).status_code == 200
    _question, history, parent, runtime = hermes.side.asked[0]
    assert parent is None and runtime is None and history == [{"role": "user", "content": "hi"}]


def test_a_chat_not_open_on_the_host_is_not_found(hermes, client):
    assert _ask(client, session_id="gone").status_code == 404
    assert not hermes.side.asked


@pytest.mark.parametrize("body", [
    {"question": ""}, {"question": "   "}, {"question": 5}, {"question": "x" * 40001},
    {"session_id": ""}, {"session_id": "s" * 201}, {"wait_s": 0}, {"wait_s": 111}, {"wait_s": True},
    {"wait_s": "5"},
])
def test_a_bad_request_is_refused(hermes, client, body):
    hermes._sessions["s1"] = {"agent": FakeAgent([])}
    assert _ask(client, **body).status_code == 400
    assert not hermes.side.asked


def test_a_hermes_without_side_questions_says_so(hermes, client, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.side_question", types.ModuleType("agent.side_question"))
    hermes._sessions["s1"] = {"agent": FakeAgent([])}
    response = _ask(client)
    assert response.status_code == 501 and "update Hermes" in response.text


def test_two_at_a_time_per_chat_and_four_on_the_host(hermes, client):
    hermes._sessions["s1"] = {"agent": FakeAgent([])}
    api.chats._side_question_running["s1"] = api.chats.SIDE_QUESTION_PER_CHAT
    assert _ask(client).status_code == 429
    api.chats._side_question_running.clear()
    for n in range(api.chats.SIDE_QUESTION_MAX_RUNNING):
        api.chats._side_question_running[f"busy{n}"] = 1
    assert _ask(client).status_code == 503
    assert not hermes.side.asked


def test_a_slow_answer_times_out_and_frees_its_place_when_it_ends(hermes, client):
    hermes._sessions["s1"] = {"agent": FakeAgent([])}
    hermes.side.release = threading.Event()
    assert _ask(client, wait_s=0.1).status_code == 504
    assert api.chats._side_question_running.get("s1") == 1
    hermes.side.release.set()
    for _ in range(100):
        if "s1" not in api.chats._side_question_running:
            break
        threading.Event().wait(0.02)
    assert "s1" not in api.chats._side_question_running
    hermes.side.release = None
    assert _ask(client).status_code == 200


def test_side_questions_are_limited_per_minute_for_the_host(hermes, client, monkeypatch):
    monkeypatch.setattr(api.chats, "_side_question_limiter", api.core._MintLimiter(2, 60.0))
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    hermes._sessions["s1"] = {"agent": FakeAgent([])}
    codes = [client.post(f"{BASE}/side-question", params={"profile": name},
                         json={"session_id": "s1", "question": QUESTION}).status_code
             for name in ("current", "main", "work")]
    assert codes == [200, 200, 429]


# --- on the gateway's own listener (hermes-conduit#1618) ---------------------------

def _listener(monkeypatch, hermes, agent):
    authority = types.SimpleNamespace(sessions={"auth-1": types.SimpleNamespace(source="src")})
    runner = types.SimpleNamespace(session_authority=authority,
                                   _session_key_for_source=lambda source: "key",
                                   _resident_agent_for=lambda key: agent)
    run_api = types.ModuleType("gateway.run_api")
    run_api._ACTIVE_RUNNER = runner
    authorities = types.ModuleType("gateway.session_authorities")
    authorities.all_authorities = lambda gateway: [authority]
    authorities.owner_scope = lambda owner: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "gateway.run_api", run_api)
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", authorities)
    hermes._sessions.clear()
    return runner


def test_on_the_listener_the_gateways_agent_answers(hermes, client, monkeypatch):
    agent = FakeAgent([{"role": "user", "content": "hi"}])
    _listener(monkeypatch, hermes, agent)
    assert _ask(client, session_id="auth-1").status_code == 200
    assert hermes.side.asked[0][2] is agent


def test_on_the_listener_a_let_go_agent_answers_from_the_question(hermes, client, monkeypatch):
    _listener(monkeypatch, hermes, None)
    assert _ask(client, session_id="auth-1").status_code == 200
    _question, history, parent, runtime = hermes.side.asked[0]
    assert parent is None and runtime is None and history == []


def test_on_the_listener_a_failed_agent_lookup_says_so(hermes, client, monkeypatch):
    runner = _listener(monkeypatch, hermes, None)

    def broken(key):
        raise AttributeError("renamed")

    runner._resident_agent_for = broken
    response = _ask(client, session_id="auth-1")
    assert response.status_code == 502 and "couldn't read the chat's agent" in response.text
    assert not hermes.side.asked
