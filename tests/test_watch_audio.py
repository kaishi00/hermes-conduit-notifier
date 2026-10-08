"""Watch audio: GPT-Live and Grok on a Conduit Watch call through the relay.

The sealing (shared vectors for Conduit), the pacing of Grok's audio, the
helper's Python, the grant's audio flag and the bridge's handling of a
Watch stream, each without a relay. tests/test_watch_audio_relay_e2e.py runs
the same code against a real relay.
"""

import asyncio
import importlib.util
import json
import os
import pathlib
import time
import types

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch_audio", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()

GRANT_ID = "G" * 22
SECRET = bytes(range(32))
SID = bytes(range(100, 116))
UP = "watch-to-host"
DOWN = "host-to-watch"


# --- Sealing ------------------------------------------------------------------


def test_stream_keys_follow_the_documented_derivation():
    keys = api.watch_audio_keys(SECRET, SID)
    for direction in (UP, DOWN):
        expected = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"conduit-watch-audio-v1",
                        info=f"conduit-watch-audio-v1 {direction} stream=ZGVmZ2hpamtsbW5vcHFycw".encode()).derive(SECRET)
        assert keys[direction] == expected
    assert keys[UP] != keys[DOWN]
    assert api.watch_audio_keys(SECRET, bytes(16))[UP] != keys[UP]


def test_shared_vectors_for_conduit():
    # Conduit's tests carry the same values.
    keys = api.watch_audio_keys(SECRET, SID)
    assert keys[UP].hex() == "a970c88b4c9fad954c1c7c46e50ba2a6cbb65e7cc660cb9c9903aaba94ec21d5"
    assert keys[DOWN].hex() == "bfba0a519d8cc7330fb2c5eaafbada1094ba8906e9c613cdae24ec67a7f5791a"
    assert api.watch_audio_aad(UP, GRANT_ID, SID, 3) == (
        b"conduit-watch-audio/1\nwatch-to-host\ngrant=GGGGGGGGGGGGGGGGGGGGGG\nsid=ZGVmZ2hpamtsbW5vcHFycw\ntype=3")
    start = api.seal_watch_audio(keys[UP], UP, GRANT_ID, SID, 3, 0, b'{"type":"start","engine":"gpt_live"}')
    assert start.hex() == ("03000000000000000065911b5180de3d861bf69d75d796d56ed8363e51361e991a4f351df10440d35e"
                           "3239930328e694505b7254359bab106607b55cf2")
    audio = api.seal_watch_audio(keys[DOWN], DOWN, GRANT_ID, SID, 1, 1, b"\x01\x00\x02\x00")
    assert audio.hex() == "01000000000000000163f89b82063f879f7eccbfb0b3f896f105565f24"


def test_a_sealed_message_is_chacha20_poly1305_with_the_counter_nonce():
    key = api.watch_audio_keys(SECRET, SID)[UP]
    sealed = api.seal_watch_audio(key, UP, GRANT_ID, SID, 2, 7, b"hello")
    assert sealed[0] == 2 and int.from_bytes(sealed[1:9], "big") == 7
    plain = ChaCha20Poly1305(key).decrypt(b"\0" * 4 + (7).to_bytes(8, "big"), sealed[9:],
                                          api.watch_audio_aad(UP, GRANT_ID, SID, 2))
    assert plain == b"hello"
    assert api.open_watch_audio(key, UP, GRANT_ID, SID, sealed) == (2, 7, b"hello")


@pytest.mark.parametrize("change", ["type", "counter", "body", "grant", "sid", "direction", "short"])
def test_a_changed_message_does_not_open(change):
    key = api.watch_audio_keys(SECRET, SID)[UP]
    sealed = bytearray(api.seal_watch_audio(key, UP, GRANT_ID, SID, 2, 3, b'{"type":"x"}'))
    grant, sid, direction = GRANT_ID, SID, UP
    if change == "type":
        sealed[0] = 1  # replayed as audio
    elif change == "counter":
        sealed[8] ^= 1
    elif change == "body":
        sealed[-1] ^= 1
    elif change == "grant":
        grant = "H" * 22
    elif change == "sid":
        sid = bytes(16)
    elif change == "direction":
        direction = DOWN
    else:
        sealed = sealed[:20]
    with pytest.raises(api.WatchAudioError):
        api.open_watch_audio(key, direction, grant, sid, bytes(sealed))


