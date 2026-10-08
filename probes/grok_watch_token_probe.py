"""Can this host's xAI sign-in mint a short-lived Grok Live token for the Watch?

Gemini Live on the Watch connects straight to Google with a short-lived token
the host mints; the Watch never holds the Gemini key. Grok Live speaks the
same kind of WebSocket, and xAI documents short-lived client tokens
(POST /v1/realtime/client_secrets), but only shows them minted from an API key.
Conduit's Grok usually runs on a SuperGrok sign-in, which is why the phone's
Grok goes through this host today. If a SuperGrok sign-in can mint one too, the
Watch's Grok can connect directly like its Gemini (hermes-conduit
designs/apple-watch-gpt-live.md).

The probe uses the plugin's own xAI sign-in lookup (the same one the phone's
Grok uses) and answers four things:
  1. Does xAI mint a client token from this sign-in?
  2. Does a realtime call open with that token, and does Grok answer?
  3. Does the open call keep working after the token expires?
  4. Can the same token open a second call (while the first is open, and after
     it expired)? That decides how tightly the Watch's token has to be held.

Run it with the Python Hermes itself runs on (the one that can
`import hermes_cli`), from a checkout of this repo. It needs nothing beyond
what Hermes ships. It takes about a minute plus --expires.

    <hermes python> probes/grok_watch_token_probe.py [--expires 60] [--profile NAME]

The printed log carries event types, timings and transcripts only; never the
sign-in, the minted token, or account details.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.x.ai/v1"
RATE = 24_000
TURN_TIMEOUT_S = 30.0
# What a Watch microphone sends between turns: 100 ms of silence a second
# keeps the call from looking idle without tripping turn detection.
SILENCE = base64.b64encode(b"\0" * (RATE // 10 * 2)).decode("ascii")
FIRST_TURN = "Say exactly this and nothing else: Grok token test."
LATER_TURN = "What is two plus two? Answer in one short sentence."


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
    missing = [name for name in ("_profile_scope", "grok_live_credentials", "grok_live_model_voice", "TokenError")
               if not hasattr(module, name)]
    if missing:
        sys.exit(f"This plugin checkout lacks {', '.join(missing)}; update the checkout")
    return module


def scoped(api, profile: Optional[str], fn, *args, **kwargs):
    """`fn` under the Hermes profile the dashboard would use for `?profile=`."""
    with api._profile_scope(profile):
        return fn(*args, **kwargs)


def find_token(body: Any) -> Tuple[str, Optional[Any]]:
    """(token, expires_at) from xAI's response, whichever shape it uses."""
    if not isinstance(body, dict):
        return "", None
    for holder in (body, body.get("client_secret")):
        if isinstance(holder, dict):
            for key in ("value", "token", "client_secret", "secret"):
                value = holder.get(key)
                if isinstance(value, str) and value:
                    return value, holder.get("expires_at", body.get("expires_at"))
    return "", None


def shape(body: Any) -> Any:
    """The response's field names and types, never its values."""
    if isinstance(body, dict):
        return {key: shape(value) for key, value in body.items()}
    if isinstance(body, list):
        return [shape(body[0])] if body else []
    return type(body).__name__


def mint(api_base: str, bearer: str, seconds: int) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"{api_base}/realtime/client_secrets",
        data=json.dumps({"expires_after": {"seconds": seconds}}).encode("utf-8"),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    result: Dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result["status"] = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        raw = exc.read()
    except Exception as exc:
        result.update(status=None, error=exc.__class__.__name__)
        return result
    result["ms"] = round((time.monotonic() - started) * 1000)
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        body = None
    token, expires_at = find_token(body)
    if token:
        result.update(token=token, expires_at=expires_at, shape=shape(body))
    else:
        # A refusal: xAI's own error text only, short (it never carries the bearer).
        detail = body.get("error") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code")
        result["error"] = str(detail or (body.get("message") if isinstance(body, dict) else "") or "")[:200]
    return result


