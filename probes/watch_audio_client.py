"""A command-line stand-in for the Watch: a GPT-Live (or Grok) Watch call
through the real push relay, from this Hermes host, with no Watch.

It does what Conduit's Watch app will do (hermes-conduit
designs/apple-watch-gpt-live.md, "Bridge design"): it opens a Watch grant with
audio on this profile's relay pairing, which starts the plugin's audio
bridge to the relay, then dials the relay's Watch side with the grant's
relay key, says hello, seals a start, streams a WAV question as the mic in
real time and records the answer to a WAV. Everything goes through the relay
sealed, exactly as from a Watch; only the Watch is missing.

When GPT-Live hands a lookup to the app (a delegation), the stand-in answers
it through the grant as the Watch would: a web_search sealed to this host
via the relay's Watch tools route, its results appended on the delegation.
The Watch app will run delegations as Hermes jobs, as the phone does; a job
needs the gateway's own process, so the stand-in looks things up instead.
The summary's timeline and latency show how long each turn took: from the
end of the question to the model's first speech, and from the lookup's
answer to the model speaking it.

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
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
FRAME_S = 0.02
GREETING = "Hi, this is a Watch audio bridge test."
ANSWER_TAIL_S = 15.0
# Model audio louder than this is speech; a pause longer than SPEECH_GAP_S
# ends a stretch of it.
LOUD = 1000
SPEECH_GAP_S = 0.6
# GPT-Live's bound on one context append, in UTF-8 bytes, as on the phone
# (GPTLiveProtocol.contextAppendMaxBytes), and how many one answer may use.
APPEND_MAX_BYTES = 500
APPENDS_PER_ANSWER = 3
# The relay holds a Watch call 25 s for the host (relay/src/watch-tools.mjs).
LOOKUP_TIMEOUT_S = 35.0
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
        self.timeline: List[list] = []
        self.events: List[str] = []
        self.transcripts: List[str] = []
        self.audio = bytearray()
        self.last_loud: Optional[float] = None

    def mark(self, name: str) -> None:
        if name not in self.marks:
            self.marks[name] = round(time.monotonic() - self.t0, 2)
            print(f"  [{self.marks[name]:6.2f}s] {name}")

    def note(self, label: str) -> None:
        """A step of the conversation, for the timeline."""
        at = round(time.monotonic() - self.t0, 2)
        self.timeline.append([at, label])
        print(f"  [{at:6.2f}s] {label}")

    def model_audio(self, pcm: bytes) -> None:
        self.audio += pcm
        if peak(pcm) <= LOUD:
            return
        self.mark("first model audio")
        now = time.monotonic()
        if self.last_loud is None or now - self.last_loud > SPEECH_GAP_S:
            self.note("model speech starts")
        self.last_loud = now


def latency(timeline: List[list]) -> dict:
    """Seconds from the end of the question, and from each lookup's answer,
    to what came next."""

    def first(prefix: str, since: float = 0.0) -> Optional[float]:
        return next((at for at, label in timeline if at >= since and label.startswith(prefix)), None)

    result: dict = {}
    pairs = [("question_end_to_heard_s", "question sent", "heard:"),
             ("question_end_to_speech_s", "question sent", "model speech starts"),
             ("delegation_to_lookup_answer_s", "delegation:", "lookup answered"),
             ("lookup_answer_to_speech_s", "lookup answered", "model speech starts")]
    for name, start, end in pairs:
        began = first(start)
        finished = first(end, began) if began is not None else None
        if finished is not None:
            result[name] = round(finished - began, 2)
    return result


def lookup(api, grant: dict, query: str) -> dict:
    """A web_search through the grant, as the Watch calls it: sealed, to this
    host's poller via the relay."""
    if not query.strip():
        return {"ok": False, "detail": "nothing to look up"}
    secret = base64.urlsafe_b64decode(grant["key"] + "=" * (-len(grant["key"]) % 4))
    keys = api.watch_tool_keys(secret)
    rid = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii")
    sealed = api.seal_watch_tool(keys["call"], "call", grant["grant_id"], rid,
                                 {"tool": "web_search", "args": {"query": query.strip()[:300], "limit": 3}})
    request = urllib.request.Request(
        f"{grant['relay_url']}/v1/watch-tools/grants/{grant['grant_id']}/calls",
        data=json.dumps({"rid": rid, **sealed}).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {grant['watch_key']}", "Content-Type": "application/json"})
    try:
        # The plugin's opener follows no redirect: the relay key stays with the relay.
        with api._relay_opener.open(request, timeout=LOOKUP_TIMEOUT_S) as response:
            body = json.loads(response.read(64 * 1024))
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"ok": False, "detail": f"the relay answered HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "detail": f"the relay couldn't be reached ({exc.__class__.__name__})"}
    try:
        return api.open_watch_tool(keys["result"], "result", grant["grant_id"], rid, body, max_bytes=64 * 1024)
    except api.WatchToolError as exc:
        return {"ok": False, "detail": f"the answer didn't open ({exc})"}


def lookup_text(answer: dict) -> str:
    """A web_search answer as the text GPT-Live speaks from."""
    if not answer.get("ok"):
        return f"The lookup failed ({answer.get('detail') or 'no answer'}). Tell the user it didn't work."
    lines = [f"- {result.get('title') or ''}: {result.get('snippet') or ''}"
             for result in answer.get("results") or [] if isinstance(result, dict)]
    if not lines:
        return "The web search found nothing for that."
    return "Web search results for the user's question:\n" + "\n".join(lines)


def delegation_appends(text: str, item_id: str) -> List[dict]:
    """The answer on a delegation, as the phone sends it: speakable appends of
    at most APPEND_MAX_BYTES each (GPTLiveProtocol.contextAppendMessages)."""
    chunks: List[str] = []
    current, size = "", 0
    for char in text.strip():
        width = len(char.encode("utf-8"))
        if size + width > APPEND_MAX_BYTES and current:
            chunks.append(current)
            current, size = "", 0
        current += char
        size += width
    if current:
        chunks.append(current)
    return [{"type": "delegation.context.append", "channel": "speakable",
             "content": [{"type": "input_text", "text": chunk}], "delegation_item_id": item_id}
            for chunk in chunks[:APPENDS_PER_ANSWER]]


async def call(api, grant: dict, engine: str, speech: Dict[int, bytes], seconds: float, run: Run,
               answer_tail_s: float = ANSWER_TAIL_S) -> dict:
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
    # The mic and the lookups both send: one at a time, so counters rise in order.
    sending = asyncio.Lock()
    state = {"deadline": time.monotonic() + seconds, "heard": "", "lookups": 0}
    lookups: List[asyncio.Future] = []

    async def send(kind: int, plain: bytes) -> None:
        async with sending:
            await socket.send(api.seal_watch_audio(keys[UP], UP, grant["grant_id"], sid, kind, sent["n"], plain))
            sent["n"] += 1

    started = asyncio.get_running_loop().create_future()
    ended = asyncio.Event()
    result: dict = {}

    async def answer_delegation(item_id: str, request: str) -> None:
        run.note(f"delegation: {request[:120] or '(no request)'}")
        try:
            answer = await asyncio.to_thread(lookup, api, grant, request)
            found = len(answer.get("results") or []) if answer.get("ok") else 0
            run.note(f"lookup answered ({found} result{'' if found == 1 else 's'})" if answer.get("ok")
                     else f"lookup answered (failed: {str(answer.get('detail'))[:120]})")
            result["lookups"] = result.get("lookups", 0) + 1
            for event in delegation_appends(lookup_text(answer), item_id):
                await send(2, json.dumps(event).encode("utf-8"))
        finally:
            state["lookups"] -= 1
            # Room for the model to speak what it found.
            state["deadline"] = max(state["deadline"], time.monotonic() + answer_tail_s)

    def handle(kind: int, plain: bytes) -> None:
        if kind == 1:
            run.model_audio(plain)
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
            transcript = str(turn.get("transcript") or "").strip()
            print(f"  turn.done {turn.get('role')}: {transcript[:200]}")
            if turn.get("role") == "user" and transcript:
                state["heard"] = transcript
                run.note(f"heard: {transcript[:120]}")
        elif name == "delegation.created":
            item = message.get("item") if isinstance(message.get("item"), dict) else {}
            if item.get("type") != "delegation" or item.get("target") != "client" or not item.get("id"):
                run.note("delegation not for the app; skipped")
                return
            text = "".join(str(part.get("text") or "") for part in item.get("content") or []
                           if isinstance(part, dict) and part.get("type") == "input_text").strip()
            # An empty delegation means the request is what the user just said.
            # Counted now, so the call can't close before the lookup starts.
            state["lookups"] += 1
            lookups.append(asyncio.ensure_future(answer_delegation(str(item["id"]), text or state["heard"])))
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
        # Let the greeting play, then the question, then room for the answer;
        # open while a lookup is out.
        clock = time.monotonic()
        offset = 0
        speak_at = time.monotonic() + (8.0 if question else seconds)
        while (time.monotonic() < state["deadline"] or state["lookups"]) and not ended.is_set():
            chunk = b""
            if question and time.monotonic() >= speak_at and offset < len(question):
                if offset == 0:
                    run.mark("mic speaking")
                chunk = question[offset:offset + frame]
                offset += frame
                if offset >= len(question):
                    run.note("question sent")
                    state["deadline"] = max(state["deadline"], time.monotonic() + answer_tail_s)
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
        for task in lookups:
            task.cancel()
        await asyncio.gather(reader, *lookups, return_exceptions=True)
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
    summary.update(marks=run.marks, timeline=run.timeline, latency=latency(run.timeline),
                   events=dict(collections.Counter(run.events)), transcripts=run.transcripts[-20:],
                   audio_seconds=round(len(run.audio) / (2 * output_rate), 1))
    summary["ok"] = "first model audio" in run.marks and "error" not in result
    print("\nSummary (paste this back into the thread):")
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