def test_a_stream_rejects_replays_and_counts_what_it_sends():
    stream = api._WatchAudioStream(GRANT_ID, SECRET, SID)
    up = api.watch_audio_keys(SECRET, SID)[UP]
    first = api.seal_watch_audio(up, UP, GRANT_ID, SID, 1, 0, b"a")
    second = api.seal_watch_audio(up, UP, GRANT_ID, SID, 1, 5, b"b")
    assert stream.open(first) == (1, b"a")
    assert stream.open(second) == (1, b"b")
    for again in (first, second, api.seal_watch_audio(up, UP, GRANT_ID, SID, 1, 4, b"c")):
        with pytest.raises(api.WatchAudioError):
            stream.open(again)
    down = api.watch_audio_keys(SECRET, SID)[DOWN]
    sent = [stream.seal(3, b"x"), stream.seal(1, b"y")]
    assert [api.open_watch_audio(down, DOWN, GRANT_ID, SID, m)[:2] for m in sent] == [(3, 0), (1, 1)]


def test_a_message_too_large_for_the_relay_is_refused():
    key = api.watch_audio_keys(SECRET, SID)[DOWN]
    sealed = api.seal_watch_audio(key, DOWN, GRANT_ID, SID, 2, 0, b"x" * api.WATCH_AUDIO_MAX_PLAIN_BYTES)
    assert len(sealed) == api.WATCH_AUDIO_MAX_MESSAGE_BYTES
    with pytest.raises(api.WatchAudioError):
        api.seal_watch_audio(key, DOWN, GRANT_ID, SID, 2, 0, b"x" * (api.WATCH_AUDIO_MAX_PLAIN_BYTES + 1))
    with pytest.raises(api.WatchAudioError):
        api.seal_watch_audio(key, DOWN, GRANT_ID, SID, 4, 0, b"")


def test_an_event_too_large_to_seal_goes_as_its_type():
    big = json.dumps({"type": "session.updated", "session": {"instructions": "x" * 70_000}})
    assert json.loads(api._watch_audio_event_text(big)) == {"type": "session.updated", "conduit_truncated": True}
    assert api._watch_audio_event_text('{"type":"a"}') == b'{"type":"a"}'


# --- Pacing -------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_grok_audio_is_paced_to_real_time_and_events_keep_their_place():
    async def scenario():
        sent = []

        async def send(kind, data):
            sent.append((time.monotonic(), kind, data))
            return True

        pacer = api._WatchAudioPacer(send, 24_000, lead_s=0.0)
        task = asyncio.ensure_future(pacer.run())
        start = time.monotonic()
        pacer.audio(b"\1\0" * 12_000)  # 0.5 s
        pacer.event('{"type":"response.done"}')
        while len(sent) < 6:
            await asyncio.sleep(0.01)
        task.cancel()
        return start, sent

    start, sent = _run(scenario())
    kinds = [kind for _, kind, _ in sent]
    assert kinds == [1, 1, 1, 1, 1, 2]
    assert all(len(data) == 4800 for _, kind, data in sent if kind == 1)
    # Five 100 ms pieces: the last leaves about 400 ms in, and the event right after it.
    assert sent[4][0] - start >= 0.35
    assert sent[5][2] == b'{"type":"response.done"}'


def test_an_interruption_drops_audio_not_yet_sent_but_keeps_events():
    async def scenario():
        sent = []

        async def send(kind, data):
            sent.append((kind, data))
            return True

        pacer = api._WatchAudioPacer(send, 24_000, lead_s=0.0)
        pacer.audio(b"\1\0" * 48_000)  # 2 s
        pacer.event('{"type":"response.output_audio_transcript.delta"}')
        task = asyncio.ensure_future(pacer.run())
        await asyncio.sleep(0.15)
        pacer.interrupt()
        pacer.event('{"type":"input_audio_buffer.speech_started"}')
        await asyncio.sleep(0.1)
        task.cancel()
        return sent

    sent = _run(scenario())
    audio = [data for kind, data in sent if kind == 1]
    assert 1 <= len(audio) <= 3
    assert [data for kind, data in sent if kind == 2] == [b'{"type":"response.output_audio_transcript.delta"}',
                                                           b'{"type":"input_audio_buffer.speech_started"}']


