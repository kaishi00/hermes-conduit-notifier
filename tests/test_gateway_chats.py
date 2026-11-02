"""The plugin's chats on the gateway's session authority (hermes-conduit#1618).

upstream/test_gateway_listener.py runs these against a real gateway; here the
translation between the TUI gateway's shapes and the authority's is held
without one, and the turn-surface store the authority path keeps."""
import asyncio
import pathlib
import sys
import threading
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg

from conduit_push import gateway_chats, pulse_host, turn_surface  # noqa: E402


class FakeConnection:
    """AuthorityConnection's dispatch: records requests, answers from ``answers``."""

    def __init__(self, link, answers):
        self.link = link
        self.answers = answers
        self.requests = []
        self.closed = False

    async def dispatch(self, request):
        self.requests.append(request)
        answer = self.answers.get(request["method"], {"result": {}})
        answer = answer(request) if callable(answer) else answer
        return {"jsonrpc": "2.0", "id": request["id"], **answer}

    async def close(self):
        self.closed = True


@pytest.fixture
def gateway():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    runner = types.SimpleNamespace(_gateway_loop=loop, session_authority=object())
    chats = gateway_chats._AuthorityChats(runner)
    answers = {}
    connections = []

    def connect(link):
        connection = FakeConnection(link, answers)
        connections.append(connection)
        return connection

    chats.connect = connect
    yield chats, answers, connections
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


class Transport:
    conduit_surface = "watch"
    conduit_input = "spoken"

    def __init__(self):
        self.frames = []

    def write(self, obj):
        self.frames.append(obj)
        return True


def _event(sid, kind, payload, generation=3):
    return {"jsonrpc": "2.0", "method": "event",
            "params": {"type": kind, "session_id": sid, "payload": payload, "execution_generation": generation}}


def _call(chats, transport, method, params):
    return chats.dispatch({"jsonrpc": "2.0", "id": "r1", "method": method, "params": params}, transport)


def test_a_create_takes_the_authoritys_shape_and_keeps_its_fences(gateway):
    chats, answers, connections = gateway
    answers["session.create"] = {"result": {"session_id": "s1", "execution_generation": 2, "subscription_id": "sub"}}
    transport = Transport()
    created = _call(chats, transport, "session.create",
                    {"cols": 96, "source": "desktop", "title": "Job", "reasoning_effort": "low", "fast": True})
    assert created["result"]["session_id"] == "s1"
    sent = connections[0].requests[0]["params"]
    assert sent == {"source": "gui", "title": "Job", "reasoning": "low", "service_tier": "fast"}
    assert chats._sessions.get("s1") == {"transport": transport}
    # A steer names the turn by its generation.
    _call(chats, transport, "session.steer", {"session_id": "s1", "text": "more"})
    assert connections[0].requests[-1]["params"] == {"session_id": "s1", "text": "more", "execution_generation": 2}
    # Closing detaches this connection's view; the chat stays.
    _call(chats, transport, "session.close", {"session_id": "s1"})
    assert connections[0].requests[-1]["method"] == "session.detach"
    assert connections[0].requests[-1]["params"] == {"session_id": "s1", "subscription_id": "sub"}
    assert chats._sessions.get("s1") is None


def test_a_submit_gets_its_own_admission_and_names_the_watch(gateway, monkeypatch):
    chats, answers, connections = gateway
    answers["prompt.submit"] = {"result": {"status": "queued"}}
    kept = {}
    monkeypatch.setattr(turn_surface, "record_for_authority", lambda report: kept.update(
        session_id=report.session_id, surface=report.surface, entry=report.entry))
    transport = Transport()
    _call(chats, transport, "prompt.submit", {"session_id": "s1", "text": "hi"})
    _call(chats, transport, "prompt.submit", {"session_id": "s1", "text": "hi"})
    first, second = (r["params"]["submission_id"] for r in connections[0].requests)
    assert first and first != second
    assert kept == {"session_id": "s1", "surface": "watch", "entry": "spoken"}


