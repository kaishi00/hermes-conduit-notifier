"""Hermes Voice on the Watch: the speech_server engine of the Watch audio bridge.

The Watch's microphone goes to the profile's speech server, each transcript
is a turn of the call's own Hermes chat, and the reply is spoken by the
server sentence by sentence. Here with a stand-in speech server (a read-back
OpenAI Realtime subset, as the fleet's voice adapter is), a stand-in Hermes
session API and a stand-in relay socket.
"""

import asyncio
import base64
import importlib.util
import json
import os
import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch_speech", ROOT / "dashboard" / "plugin_api.py")
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


# --- Sentences ------------------------------------------------------------------


def _split(*deltas):
    splitter = api._SpeechSplitter()
    out = []
    for delta in deltas:
        out += splitter.feed(delta)
    return out, splitter.flush()


def test_the_first_sentence_goes_at_once_and_short_ones_join_the_next():
    out, rest = _split("Sure. ", "It is sunny. It is ", "twenty degrees in Montréal today, with light wind. Bye")
    assert out == ["Sure.", "It is sunny. It is twenty degrees in Montréal today, with light wind."]
    assert rest == ["Bye."]


def test_markdown_is_spoken_as_plain_sentences():
    out, rest = _split("## Summary\n", "- **Bold** item with a [link](https://example.com/x)\n",
                       "1. Step one is `ls` at https://example.com.\n", "> quoted <b>text</b>\n")
    assert " ".join(out + rest) == ("Summary. Bold item with a link. Step one is ls at. quoted text.")


def test_code_and_tables_stay_in_the_chat_said_once():
    out, rest = _split("Here you go:\n```python\nprint('a. b')\n```\n", "| a | b |\n|---|---|\n| 1 | 2 |\n",
                       "```\nmore\n```\nThat's all.\n")
    spoken = " ".join(out + rest)
    assert spoken.count(api.SPEECH_WATCH_CODE_NOTE) == 1
    assert "print" not in spoken and "|" not in spoken and "more" not in spoken
    assert spoken.endswith("That's all.")


def test_numbers_and_decimals_dont_end_sentences():
    out, rest = _split("In 2024. the rate was 3.5 percent and it rose.")
    assert out + rest == ["In 2024. the rate was 3.5 percent and it rose."]


def test_a_long_run_without_an_end_is_cut_on_a_space():
    out, _ = _split("word " * 120)
    assert out and all(len(sentence) <= api.SPEECH_WATCH_MAX_SENTENCE_CHARS for sentence in out)


def test_text_that_waits_with_no_end_is_spoken_after_the_flush_delay():
    clock = {"now": 0.0}
    splitter = api._SpeechSplitter(clock=lambda: clock["now"])
    assert splitter.feed("Okay. ") == ["Okay."]
    assert splitter.feed("Let me look that up for you") == []
    assert splitter.tick() == []
    clock["now"] = api.SPEECH_WATCH_FLUSH_S + 0.1
    assert splitter.tick() == ["Let me look that up for"]
    assert splitter.flush() == ["you."]


def test_readback_judges_only_sentences_that_can_tell():
    assert api._speech_watch_readback("It's sunny in Montreal today", "It is sunny in Montréal today.") is True
    assert api._speech_watch_readback("I am a different assistant with my own ideas", "It is sunny in Montréal today.") is False
    # Short ones, and ones with numbers a server may say in words, can't tell.
    assert api._speech_watch_readback("Okay", "OK.") is None
    assert api._speech_watch_readback("It is twenty degrees outside right now", "It is 20 degrees outside right now.") is None
    assert api._speech_watch_readback("", "It is sunny in Montréal today.") is None


def test_a_table_cut_mid_row_and_an_inline_fence_stay_unspoken():
    splitter = api._SpeechSplitter()
    out = splitter.feed("| a. | b |")
    out += splitter.feed(" c |\n") + splitter.feed("Use this: ```py\nprint(1)\n```\nDone now.\n")
    out += splitter.flush()
    spoken = " ".join(out)
    assert "b" not in spoken.split() and "print" not in spoken
    assert spoken == f"Use this: {api.SPEECH_WATCH_CODE_NOTE} Done now."


def test_the_timed_flush_waits_for_the_stream_to_pause():
    clock = {"now": 0.0}
    splitter = api._SpeechSplitter(clock=lambda: clock["now"])
    splitter.feed("Okay. ")
    for word in ("Let", "me", "check", "the", "forecast"):
        clock["now"] += 1.0  # each word under the delay after the last
        assert splitter.feed(f" {word}") == [] and splitter.tick() == []
    clock["now"] += api.SPEECH_WATCH_FLUSH_S + 0.1
    assert splitter.tick() == ["Let me check the"]