def test_the_lead_lets_the_watch_buffer_ahead():
    async def scenario():
        sent = []

        async def send(kind, data):
            sent.append(kind)
            return True

        pacer = api._WatchAudioPacer(send, 24_000, lead_s=1.0)
        task = asyncio.ensure_future(pacer.run())
        pacer.audio(b"\1\0" * 72_000)  # 3 s
        await asyncio.sleep(0.1)
        task.cancel()
        return sent

    # About a second (ten pieces) goes at once, then real time.
    assert 10 <= len(_run(scenario())) <= 12


def test_events_held_behind_audio_are_bounded(monkeypatch):
    monkeypatch.setattr(api, "WATCH_AUDIO_MAX_QUEUED_EVENT_BYTES", 100)
    pacer = api._WatchAudioPacer(lambda kind, data: True, 24_000)
    pacer.audio(b"\1\0" * 2_400)
    for index in range(10):
        pacer.event(json.dumps({"type": "delta", "n": index}))
    events = [json.loads(data)["n"] for kind, data in pacer.items if kind == 2]
    # The newest fit; the audio ahead of them stays.
    assert events == list(range(10 - len(events), 10)) and 0 < len(events) < 10
    assert pacer.queued_event_bytes == sum(len(data) for kind, data in pacer.items if kind == 2) <= 100
    assert [kind for kind, _ in pacer.items][:1] == [1]


def test_sent_events_leave_the_bound():
    async def scenario():
        async def send(kind, data):
            return True

        pacer = api._WatchAudioPacer(send, 24_000, lead_s=0.0)
        task = asyncio.ensure_future(pacer.run())
        pacer.event('{"type":"response.done"}')
        await asyncio.sleep(0.05)
        task.cancel()
        return pacer.queued_event_bytes

    assert _run(scenario()) == 0


# --- The helper's Python --------------------------------------------------------


class FakeRun:
    def __init__(self, fail=()):
        self.commands = []
        self.fail = fail

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        name = " ".join(str(part) for part in command)
        if any(text in name for text in self.fail):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Error: ensurepip is not available\n")
        if "-m venv" in name or (command[1:2] == ["venv"]):
            env_dir = pathlib.Path(command[-1])
            (env_dir / "bin").mkdir(parents=True, exist_ok=True)
            (env_dir / "bin" / "python").write_text("")
            (env_dir / "pyvenv.cfg").write_text("home = /usr\n")
        return types.SimpleNamespace(returncode=0, stdout="1.15.0\n" if "import aiortc" in name else "", stderr="")


def _runtime(tmp_path, run=None, which=lambda name: None, has=lambda name: False):
    return api._WatchAudioRuntime(env_dir=lambda: str(tmp_path / "env"), run=run or FakeRun(), which=which,
                                  has_module=has, base_python="/usr/bin/python3")


def test_no_python_with_aiortc_reads_as_missing(tmp_path):
    runtime = _runtime(tmp_path)
    assert runtime.python() is None
    assert runtime.status() == {"runtime": "missing", "source": None, "reason": api.WATCH_AUDIO_NEEDS_RUNTIME}


def test_hermes_own_python_is_used_when_it_has_aiortc(tmp_path):
    runtime = _runtime(tmp_path, has=lambda name: True)
    python, env, flags, source = runtime.python()
    assert (python, flags, source) == ("/usr/bin/python3", (), "hermes")
    assert env["PYTHONPATH"]


@pytest.mark.parametrize("has", [False, True])
def test_the_helper_never_gets_hermes_keys(tmp_path, monkeypatch, has):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    runtime = _runtime(tmp_path, has=lambda name: has)
    if not has:
        runtime.prepare(start=lambda target: target())
    _, env, _, _ = runtime.python()
    assert "OPENAI_API_KEY" not in env and "HERMES_GATEWAY_TOKEN" not in env
    assert env["PATH"] == "/usr/bin" and env["LC_ALL"] == "C.UTF-8"


