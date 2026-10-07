"""Can this Hermes host hold a GPT-Live call itself? (Apple Watch spike.)

The Watch can't run WebRTC, and GPT-Live on the ChatGPT subscription only
speaks WebRTC, so a Watch call would have the host hold the WebRTC side and
pass audio to the Watch through the push relay (hermes-conduit
designs/apple-watch-gpt-live.md, option A). This probe settles the one
unknown: does chatgpt.com accept an offer from a Python WebRTC stack
(aiortc) rather than a browser or Conduit's libwebrtc?

It starts the call exactly as Conduit's does (the plugin's own
create_gpt_live_session, same Codex sign-in, same URL), asks the model to
greet first, optionally plays a WAV as the microphone, saves what the model
says to a WAV, and prints timings. Nothing is sent to Conduit or the relay.

Run it with Hermes' own Python, from this plugin's folder:

    <hermes venv>/bin/python -m pip install aiortc
    <hermes venv>/bin/python probes/gpt_live_bridge_probe.py --tries 3 [--wav question.wav]

The printed log carries event types, timings and transcripts only; no
tokens, SDP or account details.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import collections
import fractions
import importlib.util
import json
import sys
import time
import wave
from pathlib import Path
from typing import List, Optional

try:
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack, MediaStreamError
except ImportError:
    sys.exit("This probe needs aiortc in Hermes' Python: <hermes venv>/bin/python -m pip install aiortc")

ROOT = Path(__file__).resolve().parents[1]
RATE = 48_000
FRAME_SAMPLES = 960  # 20 ms, what aiortc's Opus encoder takes
GREETING = "Hi, this is a GPT-Live bridge test."
# Peak above which a received frame counts as speech rather than comfort noise.
SPEECH_PEAK = 1_000


def load_plugin_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("conduit_push_plugin_api_probe", path)
    if spec is None or spec.loader is None:
        sys.exit(f"plugin_api.py not found at {path}; run the probe from a hermes-conduit-notifier checkout")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.exit(f"Could not load {path} ({exc.__class__.__name__}: {str(exc)[:120]}); "
                 "run the probe with the Python Hermes itself uses")
    missing = [name for name in ("_profile_scope", "create_gpt_live_session", "gpt_live_status", "TokenError")
               if not hasattr(module, name)]
    if missing:
        sys.exit(f"This plugin checkout lacks {', '.join(missing)}; use the probe's own branch")
    return module


def pcm_from_wav(path: Path) -> bytes:
    """Any audio file av can decode, as 48 kHz mono s16 bytes."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    out = bytearray()
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                out += bytes(converted.planes[0])[: converted.samples * 2]
    for converted in resampler.resample(None):
        out += bytes(converted.planes[0])[: converted.samples * 2]
    return bytes(out)


class Microphone(AudioStreamTrack):
    """Silence, then `pcm` once `start_speaking` is set, paced in real time."""

    def __init__(self, pcm: bytes = b""):
        super().__init__()
        self.pcm = pcm
        self.offset = 0
        self.start_speaking = asyncio.Event()
        self._start: Optional[float] = None
        self._timestamp = 0

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.monotonic()
        else:
            self._timestamp += FRAME_SAMPLES
            await asyncio.sleep(max(0.0, self._start + self._timestamp / RATE - time.monotonic()))
        size = FRAME_SAMPLES * 2
        chunk = b""
        if self.start_speaking.is_set() and self.offset < len(self.pcm):
            chunk = self.pcm[self.offset:self.offset + size]
            self.offset += size
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(chunk.ljust(size, b"\0"))
        frame.pts = self._timestamp
        frame.sample_rate = RATE
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


class Try:
    def __init__(self, number: int):
        self.number = number
        self.t0 = time.monotonic()
        self.marks: dict = {}
        self.events: List[str] = []
        self.transcripts: List[str] = []
        self.audio = bytearray()

    def mark(self, name: str) -> None:
        if name not in self.marks:
            self.marks[name] = round(time.monotonic() - self.t0, 2)
            print(f"  [{self.marks[name]:6.2f}s] {name}")


def scoped(api, profile: Optional[str], fn, *args, **kwargs):
    """`fn` under the Hermes profile the dashboard would use for `?profile=`."""
    with api._profile_scope(profile):
        return fn(*args, **kwargs)


