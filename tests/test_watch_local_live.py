"""Local Live on the Watch: the local_live engine of the Watch audio bridge.

The Watch runs the phone's Local Live session over the bridge: its events
and audio are relayed to the profile's speech server, the server's ask for
each reply (the model address) reaches the Watch's call, and this host runs
it in the call's own Hermes chat. Here with a stand-in speech server, a
stand-in Hermes session API and a stand-in relay socket.
"""

import asyncio
import base64
import importlib.util
import json
import os
import pathlib
import threading
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch_local_live",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()

GRANT_ID = "G" * 22
SECRET = bytes(range(32))
UP = "watch-to-host"
DOWN = "host-to-watch"


def _run(coro):
    return asyncio.run(coro)


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


async def _spawned_done(held=0):
    """Until the plugin's tasks on this loop are done, but ``held`` ones a
    test keeps waiting: what they'd send has been sent by then."""
    loop = asyncio.get_running_loop()
    await _until(lambda: sum(1 for task in api._local_live_tasks
                             if task.get_loop() is loop and not task.done()) <= held)
    for _ in range(5):
        await asyncio.sleep(0)


async def _frames(turn, timeout=5.0):
    """A turn's words until its end, as the model address reads them."""
    words = []
    while True:
        kind, text = await asyncio.wait_for(turn.frames.get(), timeout)
        if kind == "done":
            return "".join(words)
        words.append(text)


class FakeRelaySocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class Watch:
    def __init__(self):
        self.sid = os.urandom(16)
        keys = api.watch_audio_keys(SECRET, self.sid)
        self.up, self.down = keys[UP], keys[DOWN]
        self.counter = 0

    def hello(self):
        return bytes([4]) + self.sid + bytes([1])

    def seal(self, kind, plain):
        message = api.seal_watch_audio(self.up, UP, GRANT_ID, self.sid, kind, self.counter, plain)
        self.counter += 1
        return message

    def control(self, payload):
        return self.seal(3, json.dumps(payload).encode())

    def event(self, payload):
        return self.seal(2, json.dumps(payload).encode())

    def received(self, socket):
        out = []
        for message in socket.sent:
            try:
                kind, _, plain = api.open_watch_audio(self.down, DOWN, GRANT_ID, self.sid, message)
            except api.WatchAudioError:
                continue
            out.append((kind, json.loads(plain) if kind in (2, 3) else plain))
        return out

    def events(self, socket, kind=None):
        found = [value for k, value in self.received(socket) if k in (2, 3)]
        return [value for value in found if kind is None or value.get("type") == kind]


class SpeechServer:
    def __init__(self):
        self.connects = []
        self.sockets = []

    async def connect(self, url, headers):
        socket = SpeechSocket()
        self.connects.append((url, headers))
        self.sockets.append(socket)
        return socket


class SpeechSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.received = []
        self.close_code = None
        self.close_reason = ""
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def push(self, event):
        self.incoming.put_nowait(json.dumps(event))

    async def send(self, message):
        if self.closed:
            raise ConnectionError("closed")
        self.received.append(json.loads(message))

    async def close(self):
        if not self.closed:
            self.closed = True
            self.close_code = 1000
            self.incoming.put_nowait(None)


class Hermes:
    """Hermes' session API as the plugin calls it."""

    def __init__(self):
        self.calls = []
        self.transport = None
        self.sessions = 0
        self.redirect_status = "redirected"
        self.submit_status = ""
        self.resolved = True

    def dispatch(self, request, transport=None):
        self.transport = transport
        method, params = request["method"], request["params"]
        self.calls.append((method, dict(params)))
        if method == "session.create":
            self.sessions += 1
            return {"id": request["id"], "result": {"session_id": f"s{self.sessions}",
                                                    "stored_session_id": f"stored{self.sessions}"}}
        if method == "session.redirect":
            return {"id": request["id"], "result": {"status": self.redirect_status}}
        if method == "prompt.submit":
            return {"id": request["id"], "result": {"status": self.submit_status}}
        if method == "approval.respond":
            return {"id": request["id"], "result": {"resolved": self.resolved}}
        return {"id": request["id"], "result": {}}

    def emit(self, kind, payload, sid="s1"):
        self.transport.write({"jsonrpc": "2.0", "method": "event",
                              "params": {"type": kind, "session_id": sid, "payload": payload}})

    def prompts(self):
        return [params for method, params in self.calls if method == "prompt.submit"]

    def redirects(self):
        return [params for method, params in self.calls if method == "session.redirect"]


@pytest.fixture()
def call(monkeypatch):
    speech = SpeechServer()
    hermes = Hermes()
    monkeypatch.setattr(api, "_connect_speech_server", speech.connect)
    monkeypatch.setattr(api, "speech_server_target", lambda settings=None: ("ws://10.0.0.5:8765/v1/realtime", "tok"))
    monkeypatch.setattr(api, "local_live_watch_status",
                        lambda profile: {"runtime": "ready", "source": "local_live", "reason": None})
    monkeypatch.setattr(api, "_watch_chat_session_api", lambda: hermes)
    monkeypatch.setattr(api, "_speech_server_limiter", api._MintLimiter(100, 60.0))
    monkeypatch.setattr(api, "_speech_server_open", {})
    monkeypatch.setattr(api, "_local_live_calls", {})
    return types.SimpleNamespace(speech=speech, hermes=hermes)


def _bridge(profile=None):
    grant = types.SimpleNamespace(grant_id=GRANT_ID, profile=profile, relay_url="https://relay.example",
                                  credential="c", expires_at=time.monotonic() + 600,
                                  closed=types.SimpleNamespace(is_set=lambda: False))
    return api._WatchAudioBridge(grant, SECRET, slots=api._WatchAudioSlots(4))