@pytest.mark.parametrize("hermes_has_aiortc", [False, True])
@pytest.mark.parametrize("marker", ['{"aiortc": "1.14.0", "requirement": "aiortc==1.14.0"}', "not json", "[]"])
def test_an_environment_made_for_another_pin_is_not_ready(tmp_path, marker, hermes_has_aiortc):
    has = {"aiortc": False}
    runtime = _runtime(tmp_path, has=lambda name: has["aiortc"])
    runtime.prepare(start=lambda target: target())
    (tmp_path / "env" / "conduit-watch-audio.json").write_text(marker)
    # Not even Hermes' own aiortc stands in for it.
    has["aiortc"] = hermes_has_aiortc
    assert runtime.python() is None
    # Only a marker naming another pin says the runtime is out of date.
    reason = api.WATCH_AUDIO_STALE_RUNTIME if "requirement" in marker else api.WATCH_AUDIO_NEEDS_RUNTIME
    assert runtime.status() == {"runtime": "missing", "source": None, "reason": reason}
    assert runtime.missing_reason() == reason
    # Prepare makes it again for the current pin.
    assert runtime.prepare(start=lambda target: target())["runtime"] == "ready"


def test_preparing_makes_a_private_environment_with_venv_and_pip(tmp_path):
    run = FakeRun()
    runtime = _runtime(tmp_path, run=run)
    status = runtime.prepare(start=lambda target: target())
    assert status == {"runtime": "ready", "source": "plugin", "reason": None}
    env = tmp_path / "env"
    assert run.commands[0] == ["/usr/bin/python3", "-m", "venv", str(env)]
    assert run.commands[1][:4] == [str(env / "bin" / "python"), "-m", "pip", "install"]
    assert run.commands[1][-1] == "aiortc==1.15.0"
    assert json.loads((env / "conduit-watch-audio.json").read_text()) == {"aiortc": "1.15.0", "requirement": "aiortc==1.15.0"}
    python, _, flags, source = runtime.python()
    assert (python, flags, source) == (str(env / "bin" / "python"), ("-I",), "plugin")


def test_a_python_without_venv_falls_back_to_uv(tmp_path):
    run = FakeRun(fail=("-m venv",))
    runtime = _runtime(tmp_path, run=run, which=lambda name: "/opt/uv" if name == "uv" else None)
    assert runtime.prepare(start=lambda target: target())["runtime"] == "ready"
    assert [command[:2] for command in run.commands[1:3]] == [["/opt/uv", "venv"], ["/opt/uv", "pip"]]


def test_a_failed_prepare_says_why_and_can_be_asked_again(tmp_path):
    runtime = _runtime(tmp_path, run=FakeRun(fail=("-m venv",)))
    status = runtime.prepare(start=lambda target: target())
    assert status["runtime"] == "failed"
    assert "ensurepip is not available" in status["reason"] and "uv" in status["reason"]
    runtime._run = FakeRun()
    assert runtime.prepare(start=lambda target: target())["runtime"] == "ready"


def test_prepare_never_deletes_a_folder_it_did_not_make(tmp_path):
    env = tmp_path / "env"
    env.mkdir()
    (env / "notes.txt").write_text("keep me")
    status = _runtime(tmp_path).prepare(start=lambda target: target())
    assert status["runtime"] == "failed" and "isn't a Python environment" in status["reason"]
    assert (env / "notes.txt").read_text() == "keep me"


def test_the_status_route_reports_both_engines(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, "_watch_audio_runtime", _runtime(tmp_path))
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    body = TestClient(app).get("/p/watch-audio/status").json()
    assert body["version"] == 1
    assert body["engines"]["gpt_live"]["runtime"] == "missing"
    assert body["engines"]["grok"]["runtime"] == "ready"


# --- The grant's audio flag -------------------------------------------------------


def _pairing(tmp_path):
    path = tmp_path / "conduit-push.json"
    path.write_text(json.dumps({"credential": "install-1.gateway-1.secret", "installation_id": "install-1",
                                "gateway_id": "gateway-1", "relay_url": "https://relay.example"}), encoding="utf-8")
    return path


