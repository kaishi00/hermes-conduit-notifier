"""GPT-Live's WebRTC side for a Conduit Watch call, as a helper process.

The Watch can't run WebRTC, so for a GPT-Live call on the Watch this host
holds the WebRTC connection (hermes-conduit designs/apple-watch-gpt-live.md).
The plugin runs this file as a child process, with a Python that has aiortc:
the plugin's own private environment, or Hermes' when it already has aiortc.
It never imports the plugin or Hermes, and it never sees the Codex sign-in:
it makes the offer, the plugin posts it to GPT-Live, and hands back the answer.

Framing on stdin and stdout: one ASCII kind byte, a 4-byte big-endian length,
then the payload.

    plugin -> helper   C config JSON (first)   A answer SDP
                       P mic PCM16LE mono at the input rate
                       E data-channel text     Q quit
    helper -> plugin   O offer SDP             R connected
                       p model PCM16LE mono at the output rate
                       e data-channel text     X ended (JSON with a reason)

Errors go to stderr as one short line, never with audio or SDP.
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import json
import os
import struct
import sys
import threading
import time
from collections import deque
from typing import Any, Optional

KINDS = set(b"CAPEQOR" + b"pex")
MAX_FRAME = 1024 * 1024
WEBRTC_RATE = 48_000
MIN_RATE = 8_000  # config rates outside MIN_RATE..WEBRTC_RATE are refused
FRAME_SAMPLES = 960  # 20 ms at 48 kHz, what aiortc's Opus encoder takes
# Mic audio waiting to be sent, at 48 kHz: past this, the oldest is dropped
# so a stall never turns into lasting delay.
MAX_MIC_BACKLOG_BYTES = WEBRTC_RATE * 2 * 2  # 2 s
# Model audio waiting for the plugin to read it, in seconds at the output
# rate: past this, the oldest is dropped, the same policy as the mic.
MAX_PLAY_BACKLOG_S = 2
# Every other frame waiting for the plugin is kept; this much means the
# plugin stopped reading, and the call ends.
MAX_PENDING_OTHER_BYTES = 4 * 1024 * 1024
# How long the ended frame gets to reach the plugin when the helper quits.
DRAIN_TIMEOUT_S = 2.0
DATA_CHANNEL = "oai-events"


def write_frame(stream: Any, kind: str, payload: bytes) -> None:
    stream.write(kind.encode("ascii") + struct.pack(">I", len(payload)) + payload)
    stream.flush()


class FrameWriter:
    """Writes frames to stdout from its own thread.

    The plugin forwards each frame to the relay before it reads the next, so
    a slow Watch connection stops it reading. A write from the WebRTC loop
    would then block the loop: ICE, the data channel and the mic would all
    stall. ``send`` only queues. Model audio over ``max_audio_bytes`` is
    dropped oldest first; every other frame is kept, and ``on_broken`` is
    called once when the pipe breaks or they pile up past
    MAX_PENDING_OTHER_BYTES.
    """

    def __init__(self, stream: Any, max_audio_bytes: int, on_broken: Any) -> None:
        self.stream = stream
        self.max_audio_bytes = max_audio_bytes
        self.on_broken = on_broken
        self.frames: deque = deque()
        self.audio_bytes = 0
        self.other_bytes = 0
        self.closed = False
        self.broken = False
        self.condition = threading.Condition()
        self.thread = threading.Thread(target=self._run, name="watch-audio-stdout", daemon=True)
        self.thread.start()

    def send(self, kind: str, payload: bytes = b"") -> None:
        with self.condition:
            if self.closed or self.broken:
                return
            self.frames.append((kind, payload))
            if kind == "p":
                self.audio_bytes += len(payload)
                if self.audio_bytes > self.max_audio_bytes:
                    kept: deque = deque()
                    for queued in self.frames:
                        if queued[0] == "p" and self.audio_bytes > self.max_audio_bytes:
                            self.audio_bytes -= len(queued[1])
                        else:
                            kept.append(queued)
                    self.frames = kept
            else:
                self.other_bytes += len(payload)
                if self.other_bytes > MAX_PENDING_OTHER_BYTES:
                    self._break()
                    return
            self.condition.notify()

    def close(self, timeout: float) -> bool:
        """Takes no more frames; True once the queued ones went out within ``timeout``."""
        with self.condition:
            self.closed = True
            self.condition.notify()
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def _break(self) -> None:
        # Called holding the condition.
        if self.broken:
            return
        self.broken = True
        self.frames.clear()
        self.condition.notify()
        self.on_broken()

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.frames and not (self.closed or self.broken):
                    self.condition.wait()
                if not self.frames:
                    return
                kind, payload = self.frames.popleft()
                if kind == "p":
                    self.audio_bytes -= len(payload)
                else:
                    self.other_bytes -= len(payload)
            try:
                write_frame(self.stream, kind, payload)
            except (BrokenPipeError, ValueError, OSError):
                with self.condition:
                    self._break()
                return


def read_frame(stream: Any) -> Optional[tuple]:
    header = stream.read(5)
    if len(header) < 5:
        return None
    kind, length = header[:1], struct.unpack(">I", header[1:])[0]
    if kind[0] not in KINDS or length > MAX_FRAME:
        raise ValueError("bad frame")
    payload = stream.read(length)
    if len(payload) < length:
        return None
    return kind.decode("ascii"), payload


async def run(stdin: Any, stdout: Any) -> int:
    try:
        import av
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
        from aiortc.mediastreams import AudioStreamTrack, MediaStreamError
    except ImportError as exc:
        print(f"aiortc is not available: {exc.name}", file=sys.stderr)
        return 3

    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()

    def reader() -> None:
        try:
            while True:
                frame = read_frame(stdin)
                if frame is None:
                    break
                loop.call_soon_threadsafe(inbox.put_nowait, frame)
        except Exception as exc:  # noqa: BLE001 — a broken pipe ends the call
            print(f"stdin failed: {type(exc).__name__}", file=sys.stderr)
        loop.call_soon_threadsafe(inbox.put_nowait, ("Q", b""))

    threading.Thread(target=reader, name="watch-audio-stdin", daemon=True).start()

    kind, payload = await inbox.get()
    if kind != "C":
        print("expected config first", file=sys.stderr)
        return 2
    try:
        config = json.loads(payload or b"{}")
        input_rate = int(config.get("input_rate", 16_000))
        output_rate = int(config.get("output_rate", 24_000))
        stun = config.get("stun") or []
        if not isinstance(stun, list) or not all(MIN_RATE <= rate <= WEBRTC_RATE for rate in (input_rate, output_rate)):
            raise ValueError
    except (ValueError, TypeError, AttributeError, OverflowError):
        print("config isn't usable", file=sys.stderr)
        return 2
    ice = [RTCIceServer(urls=[url]) for url in stun if isinstance(url, str)]

    def broken() -> None:
        with contextlib.suppress(RuntimeError):  # the loop is already closed
            loop.call_soon_threadsafe(inbox.put_nowait, ("Q", b""))

    writer = FrameWriter(stdout, output_rate * 2 * MAX_PLAY_BACKLOG_S, broken)
    send = writer.send

    class Microphone(AudioStreamTrack):
        """The Watch's audio as a WebRTC track, paced at 20 ms."""

        def __init__(self) -> None:
            super().__init__()
            self.backlog = bytearray()
            self.resampler = av.AudioResampler(format="s16", layout="mono", rate=WEBRTC_RATE)
            self._start: Optional[float] = None
            self._timestamp = 0

        def feed(self, pcm: bytes) -> None:
            samples = len(pcm) // 2
            if not samples:
                return
            frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
            frame.planes[0].update(pcm[: samples * 2])
            frame.sample_rate = input_rate
            for converted in self.resampler.resample(frame):
                self.backlog += bytes(converted.planes[0])[: converted.samples * 2]
            if len(self.backlog) > MAX_MIC_BACKLOG_BYTES:
                del self.backlog[: len(self.backlog) - MAX_MIC_BACKLOG_BYTES]

        async def recv(self):
            if self.readyState != "live":
                raise MediaStreamError
            if self._start is None:
                self._start = time.monotonic()
            else:
                self._timestamp += FRAME_SAMPLES
                await asyncio.sleep(max(0.0, self._start + self._timestamp / WEBRTC_RATE - time.monotonic()))
            size = FRAME_SAMPLES * 2
            chunk = bytes(self.backlog[:size])
            del self.backlog[:size]
            frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
            frame.planes[0].update(chunk.ljust(size, b"\0"))
            frame.pts = self._timestamp
            frame.sample_rate = WEBRTC_RATE
            frame.time_base = fractions.Fraction(1, WEBRTC_RATE)
            return frame

    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice))
    mic = Microphone()
    pc.addTrack(mic)
    channel = pc.createDataChannel(DATA_CHANNEL)
    pending_text: deque = deque(maxlen=64)
    readers = []
    ended = asyncio.Event()
    reason = {"value": "closed"}

    def end(why: str) -> None:
        if not ended.is_set():
            reason["value"] = why
            ended.set()
            inbox.put_nowait(("Q", b""))

    @channel.on("open")
    def _open() -> None:
        while pending_text:
            channel.send(pending_text.popleft())

    @channel.on("message")
    def _message(text: Any) -> None:
        if isinstance(text, str):
            send("e", text.encode("utf-8"))

    @pc.on("connectionstatechange")
    def _state() -> None:
        if pc.connectionState == "connected":
            send("R")
        elif pc.connectionState in ("failed", "closed"):
            end(f"webrtc {pc.connectionState}")

    @pc.on("track")
    def _track(track: Any) -> None:
        if track.kind != "audio":
            return

        async def play() -> None:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=output_rate)
            while True:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    return
                except Exception as exc:  # noqa: BLE001
                    print(f"remote audio stopped: {type(exc).__name__}", file=sys.stderr)
                    return
                data = b"".join(bytes(c.planes[0])[: c.samples * 2] for c in resampler.resample(frame))
                if data:
                    send("p", data)

        readers.append(asyncio.ensure_future(play()))

    try:
        await pc.setLocalDescription(await pc.createOffer())
        send("O", pc.localDescription.sdp.encode("utf-8"))
        while not ended.is_set():
            kind, payload = await inbox.get()
            if kind == "Q":
                break
            if kind == "A":
                await pc.setRemoteDescription(RTCSessionDescription(sdp=payload.decode("utf-8"), type="answer"))
            elif kind == "P":
                mic.feed(payload)
            elif kind == "E":
                text = payload.decode("utf-8", "replace")
                if channel.readyState == "open":
                    channel.send(text)
                else:
                    pending_text.append(text)
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        reason["value"] = f"helper failed ({type(exc).__name__})"
        print(reason["value"], file=sys.stderr)
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        try:
            await pc.close()
        except Exception:  # noqa: BLE001
            pass
        send("X", json.dumps({"reason": reason["value"]}).encode("utf-8"))
        if not writer.close(DRAIN_TIMEOUT_S):
            # The plugin stopped reading, and a write still holds stdout: the
            # interpreter's own flush at exit would wait on it for good.
            sys.stderr.flush()
            os._exit(0)
    return 0


def main() -> int:
    return asyncio.run(run(sys.stdin.buffer, sys.stdout.buffer))


if __name__ == "__main__":
    sys.exit(main())