async def _attach(bridge):
    socket = FakeRelaySocket()
    bridge.loop = asyncio.get_running_loop()
    bridge.wake = asyncio.Event()
    bridge.send_lock = asyncio.Lock()
    bridge.socket = socket
    return socket


async def _start(bridge, socket, watch, **extra):
    await bridge._handle(watch.hello())
    await bridge._handle(watch.control({"type": "start", "engine": "local_live", **extra}))
    await _until(lambda: watch.events(socket, "started") or watch.events(socket, "error"))
    return (watch.events(socket, "started") or watch.events(socket, "error"))[-1]


# --- Readiness ------------------------------------------------------------------


@pytest.mark.parametrize("live,sessions,ready", [(False, True, False), (True, False, False), (True, True, True)])
def test_the_engine_is_ready_with_local_live_and_hermes_chats(monkeypatch, live, sessions, ready):
    monkeypatch.setattr(api, "_local_live_auth_state", {"ready": True, "reason": None, "started": True})
    monkeypatch.setattr(api, "speech_server_status", lambda: {"available": live,
                                                              "reason": None if live else "No speech server"})
    monkeypatch.setattr(api, "local_live_key", lambda *args, **kwargs: pytest.fail("a status check makes no key"))
    monkeypatch.setattr(api, "_hermes_session_api", lambda: object() if sessions else None)
    monkeypatch.setattr(api, "_watch_chats_can_load", lambda: False)
    monkeypatch.setattr(api, "_watch_chat_session_api", lambda: pytest.fail("a status check never imports"))
    state = api.local_live_watch_status(None)
    assert (state["runtime"] == "ready") is ready
    if not ready:
        assert state["reason"]
    runtime = types.SimpleNamespace(python=lambda: None, status=lambda: {"runtime": "missing"})
    engines = api._watch_audio_grant_engines(runtime, None)
    assert engines == (["grok", "local_live"] if ready else ["grok"])
    assert api.watch_audio_status(runtime)["engines"]["local_live"] == state


def test_the_engine_says_why_local_live_is_off(monkeypatch):
    monkeypatch.setattr(api, "_local_live_auth_state", {"ready": False, "reason": "No token routes", "started": True})
    assert api.local_live_watch_status(None)["reason"] == "No token routes"
    monkeypatch.setattr(api, "_local_live_auth_state", {"ready": True, "reason": None, "started": True})
    assert api.local_live_watch_status("bad name!")["reason"] == api.LOCAL_LIVE_PROFILE_NAME_REASON


def test_capability_is_advertised():
    assert "watch-local-live" in api.ROUTE_CAPABILITIES


# --- The call -------------------------------------------------------------------


def test_the_watch_runs_its_local_live_session_through_the_bridge(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        started = await _start(bridge, socket, watch)
        assert started == {"type": "started", "engine": "local_live", "voice": None, "input_rate": 24000,
                           "output_rate": 24000, "session_id": "stored1", "resumed": False}
        assert call.speech.connects == [("ws://10.0.0.5:8765/v1/realtime", {"Authorization": "Bearer tok"})]
        [created] = [params for method, params in call.hermes.calls if method == "session.create"]
        assert created["title"] == "Watch call" and created["source"] == "desktop"
        upstream = call.speech.sockets[0]

        # The Watch's own session.update goes to the server unchanged, its
        # keepalive stays here, and its microphone is appended.
        update = {"type": "session.update", "session": {"type": "realtime"}}
        await bridge._handle(watch.event(update))
        await bridge._handle(watch.event({"type": "conduit.ping"}))
        await bridge._handle(watch.seal(1, b"\x01\x00\x02\x00"))
        await _until(lambda: len(upstream.received) == 2)
        assert upstream.received[0] == update
        assert upstream.received[1] == {"type": "input_audio_buffer.append",
                                        "audio": base64.b64encode(b"\x01\x00\x02\x00").decode()}

        # The server's events reach the Watch, its audio as audio.
        upstream.push({"type": "session.updated"})
        upstream.push({"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x10\x27" * 240).decode()})
        await _until(lambda: watch.events(socket, "session.updated") and any(k == 1 for k, _ in watch.received(socket)))
        assert b"".join(v for k, v in watch.received(socket) if k == 1) == b"\x10\x27" * 240

        # The call answers the model address.
        assert api._local_live_active_call("") is bridge.session.call

        await bridge._handle(watch.control({"type": "end"}))
        await _until(lambda: watch.events(socket, "ended"))
        assert watch.events(socket, "ended") == [{"type": "ended", "engine": "local_live", "reason": "ended"}]
        assert upstream.closed and api._speech_server_open == {} and bridge.slots.used == 0
        assert api._local_live_calls == {}
        # The chat outlives the stream, for a rejoin; the grant's end closes it.
        assert list(bridge.call_chats) == ["stored1"]

    _run(scenario())