class GrantRelay:
    def __init__(self, audio_echo=True):
        self.requests = []
        self.audio_echo = audio_echo

    def __call__(self, url, method, credential, payload, timeout):
        self.requests.append((method, url, payload))
        if method == "POST":
            body = {"grant_id": "A" * 22, "expires_at": None}
            if self.audio_echo and payload.get("audio"):
                body["audio"] = True
            return 201, body
        return 204, {}


@pytest.fixture()
def fresh_grants(monkeypatch):
    monkeypatch.setattr(api, "_watch_grants", api._WatchGrants())


def test_an_audio_grant_carries_the_watch_side_url_and_engines(tmp_path, monkeypatch, fresh_grants):
    monkeypatch.setattr(api, "_watch_audio_runtime", _runtime(tmp_path, has=lambda name: True))
    relay = GrantRelay()
    started = []
    grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=_pairing(tmp_path),
                                 relay=relay, start=lambda g: None, start_audio=started.append)
    assert relay.requests[0][2]["audio"] is True
    assert grant["audio"] == {"url": f"wss://relay.example/v1/watch-audio/{'A' * 22}/watch", "version": 1,
                              "engines": ["gpt_live", "grok"]}
    [live] = api._watch_grants.all()
    assert started == [live] and live.audio.secret == api._unb64u(grant["key"])
    # Closing the grant stops its bridge.
    api._close_watch_grant_here(live)
    assert live.audio.stop_requested.is_set()


def test_gpt_live_is_left_out_until_its_runtime_is_ready(tmp_path, monkeypatch, fresh_grants):
    monkeypatch.setattr(api, "_watch_audio_runtime", _runtime(tmp_path))
    grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=_pairing(tmp_path),
                                 relay=GrantRelay(), start=lambda g: None, start_audio=lambda g: None)
    assert grant["audio"]["engines"] == ["grok"]


def test_a_relay_without_watch_audio_refuses_the_grant_and_closes_it_there(tmp_path, monkeypatch, fresh_grants):
    relay = GrantRelay(audio_echo=False)
    closed = []
    monkeypatch.setattr(api, "_close_watch_grant_on_relay_later", lambda grant, relay: closed.append(grant.grant_id))
    with pytest.raises(api.TokenError) as raised:
        api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=_pairing(tmp_path),
                             relay=relay, start=lambda g: None, start_audio=lambda g: None)
    assert raised.value.status == 501 and "doesn't support Watch audio" in str(raised.value)
    assert closed == ["A" * 22]
    assert api._watch_grants.all() == []


def test_a_grant_without_audio_asks_the_relay_for_none(tmp_path, fresh_grants):
    relay = GrantRelay()
    grant = api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=_pairing(tmp_path), relay=relay,
                                 start=lambda g: None)
    assert "audio" not in relay.requests[0][2] and "audio" not in grant


def test_audio_must_be_a_boolean(tmp_path, fresh_grants):
    with pytest.raises(api.TokenError) as raised:
        api.open_watch_grant({"tools": ["web_search"], "audio": "yes"}, profile=None, path=_pairing(tmp_path),
                             relay=GrantRelay(), start=lambda g: None)
    assert raised.value.status == 400


def test_a_bridge_that_cannot_start_closes_the_grant(tmp_path, monkeypatch, fresh_grants):
    closed = []
    monkeypatch.setattr(api, "_close_watch_grant_on_relay_later", lambda grant, relay: closed.append(grant.grant_id))

    def no_thread(grant):
        raise RuntimeError("can't start new thread")

    with pytest.raises(api.TokenError) as raised:
        api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=_pairing(tmp_path),
                             relay=GrantRelay(), start=lambda g: None, start_audio=no_thread)
    assert raised.value.status == 503
    assert closed == ["A" * 22] and api._watch_grants.all() == []


# --- The bridge, with a stand-in relay socket --------------------------------------