# --- Readiness ------------------------------------------------------------------


@pytest.mark.parametrize("configured,sessions,ready", [(False, True, False), (True, False, False), (True, True, True)])
def test_the_engine_is_ready_with_a_speech_server_and_hermes_chats(monkeypatch, configured, sessions, ready):
    status = {"available": configured, "reason": None if configured else "No speech server"}
    monkeypatch.setattr(api, "speech_server_status", lambda: status)
    monkeypatch.setattr(api, "_hermes_session_api", lambda: object() if sessions else None)
    monkeypatch.setattr(api, "_watch_voice_can_load_sessions", lambda: False)
    monkeypatch.setattr(api, "_watch_voice_session_api", lambda: pytest.fail("a status check never imports"))
    state = api.speech_watch_status()
    assert (state["runtime"] == "ready") is ready
    if not ready:
        assert state["reason"]
    runtime = types.SimpleNamespace(python=lambda: None, status=lambda: {"runtime": "missing"})
    engines = api._watch_audio_grant_engines(runtime)
    assert engines == (["grok", "speech_server"] if ready else ["grok"])
    assert api.watch_audio_status(runtime)["engines"]["speech_server"] == state


def test_a_dashboard_without_a_chat_socket_yet_loads_hermes_session_api(monkeypatch):
    server = types.ModuleType("tui_gateway.server")
    server.dispatch = lambda request, transport=None: None
    server._sessions = {}
    imported = []

    def import_module(name):
        imported.append(name)
        monkeypatch.setitem(sys.modules, name, server)
        return server

    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    monkeypatch.setattr(api.importlib, "import_module", import_module)
    real_find_spec = api.importlib.util.find_spec
    monkeypatch.setattr(api.importlib.util, "find_spec",
                        lambda name: object() if name == "tui_gateway.server" else real_find_spec(name))
    # Not the dashboard: nothing is imported.
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server", raising=False)
    assert api._watch_voice_session_api() is None and imported == []
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", types.ModuleType("hermes_cli.web_server"))
    assert api._watch_voice_session_api() is server and imported == ["tui_gateway.server"]


# --- The call -------------------------------------------------------------------


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
        self.read = 0

    def hello(self):
        return bytes([4]) + self.sid + bytes([1])

    def seal(self, kind, plain):
        message = api.seal_watch_audio(self.up, UP, GRANT_ID, self.sid, kind, self.counter, plain)
        self.counter += 1
        return message

    def control(self, payload):
        return self.seal(3, json.dumps(payload).encode())

    def received(self, socket):
        """Everything sealed to this stream so far, opened: (kind, value)."""
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
    """A read-back speech server: each response.create speaks its text."""

    def __init__(self, say=None, close_after=None):
        self.connects = []
        self.sockets = []
        self.say = say or (lambda text: text)
        self.close_after = close_after

    async def connect(self, url, headers):
        socket = SpeechSocket(self)
        self.connects.append((url, headers))
        self.sockets.append(socket)
        return socket


class SpeechSocket:
    def __init__(self, server):
        self.server = server
        self.incoming = asyncio.Queue()
        self.received = []
        self.close_code = None
        self.closed = False
        self.responses = 0

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
        event = json.loads(message)
        self.received.append(event)
        if event["type"] == "response.create":
            text = event["response"]["input"][0]["content"][0]["text"]
            self.responses += 1
            if self.server.close_after is not None and self.responses > self.server.close_after:
                self.server.close_after = None
                await self.close()
                return
            pcm = base64.b64encode(b"\x10\x27" * 2400).decode()  # 0.1 s at 24 kHz, loud
            self.push({"type": "response.created", "response": {"id": "r"}})
            self.push({"type": "response.output_audio.delta", "delta": pcm})
            self.push({"type": "response.output_audio_transcript.done", "transcript": self.server.say(text)})
            self.push({"type": "response.done", "response": {"status": "completed"}})

    async def close(self):
        if not self.closed:
            self.closed = True
            self.close_code = 4408
            self.incoming.put_nowait(None)

    def said(self):
        return [event["response"]["input"][0]["content"][0]["text"] for event in self.received
                if event["type"] == "response.create"]