def test_a_refused_submit_leaves_no_surface_for_the_next_turn(gateway):
    chats, answers, _connections = gateway
    answers["prompt.submit"] = {"error": {"code": 4009, "message": "busy", "data": {"reason": "busy"}}}
    transport = Transport()
    refused = _call(chats, transport, "prompt.submit", {"session_id": "drop-1", "text": "again"})
    assert refused["error"]
    # The same text typed later isn't read as the Watch's.
    assert turn_surface.note(False, "again", session_id="drop-1") is None
    answers["prompt.submit"] = {"result": {"status": "queued"}}
    _call(chats, transport, "prompt.submit", {"session_id": "drop-1", "text": "again"})
    assert turn_surface.note(False, "again", session_id="drop-1")


def test_a_turn_the_authority_starts_at_once_keeps_its_surface(gateway):
    chats, answers, _connections = gateway
    answers["prompt.submit"] = {"result": {"status": "started"}}
    _call(chats, Transport(), "prompt.submit", {"session_id": "start-1", "text": "now"})
    assert turn_surface.note(False, "now", session_id="start-1")
    answers["prompt.submit"] = {"result": {"status": "terminal"}}
    _call(chats, Transport(), "prompt.submit", {"session_id": "start-2", "text": "done"})
    assert turn_surface.note(False, "done", session_id="start-2") is None


def test_a_resumed_chat_detaches_on_close(gateway):
    chats, answers, connections = gateway
    answers["session.resume"] = {"result": {"session_id": "r1", "execution_generation": 4, "subscription_id": "sub-r"}}
    transport = Transport()
    _call(chats, transport, "session.resume", {"session_id": "r1"})
    assert chats._sessions.get("r1") == {"transport": transport}
    _call(chats, transport, "session.close", {"session_id": "r1"})
    assert connections[0].requests[-1]["method"] == "session.detach"
    assert connections[0].requests[-1]["params"] == {"session_id": "r1", "subscription_id": "sub-r"}


def test_a_closed_link_opens_no_new_connection(gateway):
    chats, _answers, connections = gateway
    transport = Transport()
    link = chats.link(transport)
    link.close()
    answer = link.call({"jsonrpc": "2.0", "id": "r1", "method": "session.create", "params": {}})
    assert "closed" in answer["error"]["message"]
    assert connections == []


def test_a_call_that_times_out_is_cancelled_and_says_it_may_have_started(gateway):
    chats, _answers, _connections = gateway
    started = []

    async def slow():
        started.append(True)
        await asyncio.sleep(30)

    with pytest.raises(TimeoutError, match="no answer within"):
        chats.run(slow(), timeout=0.2)
    assert started
    chats.connect = lambda link: types.SimpleNamespace(dispatch=lambda request: slow())
    original = chats.run
    chats.run = lambda coroutine: original(coroutine, timeout=0.2)
    answer = _call(chats, Transport(), "session.interrupt", {"session_id": "s9"})
    assert answer["error"]["message"].endswith("timed out; it may still have started")


def test_a_failing_owner_scope_fails_closed(monkeypatch):
    module = types.ModuleType("gateway.session_authorities")

    def owner_scope(authority):
        raise RuntimeError("no such profile")

    module.owner_scope = owner_scope
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", module)
    with pytest.raises(RuntimeError, match="no such profile"):
        gateway_chats.authority_scope(object())
    monkeypatch.setitem(sys.modules, "gateway.session_authorities", types.ModuleType("gateway.session_authorities"))
    with gateway_chats.authority_scope(object()):
        pass