def test_the_readiness_check_goes_to_the_watch_and_its_greeting_back(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        marker = "conduit-ready-0f1e"
        live, read_back = api._local_live_route("", marker)
        assert live is bridge.session.call and read_back is False
        turn = await live.start_turn(marker)
        await _until(lambda: watch.events(socket, "conduit.turn"))
        [ask] = watch.events(socket, "conduit.turn")
        assert ask == {"type": "conduit.turn", "turn_id": turn.turn_id, "text": marker}
        await bridge._handle(watch.event({"type": "conduit.turn.delta", "turn_id": turn.turn_id, "text": "Hi there."}))
        await bridge._handle(watch.event({"type": "conduit.turn.done", "turn_id": turn.turn_id}))
        assert await _frames(turn) == "Hi there."
        # Never Hermes', and never the server's.
        assert call.hermes.prompts() == []
        assert not any(event["type"].startswith("conduit.") for event in call.speech.sockets[0].received)
        await bridge._end_session("test")

    _run(scenario())


def test_a_turn_runs_in_the_calls_chat_and_its_reply_streams_back_spoken(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        live = bridge.session.call
        turn = await live.start_turn("  What's the   weather? ")
        await _until(lambda: call.hermes.prompts())
        [prompt] = call.hermes.prompts()
        assert prompt == {"session_id": "s1", "text": "What's the weather?", "surface": "voice-live",
                          "voice_turn": True}
        call.hermes.emit("message.start", {})
        call.hermes.emit("message.delta", {"text": "Sunny. "})
        call.hermes.emit("message.delta", {"text": "**It is** 20 degrees, see [the forecast](https://x.example).\n"})
        call.hermes.emit("message.delta", {"text": "```\nweather --now\n```\n"})
        # Another chat's events aren't this call's.
        call.hermes.emit("message.delta", {"text": "Not mine."}, sid="s9")
        call.hermes.emit("message.complete", {"text": "whole reply", "status": "complete"})
        assert await _frames(turn) == ("Sunny. It is 20 degrees, see the forecast. "
                                       f"{api.LOCAL_LIVE_WATCH_CODE_NOTE} ")
        await bridge._end_session("test")

    _run(scenario())


def test_a_reply_that_comes_whole_is_spoken_whole(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        turn = await bridge.session.call.start_turn("Hello")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.complete", {"text": "Hello! How can I help?"})
        assert await _frames(turn) == "Hello! How can I help?"
        await bridge._end_session("test")

    _run(scenario())


def test_words_spoken_over_a_running_reply_correct_it(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        live = bridge.session.call
        first = await live.start_turn("Book a table for two")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Sure, booking for two. "})
        assert (await asyncio.wait_for(first.frames.get(), 5)) == ("delta", "Sure, booking for two. ")
        # The server let the first turn go (the user talked over it) and asks again.
        second = await live.start_turn("Make it three")
        assert (await asyncio.wait_for(first.frames.get(), 5)) == ("done", "")
        await _until(lambda: call.hermes.redirects())
        assert call.hermes.redirects() == [{"session_id": "s1", "text": "Make it three"}]
        assert len(call.hermes.prompts()) == 1
        await _until(lambda: not bridge.session.runner.reply.correcting)
        # The cut-off reply's notice isn't the answer; what follows is.
        call.hermes.emit("message.interrupted", {})
        call.hermes.emit("message.delta", {"text": "Okay, three people."})
        call.hermes.emit("message.complete", {"text": "Okay, three people."})
        assert await _frames(second) == "Okay, three people."
        await bridge._end_session("test")

    _run(scenario())


def test_a_correction_with_nothing_running_is_a_turn_of_its_own(call):
    call.hermes.redirect_status = "rejected"

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        live = bridge.session.call
        first = await live.start_turn("One")
        await _until(lambda: call.hermes.prompts())
        second = await live.start_turn("Two")
        await _until(lambda: len(call.hermes.prompts()) == 2)
        assert call.hermes.prompts()[1]["text"] == "Two" and len(call.hermes.redirects()) == 1
        call.hermes.emit("message.complete", {"text": "Two it is."})
        assert await _frames(second) == "Two it is."
        assert await _frames(first) == ""
        await bridge._end_session("test")

    _run(scenario())


def test_a_cancelled_turn_stops_being_spoken_and_its_reply_goes_on_in_the_chat(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        live = bridge.session.call
        turn = await live.start_turn("Tell me a story")
        await _until(lambda: call.hermes.prompts())
        await live.cancel_turn(turn)
        call.hermes.emit("message.delta", {"text": "Once upon a time. "})
        assert await _frames(turn) == ""
        assert bridge.session.runner.turn is None and bridge.session.runner.reply is not None
        assert not any(method == "session.interrupt" for method, _ in call.hermes.calls)
        await bridge._end_session("test")

    _run(scenario())


@pytest.mark.parametrize("event,line", [("approval.request", api.LOCAL_LIVE_WATCH_NEEDS_INPUT_LINE),
                                        ("error", api.LOCAL_LIVE_WATCH_ERROR_LINE)])
def test_a_question_or_error_from_hermes_is_said_instead(call, event, line):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        turn = await bridge.session.call.start_turn("Delete my files")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit(event, {"message": "x"})
        assert await _frames(turn) == line
        await bridge._end_session("test")

    _run(scenario())


def test_words_hermes_steers_into_a_typed_turn_are_said_so(call):
    call.hermes.submit_status = "queued"

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        turn = await bridge.session.call.start_turn("And then?")
        assert await _frames(turn) == api.LOCAL_LIVE_WATCH_QUEUED_LINE
        await bridge._end_session("test")

    _run(scenario())


def test_a_rejoining_watch_carries_on_in_the_same_chat_and_the_grants_end_closes_it(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        assert api._local_live_calls == {}
        again = Watch()
        started = await _start(bridge, socket, again, resume_session_id="stored1")
        assert started["session_id"] == "stored1" and started["resumed"] is True
        assert call.hermes.sessions == 1
        await bridge._end_session("test")
        fresh = Watch()
        started = await _start(bridge, socket, fresh)
        assert started["session_id"] == "stored2" and started["resumed"] is False
        await bridge._end_session("test")
        return bridge

    bridge = _run(scenario())
    chats = dict(bridge.call_chats)
    bridge.stop_requested.set()
    bridge._run()
    assert bridge.call_chats == {}
    # Closed in the background, so the bridge's end doesn't wait on Hermes.
    deadline = time.monotonic() + 5
    while not all(chat.closed for chat in chats.values()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(chat.closed for chat in chats.values())
    assert ("session.close", {"session_id": "s1"}) in call.hermes.calls


def test_a_watch_call_waits_while_another_local_live_call_runs_on_the_profile(call):
    class PhoneCall:
        speaks = False
        closed = False
        profile = ""

    phone = PhoneCall()
    api._local_live_register(phone)
    try:
        async def scenario():
            bridge = _bridge()
            socket = await _attach(bridge)
            error = await _start(bridge, socket, Watch())
            assert error["type"] == "error" and error["code"] == "busy"
            # Refused before it dialled the server or opened a chat.
            assert call.speech.connects == [] and call.hermes.sessions == 0

        _run(scenario())
    finally:
        api._local_live_unregister(phone)


def test_a_rejoin_from_the_same_watch_starts_while_its_last_call_winds_down(call):
    async def scenario():
        bridge = _bridge()
        # The bridge's previous call, still registered and open.
        earlier = object.__new__(api._LocalLiveWatchCall)
        earlier.session, earlier.profile, earlier.closed = types.SimpleNamespace(bridge=bridge), "", False
        api._local_live_register(earlier)
        try:
            socket = await _attach(bridge)
            started = await _start(bridge, socket, Watch())
            assert started["type"] == "started"
        finally:
            api._local_live_unregister(earlier)
        await bridge._end_session("test")

    _run(scenario())


def test_the_registration_gate_skips_only_the_calls_it_may_ignore():
    class Call:
        speaks = False
        closed = False
        profile = "busy-profile"

    running, other = Call(), Call()
    assert api._local_live_register(running)
    try:
        assert not api._local_live_register(other)
        assert api._local_live_register(other, lambda call: call is running)
        api._local_live_unregister(other)
    finally:
        api._local_live_unregister(running)


def test_a_profile_call_is_filed_under_its_profile(call, monkeypatch):
    monkeypatch.setattr(api, "_profile_scope", lambda profile: api.nullcontext())

    async def scenario():
        bridge = _bridge(profile="Work")
        socket = await _attach(bridge)
        await _start(bridge, socket, Watch())
        assert api._local_live_active_call("Work") is bridge.session.call
        assert api._local_live_active_call("") is None
        [created] = [params for method, params in call.hermes.calls if method == "session.create"]
        assert created["profile"] == "Work"
        await bridge._end_session("test")

    _run(scenario())


def test_a_server_that_cant_be_reached_is_told_to_the_watch(call, monkeypatch):
    async def refuse(url, headers):
        raise api.RealtimeUpstreamRefused(403)

    monkeypatch.setattr(api, "_connect_speech_server", refuse)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        error = await _start(bridge, socket, Watch())
        assert error["type"] == "error" and error["code"] == "refused"
        await _until(lambda: bridge.session is None)
        assert api._speech_server_open == {} and api._local_live_calls == {}
        # No chat for a call that never reached its server.
        assert bridge.call_chats == {} and call.hermes.sessions == 0

    _run(scenario())


def test_local_live_off_for_the_profile_refuses_the_start(call, monkeypatch):
    monkeypatch.setattr(api, "local_live_watch_status",
                        lambda profile: {"runtime": "missing", "source": None, "reason": "No speech server is set"})

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        error = await _start(bridge, socket, Watch())
        assert error["type"] == "error" and error["message"] == "No speech server is set"
        assert call.speech.connects == []

    _run(scenario())


def test_turns_cross_from_the_dashboards_loop_to_a_bridge_on_its_own_thread(call):
    """In the dashboard the model address and the bridge run on two loops."""
    bridge_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=bridge_loop.run_forever, daemon=True)
    thread.start()
    try:
        async def on_bridge():
            bridge = _bridge()
            socket = await _attach(bridge)
            watch = Watch()
            await _start(bridge, socket, watch)
            return bridge, socket, watch

        bridge, socket, watch = asyncio.run_coroutine_threadsafe(on_bridge(), bridge_loop).result(10)

        async def on_dashboard():
            live, _ = api._local_live_route("", "conduit-ready-1")
            check = await live.start_turn("conduit-ready-1")
            await _until(lambda: watch.events(socket, "conduit.turn"))
            asyncio.run_coroutine_threadsafe(bridge._handle(
                watch.event({"type": "conduit.turn.done", "turn_id": check.turn_id})), bridge_loop).result(5)
            assert await _frames(check) == ""
            turn = await live.start_turn("Hi")
            await _until(lambda: call.hermes.prompts())
            # Hermes writes from its own thread.
            threading.Thread(target=call.hermes.emit, args=("message.complete", {"text": "Hello."})).start()
            assert await _frames(turn) == "Hello."

        asyncio.run(on_dashboard())
        asyncio.run_coroutine_threadsafe(bridge._end_session("test"), bridge_loop).result(10)
    finally:
        bridge_loop.call_soon_threadsafe(bridge_loop.stop)
        thread.join(5)


# --- Sentences ------------------------------------------------------------------


def _spoken(*deltas):
    text = api._SpokenText()
    return "".join(text.feed(delta) for delta in deltas) + "|" + text.finish()


def test_markdown_is_spoken_as_plain_sentences():
    assert _spoken("## Summary\n", "- **Bold** item with a [link](https://example.com/x)\n",
                   "1. Step one is `ls` at https://example.com.\n") == \
        "Summary.Bold item with a link.Step one is ls at.|"


def test_code_and_tables_stay_in_the_chat_said_once():
    spoken = _spoken("Here you go:\n```python\nprint('a. b')\n```\n", "| a | b |\n|---|---|\n| 1 | 2 |\n",
                     "```\nmore\n```\nThat's all.")
    assert spoken.count(api.LOCAL_LIVE_WATCH_CODE_NOTE) == 1
    assert spoken == f"Here you go: {api.LOCAL_LIVE_WATCH_CODE_NOTE}|That's all."


def test_numbers_dont_end_sentences_and_a_long_run_is_cut_on_a_space():
    assert _spoken("It was 3.5 percent. ", "Then") == "It was 3.5 percent.|Then."
    text = api._SpokenText()
    out = text.feed("word " * 120)
    assert out and len(out) <= api.LOCAL_LIVE_WATCH_MAX_SENTENCE_CHARS


# --- A read-back speech server ----------------------------------------------------


class ReadBackSocket(SpeechSocket):
    """The farm voice adapter: no automatic responses, each response read back."""

    hold = False
    # What its transcripts say instead of the text (a server with a model).
    says = None

    async def send(self, message):
        await super().send(message)
        event = self.received[-1]
        kind = event.get("type")
        if kind == "session.update":
            if api._local_live_turn_detection(event) is not None:
                self.push({"type": "error", "error": {"code": "realtime_live_unavailable",
                                                      "param": "session.audio.input.turn_detection.create_response"}})
            else:
                self.push({"type": "session.updated", "session": event["session"]})
        elif kind == "response.create":
            text = api._local_live_item_words(event["response"]["input"][0])
            rid = f"r{len(self.received)}"
            self.push({"type": "response.created", "response": {"id": rid}})
            if self.hold:
                self.held = rid
                return
            said = self.says or text
            self.push({"type": "response.output_audio_transcript.delta", "delta": said})
            self.push({"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x10\x27" * 240).decode()})
            self.push({"type": "response.output_audio_transcript.done", "transcript": said})
            self.push({"type": "response.done", "response": {"id": rid, "status": "completed"}})
        elif kind == "response.cancel" and self.hold:
            # Audio already on its way when the cancel lands, then the end.
            self.push({"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x00" * 240).decode()})
            self.push({"type": "response.done", "response": {"id": self.held, "status": "cancelled"}})


WATCH_UPDATE = {"type": "session.update", "session": {"type": "realtime", "audio": {"input": {
    "turn_detection": {"type": "server_vad", "create_response": True, "interrupt_response": True}}}}}


def test_on_a_read_back_server_the_host_runs_the_watchs_turns_and_has_each_sentence_read_back(call, monkeypatch):
    async def connect(url, headers):
        socket = ReadBackSocket()
        call.speech.sockets.append(socket)
        return socket

    monkeypatch.setattr(api, "_connect_speech_server", connect)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        await bridge._handle(watch.event(WATCH_UPDATE))
        await _until(lambda: watch.events(socket, "session.updated"))
        # The refusal stays on the host; the server got automatic responses off.
        assert not watch.events(socket, "error")
        assert upstream.received[1]["session"]["audio"]["input"]["turn_detection"]["create_response"] is False

        # The readiness check goes to the Watch, never to Hermes or the server.
        marker = "conduit-ready-9a"
        check = {"type": "response.create", "response": {"conversation": "none", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": marker}]}]}}
        await bridge._handle(watch.event(check))
        await _until(lambda: watch.events(socket, "conduit.turn"))
        [ask] = watch.events(socket, "conduit.turn")
        assert ask["text"] == marker
        await bridge._handle(watch.event({"type": "conduit.turn.done", "turn_id": ask["turn_id"]}))
        await _until(lambda: watch.events(socket, "response.done"))
        assert [event["type"] for event in watch.events(socket) if event["type"].startswith("response.")] == \
            ["response.created", "response.done"]

        # A spoken turn runs in the call's chat; its reply is read back.
        upstream.push({"type": "conversation.item.input_audio_transcription.completed",
                       "transcript": "What's the weather?"})
        await _until(lambda: call.hermes.prompts())
        assert call.hermes.prompts()[0]["text"] == "What's the weather?"
        call.hermes.emit("message.start", {})
        call.hermes.emit("message.delta", {"text": "Sunny. "})
        call.hermes.emit("message.delta", {"text": "It is twenty degrees, with a light wind from the west. "})
        call.hermes.emit("message.complete", {"text": "whole", "status": "complete"})
        await _until(lambda: len(watch.events(socket, "response.done")) == 2)
        spoken = [api._local_live_item_words(event["response"]["input"][0])
                  for event in upstream.received if event["type"] == "response.create"]
        assert spoken == ["Sunny.", "It is twenty degrees, with a light wind from the west."]
        events = watch.events(socket)
        reply = events[events.index(watch.events(socket, "response.done")[0]) + 1:]
        assert [event["type"] for event in reply].count("response.created") == 1
        assert "".join(event["delta"] for event in reply if event["type"] == "response.output_audio_transcript.delta") \
            == "Sunny. It is twenty degrees, with a light wind from the west."
        await _until(lambda: sum(1 for kind, _ in watch.received(socket) if kind == 1) >= 2, timeout=10)
        await bridge._end_session("test")

    _run(scenario())


def test_on_a_read_back_server_speaking_over_the_watchs_reply_stops_it(call, monkeypatch):
    async def connect(url, headers):
        socket = ReadBackSocket()
        socket.hold = True
        call.speech.sockets.append(socket)
        return socket

    monkeypatch.setattr(api, "_connect_speech_server", connect)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        await bridge._handle(watch.event(WATCH_UPDATE))
        await _until(lambda: watch.events(socket, "session.updated"))
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Tell me a story"})
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        call.hermes.emit("message.delta", {"text": "Once upon a time, there was a fox. "})
        await _until(lambda: watch.events(socket, "response.created"))
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "i2"})
        await _until(lambda: watch.events(socket, "response.done"))
        events = [event["type"] for event in watch.events(socket)]
        assert events[events.index("input_audio_buffer.speech_started"):] == \
            ["input_audio_buffer.speech_started", "response.done"]
        assert watch.events(socket, "response.done")[0]["response"]["status"] == "cancelled"
        assert {"type": "response.cancel"} in upstream.received
        # The turn went; Hermes' reply goes on in the chat, unspoken.
        assert bridge.session.runner.turn is None
        await asyncio.sleep(0.1)
        # The cancelled response's late audio never reached the Watch.
        assert not any(kind == 1 and value.startswith(b"\x01\x00") for kind, value in watch.received(socket))
        await bridge._end_session("test")

    _run(scenario())


def test_a_server_that_doesnt_read_back_ends_the_watch_call_as_refused(call, monkeypatch):
    async def connect(url, headers):
        socket = ReadBackSocket()
        socket.says = "I am a model and I say what I like"
        call.speech.sockets.append(socket)
        return socket

    monkeypatch.setattr(api, "_connect_speech_server", connect)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        await bridge._handle(watch.event(WATCH_UPDATE))
        await _until(lambda: watch.events(socket, "session.updated"))
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hello"})
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Hello there, how are you doing today? "})
        call.hermes.emit("message.delta", {"text": "I hope the weather is kind to you. "})
        call.hermes.emit("message.complete", {"text": "whole", "status": "complete"})
        await _until(lambda: watch.events(socket, "error"))
        [error] = watch.events(socket, "error")
        # A refusal: the Watch doesn't dial the same server again.
        assert error == {"type": "error", "code": "refused", "message": api.LOCAL_LIVE_NOT_READ_BACK}
        await bridge._end_session("test")

    _run(scenario())


# --- Approvals on the Watch -------------------------------------------------------


def _approval(command="rm -rf build", request_id="req1", description="Delete the build folder"):
    return {"command": command, "description": description, "request_id": request_id}


def _responds(hermes):
    return [params for method, params in hermes.calls if method == "approval.respond"]


async def _approvals_call(call, **extra):
    bridge = _bridge()
    socket = await _attach(bridge)
    watch = Watch()
    await _start(bridge, socket, watch, approvals=True, **extra)
    return bridge, socket, watch


def test_a_watch_that_takes_approvals_is_shown_hermes_request_and_answers_it(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        turn = await bridge.session.call.start_turn("Clean the build")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        call.hermes.emit("message.delta", {"text": "Okay, cleaning up. "})
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        [card] = watch.events(socket, "conduit.approval")
        assert card == {"type": "conduit.approval", "id": card["id"], "command": "rm -rf build",
                        "description": "Delete the build folder", "choices": ["once", "deny"], "pending": 1,
                        "cut": False}
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "once"}))
        await _until(lambda: watch.events(socket, "conduit.approval.done"))
        assert watch.events(socket, "conduit.approval.done") == [
            {"type": "conduit.approval.done", "id": card["id"], "resolved": True}]
        assert _responds(call.hermes) == [{"session_id": "s1", "choice": "once", "request_id": "req1"}]
        # The turn stayed open through the card: what Hermes says after is spoken too.
        call.hermes.emit("message.delta", {"text": "Done, the build folder is gone."})
        call.hermes.emit("message.complete", {"text": "whole", "status": "complete"})
        assert await _frames(turn) == ("Okay, cleaning up. " + api.LOCAL_LIVE_WATCH_APPROVAL_LINE + " "
                                       "Done, the build folder is gone.")
        await bridge._end_session("test")

    _run(scenario())


def test_with_two_pending_or_a_cut_command_the_card_offers_deny_only(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Do both")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval("one", "r1"))
        call.hermes.emit("approval.request", _approval("two", "r2"))
        await _until(lambda: any(card["choices"] == ["deny"] for card in watch.events(socket, "conduit.approval")))
        card = watch.events(socket, "conduit.approval")[-1]
        assert card["command"] == "one" and card["pending"] == 2
        # Once isn't taken while the card might not be Hermes' oldest.
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "once"}))
        await _until(lambda: watch.events(socket, "conduit.approval.done"))
        assert watch.events(socket, "conduit.approval.done")[0]["resolved"] is False
        assert _responds(call.hermes) == []
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "deny"}))
        await _until(lambda: _responds(call.hermes))
        # The next one is shown, and alone it can be approved.
        await _until(lambda: watch.events(socket, "conduit.approval")[-1]["command"] == "two")
        assert watch.events(socket, "conduit.approval")[-1]["choices"] == ["once", "deny"]
        call.hermes.emit("message.complete", {"text": "whole"})
        await _until(lambda: watch.events(socket, "conduit.approval.gone"))
        long = "x" * (api.LOCAL_LIVE_WATCH_APPROVAL_COMMAND_CHARS + 1)
        call.hermes.emit("approval.request", _approval(long, "r3"))
        await _until(lambda: watch.events(socket, "conduit.approval")[-1].get("cut"))
        cut = watch.events(socket, "conduit.approval")[-1]
        assert cut["choices"] == ["deny"] and len(cut["command"]) == api.LOCAL_LIVE_WATCH_APPROVAL_COMMAND_CHARS
        await bridge._end_session("test")

    _run(scenario())