class Hermes:
    """Hermes' session API as the plugin calls it."""

    def __init__(self, refuse_voice_turn=False):
        self.calls = []
        self.transport = None
        self.sessions = 0
        self.refuse_voice_turn = refuse_voice_turn
        self.steers = False

    def dispatch(self, request, transport=None):
        self.transport = transport
        method, params = request["method"], request["params"]
        self.calls.append((method, dict(params)))
        if method == "session.create":
            self.sessions += 1
            return {"id": request["id"], "result": {"session_id": f"s{self.sessions}",
                                                    "stored_session_id": f"stored{self.sessions}"}}
        if method == "prompt.submit" and self.refuse_voice_turn and "voice_turn" in params:
            return {"id": request["id"], "error": {"code": -32602, "message": "unknown param voice_turn"}}
        if method == "session.steer":
            return {"id": request["id"], "result": {"status": "queued" if self.steers else "rejected"}}
        if method == "approval.respond":
            return {"id": request["id"], "result": {"resolved": True}}
        return {"id": request["id"], "result": {}}

    def emit(self, kind, payload, sid="s1"):
        self.transport.write({"jsonrpc": "2.0", "method": "event",
                              "params": {"type": kind, "session_id": sid, "payload": payload}})

    def prompts(self):
        return [params for method, params in self.calls if method == "prompt.submit"]


@pytest.fixture()
def call(monkeypatch):
    speech = SpeechServer()
    hermes = Hermes()
    monkeypatch.setattr(api, "_connect_speech_server", speech.connect)
    monkeypatch.setattr(api, "speech_server_target", lambda settings=None: ("ws://10.0.0.5:8088/v1/realtime", "tok"))
    monkeypatch.setattr(api, "_watch_voice_session_api", lambda: hermes)
    monkeypatch.setattr(api, "_speech_server_limiter", api._MintLimiter(100, 60.0))
    monkeypatch.setattr(api, "_speech_server_open", {})
    return types.SimpleNamespace(speech=speech, hermes=hermes)