def test_approvals_read_in_the_tui_gateways_words(gateway):
    chats, answers, connections = gateway
    answers["approval.respond"] = {"result": {"status": "resolved", "prompt_id": "p1"}}
    transport = Transport()
    link = chats.link(transport)
    link.write(_event("s1", "approval.request", {"prompt_id": "p1", "execution_generation": 4, "command": "ls"}))
    assert transport.frames[-1]["params"]["payload"]["request_id"] == "p1"
    # A requestless answer is the oldest one waiting, as the TUI gateway takes it.
    answered = _call(chats, transport, "approval.respond", {"session_id": "s1", "choice": "deny"})
    assert answered["result"] == {"resolved": 1}
    assert connections[0].requests[-1]["params"] == {"session_id": "s1", "execution_generation": 4,
                                                     "prompt_id": "p1", "choice": "deny"}
    # Nothing waits now: answered without asking.
    assert _call(chats, transport, "approval.respond", {"session_id": "s1", "choice": "deny"})["result"] == {"resolved": 0}
    link.write(_event("s1", "approval.settled", {"prompt_id": "p1"}))
    assert transport.frames[-1]["params"]["type"] == "approval.cancelled"
    assert transport.frames[-1]["params"]["payload"] == {"request_ids": ["p1"]}


def test_an_ended_turn_answers_as_nothing_running(gateway):
    chats, answers, connections = gateway
    stale = {"error": {"code": 4001, "message": "stale_generation", "data": {"reason": "stale_generation"}}}
    answers.update({"session.steer": stale, "session.interrupt": stale, "approval.respond": stale})
    transport = Transport()
    chats.link(transport).write(_event("s1", "approval.request", {"prompt_id": "p1"}))
    assert _call(chats, transport, "session.steer", {"session_id": "s1", "text": "x"})["result"] == {"status": "rejected"}
    assert _call(chats, transport, "session.interrupt", {"session_id": "s1"})["result"] == {}
    assert _call(chats, transport, "approval.respond",
                 {"session_id": "s1", "choice": "once", "request_id": "p1"})["result"] == {"resolved": 0}


def test_an_unknown_generation_is_read_from_the_chat(gateway):
    chats, answers, connections = gateway
    answers["session.resume"] = {"result": {"session_id": "s2", "execution_generation": 7}}
    transport = Transport()
    _call(chats, transport, "session.interrupt", {"session_id": "s2"})
    assert [r["method"] for r in connections[0].requests] == ["session.resume", "session.interrupt"]
    assert connections[0].requests[-1]["params"]["execution_generation"] == 7


def test_a_released_transport_closes_its_connection(gateway):
    chats, answers, connections = gateway
    transport = Transport()
    _call(chats, transport, "session.list", {})
    gateway_chats.release(transport)
    pause = threading.Event()
    for _ in range(100):
        if connections[0].closed:
            break
        pause.wait(0.01)
    assert connections[0].closed
    # The next call opens a new connection.
    _call(chats, transport, "session.list", {})
    assert len(connections) == 2


def test_calls_on_the_gateways_own_loop_are_refused(gateway):
    chats, _answers, _connections = gateway

    async def on_loop():
        return chats.run(asyncio.sleep(0))

    with pytest.raises(RuntimeError, match="own loop"):
        asyncio.run_coroutine_threadsafe(on_loop(), chats.runner._gateway_loop).result(5)


def test_no_gateway_no_authority():
    assert gateway_chats.session_api() is None
    assert gateway_chats.authority_session("s1") is None


def test_an_authority_chats_report_is_taken_by_its_turn_only():
    report = turn_surface.Report("auth-1", turn_surface.text_sha256("hello"), "carplay", "spoken")
    turn_surface.record_for_authority(report)
    # Another chat's turn of the same text gets nothing.
    assert turn_surface.note(False, "hello", session_id="auth-2") is None
    line = turn_surface.note(False, "hello", session_id="auth-1")
    assert line and line.startswith("[Conduit: this message came from CarPlay in the user's car, spoken.")
    # Taken once.
    assert turn_surface.note(False, "hello", session_id="auth-1") is None


