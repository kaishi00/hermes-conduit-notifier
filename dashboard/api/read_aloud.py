"""Read aloud, streamed: the first sentence plays while the rest is synthesized.

Why this exists
---------------
Read aloud today asks the host for a finished audio file, so nothing is heard
until the whole reply has been synthesized. Hosted TTS synthesises at roughly
realtime, so a 7 s paragraph is a 6 s silence first — measured on Gemini TTS,
`generateContent` took 6.1 s where `streamGenerateContent` returned its first
chunk at 1.56 s and finished the same clip in 3.15 s.

This module exposes the chunked path the host already knows how to walk
(`tools/tts_streaming.resolve_streaming_provider`, which covers built-in
streamers and any plugin `TTSProvider` that opted into raw PCM). A client that
calls `/read-aloud/stream` gets audio as it is produced; a client that does not
keeps using whatever it used before, because nothing here replaces the existing
synthesis path.

The host half is deliberately client-agnostic: one status route so an app can
see whether streaming is available and what it would receive, and one POST that
streams. The app-side change (playing a chunked body instead of waiting for a
file) is Conduit's to make.
"""

from __future__ import annotations

import subprocess
import threading
from typing import Any, Dict, Iterator, Optional

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from . import core

router = core.router


# --- Read aloud, streamed ----------------------------------------------------
#
# Two routes, both cacheable never:
#
#   GET  /read-aloud/status   does this host have a chunked TTS provider, and
#                             in what format would it stream?
#   POST /read-aloud/stream   {text, voice?, format?} -> the audio, chunked.
#
# `format` is "mp3" (default: a progressive body any player accepts) or "pcm"
# (raw int16 mono at the reported rate, for a client that wants to skip the
# encoder). The route never substitutes a voice to get streaming: a host whose
# provider has no chunked API answers 409 and the client does what it does now.

READ_ALOUD_MAX_BODY_BYTES = 32_000     # one spoken message, not a document
READ_ALOUD_MAX_CHARS = 8_000
READ_ALOUD_LIMIT = 30
READ_ALOUD_WINDOW_S = 60.0
READ_ALOUD_FORMATS = ("mp3", "pcm")
READ_ALOUD_MP3_RATE = 24_000

_read_aloud_limiter = core._MintLimiter(READ_ALOUD_LIMIT, READ_ALOUD_WINDOW_S,
                                       message="Too many read-aloud requests; try again shortly")

_MISSING_MODULES = frozenset({"agent", "agent.tts_provider", "hermes_cli", "hermes_cli.config",
                              "hermes_cli.plugins", "tools", "tools.tts_streaming", "tools.tts_tool"})


def _tts_config() -> Optional[Dict[str, Any]]:
    """The profile's ``tts`` block, or None when Hermes' modules are unreachable."""
    try:
        from tools.tts_tool import _load_tts_config
    except ImportError:
        return None
    try:
        return _load_tts_config() or {}
    except Exception:
        return None


def _streamer(tts_config: Dict[str, Any]) -> Any:
    """The provider that can stream on this host, or None."""
    try:
        from tools.tts_streaming import resolve_streaming_provider
    except ImportError:
        return None
    try:
        # preferred = the configured provider: never swap the user's voice for
        # one that happens to stream.
        return resolve_streaming_provider(tts_config, tts_config.get("provider"))
    except Exception:
        return None


def _mp3_stream(pcm: Iterator[bytes], rate: int) -> Iterator[bytes]:
    """Re-encode chunked PCM to chunked MP3, so the client needs no PCM plumbing.

    ffmpeg is fed on a writer thread while this generator reads its stdout: a
    single thread writing then reading one shared pipe can deadlock as soon as
    the encoder's output buffer fills.
    """
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
            "-f", "mp3", "-b:a", "64k", "pipe:1"]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)
    except OSError:
        yield from pcm          # no encoder: hand back the raw chunks rather than nothing
        return
    assert process.stdin is not None and process.stdout is not None  # both are PIPE

    def feed() -> None:
        try:
            for chunk in pcm:
                process.stdin.write(chunk)
        except Exception:
            pass
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        while True:
            data = process.stdout.read(8192)
            if not data:
                break
            yield data
    finally:
        try:
            process.stdout.close()
        except Exception:
            pass
        writer.join(timeout=5)
        if process.poll() is None:
            process.kill()


def _pcm_stream(streamer: Any, text: str, voice: Optional[str]) -> Iterator[bytes]:
    chunks = streamer.stream(text) if voice is None else streamer.stream(text, voice=voice)
    for chunk in chunks:
        if not chunk:
            continue
        yield chunk if isinstance(chunk, bytes) else bytes(chunk)


@router.get("/read-aloud/status")
async def get_read_aloud_status(response: Response) -> Dict[str, Any]:
    """Whether this host can stream a spoken reply, and in what format."""
    response.headers["Cache-Control"] = "no-store"
    tts_config = _tts_config()
    if tts_config is None:
        return {"ok": True, "streaming": False, "reason": "hermes_tts_unavailable"}
    streamer = _streamer(tts_config)
    if streamer is None:
        return {"ok": True, "streaming": False, "provider": tts_config.get("provider"),
                "reason": "provider_has_no_chunked_api"}
    rate = int(getattr(streamer, "sample_rate", READ_ALOUD_MP3_RATE) or READ_ALOUD_MP3_RATE)
    return {"ok": True, "streaming": True, "provider": tts_config.get("provider"),
            "sample_rate": rate, "formats": list(READ_ALOUD_FORMATS)}


@router.post("/read-aloud/stream")
async def post_read_aloud_stream(request: Request, response: Response,
                                 profile: Optional[str] = None) -> Any:
    """Stream `text` as audio. 409 when this host cannot stream, never a silent downgrade."""
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, READ_ALOUD_MAX_BODY_BYTES)
    text = (body.get("text") if isinstance(body, dict) else None) or ""
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text is required"}
    if len(text) > READ_ALOUD_MAX_CHARS:
        return {"ok": False, "error": f"text is longer than {READ_ALOUD_MAX_CHARS} characters"}
    fmt = (body.get("format") or "mp3") if isinstance(body, dict) else "mp3"
    fmt = str(fmt).lower().strip()
    if fmt not in READ_ALOUD_FORMATS:
        return {"ok": False, "error": f"format must be one of {', '.join(READ_ALOUD_FORMATS)}"}
    voice = body.get("voice") if isinstance(body, dict) else None
    voice = voice if isinstance(voice, str) and voice.strip() else None

    tts_config = _tts_config()
    streamer = _streamer(tts_config) if tts_config is not None else None
    if streamer is None:
        return {"ok": False, "error": "this host's TTS provider has no chunked API",
                "reason": "provider_has_no_chunked_api"}

    await core._scoped_acquire(_read_aloud_limiter, profile)
    rate = int(getattr(streamer, "sample_rate", READ_ALOUD_MP3_RATE) or READ_ALOUD_MP3_RATE)
    pcm = _pcm_stream(streamer, text, voice)
    if fmt == "pcm":
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                   "X-Audio-Sample-Rate": str(rate), "X-Audio-Channels": "1",
                   "X-Audio-Sample-Width": "2"}
        return StreamingResponse(pcm, media_type=f"audio/L16;rate={rate};channels=1", headers=headers)
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    return StreamingResponse(_mp3_stream(pcm, rate), media_type="audio/mpeg", headers=headers)