def test_a_rejoining_watch_is_shown_the_approval_still_pending(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        # Hermes asks between the two streams.
        call.hermes.emit("approval.request", _approval())
        again = Watch()
        await _start(bridge, socket, again, approvals=True, resume_session_id="stored1")
        await _until(lambda: again.events(socket, "conduit.approval"))
        assert again.events(socket, "conduit.approval")[0]["command"] == "rm -rf build"
        await bridge._end_session("test")

    _run(scenario())


def test_an_approval_nobody_answers_goes_with_hermes_timeout(call, monkeypatch):
    monkeypatch.setattr(api, "_hermes_approval_timeout", lambda: 0.2)

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        await _until(lambda: watch.events(socket, "conduit.approval.gone"), timeout=3)
        await bridge._end_session("test")

    _run(scenario())


def test_a_watch_that_doesnt_take_approvals_is_told_to_answer_in_the_chat(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        turn = await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        assert await _frames(turn) == api.LOCAL_LIVE_WATCH_NEEDS_INPUT_LINE
        await _spawned_done()
        assert watch.events(socket, "conduit.approval") == []
        # And its answers aren't taken.
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": "x", "choice": "once"}))
        await _spawned_done()
        assert _responds(call.hermes) == []
        await bridge._end_session("test")

    _run(scenario())


def test_a_second_answer_to_the_same_card_never_reaches_hermes(call):
    gate = threading.Event()
    respond = call.hermes.dispatch

    def slow(request, transport=None):
        if request["method"] == "approval.respond":
            gate.wait(5)
        return respond(request, transport)

    call.hermes.dispatch = slow

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval("one", "r1"))
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[0]
        answer = watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "once"})
        again = watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "once"})
        await bridge._handle(answer)
        await _until(lambda: bridge.session.chat.answering)
        # Hermes asks again meanwhile: not shown until the first answer is back.
        call.hermes.emit("approval.request", _approval("two", "r2"))
        await bridge._handle(again)
        await _spawned_done(held=1)
        # The repeat gets no answer of its own, and the next card waits.
        assert watch.events(socket, "conduit.approval.done") == []
        assert [c["command"] for c in watch.events(socket, "conduit.approval")] == ["one"]
        gate.set()
        await _until(lambda: watch.events(socket, "conduit.approval.done"))
        assert watch.events(socket, "conduit.approval.done") == [
            {"type": "conduit.approval.done", "id": card["id"], "resolved": True}]
        await _until(lambda: watch.events(socket, "conduit.approval")[-1]["command"] == "two")
        assert len(_responds(call.hermes)) == 1
        await bridge._end_session("test")

    _run(scenario())