def test_pulse_sends_on_the_authority_with_its_model(monkeypatch):
    calls = []

    class Chats:
        def dispatch(self, frame, transport):
            calls.append((frame["method"], frame["params"]))
            if frame["method"] == "session.resume":
                return {"result": {"session_id": "pulse-1", "revision": 5, "execution_generation": 2,
                                   "info": {"model": "old", "provider": "custom"}}}
            if frame["method"] == "session.mutate":
                if "confirm" in frame["params"]["payload"]:
                    return {"result": {"model": "fake-model-2"}}
                return {"result": {"status": "confirmation_required", "confirm": "tok"}}
            return {"result": {"status": "started"}}

        def release(self, transport):
            calls.append(("release", None))

    monkeypatch.setattr(pulse_host, "_authority_chats", lambda: Chats())
    sent = pulse_host.send("pulse-1", "check in", profile="pulsebot", model={"provider": "custom", "model": "fake-model-2"})
    # The authority started it at once: Pulse reads it as the TUI gateway's "streaming".
    assert sent == pulse_host.Sent(True, "streaming", "started")
    methods = [method for method, _ in calls]
    assert methods == ["session.resume", "session.mutate", "session.mutate", "prompt.submit", "release"]
    assert calls[0][1] == {"session_id": "pulse-1", "omit_messages": True, "profile": "pulsebot"}
    assert calls[2][1]["payload"] == {"model": "fake-model-2", "provider": "custom", "confirm": "tok"}
    assert calls[2][1]["expected_revision"] == 5 and calls[2][1]["expected_generation"] == 2
    assert calls[3][1] == {"session_id": "pulse-1", "text": "check in", "display_kind": "hidden", "profile": "pulsebot"}
    assert pulse_host.on_authority() and pulse_host.available()


def test_an_authority_report_waits_for_a_queued_turn():
    now = [1000.0]
    report = turn_surface.Report("auth-old", turn_surface.text_sha256("yes"), "watch", "spoken")
    turn_surface.record_for_authority(report, clock=lambda: now[0])
    # Queued behind a long turn: it runs minutes later and still gets its line.
    now[0] += 300
    assert turn_surface._take_for_authority("auth-old", "yes", lambda: now[0])
    turn_surface.record_for_authority(report, clock=lambda: now[0])
    now[0] += turn_surface.AUTHORITY_FRESH_S + 1
    assert turn_surface._take_for_authority("auth-old", "yes", lambda: now[0]) is None


def test_a_refused_answer_keeps_its_prompt_to_try_again(gateway):
    chats, answers, connections = gateway
    transport = Transport()
    link = chats.link(transport)
    link.write(_event("s1", "approval.request", {"prompt_id": "p1", "command": "ls"}))
    answers["approval.respond"] = {"error": {"code": 4001, "message": "invalid_params", "data": {"reason": "invalid_params"}}}
    assert _call(chats, transport, "approval.respond", {"session_id": "s1", "request_id": "p1", "choice": "bogus"})["error"]
    answers["approval.respond"] = {"result": {"status": "resolved", "prompt_id": "p1"}}
    assert _call(chats, transport, "approval.respond", {"session_id": "s1", "request_id": "p1", "choice": "once"})["result"] == {"resolved": 1}


def test_a_failed_detach_keeps_the_view_to_close_again(gateway):
    chats, answers, connections = gateway
    answers["session.resume"] = {"result": {"session_id": "d1", "execution_generation": 1, "subscription_id": "sub-d"}}
    transport = Transport()
    _call(chats, transport, "session.resume", {"session_id": "d1"})
    answers["session.detach"] = {"error": {"code": 5000, "message": "storage_unavailable",
                                           "data": {"reason": "storage_unavailable"}}}
    assert _call(chats, transport, "session.close", {"session_id": "d1"})["error"]
    answers["session.detach"] = {"result": {}}
    _call(chats, transport, "session.close", {"session_id": "d1"})
    detaches = [r for r in connections[0].requests if r["method"] == "session.detach"]
    assert len(detaches) == 2 and detaches[1]["params"]["subscription_id"] == "sub-d"
