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
import fractions
import json
import struct
import sys
import threading
import time
from collections import deque
from typing import Any, Optional

KINDS = set(b"CAPEQOR" + b"pex")
MAX_FRAME = 1024 * 1024
WEBRTC_RATE = 48_000
FRAME_SAMPLES = 960  # 20 ms at 48 kHz, what aiortc's Opus encoder takes
# Mic audio waiting to be sent, at 48 kHz: past this, the oldest is dropped
# so a stall never turns into lasting delay.
MAX_MIC_BACKLOG_BYTES = WEBRTC_RATE * 2 * 2  # 2 s
DATA_CHANNEL = "oai-events"


def write_frame(stream: Any, lock: threading.Lock, kind: str, payload: bytes) -> None:
    with lock:
        stream.write(kind.encode("ascii") + struct.pack(">I", len(payload)) + payload)
        stream.flush()


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
    out_lock = threading.Lock()
    inbox: asyncio.Queue = asyncio.Queue()

    def send(kind: str, payload: bytes = b"") -> None:
        try:
            write_frame(stdout, out_lock, kind, payload)
        except (BrokenPipeError, ValueError, OSError):
            loop.call_soon_threadsafe(inbox.put_nowait, ("Q", b""))

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
    config = json.loads(payload or b"{}")
    input_rate = int(config.get("input_rate") or 16_000)
    output_rate = int(config.get("output_rate") or 24_000)
    ice = [RTCIceServer(urls=[url]) for url in config.get("stun") or [] if isinstance(url, str)]

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
    return 0


def main() -> int:
    return asyncio.run(run(sys.stdin.buffer, sys.stdout.buffer))


if __name__ == "__main__":
    sys.exit(main())