def test_a_reply_that_comes_whole_after_the_approval_is_still_said(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        turn = await bridge.session.call.start_turn("Clean the build")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[0]
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "deny"}))
        await _until(lambda: watch.events(socket, "conduit.approval.done"))
        call.hermes.emit("message.complete", {"text": "I left the build folder alone."})
        assert await _frames(turn) == api.LOCAL_LIVE_WATCH_APPROVAL_LINE + " I left the build folder alone."
        await bridge._end_session("test")

    _run(scenario())


def test_the_turn_stays_open_while_the_card_waits(call, monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_WATCH_APPROVAL_KEEPALIVE_S", 0.05)

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        turn = await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        frames = []
        while len([kind for kind, _ in frames if kind == "keepalive"]) < 3:
            frames.append(await asyncio.wait_for(turn.frames.get(), 5))
        # Nothing said for them: they only keep the turn open.
        assert [text for kind, text in frames if kind == "keepalive"] == ["", "", ""]
        call.hermes.emit("error", {"message": "stopped"})
        await _until(lambda: watch.events(socket, "conduit.approval.gone"))
        await bridge._end_session("test")

    _run(scenario())


def test_a_call_the_server_ends_sends_no_card_after_its_end(call):
    gate = threading.Event()
    respond = call.hermes.dispatch

    def slow(request, transport=None):
        if request["method"] == "approval.respond":
            gate.wait(5)
        return respond(request, transport)

    call.hermes.dispatch = slow

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval("one", "r1"))
        call.hermes.emit("approval.request", _approval("two", "r2"))
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[-1]
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "deny"}))
        await _until(lambda: bridge.session.chat.answering)
        session = bridge.session
        await call.speech.sockets[0].close()  # the speech server ends the call
        await _until(lambda: watch.events(socket, "ended"))
        gate.set()
        await _until(lambda: not session.chat.answering)
        await _spawned_done()
        events = [event["type"] for event in watch.events(socket)]
        assert events[events.index("ended"):] == ["ended"]
        assert session.approval_timer is None

    _run(scenario())