async def open_socket(url: str, token: str, how: str):
    """(socket, None) or (None, HTTP status or error name); `how` is "header" or "subprotocol"."""
    try:
        from websockets.asyncio.client import connect
        headers_kw = "additional_headers"
    except ImportError:  # websockets < 13
        from websockets import connect  # type: ignore[no-redef]
        headers_kw = "extra_headers"
    kwargs: Dict[str, Any] = {"max_size": 16 * 1024 * 1024, "open_timeout": 15}
    if how == "header":
        kwargs[headers_kw] = {"Authorization": f"Bearer {token}"}
    else:
        kwargs["subprotocols"] = [f"xai-client-secret.{token}"]
    try:
        return await connect(url, **kwargs), None
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
        return None, status or exc.__class__.__name__


class Call:
    """One realtime call: a reader task, plus turns asked by text."""

    def __init__(self, socket: Any, t0: float):
        self.socket = socket
        self.t0 = t0
        self.events: List[Dict[str, Any]] = []
        self.arrived = asyncio.Event()
        self.closed: Optional[str] = None
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            async for message in self.socket:
                if isinstance(message, (bytes, bytearray)):
                    continue
                try:
                    event = json.loads(message)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.events.append(event)
                    self.arrived.set()
        except Exception as exc:
            self.closed = exc.__class__.__name__
        code = getattr(self.socket, "close_code", None)
        reason = getattr(self.socket, "close_reason", None) or ""
        self.closed = f"{self.closed or 'closed'} code={code} {reason[:120]}".strip()
        self.arrived.set()

    async def send(self, event: Dict[str, Any]) -> None:
        await self.socket.send(json.dumps(event))

    async def wait_for(self, kinds: Tuple[str, ...], timeout: float, start: int = 0) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        seen = start
        while True:
            for event in self.events[seen:]:
                seen += 1
                if event.get("type") in kinds:
                    return event
            if self.closed:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.arrived.clear()
            try:
                await asyncio.wait_for(self.arrived.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def turn(self, text: str) -> Dict[str, Any]:
        start = len(self.events)
        asked = time.monotonic()
        await self.send({"type": "conversation.item.create",
                         "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        await self.send({"type": "response.create"})
        done = await self.wait_for(("response.done", "error"), TURN_TIMEOUT_S, start)
        audio = 0
        first_audio: Optional[float] = None
        transcript: List[str] = []
        errors: List[str] = []
        for event in self.events[start:]:
            kind = event.get("type")
            if kind in ("response.output_audio.delta", "response.audio.delta"):
                size = len(base64.b64decode(str(event.get("delta") or ""), validate=False))
                audio += size
                if size and first_audio is None:
                    first_audio = round(time.monotonic() - asked, 2)
            elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                transcript.append(str(event.get("delta") or ""))
            elif kind == "error":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                errors.append(f"{error.get('code')} {str(error.get('message') or '')[:160]}")
        return {
            "ok": bool(done and done.get("type") == "response.done" and audio),
            "first_audio_s": first_audio,
            "audio_s": round(audio / (RATE * 2), 1),
            "transcript": "".join(transcript)[:200],
            "errors": errors,
            "closed": self.closed,
        }

    async def close(self) -> None:
        try:
            await self.socket.close()
        except Exception:
            pass
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


def mark(t0: float, text: str) -> None:
    print(f"  [{time.monotonic() - t0:6.1f}s] {text}")


async def run(args, api) -> Dict[str, Any]:
    t0 = time.monotonic()
    summary: Dict[str, Any] = {}
    try:
        bearer, auth = await asyncio.to_thread(scoped, api, args.profile, api.grok_live_credentials)
        model, voice = await asyncio.to_thread(scoped, api, args.profile, api.grok_live_model_voice)
    except Exception as exc:
        # TokenError texts are the plugin's own user-facing messages.
        summary["error"] = str(exc)[:200] if isinstance(exc, api.TokenError) else exc.__class__.__name__
        return summary
    summary.update(auth=auth, model=model, voice=voice, expires_requested_s=args.expires)
    mark(t0, f"xAI sign-in: {auth}")

    minted = await asyncio.to_thread(mint, args.api_base, bearer, args.expires)
    token = minted.pop("token", "")
    summary["mint"] = minted
    if not token:
        mark(t0, f"mint refused: HTTP {minted.get('status')} {minted.get('error', '')}")
        return summary
    minted_at = time.monotonic()
    mark(t0, f"token minted in {minted.get('ms')} ms, expires_at={minted.get('expires_at')}")

    ws_base = args.api_base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = f"{ws_base}/realtime?{urllib.parse.urlencode({'model': model})}"
    socket, refused = await open_socket(url, token, "header")
    summary["connect_header"] = "ok" if socket else refused
    if socket is None:
        socket, refused = await open_socket(url, token, "subprotocol")
        summary["connect_subprotocol"] = "ok" if socket else refused
    if socket is None:
        mark(t0, "the token did not open a call")
        return summary
    mark(t0, f"call open ({'header' if summary['connect_header'] == 'ok' else 'subprotocol'})")
    call = Call(socket, t0)
    try:
        await call.send({"type": "session.update", "session": {
            "instructions": "You are a voice assistant being tested. Answer briefly.",
            "voice": voice,
            "reasoning": {"effort": "none"},
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 700, "prefix_padding_ms": 300},
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE}},
                      "output": {"format": {"type": "audio/pcm", "rate": RATE}}},
            # One tool like the Watch's, so a token that refuses tools shows up here.
            "tools": [{"type": "function", "name": "start_job", "description": "Hand a task to Hermes.",
                       "parameters": {"type": "object", "properties": {"task": {"type": "string"}},
                                      "required": ["task"]}}],
            "tool_choice": "auto",
        }})
        updated = await call.wait_for(("session.updated", "error"), 10)
        summary["session_updated"] = bool(updated and updated.get("type") == "session.updated")
        mark(t0, f"session.update {'applied' if summary['session_updated'] else 'not applied'}")

        summary["first_turn"] = await call.turn(FIRST_TURN)
        mark(t0, f"first turn: {summary['first_turn']['transcript']!r}")

        # 4a. The same token again while the call is open.
        second, refused = await open_socket(url, token, "header")
        summary["reuse_while_open"] = "accepted" if second else refused
        if second is not None:
            await second.close()
        mark(t0, f"same token, second call: {summary['reuse_while_open']}")

        # 3. Hold the call past the token's expiry, streaming silence like a mic.
        hold_until = minted_at + args.expires + 20
        mark(t0, f"holding the call {max(0, hold_until - time.monotonic()):.0f} s, past the token's expiry")
        while time.monotonic() < hold_until and not call.closed:
            try:
                await call.send({"type": "input_audio_buffer.append", "audio": SILENCE})
            except Exception:
                await call.wait_for((), 2)  # lets the reader record how it closed
                break
            await asyncio.sleep(1)
        summary["closed_during_hold"] = call.closed
        if call.closed:
            mark(t0, f"call closed during the hold: {call.closed}")
        if not call.closed:
            summary["turn_after_expiry"] = await call.turn(LATER_TURN)
            mark(t0, f"after expiry: {summary['turn_after_expiry']['transcript']!r}")

        # 4b. The expired token, for a new call.
        late, refused = await open_socket(url, token, "header")
        summary["reconnect_after_expiry"] = "accepted" if late else refused
        if late is not None:
            await late.close()
        mark(t0, f"expired token, new call: {summary['reconnect_after_expiry']}")
    finally:
        await call.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--expires", type=int, default=60, help="the token's lifetime in seconds")
    parser.add_argument("--profile", help="Hermes profile whose xAI sign-in and Grok settings to use")
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 10 <= args.expires <= 3600:
        parser.error("--expires must be between 10 and 3600 seconds")
    api = load_plugin_api()
    summary = asyncio.run(run(args, api))
    print("\nSummary (paste this back into the thread):")
    print(json.dumps(summary, indent=2))
    ok = summary.get("session_updated") and (summary.get("first_turn") or {}).get("ok")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
