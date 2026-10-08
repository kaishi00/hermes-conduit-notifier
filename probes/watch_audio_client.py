"""A command-line stand-in for the Watch: a GPT-Live (or Grok) Watch call
through the real push relay, from this Hermes host, with no Watch.

It does what Conduit's Watch app will do (hermes-conduit
designs/apple-watch-gpt-live.md, "Bridge design"): it opens a Watch grant with
audio on this profile's relay pairing, which starts the plugin's audio
bridge to the relay, then dials the relay's Watch side with the grant's
relay key, says hello, seals a start, streams a WAV question as the mic in
real time and records the answer to a WAV. Everything goes through the relay
sealed, exactly as from a Watch; only the Watch is missing.

GPT-Live needs its WebRTC runtime, the plugin's own small Python environment
with aiortc. --prepare makes it first, the same way Conduit's "Prepare"
button will, and works on any Hermes install (it never installs into
Hermes' own Python).

Run it with the Python Hermes itself runs on (the one that can
`import hermes_cli`), from a checkout of this repo, on a host paired with
Conduit notifications whose relay has Watch audio (relay 0.7.0 or later):

    <hermes python> probes/watch_audio_client.py --prepare
    <hermes python> probes/watch_audio_client.py --wav question.wav [--engine gpt_live|grok] [--profile NAME]

The WAV is any PCM16 WAV (mono or stereo, any rate). The printed log carries
event types, timings and transcripts only; never keys, tokens or SDP.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import collections
import contextlib
import importlib.util
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
FRAME_S = 0.02
GREETING = "Hi, this is a Watch audio bridge test."
ANSWER_TAIL_S = 15.0
UP = "watch-to-host"
DOWN = "host-to-watch"


def load_plugin_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("conduit_push_plugin_api_watch_client", path)
    if spec is None or spec.loader is None:
        sys.exit(f"plugin_api.py not found at {path}; run the probe from a hermes-conduit-notifier checkout")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.exit(f"Could not load {path} ({exc.__class__.__name__}: {str(exc)[:120]}); "
                 "run the probe with the Python Hermes itself uses")
    if not hasattr(module, "_WatchAudioBridge"):
        sys.exit("This plugin checkout has no Watch audio; use the probe's own branch")
    return module


def read_wav(path: Path, rate: int) -> bytes:
    """A PCM16 WAV as mono PCM16 at ``rate`` (linear resampling: a test question, not music)."""
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("the WAV must be 16-bit PCM")
        channels, source_rate = source.getnchannels(), source.getframerate()
        samples = array.array("h", source.readframes(source.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array.array("h", (sum(samples[i:i + channels]) // channels for i in range(0, len(samples), channels)))
    if source_rate != rate and samples:
        count = int(len(samples) * rate / source_rate)
        step = source_rate / rate
        samples = array.array("h", (samples[min(len(samples) - 1, int(i * step))] for i in range(count)))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def peak(pcm: bytes) -> int:
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return max((abs(s) for s in samples), default=0)


class Run:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.marks: dict = {}
        self.events: List[str] = []
        self.transcripts: List[str] = []
        self.audio = bytearray()

    def mark(self, name: str) -> None:
        if name not in self.marks:
            self.marks[name] = round(time.monotonic() - self.t0, 2)
            print(f"  [{self.marks[name]:6.2f}s] {name}")


async def call(api, grant: dict, engine: str, speech: Dict[int, bytes], seconds: float, run: Run) -> dict:
    """One Watch call; ``speech`` is the question at each rate the engine may ask for."""
    try:
        from websockets.asyncio.client import connect
        header_option = "additional_headers"
    except ImportError:  # websockets < 13
        from websockets import connect
        header_option = "extra_headers"

    secret = base64.urlsafe_b64decode(grant["key"] + "=" * (-len(grant["key"]) % 4))
    url = grant["audio"]["url"]
    headers = {"Authorization": f"Bearer {grant['watch_key']}"}
    # The host's bridge dials the relay on its own thread: until it's there,
    # the relay turns the Watch away (4503).
    socket = None
    for _ in range(50):
        candidate = await connect(url, max_size=None, **{header_option: headers})
        try:
            await asyncio.wait_for(candidate.recv(), 0.3)
        except asyncio.TimeoutError:
            socket = candidate
            break
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await candidate.close()
        await asyncio.sleep(0.2)
    if socket is None:
        return {"error": "the host's bridge never reached the relay"}
    run.mark("Watch socket open (host there)")
    sid = os.urandom(16)
    keys = api.watch_audio_keys(secret, sid)
    sent = {"n": 0}
    received = {"n": -1}

    async def send(kind: int, plain: bytes) -> None:
        await socket.send(api.seal_watch_audio(keys[UP], UP, grant["grant_id"], sid, kind, sent["n"], plain))
        sent["n"] += 1

    started = asyncio.get_running_loop().create_future()
    ended = asyncio.Event()
    result: dict = {}

    def handle(kind: int, plain: bytes) -> None:
        if kind == 1:
            run.audio += plain
            if peak(plain) > 1000:
                run.mark("first model audio")
            return
        try:
            message = json.loads(plain)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if kind == 3:
            print(f"  control: {json.dumps(message)[:300]}")
            if message.get("type") == "started" and not started.done():
                started.set_result(message)
            elif message.get("type") == "error":
                result["error"] = message
                if not started.done():
                    started.set_result(None)
            elif message.get("type") == "ended":
                result["ended"] = message.get("reason")
                ended.set()
                if not started.done():
                    started.set_result(None)
            return
        name = str(message.get("type") or "<no type>")
        run.events.append(name)
        if name in ("input_transcript.added", "output_transcript.added"):
            item = message.get("item") if isinstance(message.get("item"), dict) else {}
            run.transcripts.append(f"{name.split('_')[0]}: {item.get('text') or ''}")
        elif name == "turn.done":
            turn = message.get("turn") if isinstance(message.get("turn"), dict) else {}
            print(f"  turn.done {turn.get('role')}: {str(turn.get('transcript') or '')[:200]}")
        elif name == "conversation.item.input_audio_transcription.completed":
            run.transcripts.append(f"input: {message.get('transcript') or ''}")
        elif name in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            run.transcripts.append(f"output: {message.get('transcript') or ''}")
        elif name == "error":
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            print(f"  error event: {error.get('code')} {str(error.get('message') or '')[:200]}")

    async def receive() -> None:
        async for message in socket:
            if isinstance(message, str) or not message or message[0] == 0:
                continue
            try:
                kind, counter, plain = api.open_watch_audio(keys[DOWN], DOWN, grant["grant_id"], sid, message)
            except api.WatchAudioError as exc:
                print(f"  a message didn't open: {exc}")
                continue
            if counter <= received["n"]:
                print("  a message was replayed")
                continue
            received["n"] = counter
            handle(kind, plain)

    reader = asyncio.ensure_future(receive())
    try:
        await socket.send(bytes([4]) + sid + bytes([1]))
        start = {"type": "start", "engine": engine}
        if engine == "gpt_live":
            start["greeting"] = GREETING
        await send(3, json.dumps(start).encode())
        run.mark("start sent")
        info = await asyncio.wait_for(started, 45)
        if info is None:
            return result
        run.mark("started")
        result["started"] = info
        input_rate = int(info.get("input_rate") or 16_000)
        if engine == "grok":
            # The Watch owns Grok's session, as the phone does.
            await send(2, json.dumps({"type": "session.update", "session": {
                "instructions": "You are a voice assistant being tested. Answer briefly.",
                "turn_detection": {"type": "server_vad", "silence_duration_ms": 700, "prefix_padding_ms": 300},
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": input_rate}},
                          "output": {"format": {"type": "audio/pcm", "rate": int(info.get("output_rate") or 24_000)}}},
            }}).encode())
            await send(2, json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": f"Say exactly: {GREETING}"}]}}).encode())
            await send(2, b'{"type":"response.create"}')
        frame = int(input_rate * FRAME_S) * 2
        question = speech.get(input_rate, b"")
        # Let the greeting play, then the question, then room for the answer.
        deadline = time.monotonic() + seconds
        clock = time.monotonic()
        offset = 0
        speak_at = time.monotonic() + (8.0 if question else seconds)
        while time.monotonic() < deadline and not ended.is_set():
            chunk = b""
            if question and time.monotonic() >= speak_at and offset < len(question):
                if offset == 0:
                    run.mark("mic speaking")
                chunk = question[offset:offset + frame]
                offset += frame
                if offset >= len(question):
                    deadline = max(deadline, time.monotonic() + ANSWER_TAIL_S)
            await send(1, chunk.ljust(frame, b"\0"))
            clock += FRAME_S
            await asyncio.sleep(max(0.0, clock - time.monotonic()))
        if not ended.is_set():
            await send(3, b'{"type":"end"}')
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ended.wait(), 10)
        return result
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await socket.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engine", choices=("gpt_live", "grok"), default="gpt_live")
    parser.add_argument("--wav", type=Path, help="a short spoken question (16-bit PCM WAV), played after the greeting")
    parser.add_argument("--seconds", type=float, default=25.0, help="how long the call stays open")
    parser.add_argument("--profile", help="Hermes profile whose relay pairing and sign-ins to use")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="where the model's audio is saved")
    parser.add_argument("--prepare", action="store_true",
                        help="make GPT-Live's WebRTC runtime first (then call only with --wav)")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if not args.out.is_dir():
        parser.error(f"--out {args.out} is not a folder")
    api = load_plugin_api()

    if args.prepare:
        runtime = api._watch_audio_runtime
        print(f"Preparing GPT-Live's WebRTC runtime in {api._watch_audio_env_dir()} (a few minutes the first time)")
        started_at = time.monotonic()
        status = runtime.prepare(start=lambda target: target())
        print(f"  {status['runtime']} in {time.monotonic() - started_at:.0f} s"
              + (f": {status['reason']}" if status.get("reason") else ""))
        if status["runtime"] != "ready":
            return 3
        if not args.wav:
            return 0
    print(f"Watch audio status: {json.dumps(api.watch_audio_status())}")

    speech: Dict[int, bytes] = {}
    if args.wav:
        try:
            speech = {rate: read_wav(args.wav, rate) for rate in (16_000, 24_000)}
        except Exception as exc:
            parser.error(f"--wav {args.wav}: {exc.__class__.__name__}: {str(exc)[:120]}")

    run = Run()
    try:
        with api._profile_scope(args.profile):
            grant = api.open_watch_grant({"tools": ["web_search"], "audio": True}, profile=args.profile)
    except Exception as exc:
        # TokenError texts are the plugin's own user-facing messages.
        print(f"Opening the grant failed: {str(exc)[:300] if isinstance(exc, api.TokenError) else exc.__class__.__name__}")
        return 3
    run.mark(f"grant open (engines {grant['audio']['engines']})")
    summary: dict = {"engine": args.engine, "relay": grant["relay_url"]}
    try:
        result = asyncio.run(call(api, grant, args.engine, speech, args.seconds, run))
    except Exception as exc:  # the probe reports, never raises
        result = {"error": exc.__class__.__name__}
    finally:
        api.revoke_watch_grant({"grant_id": grant["grant_id"]}, profile=args.profile,
                               tell_relay=api._close_watch_grant_on_relay)

    path = args.out / f"watch-audio-{args.engine}.wav"
    output_rate = int((result.get("started") or {}).get("output_rate") or 24_000)
    if run.audio:
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(output_rate)
            wav.writeframes(bytes(run.audio))
        summary["recording"] = str(path)
    summary.update(result)
    # 16-bit mono: two bytes a sample.
    summary.update(marks=run.marks, events=dict(collections.Counter(run.events)), transcripts=run.transcripts[-12:],
                   audio_seconds=round(len(run.audio) / (2 * output_rate), 1))
    summary["ok"] = "first model audio" in run.marks and "error" not in result
    print("\nSummary (paste this back into the thread):")
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