def test_a_watch_that_rejoins_while_an_answer_is_out_gets_the_next_card_once_it_is_back(call):
    gate = threading.Event()
    respond = call.hermes.dispatch

    def slow(request, transport=None):
        if request["method"] == "approval.respond":
            gate.wait(5)
        return respond(request, transport)

    call.hermes.dispatch = slow

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval("one", "r1"))
        call.hermes.emit("approval.request", _approval("two", "r2"))
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[-1]
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "deny"}))
        await _until(lambda: bridge.session.chat.answering)
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        again = Watch()
        await _start(bridge, socket, again, approvals=True, resume_session_id="stored1")
        gate.set()
        await _until(lambda: any(c["command"] == "two" for c in again.events(socket, "conduit.approval")), timeout=2)
        await bridge._end_session("test")

    _run(scenario())


def test_a_hermes_that_asks_by_server_request_gets_the_same_card_and_withdraws_only_that_one(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        turn = await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())

        def ask(rpc_id, command):
            call.hermes.transport.write({"jsonrpc": "2.0", "id": rpc_id, "method": "approval",
                                         "params": {"session_id": "s1", "command": command, "description": "Run it"}})

        ask(7, "one")
        ask(8, "two")
        await _until(lambda: watch.events(socket, "conduit.approval"))
        assert watch.events(socket, "conduit.approval")[0]["command"] == "one"
        # Withdrawn by its id (answered in Conduit): the other stays.
        call.hermes.emit("request.cancel", {"id": 7})
        await _until(lambda: watch.events(socket, "conduit.approval")[-1]["command"] == "two")
        assert watch.events(socket, "conduit.approval")[-1]["choices"] == ["once", "deny"]
        # A cancel naming no request the call holds changes nothing.
        call.hermes.emit("request.cancel", {"id": 99})
        await _spawned_done()
        assert watch.events(socket, "conduit.approval.gone")[-1]["id"] != watch.events(socket, "conduit.approval")[-1]["id"]
        call.hermes.emit("approval.cancelled", {})
        await _until(lambda: watch.events(socket, "conduit.approval.gone")[-1]["id"]
                     == watch.events(socket, "conduit.approval")[-1]["id"])
        call.hermes.emit("message.complete", {"text": "ok"})
        # The call said where the card is, once, as for an event-asked request.
        assert (await _frames(turn)).startswith(api.LOCAL_LIVE_WATCH_APPROVAL_LINE)
        await bridge._end_session("test")

    _run(scenario())



