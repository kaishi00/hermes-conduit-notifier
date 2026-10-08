"""Watch audio end to end: the real plugin bridge against a real relay.

A grant is opened with audio through open_watch_grant, the plugin's bridge
dials the relay as the host, and a stand-in Watch dials in with the grant's
relay key and seals its messages as Conduit does. The engines behind the
bridge are an echo, Grok against a fake xAI socket, and GPT-Live through the
real WebRTC helper against a fake GPT-Live peer (when aiortc is installed).
"""

import asyncio
import base64
import contextlib
import importlib.util
import json
import math
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the real relay")


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch_audio_e2e", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
UP = "watch-to-host"
DOWN = "host-to-watch"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base, path, *, method="GET", body=None, credential=""):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}


@pytest.fixture(scope="module")
def relay():
    node = shutil.which("node")
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="conduit-watch-audio-e2e-"))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    key = subprocess.run(
        [node, "-e",
         "const {generateKeyPairSync}=require('node:crypto');"
         "const {privateKey}=generateKeyPairSync('ec',{namedCurve:'P-256'});"
         "process.stdout.write(privateKey.export({type:'sec1',format:'pem'}));"],
        capture_output=True, text=True, check=True,
    ).stdout
    (tmpdir / "key.pem").write_text(key, encoding="utf-8")
    process = subprocess.Popen(
        [node, "src/server.mjs"],
        cwd=ROOT / "relay",
        env={
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PUBLIC_URL": f"https://relay-{port}.example",
            "DATA_PATH": str(tmpdir / "relay.json"),
            "APNS_KEY_PATH": str(tmpdir / "key.pem"),
            "APNS_KEY_ID": "AAAAAAAAAA",
            "APNS_TEAM_ID": "BBBBBBBBBB",
            "APNS_TOPIC": "com.milim.relay",
            "APNS_MODE": "accept",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1):
                    break
            except Exception:
                if time.time() > deadline or process.poll() is not None:
                    raise RuntimeError("relay did not start")
                time.sleep(0.1)
        yield base
    finally:
        process.kill()
        process.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def paired(relay, tmp_path, monkeypatch):
    status, registered = _request(relay, "/v1/installations", method="POST", body={
        "bundle_id": "com.milim.relay", "device_token": os.urandom(32).hex(), "environment": "production"})
    assert status == 201
    installation_id = registered["installation"]["id"]
    status, pairing = _request(relay, f"/v1/installations/{installation_id}/pairings", method="POST",
                               credential=registered["credential"])
    assert status == 201
    status, claimed = _request(relay, "/v1/pairings/claim", method="POST",
                               body={"pairing_code": pairing["pairing_code"], "gateway_name": "watch audio e2e"})
    assert status == 200
    path = tmp_path / "conduit-push.json"
    path.write_text(json.dumps({
        "credential": claimed["credential"],
        "installation_id": claimed["installation_id"],
        "gateway_id": claimed["gateway_id"],
        "relay_url": relay,
    }), encoding="utf-8")
    monkeypatch.setattr(api, "_WATCH_RELAY_SCHEMES", ("https://", "http://127.0.0.1"))
    grants = api._WatchGrants()
    monkeypatch.setattr(api, "_watch_grants", grants)
    yield path
    for grant in grants.all():
        api._close_watch_grant(grant, tell_relay=True)
        if grant.audio is not None:
            grant.audio.finished.wait(10)


def _ws_connect(url, credential):
    from websockets.asyncio.client import connect

    return connect(url, additional_headers={"Authorization": f"Bearer {credential}"}, max_size=None)


class Watch:
    """The Watch's side of a call: one stream, sealed as Conduit seals it."""

    def __init__(self, grant):
        self.grant_id = grant["grant_id"]
        self.secret = base64.urlsafe_b64decode(grant["key"] + "=")
        self.url = grant["audio"]["url"]
        self.relay_key = grant["watch_key"]
        self.socket = None

    async def connect(self, timeout=10.0):
        """Dials until the host is there (the relay turns a Watch away before it)."""
        deadline = time.monotonic() + timeout
        while True:
            socket = await _ws_connect(self.url, self.relay_key)
            try:
                await asyncio.wait_for(socket.recv(), 0.3)
            except asyncio.TimeoutError:
                break  # still open: the host is there
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await socket.close()
            if time.monotonic() > deadline:
                raise AssertionError("the host never reached the relay")
            await asyncio.sleep(0.2)
        self.socket = socket
        self.sid = os.urandom(16)
        keys = api.watch_audio_keys(self.secret, self.sid)
        self.up, self.down = keys[UP], keys[DOWN]
        self.counter = 0
        self.received = -1
        await socket.send(bytes([4]) + self.sid + bytes([1]))

    async def send(self, kind, plain):
        await self.socket.send(api.seal_watch_audio(self.up, UP, self.grant_id, self.sid, kind, self.counter, plain))
        self.counter += 1

    async def control(self, payload):
        await self.send(3, json.dumps(payload).encode())

    async def receive(self, timeout=10.0):
        message = await asyncio.wait_for(self.socket.recv(), timeout)
        kind, counter, plain = api.open_watch_audio(self.down, DOWN, self.grant_id, self.sid, message)
        assert counter > self.received
        self.received = counter
        if kind in (2, 3):
            return kind, json.loads(plain)
        return kind, plain

    async def until(self, predicate, timeout=15.0):
        """Messages up to and including the first that ``predicate`` accepts."""
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            message = await self.receive(max(0.1, deadline - time.monotonic()))
            seen.append(message)
            if predicate(message):
                return seen


class EchoSession(api._WatchAudioSession):
    engine = "grok"

    async def run(self):
        await self.bridge.control(self.stream, {"type": "started", "engine": self.engine})
        await self.stopping.wait()

    async def audio(self, pcm):
        await self.send(1, pcm[::-1])

    async def event(self, data):
        await self.send(2, data)


def test_a_watch_call_crosses_the_real_relay_sealed_both_ways(paired, monkeypatch):
    monkeypatch.setitem(api._WATCH_AUDIO_SESSIONS, "grok", EchoSession)
    grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=paired)
    assert grant["audio"]["url"].startswith("ws://127.0.0.1:")

    async def call():
        watch = Watch(grant)
        await watch.connect()
        await watch.control({"type": "start", "engine": "grok"})
        assert await watch.receive() == (3, {"type": "started", "engine": "grok"})
        await watch.send(1, b"\x01\x02\x03\x04")
        await watch.send(2, b'{"type":"hello"}')
        assert await watch.receive() == (1, b"\x04\x03\x02\x01")
        assert await watch.receive() == (2, {"type": "hello"})
        await watch.control({"type": "end"})
        assert await watch.receive() == (3, {"type": "ended", "engine": "grok", "reason": "ended"})
        # Revoking the grant ends the call: the host leaves first (4503), or
        # the relay closes the grant before it does (4010). Either way the
        # Watch can't come back on this grant.
        api.revoke_watch_grant({"grant_id": grant["grant_id"]}, profile=None,
                               tell_relay=api._close_watch_grant_on_relay)
        with pytest.raises(Exception):
            await watch.receive(5)
        assert watch.socket.close_code in (4010, 4503)
        status, _ = _request(grant["relay_url"], f"/v1/watch-tools/grants/{grant['grant_id']}/calls",
                             method="POST", body={}, credential=grant["watch_key"])
        assert status == 401

    asyncio.run(call())