def _bridge():
    grant = types.SimpleNamespace(grant_id=GRANT_ID, profile=None, relay_url="https://relay.example", credential="c",
                                  expires_at=time.monotonic() + 600,
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
    await bridge._handle(watch.control({"type": "start", "engine": "speech_server", **extra}))
    await _until(lambda: watch.events(socket, "started") or watch.events(socket, "error"))
    return (watch.events(socket, "started") or watch.events(socket, "error"))[-1]


def test_a_turn_goes_from_the_watch_to_hermes_and_back_as_speech(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        started = await _start(bridge, socket, watch)
        assert started == {"type": "started", "engine": "speech_server", "voice": None, "input_rate": 16000,
                           "output_rate": 24000, "session_id": "stored1", "resumed": False,
                           "briefing_applied": False, "greeting_applied": False}
        assert call.speech.connects == [("ws://10.0.0.5:8088/v1/realtime", {"Authorization": "Bearer tok"})]
        [created] = [params for method, params in call.hermes.calls if method == "session.create"]
        assert created["title"] == "Watch call" and created["source"] == "desktop"
        upstream = call.speech.sockets[0]
        update = upstream.received[0]
        assert update["type"] == "session.update"
        assert update["session"]["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16000}
        assert update["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
        assert update["session"]["audio"]["output"]["format"]["rate"] == 24000

        # The Watch's microphone reaches the server.
        await bridge._handle(watch.seal(1, b"\x01\x00\x02\x00"))
        await _until(lambda: any(e["type"] == "input_audio_buffer.append" for e in upstream.received))
        append = next(e for e in upstream.received if e["type"] == "input_audio_buffer.append")
        assert base64.b64decode(append["audio"]) == b"\x01\x00\x02\x00"

        # The server hears a question: Hermes gets it as a spoken turn.
        upstream.push({"type": "conversation.item.input_audio_transcription.delta", "delta": "Weather"})
        upstream.push({"type": "conversation.item.input_audio_transcription.completed",
                       "transcript": " What's the weather? "})
        await _until(lambda: call.hermes.prompts())
        [prompt] = call.hermes.prompts()
        assert prompt["text"] == api.SPEECH_WATCH_FIRST_TURN_NOTE + "What's the weather?"
        assert prompt["surface"] == "voice-live" and prompt["voice_turn"] is True
        assert watch.events(socket, "session.input_transcript.delta") == [
            {"type": "session.input_transcript.delta", "delta": "Weather"}]
        assert watch.events(socket, "turn.done")[0] == {"type": "turn.done",
                                                        "turn": {"role": "user", "transcript": "What's the weather?"}}
        assert {"type": "conduit.state", "state": "thinking"} in watch.events(socket)

        # Hermes answers: the server speaks it a sentence at a time.
        call.hermes.emit("message.delta", {"text": "Sunny. "})
        call.hermes.emit("message.delta", {"text": "It is twenty degrees in Montréal, with a light wind.\n"})
        call.hermes.emit("message.complete", {"text": "Sunny. It is twenty degrees in Montréal, with a light wind.",
                                              "status": "complete"})
        await _until(lambda: {"type": "conduit.state", "state": "listening"} in watch.events(socket)[-1:])
        assert upstream.said() == ["Sunny.", "It is twenty degrees in Montréal, with a light wind."]
        received = watch.received(socket)
        kinds = [(kind, value.get("type") if isinstance(value, dict) else "pcm") for kind, value in received]
        on = kinds.index((2, "conduit.speech"))
        off = len(kinds) - 1 - kinds[::-1].index((2, "conduit.speech"))
        assert received[on][1] == {"type": "conduit.speech", "on": True}
        assert all(kind != 1 for kind, _ in kinds[:on]) and all(kind != 1 for kind, _ in kinds[off:])
        assert sum(1 for kind, _ in kinds if kind == 1) == 2
        assert watch.events(socket, "conduit.speech")[-1] == {"type": "conduit.speech", "on": False}
        assert watch.events(socket, "turn.done")[-1]["turn"] == {
            "role": "assistant", "transcript": "Sunny. It is twenty degrees in Montréal, with a light wind."}
        # The second turn carries no note.
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Thanks"})
        await _until(lambda: len(call.hermes.prompts()) == 2)
        assert call.hermes.prompts()[1]["text"] == "Thanks"

        await bridge._handle(watch.control({"type": "end"}))
        await _until(lambda: watch.events(socket, "ended"))
        assert watch.events(socket, "ended") == [{"type": "ended", "engine": "speech_server", "reason": "ended"}]
        assert upstream.closed and api._speech_server_open == {} and bridge.slots.used == 0
        # The chat outlives the stream, for a rejoin; the grant's end closes it.
        assert list(bridge.voice_chats) == ["stored1"]

    _run(scenario())


def test_a_rejoining_watch_carries_on_in_the_same_chat_and_a_new_call_gets_a_new_one(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        first = Watch()
        await _start(bridge, socket, first)
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        again = Watch()
        started = await _start(bridge, socket, again, resume_session_id="stored1")
        assert started["session_id"] == "stored1" and started["resumed"] is True
        assert call.hermes.sessions == 1
        await bridge._end_session("test")
        fresh = Watch()
        started = await _start(bridge, socket, fresh)
        assert started["session_id"] == "stored2" and started["resumed"] is False
        await bridge._end_session("test")

    _run(scenario())
    # The grant's end closes the call's chats.
    bridge = _bridge()
    bridge.voice_chats = {"stored1": api._WatchVoiceChat(call.hermes, None)}
    bridge.voice_chats["stored1"].session_id = "s9"
    bridge.stop_requested.set()
    bridge._run()
    assert ("session.close", {"session_id": "s9"}) in call.hermes.calls and bridge.voice_chats == {}


def test_a_server_session_that_ends_under_the_call_is_dialed_again_and_the_sentence_said_again(call):
    call.speech.close_after = 0  # the first response.create meets a closed session

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        call.speech.sockets[0].push({"type": "conversation.item.input_audio_transcription.completed",
                                     "transcript": "Hello"})
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Hello there.\n"})
        call.hermes.emit("message.complete", {"text": "Hello there.", "status": "complete"})
        await _until(lambda: len(call.speech.sockets) == 2 and call.speech.sockets[1].said())
        second = call.speech.sockets[1]
        assert second.received[0]["type"] == "session.update"
        assert second.said() == ["Hello there."]
        await bridge._end_session("test")

    _run(scenario())


def test_a_server_with_words_of_its_own_ends_the_call(call):
    call.speech.say = lambda text: "I am a different assistant and I think you should ask me something else"

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        call.speech.sockets[0].push({"type": "conversation.item.input_audio_transcription.completed",
                                     "transcript": "Hi"})
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Hello there, how can I help you today?\n"})
        call.hermes.emit("message.delta", {"text": "I can look up the weather or start a job for you.\n"})
        await _until(lambda: watch.events(socket, "ended"))
        [error] = [event for event in watch.events(socket, "error") if "code" in event and "error" not in event]
        assert error["code"] == "unavailable" and "speaks the text" in error["message"]

    _run(scenario())


def test_a_long_reply_stops_at_the_cap_and_points_to_the_chat(call, monkeypatch):
    monkeypatch.setattr(api, "SPEECH_WATCH_MAX_SPOKEN_S", 0.15)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Tell me"})
        await _until(lambda: call.hermes.prompts())
        for number in range(6):
            call.hermes.emit("message.delta", {"text": f"This is sentence number {number} of a long answer.\n"})
        call.hermes.emit("message.complete", {"text": "…", "status": "complete"})
        await _until(lambda: upstream.said() and upstream.said()[-1] == api.SPEECH_WATCH_REST_IN_CHAT)
        await asyncio.sleep(0.1)
        assert len(upstream.said()) == 3 and upstream.said()[-1] == api.SPEECH_WATCH_REST_IN_CHAT
        await bridge._end_session("test")

    _run(scenario())


def test_a_hermes_error_is_said_briefly_and_a_refused_voice_marker_is_dropped(call):
    call.hermes.refuse_voice_turn = True

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Do it"})
        await _until(lambda: len(call.hermes.prompts()) == 2)
        assert "voice_turn" in call.hermes.prompts()[0] and "voice_turn" not in call.hermes.prompts()[1]
        call.hermes.emit("message.complete", {"text": "Error: provider down", "status": "error"})
        await _until(lambda: upstream.said())
        assert upstream.said() == [api.SPEECH_WATCH_ERROR_REPLY]
        await bridge._end_session("test")

    _run(scenario())


def test_without_hermes_chats_or_a_speech_server_the_start_says_why(call, monkeypatch):
    async def scenario(expect):
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        reply = await _start(bridge, socket, watch)
        assert reply["type"] == "error" and reply["code"] == expect
        await _until(lambda: watch.events(socket, "ended"))
        assert api._speech_server_open == {}

    monkeypatch.setattr(api, "_watch_voice_session_api", lambda: None)
    _run(scenario("unavailable"))

    def unset(settings=None):
        raise api.TokenError(503, api.SPEECH_SERVER_NOT_CONFIGURED)

    monkeypatch.setattr(api, "speech_server_target", unset)
    _run(scenario("unavailable"))

    async def refused(url, headers):
        raise api.RealtimeUpstreamRefused(403)

    monkeypatch.setattr(api, "speech_server_target", lambda settings=None: ("ws://10.0.0.5:8088/v1/realtime", "tok"))
    monkeypatch.setattr(api, "_watch_voice_session_api", lambda: call.hermes)
    monkeypatch.setattr(api, "_connect_speech_server", refused)
    _run(scenario("refused"))


def _transcript(socket, text="Hello"):
    socket.push({"type": "conversation.item.input_audio_transcription.completed", "transcript": text})


def test_words_over_a_reply_stop_it_and_the_cut_off_turn_isnt_spoken(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Tell me a long story")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        call.hermes.emit("message.delta", {"text": "Once upon a time there was a fox.\n"})
        await _until(lambda: upstream.said())
        # The user speaks again while Hermes still streams the story.
        _transcript(upstream, "Actually, what time is it?")
        await _until(lambda: len(call.hermes.prompts()) == 2)
        # Hermes ends the cut-off turn, then runs the new one.
        call.hermes.emit("message.delta", {"text": "The fox ran into the woods.\n"})
        call.hermes.emit("message.complete", {"text": "Once upon a time…", "status": "interrupted"})
        call.hermes.emit("message.start", {})
        call.hermes.emit("message.delta", {"text": "It is three o'clock.\n"})
        call.hermes.emit("message.complete", {"text": "It is three o'clock.", "status": "complete"})
        await _until(lambda: upstream.said()[-1:] == ["It is three o'clock."])
        assert "The fox ran into the woods." not in upstream.said()
        assistant = [event["turn"]["transcript"] for event in watch.events(socket, "turn.done")
                     if event["turn"]["role"] == "assistant"]
        assert assistant == ["It is three o'clock."]
        await bridge._end_session("test")

    _run(scenario())


def test_a_refused_response_doesnt_leave_the_call_silent(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        refused = {"n": 0}
        original = upstream.send

        async def send(message):
            event = json.loads(message)
            if event["type"] == "response.create" and not refused["n"]:
                refused["n"] += 1
                upstream.received.append(event)
                upstream.push({"type": "error", "error": {"code": "response_has_no_text"}})
                # Unrelated to it: never ends a response.
                upstream.push({"type": "error", "error": {"code": "too_many_pending_segments"}})
                return
            await original(message)

        upstream.send = send
        _transcript(upstream)
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "First sentence here.\n"})
        call.hermes.emit("message.delta", {"text": "And a second sentence that follows it.\n"})
        call.hermes.emit("message.complete", {"text": "…", "status": "complete"})
        await _until(lambda: upstream.said()[-1:] == ["And a second sentence that follows it."])
        await _until(lambda: {"type": "conduit.state", "state": "listening"} in watch.events(socket)[-1:])
        await bridge._end_session("test")

    _run(scenario())


def test_a_question_hermes_asks_is_pointed_to_the_chat(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Delete the old logs")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "I'll remove them now. "})
        call.hermes.emit("approval.request", {"command": "rm -rf logs", "choices": ["once", "deny"]})
        call.hermes.emit("approval.request", {"command": "rm -rf more", "choices": ["once", "deny"]})
        await _until(lambda: api.SPEECH_WATCH_NEEDS_CHAT in upstream.said())
        assert upstream.said().count(api.SPEECH_WATCH_NEEDS_CHAT) == 1
        await bridge._end_session("test")

    _run(scenario())


def test_a_refused_redial_is_tried_again(call, monkeypatch):
    call.speech.close_after = 0
    attempts = {"n": 0}
    connect = call.speech.connect

    async def flaky(url, headers):
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise api.RealtimeUpstreamRefused(403)  # busy, as the fleet's adapter says it
        return await connect(url, headers)

    monkeypatch.setattr(api, "_connect_speech_server", flaky)
    monkeypatch.setattr(api.asyncio, "sleep", _fast_sleep(api.asyncio.sleep))

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        _transcript(call.speech.sockets[0])
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Hello there.\n"})
        await _until(lambda: len(call.speech.sockets) == 2 and call.speech.sockets[1].said())
        assert attempts["n"] == 3 and call.speech.sockets[1].said() == ["Hello there."]
        await bridge._end_session("test")

    _run(scenario())


def _fast_sleep(sleep):
    async def fast(seconds, *args, **kwargs):
        return await sleep(min(seconds, 0.01), *args, **kwargs)
    return fast


def test_a_call_cancelled_while_its_chat_opens_closes_the_chat(call, monkeypatch):
    opened = __import__("threading").Event()
    release = __import__("threading").Event()
    original = call.hermes.dispatch

    def slow(request, transport=None):
        if request["method"] == "session.create":
            opened.set()
            release.wait(5)
        return original(request, transport)

    call.hermes.dispatch = slow

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(watch.hello())
        await bridge._handle(watch.control({"type": "start", "engine": "speech_server"}))
        await _until(opened.is_set)
        bridge.session_task.cancel()
        await asyncio.gather(bridge.session_task, return_exceptions=True)
        release.set()
        await _until(lambda: any(method == "session.close" for method, _ in call.hermes.calls))
        assert bridge.voice_chats == {} and api._speech_server_open == {}

    _run(scenario())


def test_a_response_still_going_holds_the_sentence_for_after_it(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        original = upstream.send
        held = {"n": 0}

        async def send(message):
            event = json.loads(message)
            if event["type"] == "response.create" and not held["n"]:
                held["n"] += 1
                upstream.received.append(event)
                upstream.push({"type": "error", "error": {"code": "conversation_already_has_active_response"}})
                upstream.push({"type": "response.done", "response": {"status": "cancelled"}})
                return
            await original(message)

        upstream.send = send
        _transcript(upstream)
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "First sentence here.\n"})
        call.hermes.emit("message.complete", {"text": "First sentence here.", "status": "complete"})
        await _until(lambda: upstream.said().count("First sentence here.") == 2)
        await bridge._end_session("test")

    _run(scenario())


# --- Turn control (the Watch's taps, follow-ups, waits, approvals) ------------------


def _calls(hermes, method):
    return [params for name, params in hermes.calls if name == method]


def test_a_tap_stops_the_reply_and_leaves_the_rest_in_the_chat(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Tell me about Montréal")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Montréal is a city in Québec, on an island.\n"})
        await _until(lambda: upstream.said())
        await bridge._handle(watch.control({"type": "interrupt"}))
        await _until(lambda: {"type": "conduit.state", "state": "listening"} in watch.events(socket)[-3:])
        call.hermes.emit("message.delta", {"text": "It has a mountain in the middle of it, called Mount Royal.\n"})
        call.hermes.emit("message.complete", {"text": "…", "status": "complete"})
        await _until(lambda: watch.events(socket, "turn.done")[-1]["turn"]["role"] == "assistant")
        await asyncio.sleep(0.1)
        assert upstream.said() == ["Montréal is a city in Québec, on an island."]
        await bridge._end_session("test")

    _run(scenario())


def test_cancelling_the_turn_interrupts_hermes(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        _transcript(call.speech.sockets[0], "Write me a novel")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        await bridge._handle(watch.control({"type": "cancel_turn"}))
        await _until(lambda: _calls(call.hermes, "session.interrupt"))
        await bridge._end_session("test")

    _run(scenario())


def test_words_while_hermes_works_go_into_its_turn(call):
    call.hermes.steers = True

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Find me a restaurant")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        call.hermes.emit("tool.start", {"name": "web_search"})
        await asyncio.sleep(0.05)
        _transcript(upstream, "Actually, make it Italian")
        await _until(lambda: _calls(call.hermes, "session.steer"))
        assert _calls(call.hermes, "session.steer")[0]["text"] == "Actually, make it Italian"
        call.hermes.emit("tool.complete", {"name": "web_search"})
        call.hermes.emit("message.delta", {"text": "Try Bottega, an Italian place on Saint-Zotique.\n"})
        call.hermes.emit("message.complete", {"text": "…", "status": "complete"})
        await _until(lambda: upstream.said())
        assert len(call.hermes.prompts()) == 1, "no second turn"
        assert upstream.said() == ["Try Bottega, an Italian place on Saint-Zotique."]
        await bridge._end_session("test")

    _run(scenario())


def test_a_slow_turn_says_one_moment_once(call, monkeypatch):
    monkeypatch.setattr(api, "SPEECH_WATCH_ONE_MOMENT_S", 0.3)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Plan my week")
        await _until(lambda: upstream.said() == [api.SPEECH_WATCH_ONE_MOMENT], timeout=3)
        await asyncio.sleep(0.6)
        assert upstream.said() == [api.SPEECH_WATCH_ONE_MOMENT]
        call.hermes.emit("message.delta", {"text": "Here is your week.\n"})
        await _until(lambda: len(upstream.said()) == 2)
        await bridge._end_session("test")

    _run(scenario())


def test_a_watch_that_answers_approvals_gets_them_and_answers_once(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch, approvals=True)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Clean up the logs")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", {"command": "rm -rf ./logs", "description": "Delete logs",
                                              "choices": ["once", "session", "always", "deny"]})
        await _until(lambda: watch.events(socket, "approval"))
        [approval] = watch.events(socket, "approval")
        assert approval["command"] == "rm -rf ./logs" and approval["choices"] == ["once", "deny"]
        await _until(lambda: api.SPEECH_WATCH_APPROVAL_ASK in upstream.said())
        # Only once or deny, and only the request shown.
        await bridge._handle(watch.control({"type": "approval_answer", "id": approval["id"], "choice": "always"}))
        await bridge._handle(watch.control({"type": "approval_answer", "id": "other", "choice": "once"}))
        await _until(lambda: len(watch.events(socket, "approval_done")) == 2)
        assert not _calls(call.hermes, "approval.respond")
        await bridge._handle(watch.control({"type": "approval_answer", "id": approval["id"], "choice": "once"}))
        await _until(lambda: _calls(call.hermes, "approval.respond"))
        assert _calls(call.hermes, "approval.respond")[0]["choice"] == "once"
        await _until(lambda: watch.events(socket, "approval_done")[-1] == {"type": "approval_done",
                                                                           "id": approval["id"], "resolved": True})
        await bridge._end_session("test")

    _run(scenario())


def test_what_the_server_hears_while_the_reply_plays_is_dropped_as_echo(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Hello")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Hello there, nice to hear from you today.\n"})
        await _until(lambda: {"type": "conduit.speech", "on": True} in watch.events(socket))
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "echo1"})
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": "echo1",
                       "transcript": "Hello there"})
        await asyncio.sleep(0.2)
        assert len(call.hermes.prompts()) == 1
        await bridge._end_session("test")

    _run(scenario())