async def one_try(api, profile: Optional[str], number: int, speech: bytes, seconds: float, out_dir: Path) -> dict:
    run = Try(number)
    print(f"Try {number}")
    pc = RTCPeerConnection()
    mic = Microphone(speech)
    pc.addTrack(mic)
    channel = pc.createDataChannel("oai-events")
    readers: List[asyncio.Task] = []
    greeted = asyncio.Event()

    @channel.on("open")
    def _open():
        run.mark("data channel open")

    @channel.on("message")
    def _message(text):
        try:
            _handle(text)
        except Exception as exc:  # keep the channel's receive loop alive
            print(f"  handler error: {exc.__class__.__name__}")

    def _handle(text):
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get("type"))
        run.events.append(kind)
        if kind in ("session.started", "session.updated"):
            run.mark("session started")
        elif kind in ("input_transcript.added", "output_transcript.added"):
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            run.mark("first " + kind.split("_")[0] + " transcript")
            run.transcripts.append(f"{kind.split('_')[0]}: {item.get('text') or ''}")
        elif kind == "turn.done":
            turn = event.get("turn") if isinstance(event.get("turn"), dict) else {}
            print(f"  turn.done {turn.get('role')}: {str(turn.get('transcript', ''))[:200]}")
            if turn.get("role") == "assistant":
                greeted.set()
        elif kind in ("error", "session.closed"):
            # Only the code and message, never the whole provider payload.
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            message = error.get("message") or event.get("message") or event.get("reason") or ""
            print(f"  {kind}: code={error.get('code')} {str(message)[:200]}")

    @pc.on("connectionstatechange")
    def _state():
        print(f"  connection {pc.connectionState}")
        if pc.connectionState == "connected":
            run.mark("WebRTC connected")

    @pc.on("track")
    def _track(track):
        if track.kind != "audio":
            return
        run.mark("remote audio track")

        async def read():
            resampler = av.AudioResampler(format="s16", layout="mono", rate=24_000)
            while True:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    return
                except Exception as exc:
                    print(f"  audio reader stopped: {exc.__class__.__name__}")
                    return
                for converted in resampler.resample(frame):
                    data = bytes(converted.planes[0])[: converted.samples * 2]
                    run.audio += data
                    samples = array.array("h", data)
                    if sys.byteorder == "big":
                        samples.byteswap()
                    if samples and max(abs(s) for s in samples) > SPEECH_PEAK:
                        run.mark("first model audio")

        readers.append(asyncio.ensure_future(read()))

    result: dict = {"try": number}
    try:
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)  # aiortc gathers ICE before returning
        run.mark("offer ready")
        answer = await asyncio.to_thread(scoped, api, profile, api.create_gpt_live_session,
                                         pc.localDescription.sdp, greeting=GREETING)
        run.mark("answer from chatgpt.com")
        result["source"] = answer.get("source")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["transport"]["sdp"], type="answer"))
        deadline = time.monotonic() + seconds
        if speech:
            try:
                await asyncio.wait_for(greeted.wait(), timeout=max(1.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                pass
            run.mark("mic speaking")
            mic.start_speaking.set()
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
    except Exception as exc:  # the probe reports, never raises
        # TokenError texts are the plugin's own user-facing messages, never provider text.
        detail = str(exc)[:300] if isinstance(exc, api.TokenError) else exc.__class__.__name__
        print(f"  failed: {detail}")
        result["error"] = detail
    finally:
        try:
            if channel.readyState == "open":
                channel.send(json.dumps({"type": "session.close"}))
                await asyncio.sleep(0.3)
        except Exception:
            pass
        try:
            await pc.close()
        except Exception as exc:
            print(f"  close failed: {exc.__class__.__name__}")
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    path = out_dir / f"gpt-live-probe-{number}.wav"
    if run.audio:
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24_000)
                wav.writeframes(bytes(run.audio))
            result["recording"] = str(path)
        except Exception as exc:  # the summary matters more than the file
            result["recording_error"] = exc.__class__.__name__
    result.update(marks=run.marks, events=dict(collections.Counter(run.events)), transcripts=run.transcripts[-12:],
                  audio_seconds=round(len(run.audio) / 48_000, 1))
    result["ok"] = "first model audio" in run.marks and "error" not in result
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tries", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=25.0, help="how long each call stays open")
    parser.add_argument("--wav", type=Path, help="a short spoken question, played as the mic after the greeting")
    parser.add_argument("--profile", help="Hermes profile whose voice.gpt_live settings to use")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="where the model's audio is saved")
    args = parser.parse_args()
    if args.tries < 1:
        parser.error("--tries must be at least 1")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if not args.out.is_dir():
        parser.error(f"--out {args.out} is not a folder")

    api = load_plugin_api()
    try:
        speech = pcm_from_wav(args.wav) if args.wav else b""
    except Exception as exc:
        parser.error(f"--wav {args.wav}: {exc.__class__.__name__}: {str(exc)[:120]}")
    status = await asyncio.to_thread(scoped, api, args.profile, api.gpt_live_status)
    print(f"GPT-Live status: available={status.get('available')} model={status.get('model')} "
          f"source={status.get('source')} reason={status.get('reason')}")
    if not status.get("available"):
        return 2

    results = []
    for number in range(1, args.tries + 1):
        if number > 1:
            await asyncio.sleep(3)  # the plugin's rate limiter is bypassed here; stay gentle
        results.append(await one_try(api, args.profile, number, speech, args.seconds, args.out))
    print("\nSummary (paste this back into the thread):")
    print(json.dumps({"aiortc": __import__("aiortc").__version__, "results": results}, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