class FakeSocket:
    """The relay as the bridge sees it: what arrives, and what it sent."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.close_code = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.incoming.put_nowait(None)


class EchoSession(api._WatchAudioSession):
    """An engine that echoes audio and events and ends when asked."""

    engine = "grok"

    async def run(self):
        await self.bridge.control(self.stream, {"type": "started", "engine": self.engine})
        await self.stopping.wait()

    async def audio(self, pcm):
        await self.send(api.WATCH_AUDIO_AUDIO, pcm[::-1])

    async def event(self, data):
        await self.send(api.WATCH_AUDIO_EVENT, data)


class FailingSession(api._WatchAudioSession):
    engine = "gpt_live"

    async def run(self):
        raise api.WatchAudioFailure("unavailable", api.WATCH_AUDIO_NEEDS_RUNTIME)


class Watch:
    """The Watch's side of one stream, sealing as Conduit does."""

    def __init__(self, sid=None):
        self.sid = sid or os.urandom(16)
        keys = api.watch_audio_keys(SECRET, self.sid)
        self.up, self.down = keys[UP], keys[DOWN]
        self.counter = 0
        self.received = -1

    def hello(self, version=1):
        return bytes([4]) + self.sid + bytes([version])

    def seal(self, kind, plain):
        message = api.seal_watch_audio(self.up, UP, GRANT_ID, self.sid, kind, self.counter, plain)
        self.counter += 1
        return message

    def control(self, payload):
        return self.seal(3, json.dumps(payload).encode())

    def open(self, message):
        kind, counter, plain = api.open_watch_audio(self.down, DOWN, GRANT_ID, self.sid, message)
        assert counter > self.received
        self.received = counter
        return kind, json.loads(plain) if kind == 3 else plain


def _bridge(sessions=None, slots=None):
    grant = types.SimpleNamespace(grant_id=GRANT_ID, profile=None, relay_url="https://relay.example",
                                  credential="c", expires_at=time.monotonic() + 600, closed=types.SimpleNamespace(is_set=lambda: False))
    bridge = api._WatchAudioBridge(grant, SECRET, slots=slots or api._WatchAudioSlots(4))
    return bridge


@pytest.mark.parametrize("lives", [0.0, 6.0])
def test_a_host_socket_dropped_at_once_reconnects_with_backoff(lives):
    clock = {"now": 0.0}
    bridge = _bridge()
    bridge.clock = lambda: clock["now"]
    bridge.grant.expires_at = 1000.0
    delays = []

    class DroppedSocket(FakeSocket):
        async def __anext__(self):
            clock["now"] += lives  # how long the relay kept it
            raise StopAsyncIteration

    async def connect(url, credential):
        return DroppedSocket()

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 4:
            bridge.stop_requested.set()

    bridge.connect = connect
    bridge._sleep = sleep
    _run(bridge._main())
    assert delays == ([1.0, 2.0, 4.0, 8.0] if lives == 0.0 else [0.5, 0.5, 0.5, 0.5])


async def _attach(bridge):
    socket = FakeSocket()
    bridge.loop = asyncio.get_running_loop()
    bridge.wake = asyncio.Event()
    bridge.send_lock = asyncio.Lock()
    bridge.socket = socket
    return socket


async def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def test_a_watch_stream_starts_echoes_and_ends(monkeypatch):
    monkeypatch.setitem(api._WATCH_AUDIO_SESSIONS, "grok", EchoSession)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(bytes([0, 1]))
        await bridge._handle(watch.hello())
        await bridge._handle(watch.control({"type": "start", "engine": "grok"}))
        await _until(lambda: len(socket.sent) == 1)
        assert watch.open(socket.sent[0]) == (3, {"type": "started", "engine": "grok"})
        await bridge._handle(watch.seal(1, b"\x01\x02"))
        await bridge._handle(watch.seal(2, b'{"type":"x"}'))
        await _until(lambda: len(socket.sent) == 3)
        assert watch.open(socket.sent[1]) == (1, b"\x02\x01")
        assert watch.open(socket.sent[2]) == (2, b'{"type":"x"}')
        await bridge._handle(watch.control({"type": "end"}))
        await _until(lambda: len(socket.sent) == 4)
        assert watch.open(socket.sent[3]) == (3, {"type": "ended", "engine": "grok", "reason": "ended"})
        assert bridge.session is None and bridge.slots.used == 0

    _run(scenario())