def test_a_hermes_that_denies_at_once_gets_no_card(call, monkeypatch):
    monkeypatch.setattr(api, "_hermes_approval_timeout", lambda: 0.0)

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        turn = await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        # Nothing to answer on the Watch: the call says to look in the chat.
        assert await _frames(turn) == api.LOCAL_LIVE_WATCH_NEEDS_INPUT_LINE
        await _spawned_done()
        assert watch.events(socket, "conduit.approval") == []
        assert bridge.session.chat.on_approvals is None
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": "x", "choice": "once"}))
        await _spawned_done()
        assert _responds(call.hermes) == [] and watch.events(socket, "conduit.approval.done") == []
        await bridge._end_session("test")

    _run(scenario())


def test_a_card_that_couldnt_be_sent_is_sent_at_the_next_look(call, monkeypatch):
    monkeypatch.setattr(api, "LOCAL_LIVE_WATCH_APPROVAL_KEEPALIVE_S", 0.05)

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        session = bridge.session
        send = session.to_watch
        dropped = []

        async def flaky(payload):
            if payload.get("type") == "conduit.approval" and not dropped:
                dropped.append(payload)
                return False
            return await send(payload)

        session.to_watch = flaky
        await session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        assert dropped and watch.events(socket, "conduit.approval")[0]["id"] == dropped[0]["id"]
        await bridge._end_session("test")

    _run(scenario())