def test_the_watch_rejoining_gets_a_fresh_stream(paired, monkeypatch):
    monkeypatch.setitem(api._WATCH_AUDIO_SESSIONS, "grok", EchoSession)
    grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=paired)

    async def call():
        first = Watch(grant)
        await first.connect()
        await first.control({"type": "start", "engine": "grok"})
        assert (await first.receive())[1]["type"] == "started"
        second = Watch(grant)
        await second.connect()
        await second.control({"type": "start", "engine": "grok"})
        assert await second.receive() == (3, {"type": "started", "engine": "grok"})
        await second.send(1, b"\x05\x06")
        assert await second.receive() == (1, b"\x06\x05")
        await second.socket.close()

    asyncio.run(call())


# --- Grok against a fake xAI ------------------------------------------------------------


class FakeXai:
    """A realtime socket that answers like xAI's, on its own thread."""

    def __init__(self, reply_seconds=1.0):
        self.reply = b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(int(24_000 * reply_seconds)))
        self.appended = bytearray()
        self.headers = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        from websockets.asyncio.server import serve

        async def handler(connection):
            self.headers.append(dict(connection.request.headers))
            async for message in connection:
                event = json.loads(message)
                kind = event.get("type")
                if kind == "session.update":
                    await connection.send(json.dumps({"type": "session.updated", "session": event["session"]}))
                elif kind == "input_audio_buffer.append":
                    self.appended += base64.b64decode(event["audio"])
                elif kind == "response.create":
                    await connection.send(json.dumps({"type": "response.created"}))
                    await connection.send(json.dumps({"type": "response.output_audio.delta",
                                                      "delta": base64.b64encode(self.reply).decode()}))
                    await connection.send(json.dumps({"type": "response.output_audio_transcript.delta", "delta": "Hi."}))
                    await connection.send(json.dumps({"type": "response.done"}))

        async def main():
            async with serve(handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.ready.set()
                await asyncio.Future()

        with pytest.raises(BaseException):
            self.loop.run_until_complete(main())

    def close(self):
        self.loop.call_soon_threadsafe(lambda: [task.cancel() for task in asyncio.all_tasks(self.loop)])
        self.thread.join(5)


def test_grok_runs_on_the_host_sign_in_with_its_audio_paced(paired, monkeypatch):
    xai = FakeXai(reply_seconds=1.0)
    try:
        monkeypatch.setattr(api, "GROK_LIVE_URL", f"ws://127.0.0.1:{xai.port}/v1/realtime")
        monkeypatch.setattr(api, "grok_live_credentials", lambda: ("xai-secret", "subscription"))
        monkeypatch.setattr(api, "WATCH_AUDIO_LEAD_S", 0.2)
        grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=paired)

        async def call():
            watch = Watch(grant)
            await watch.connect()
            await watch.control({"type": "start", "engine": "grok"})
            kind, started = await watch.receive()
            assert kind == 3 and started["type"] == "started", started
            assert (started["engine"], started["input_rate"], started["output_rate"]) == ("grok", 24_000, 24_000)
            await watch.send(2, json.dumps({"type": "session.update", "session": {"instructions": "Be brief."}}).encode())
            assert await watch.receive() == (2, {"type": "session.updated", "session": {"instructions": "Be brief."}})
            mic = os.urandom(640)
            for _ in range(3):
                await watch.send(1, mic)
            await watch.send(2, b'{"type":"response.create"}')
            started_at = time.monotonic()
            seen = await watch.until(lambda m: m[0] == 2 and m[1].get("type") == "response.done")
            elapsed = time.monotonic() - started_at
            audio = b"".join(data for kind, data in seen if kind == 1)
            events = [data["type"] for kind, data in seen if kind == 2]
            return mic, audio, events, elapsed, seen

        mic, audio, events, elapsed, seen = asyncio.run(call())
        assert audio == xai.reply
        assert events == ["response.created", "response.output_audio_transcript.delta", "response.done"]
        # 1 s of audio, 0.2 s of lead: about 0.8 s, not all at once.
        assert elapsed >= 0.6
        # The transcript and response.done wait behind the audio before them.
        assert [kind for kind, _ in seen][-2:] == [2, 2] and seen[1][0] == 1
        assert bytes(xai.appended) == mic * 3
        assert xai.headers[0]["authorization"] == "Bearer xai-secret"
    finally:
        xai.close()