def test_the_watch_leaving_ends_the_session_without_writing_to_its_stream(monkeypatch):
    monkeypatch.setitem(api._WATCH_AUDIO_SESSIONS, "grok", EchoSession)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(watch.hello())
        await bridge._handle(watch.control({"type": "start", "engine": "grok"}))
        await _until(lambda: len(socket.sent) == 1)
        await bridge._handle(bytes([0, 2]))
        assert bridge.session is None and bridge.stream is None and bridge.slots.used == 0
        await asyncio.sleep(0.05)
        assert len(socket.sent) == 1
        # Audio from the old stream goes nowhere.
        await bridge._handle(watch.seal(1, b"\x01\x02"))
        assert len(socket.sent) == 1

    _run(scenario())


def test_a_stream_id_is_taken_once(monkeypatch):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(watch.hello())
        first = bridge.stream
        await bridge._handle(bytes([0, 1]))
        await bridge._handle(watch.hello())
        assert bridge.stream is None and first is not None
        await bridge._handle(watch.control({"type": "start", "engine": "grok"}))
        assert socket.sent == []

    _run(scenario())


def test_replayed_and_forged_messages_are_ignored(monkeypatch):
    monkeypatch.setitem(api._WATCH_AUDIO_SESSIONS, "grok", EchoSession)

    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(watch.hello())
        start = watch.control({"type": "start", "engine": "grok"})
        await bridge._handle(start)
        await _until(lambda: len(socket.sent) == 1)
        await bridge._handle(start)  # replayed: no second start
        forged = bytearray(watch.seal(1, b"\x01\x02"))
        forged[-1] ^= 1
        await bridge._handle(bytes(forged))
        await asyncio.sleep(0.05)
        assert len(socket.sent) == 1 and bridge.rejected == 2
        await bridge._end_session("test")

    _run(scenario())


def test_an_unknown_engine_a_full_host_and_a_failing_engine_each_say_why(monkeypatch):
    monkeypatch.setitem(api._WATCH_AUDIO_SESSIONS, "gpt_live", FailingSession)

    async def scenario():
        slots = api._WatchAudioSlots(0)
        bridge = _bridge(slots=slots)
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(watch.hello())
        await bridge._handle(watch.control({"type": "start", "engine": "gemini"}))
        await bridge._handle(watch.control({"type": "start", "engine": "grok"}))
        slots.limit = 1
        await bridge._handle(watch.control({"type": "start", "engine": "gpt_live"}))
        await _until(lambda: len(socket.sent) == 4)
        replies = [watch.open(message)[1] for message in socket.sent]
        assert [reply.get("code") for reply in replies] == ["bad_request", "busy", "unavailable", None]
        assert replies[3] == {"type": "ended", "engine": "gpt_live", "reason": "error"}
        assert slots.used == 0

    _run(scenario())


def test_a_watch_on_another_version_is_told_so(monkeypatch):
    async def scenario():
        bridge = _bridge()
        socket = await _attach(bridge)
        watch = Watch()
        await bridge._handle(watch.hello(version=2))
        assert bridge.stream is None
        kind, reply = watch.open(socket.sent[0])
        assert (kind, reply["code"]) == (3, "version")

    _run(scenario())


def test_the_watch_url_is_the_relay_over_websocket():
    assert api._watch_audio_ws_url("https://push.example", "A" * 22, "watch") == \
        f"wss://push.example/v1/watch-audio/{'A' * 22}/watch"
    assert api._watch_audio_ws_url("http://127.0.0.1:9000", "A" * 22, "host") == \
        f"ws://127.0.0.1:9000/v1/watch-audio/{'A' * 22}/host"


@pytest.mark.parametrize("config", [b"not json", b'{"input_rate": "fast"}', b'{"input_rate": 1e999}',
                                    b'{"input_rate": 0}', b'{"output_rate": 96000}', b'{"stun": "stun:x"}', b"[]"])
def test_the_helper_refuses_an_unusable_config_in_one_line(config):
    pytest.importorskip("aiortc")
    import struct
    import subprocess
    import sys

    helper = pathlib.Path(api.__file__).with_name("watch_audio_helper.py")
    done = subprocess.run([sys.executable, str(helper)], input=b"C" + struct.pack(">I", len(config)) + config,
                          capture_output=True, timeout=60)
    assert done.returncode == 2
    assert done.stderr.decode().strip() == "config isn't usable"