def test_a_card_update_that_didnt_go_still_leaves_the_card_to_take_away(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        session = bridge.session
        send = session.to_watch
        dropped = []

        async def flaky(payload):
            # The update to deny only (a second request came) doesn't go.
            if payload.get("type") == "conduit.approval" and payload.get("choices") == ["deny"]:
                dropped.append(payload)
                return False
            return await send(payload)

        session.to_watch = flaky
        await session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval("one", "r1"))
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[0]
        call.hermes.emit("approval.request", _approval("two", "r2"))
        await _until(lambda: dropped)
        # The turn ends: the card the Watch still holds is taken away.
        call.hermes.emit("message.complete", {"text": "ok"})
        await _until(lambda: watch.events(socket, "conduit.approval.gone"))
        assert watch.events(socket, "conduit.approval.gone") == [{"type": "conduit.approval.gone", "id": card["id"]}]
        await bridge._end_session("test")

    _run(scenario())


def test_an_answer_hermes_fails_on_is_not_taken_and_the_card_comes_back(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[0]

        def broken(choice, request_id):
            raise RuntimeError("the executor went away")

        bridge.session.chat.respond = broken
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "once"}))
        await _until(lambda: watch.events(socket, "conduit.approval.done"))
        assert watch.events(socket, "conduit.approval.done") == [
            {"type": "conduit.approval.done", "id": card["id"], "resolved": False}]
        await _until(lambda: len(watch.events(socket, "conduit.approval")) == 2)
        assert watch.events(socket, "conduit.approval")[1]["id"] == card["id"]
        assert not bridge.session.chat.answering
        await bridge._end_session("test")

    _run(scenario())


def test_no_timer_runs_while_hermes_takes_an_answer(call):
    gate = threading.Event()
    respond = call.hermes.dispatch

    def slow(request, transport=None):
        if request["method"] == "approval.respond":
            gate.wait(5)
        return respond(request, transport)

    call.hermes.dispatch = slow

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval("one", "r1"))
        await _until(lambda: watch.events(socket, "conduit.approval"))
        card = watch.events(socket, "conduit.approval")[0]
        await bridge._handle(watch.event({"type": "conduit.approval.answer", "id": card["id"], "choice": "once"}))
        await _until(lambda: bridge.session.chat.answering)
        # Something changes meanwhile: a look, but no timer until the answer is back.
        call.hermes.emit("approval.request", _approval("two", "r2"))
        await _spawned_done(held=1)
        assert bridge.session.approval_timer is None
        gate.set()
        await _until(lambda: watch.events(socket, "conduit.approval")[-1]["command"] == "two")
        assert bridge.session.approval_timer is not None
        await bridge._end_session("test")

    _run(scenario())


def test_a_cancel_that_names_its_requests_unreadably_drops_none(call):
    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", _approval())
        await _until(lambda: watch.events(socket, "conduit.approval"))
        call.hermes.emit("approval.cancelled", {"request_ids": "req1"})
        await _spawned_done()
        assert watch.events(socket, "conduit.approval.gone") == []
        assert len(bridge.session.chat.approvals) == 1
        await bridge._end_session("test")

    _run(scenario())


def test_a_resumed_chats_runner_knows_about_the_card_before_it_hears_hermes(call, monkeypatch):
    attached = []

    class Chat(api._WatchCallChat):
        @property
        def listener(self):
            return self.__dict__.get("_listener")

        @listener.setter
        def listener(self, value):
            self.__dict__["_listener"] = value
            if value is not None:
                attached.append((value.__self__.watch_approvals, getattr(self, "on_approvals", None) is not None))

    monkeypatch.setattr(api, "_WatchCallChat", Chat)

    async def scenario():
        bridge, socket, watch = await _approvals_call(call)
        await bridge.session.call.start_turn("Clean up")
        await _until(lambda: call.hermes.prompts())
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        await _start(bridge, socket, Watch(), approvals=True, resume_session_id="stored1")
        await bridge._end_session("test")

    _run(scenario())
    # Both the first stream and the rejoin attached the listener last.
    assert attached == [(True, True), (True, True)]