def test_words_while_hermes_runs_no_tool_start_the_next_turn(call):
    call.hermes.steers = True  # this Hermes would take a steer, and sit on it

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Tell me a joke")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        _transcript(upstream, "A short one")
        await _until(lambda: len(call.hermes.prompts()) == 2)
        assert not _calls(call.hermes, "session.steer")
        await bridge._end_session("test")

    _run(scenario())


def test_two_pending_approvals_show_one_at_a_time_oldest_first(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch, approvals=True)
        _transcript(call.speech.sockets[0], "Tidy up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("approval.request", {"command": "rm a", "description": "first"})
        call.hermes.emit("approval.request", {"command": "rm b", "description": "second"})
        await _until(lambda: watch.events(socket, "approval"))
        await asyncio.sleep(0.1)
        shown = watch.events(socket, "approval")
        assert {card["command"] for card in shown} == {"rm a"}, "only the oldest, the one Hermes answers"
        # With two pending, Hermes' oldest might not be the card: deny only.
        assert shown[-1]["choices"] == ["deny"] and shown[-1]["pending"] == 2
        await bridge._handle(watch.control({"type": "approval_answer", "id": shown[-1]["id"], "choice": "once"}))
        await _until(lambda: watch.events(socket, "approval_done"))
        assert not _calls(call.hermes, "approval.respond")
        await bridge._handle(watch.control({"type": "approval_answer", "id": shown[-1]["id"], "choice": "deny"}))
        await _until(lambda: watch.events(socket, "approval")[-1]["command"] == "rm b")
        assert watch.events(socket, "approval")[-1]["choices"] == ["once", "deny"]
        assert _calls(call.hermes, "approval.respond")[0]["choice"] == "deny"
        await bridge._end_session("test")

    _run(scenario())


def test_a_rejoin_shows_an_approval_still_pending_and_words_over_a_reply_clear_it(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        first = Watch()
        await _start(bridge, socket, first, approvals=True)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Tidy up")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.start", {})
        call.hermes.emit("approval.request", {"command": "rm a", "description": "first"})
        await _until(lambda: first.events(socket, "approval"))
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        again = Watch()
        await _start(bridge, socket, again, approvals=True, resume_session_id="stored1")
        await _until(lambda: again.events(socket, "approval"))
        assert again.events(socket, "approval")[0]["command"] == "rm a"
        # The user talks over it: Hermes cuts the turn off, and the card goes.
        upstream = call.speech.sockets[-1]
        _transcript(upstream, "Never mind, leave it")
        await _until(lambda: len(call.hermes.prompts()) == 2)
        call.hermes.emit("message.complete", {"text": "", "status": "interrupted"})
        await _until(lambda: again.events(socket, "approval_gone"))
        await bridge._end_session("test")

    _run(scenario())


def test_speech_in_a_quiet_moment_of_an_open_reply_is_the_user(call, monkeypatch):
    monkeypatch.setattr(api, "WATCH_AUDIO_LEAD_S", 0.05)
    monkeypatch.setattr(api, "SPEECH_WATCH_ECHO_TAIL_S", 0.05)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await _start(bridge, socket, watch)
        upstream = call.speech.sockets[0]
        _transcript(upstream, "Check the weather")
        await _until(lambda: call.hermes.prompts())
        call.hermes.emit("message.delta", {"text": "Let me check.\n"})
        await _until(lambda: upstream.said())
        await asyncio.sleep(0.4)  # the sentence has played; Hermes runs a tool
        upstream.push({"type": "input_audio_buffer.speech_started", "item_id": "user1"})
        upstream.push({"type": "conversation.item.input_audio_transcription.completed", "item_id": "user1",
                       "transcript": "Also tomorrow"})
        await _until(lambda: len(call.hermes.prompts()) == 2)
        await bridge._end_session("test")

    _run(scenario())


def test_an_approval_hermes_asks_for_between_streams_is_shown_on_the_rejoin(call):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        first = Watch()
        await _start(bridge, socket, first, approvals=True)
        _transcript(call.speech.sockets[0], "Tidy up")
        await _until(lambda: call.hermes.prompts())
        await bridge._handle(bytes([0, 2]))  # the Watch's socket went
        call.hermes.emit("approval.request", {"command": "rm a", "description": "first"})
        again = Watch()
        await _start(bridge, socket, again, approvals=True, resume_session_id="stored1")
        await _until(lambda: again.events(socket, "approval"))
        assert again.events(socket, "approval")[0]["command"] == "rm a"
        await bridge._end_session("test")

    _run(scenario())