def test_grok_without_a_sign_in_tells_the_watch(paired, monkeypatch):
    def no_sign_in():
        raise api.TokenError(503, api.GROK_LIVE_NO_CREDENTIAL)

    monkeypatch.setattr(api, "grok_live_credentials", no_sign_in)
    grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=paired)

    async def call():
        watch = Watch(grant)
        await watch.connect()
        await watch.control({"type": "start", "engine": "grok"})
        return [await watch.receive(), await watch.receive()]

    error, ended = asyncio.run(call())
    assert error == (3, {"type": "error", "code": "unavailable", "message": api.GROK_LIVE_NO_CREDENTIAL})
    assert ended == (3, {"type": "ended", "engine": "grok", "reason": "error"})


# --- GPT-Live through the real WebRTC helper -----------------------------------------------


class FakeGptLive:
    """A WebRTC peer standing in for chatgpt.com: it answers the helper's offer,
    plays a tone, says hello on the data channel and echoes what it hears there."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.peers = []
        self.offers = []
        self.requests = []
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def create_session(self, sdp, history=None, limiter_key=None, post=None, *, voice=None, briefing=None, greeting=None):
        self.requests.append({"voice": voice, "briefing": briefing, "greeting": greeting})
        answer = asyncio.run_coroutine_threadsafe(self._answer(sdp), self.loop).result(20)
        return {"auth": "subscription", "session": {"id": "sess_test"}, "transport": {"type": "webrtc", "sdp": answer},
                "source": "plugin", "voice": voice or "cove", "briefing_applied": bool(briefing),
                "greeting_applied": greeting is not None}

    async def _answer(self, sdp):
        import fractions

        import av
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        from aiortc.mediastreams import AudioStreamTrack, MediaStreamError

        class Tone(AudioStreamTrack):
            def __init__(self):
                super().__init__()
                self.samples = 0
                self.start = None

            async def recv(self):
                if self.start is None:
                    self.start = time.monotonic()
                else:
                    await asyncio.sleep(max(0, self.start + self.samples / 48_000 - time.monotonic()))
                frame = av.AudioFrame(format="s16", layout="mono", samples=960)
                frame.planes[0].update(b"".join(
                    struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (self.samples + i) / 48_000)))
                    for i in range(960)))
                frame.pts = self.samples
                frame.sample_rate = 48_000
                frame.time_base = fractions.Fraction(1, 48_000)
                self.samples += 960
                return frame

        self.offers.append(sdp)
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(pc)
        pc.addTrack(Tone())

        @pc.on("datachannel")
        def _channel(channel):
            channel.send(json.dumps({"type": "session.started"}))

            @channel.on("message")
            def _message(text):
                channel.send(json.dumps({"type": "echo", "of": json.loads(text)}))

        @pc.on("track")
        def _track(track):
            async def drain():
                while True:
                    try:
                        await track.recv()
                    except MediaStreamError:
                        return

            asyncio.ensure_future(drain())

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        return pc.localDescription.sdp

    def close(self):
        async def close_all():
            for pc in self.peers:
                await pc.close()

        asyncio.run_coroutine_threadsafe(close_all(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def test_gpt_live_runs_through_the_webrtc_helper(paired, monkeypatch):
    pytest.importorskip("aiortc")
    peer = FakeGptLive()
    try:
        monkeypatch.setattr(api, "create_gpt_live_session", peer.create_session)
        monkeypatch.setattr(api, "_watch_audio_runtime", api._WatchAudioRuntime(env_dir=lambda: "/nonexistent/env"))
        grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=paired)
        assert grant["audio"]["engines"] == ["gpt_live", "grok"]

        async def call():
            watch = Watch(grant)
            await watch.connect()
            await watch.control({"type": "start", "engine": "gpt_live", "voice": "marin", "greeting": "Hi there"})
            kind, started = await watch.receive(30)
            assert kind == 3 and started["type"] == "started", started
            seen = await watch.until(lambda m: m[0] == 2 and m[1].get("type") == "session.started", 15)
            audio = bytearray()
            while len(audio) < 24_000 * 2 // 2:  # half a second
                kind, data = await watch.receive()
                if kind == 1:
                    audio += data
            for _ in range(10):
                await watch.send(1, bytes(640))
            await watch.send(2, b'{"type":"input_text","text":"ping"}')
            echoed = await watch.until(lambda m: m[0] == 2 and m[1].get("type") == "echo", 10)
            await watch.control({"type": "end"})
            ended = await watch.until(lambda m: m[0] == 3, 10)
            return started, seen, bytes(audio), echoed[-1][1], ended[-1][1]

        started, seen, audio, echo, ended = asyncio.run(call())
        assert started == {"type": "started", "engine": "gpt_live", "voice": "marin", "input_rate": 16_000,
                           "output_rate": 24_000, "briefing_applied": False, "greeting_applied": True}
        assert peer.requests == [{"voice": "marin", "briefing": None, "greeting": "Hi there"}]
        # The tone came through: loud samples, not comfort noise.
        samples = struct.unpack(f"<{len(audio) // 2}h", audio[: len(audio) // 2 * 2])
        assert max(abs(s) for s in samples) > 2000
        assert echo == {"type": "echo", "of": {"type": "input_text", "text": "ping"}}
        assert ended == {"type": "ended", "engine": "gpt_live", "reason": "ended"}
    finally:
        peer.close()


def _load_probe():
    spec = importlib.util.spec_from_file_location("conduit_watch_audio_client_probe", ROOT / "probes" / "watch_audio_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_command_line_watch_stand_in_holds_a_gpt_live_call(paired, monkeypatch):
    pytest.importorskip("aiortc")
    probe = _load_probe()
    peer = FakeGptLive()
    try:
        monkeypatch.setattr(api, "create_gpt_live_session", peer.create_session)
        monkeypatch.setattr(api, "_watch_audio_runtime", api._WatchAudioRuntime(env_dir=lambda: "/nonexistent/env"))
        grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=None, path=paired)
        run = probe.Run()
        result = asyncio.run(probe.call(api, grant, "gpt_live", {}, 3.0, run))
        assert result["started"]["engine"] == "gpt_live"
        assert result["ended"] == "ended" and "error" not in result
        assert "first model audio" in run.marks and run.events.count("session.started") == 1
        assert peer.requests[0]["greeting"] == probe.GREETING
    finally:
        peer.close()


def test_the_stand_in_reads_any_pcm16_wav(tmp_path):
    import wave

    probe = _load_probe()
    path = tmp_path / "question.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44_100)
        wav.writeframes(struct.pack("<2h", 1000, 3000) * 44_100)
    pcm = probe.read_wav(path, 16_000)
    assert len(pcm) == 16_000 * 2
    assert set(struct.unpack(f"<{len(pcm) // 2}h", pcm)) == {2000}
