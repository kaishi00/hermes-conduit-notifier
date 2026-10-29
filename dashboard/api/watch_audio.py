"""Watch audio: GPT-Live and Grok on a Watch call, the helper's Python, the
sessions and the bridge.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
import contextlib
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core, gpt_live, grok_live, local_live, speech_server, watch_local_live, watch_tools

logger = core.logger
router = core.router


# --- Watch audio: GPT-Live and Grok on a Conduit Watch call -------------------
#
# GPT-Live on the ChatGPT subscription speaks only WebRTC, which the Watch
# can't run, and Grok runs on this host's xAI sign-in, which never leaves it.
# So for those two engines the host holds the provider session and the Watch
# streams to it through the push relay (hermes-conduit
# designs/apple-watch-gpt-live.md, "Bridge design"). A grant opened with
# "audio": true gets a bridge: this host dials the relay's
# /v1/watch-audio/{grant}/host and waits, and the Watch dials .../watch with
# the grant's relay key. The relay pairs the two sockets and copies messages
# (relay/src/watch-audio.mjs). Every message is sealed here and on the Watch
# with a per-stream key from the grant's root, so the relay can neither read
# nor forge one.
#
# Binary messages:
#   [0x00, n]                  the relay's notice: 1 Watch connected, 2 Watch gone
#   [0x04][sid 16][version 1]  the Watch's hello, in clear, first on each connection
#   [type][counter 8 BE][ct]   sealed: 1 audio (PCM16LE mono), 2 engine event
#                              (the engine's own JSON text), 3 control (JSON)
# Keys: HKDF-SHA256 of the grant root, salt conduit-watch-audio-v1, info
# "conduit-watch-audio-v1 {watch-to-host|host-to-watch} stream={sid}", the
# sid in base64url. Nonce: 4 zero bytes and the counter, which starts at 0
# for each stream and direction and must rise. The AAD names the direction,
# grant, stream and type. A stream id is taken once per grant: its keys
# would repeat nonces.
#
# Control, Watch to host: {"type": "start", "engine": "gpt_live" | "grok" | "local_live",
# "voice"?, "briefing"?, "greeting"?, "call_opening"?, "history"?, "resume_session_id"?}
# and {"type": "end"}. Host to Watch: "started" (engine, model, voice,
# input_rate, output_rate; for GPT-Live also briefing_applied and
# greeting_applied: true whenever the model was told to speak first, for a
# greeting or call_opening; for Local Live session_id, the call's chat, and
# resumed), "ended" (reason) and
# "error" (code, message). The Watch waits for "started" before it sends
# audio or events. It owns the conversation, as the phone does: it speaks each
# engine's own events (GPT-Live's frameless ones, Grok's realtime ones), and
# its tools and jobs go through the grant's lookups (Local Live's turns are
# Hermes' own, run here: watch_local_live.py). The host adds only
# what has to stay here: the sign-ins, Grok's pacing, and WebRTC for GPT-Live,
# which runs in watch_audio_helper.py under a Python with aiortc: this
# plugin's own environment, made when Conduit asks (/watch-audio/prepare),
# or Hermes' own Python when it already has aiortc.

WATCH_AUDIO_VERSION = 1
WATCH_AUDIO_ENGINES = ("gpt_live", "grok", "local_live")
WATCH_AUDIO_UP = "watch-to-host"
WATCH_AUDIO_DOWN = "host-to-watch"
WATCH_AUDIO_AUDIO = 1
WATCH_AUDIO_EVENT = 2
WATCH_AUDIO_CONTROL = 3
WATCH_AUDIO_HELLO = 4
WATCH_AUDIO_NOTICE_CONNECTED = 1
WATCH_AUDIO_NOTICE_GONE = 2
_WATCH_AUDIO_SALT = b"conduit-watch-audio-v1"
_WATCH_AUDIO_AAD_TAG = "conduit-watch-audio/1"
# The relay's bound on one message (relay/src/watch-audio.mjs MAX_MESSAGE_BYTES).
WATCH_AUDIO_MAX_MESSAGE_BYTES = 64 * 1024
# What fits in one sealed message: the type, counter and tag go around it.
WATCH_AUDIO_MAX_PLAIN_BYTES = WATCH_AUDIO_MAX_MESSAGE_BYTES - 1 - 8 - 16
# Streams one grant may open: a call and its rejoins.
WATCH_AUDIO_STREAMS_PER_GRANT = 32
# Hellos past that told why they were refused (each takes a fresh id); the
# rest only get the log line.
WATCH_AUDIO_REFUSALS_PER_GRANT = 4
# Engine sessions this host holds for Watches at once, every profile together.
WATCH_AUDIO_MAX_SESSIONS = 4
WATCH_AUDIO_RELAY_TIMEOUT_S = 15.0
WATCH_AUDIO_RECONNECT_MAX_S = 10.0
# A host socket the relay drops sooner than this (another host took the
# grant's socket, say) reconnects with backoff rather than at once.
WATCH_AUDIO_STABLE_S = 5.0
# A session that won't stop when asked is cancelled after this.
WATCH_AUDIO_STOP_TIMEOUT_S = 5.0
# Grok and Local Live: a provider that leaves a Watch event unanswered this
# long is hung (each event gets one back: session.updated, response.created,
# conversation.item.created…). Its session ends with "unreachable", which
# frees the slot, and the Watch reconnects. A quiet stretch with nothing
# asked is a pause in the conversation, and a dead connection is the
# socket's own keepalive pings' to find.
WATCH_AUDIO_UPSTREAM_QUIET_S = 60.0
WATCH_AUDIO_UPSTREAM_QUIET_CHECK_S = 5.0
# An event type fit for the log line when one goes unanswered.
_WATCH_AUDIO_EVENT_TYPE = re.compile(r"[A-Za-z0-9_.]{1,64}")
# GPT-Live: the helper's offer (ICE gathering included), then WebRTC
# connecting after the answer. Past that, one more try with STUN: host
# candidates alone usually reach OpenAI's public media servers, and skip
# STUN's gathering wait.
WATCH_AUDIO_OFFER_TIMEOUT_S = 20.0
WATCH_AUDIO_CONNECT_TIMEOUT_S = 8.0
WATCH_AUDIO_STUN = ("stun:stun.l.google.com:19302",)
WATCH_AUDIO_HELPER_EXIT_S = 3.0
# The helper's frames (watch_audio_helper.py MAX_FRAME).
WATCH_AUDIO_HELPER_MAX_FRAME = 1024 * 1024
GPT_LIVE_WATCH_INPUT_RATE = 16_000
GPT_LIVE_WATCH_OUTPUT_RATE = 24_000
# Conduit's phone Grok uses these too (GrokLiveProtocol.swift).
GROK_WATCH_RATE = 24_000
# Grok sends audio faster than real time: the Watch gets at most this much
# ahead of real time, which keeps the relay's rate limit and the Watch's
# buffer in bounds. Audio goes out in pieces of WATCH_AUDIO_CHUNK_S.
WATCH_AUDIO_LEAD_S = 1.5
WATCH_AUDIO_CHUNK_S = 0.1
# Audio held for pacing past this (a stalled Watch) drops its oldest.
WATCH_AUDIO_MAX_QUEUED_S = 60.0
# Events held behind that audio past this many bytes drop their oldest too.
WATCH_AUDIO_MAX_QUEUED_EVENT_BYTES = 1024 * 1024
# Audio a GPT-Live helper sends before "started" is out, held until it is.
WATCH_AUDIO_MAX_HELD = 200
WATCH_AUDIO_AIORTC = "aiortc==1.15.0"
WATCH_AUDIO_ENV_VAR = "CONDUIT_WATCH_AUDIO_ENV"
_WATCH_AUDIO_MARKER = "conduit-watch-audio.json"
WATCH_AUDIO_VENV_TIMEOUT_S = 180.0
WATCH_AUDIO_PIP_TIMEOUT_S = 900.0
_WATCH_AUDIO_HELPER = os.path.join(core._dashboard_dir(), "watch_audio_helper.py")
# What the helper gets of Hermes' environment: enough to start Python and
# find the system's libraries, never the keys Hermes may hold there.
_WATCH_AUDIO_HELPER_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "TEMP", "TMP",
                           "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
                           "DYLD_FALLBACK_LIBRARY_PATH")
_GROK_AUDIO_DELTAS = ("response.output_audio.delta", "response.audio.delta")
WATCH_AUDIO_NEEDS_RUNTIME = ("GPT-Live on the Watch needs its WebRTC runtime on the Hermes host; "
                             "prepare it from Conduit's Voice settings")
WATCH_AUDIO_STALE_RUNTIME = ("GPT-Live on the Watch needs its WebRTC runtime made again for this plugin version; "
                             "prepare it from Conduit's Voice settings")


class WatchAudioError(Exception):
    """A Watch audio message that can't be opened or built."""


class WatchAudioFailure(Exception):
    """Why a Watch audio session couldn't start or ended badly; ``code`` is for the Watch to branch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _watch_audio_failure(exc: core.TokenError) -> WatchAudioFailure:
    codes = {400: "bad_request", 429: "rate_limited", 501: "unavailable", 503: "unavailable",
             502: "refused", 504: "unreachable"}
    return WatchAudioFailure(codes.get(exc.status, "failed"), str(exc))


def watch_audio_keys(secret: bytes, sid: bytes) -> Dict[str, bytes]:
    """A stream's two directional keys, from the grant's 32-byte root."""
    if len(secret) != 32:
        raise WatchAudioError("the grant secret must be 32 bytes")
    if len(sid) != 16:
        raise WatchAudioError("the stream id must be 16 bytes")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    stream = watch_tools._b64u(sid)
    return {
        direction: HKDF(algorithm=hashes.SHA256(), length=32, salt=_WATCH_AUDIO_SALT,
                        info=f"conduit-watch-audio-v1 {direction} stream={stream}".encode("ascii")).derive(secret)
        for direction in (WATCH_AUDIO_UP, WATCH_AUDIO_DOWN)
    }


def watch_audio_aad(direction: str, grant_id: str, sid: bytes, kind: int) -> bytes:
    return "\n".join([_WATCH_AUDIO_AAD_TAG, direction, f"grant={grant_id}", f"sid={watch_tools._b64u(sid)}",
                      f"type={kind}"]).encode("ascii")


def _watch_audio_nonce(counter: int) -> bytes:
    return b"\0\0\0\0" + counter.to_bytes(8, "big")


def seal_watch_audio(key: bytes, direction: str, grant_id: str, sid: bytes, kind: int, counter: int,
                     plain: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if kind not in (WATCH_AUDIO_AUDIO, WATCH_AUDIO_EVENT, WATCH_AUDIO_CONTROL):
        raise WatchAudioError("unknown message type")
    if len(plain) > WATCH_AUDIO_MAX_PLAIN_BYTES:
        raise WatchAudioError("the message is too large")
    if not 0 <= counter < 2 ** 64:
        raise WatchAudioError("the counter is out of range")
    ciphertext = ChaCha20Poly1305(key).encrypt(_watch_audio_nonce(counter), bytes(plain),
                                               watch_audio_aad(direction, grant_id, sid, kind))
    return bytes([kind]) + counter.to_bytes(8, "big") + ciphertext


def open_watch_audio(key: bytes, direction: str, grant_id: str, sid: bytes,
                     message: bytes) -> Tuple[int, int, bytes]:
    """(type, counter, plaintext) of one sealed message; WatchAudioError when it doesn't verify."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if not 1 + 8 + 16 <= len(message) <= WATCH_AUDIO_MAX_MESSAGE_BYTES:
        raise WatchAudioError("the message is malformed")
    kind = message[0]
    if kind not in (WATCH_AUDIO_AUDIO, WATCH_AUDIO_EVENT, WATCH_AUDIO_CONTROL):
        raise WatchAudioError("unknown message type")
    counter = int.from_bytes(message[1:9], "big")
    try:
        plain = ChaCha20Poly1305(key).decrypt(_watch_audio_nonce(counter), bytes(message[9:]),
                                              watch_audio_aad(direction, grant_id, sid, kind))
    except Exception as error:
        raise WatchAudioError("the message did not verify") from error
    return kind, counter, plain


class _WatchAudioStream:
    """One Watch connection: its keys and both counters."""

    def __init__(self, grant_id: str, secret: bytes, sid: bytes) -> None:
        keys = watch_audio_keys(secret, sid)
        self.grant_id = grant_id
        self.sid = sid
        self.up_key = keys[WATCH_AUDIO_UP]
        self.down_key = keys[WATCH_AUDIO_DOWN]
        self.received = -1
        self.sent = 0

    def open(self, message: bytes) -> Tuple[int, bytes]:
        kind, counter, plain = open_watch_audio(self.up_key, WATCH_AUDIO_UP, self.grant_id, self.sid, message)
        # Checked once it verifies, so a forged counter can't move the window.
        if counter <= self.received:
            raise WatchAudioError("the message was replayed")
        self.received = counter
        return kind, plain

    def seal(self, kind: int, plain: bytes) -> bytes:
        sealed = seal_watch_audio(self.down_key, WATCH_AUDIO_DOWN, self.grant_id, self.sid, kind, self.sent, plain)
        self.sent += 1
        return sealed


def _watch_audio_event_text(text: str) -> bytes:
    """An engine event for the Watch; one too large to seal goes as its type alone."""
    data = text.encode("utf-8")
    if len(data) <= WATCH_AUDIO_MAX_PLAIN_BYTES:
        return data
    try:
        kind = json.loads(text).get("type")
    except (ValueError, AttributeError):
        kind = None
    return json.dumps({"type": kind if isinstance(kind, str) else "unknown", "conduit_truncated": True}).encode("utf-8")


# --- The helper's Python ----------------------------------------------------


def _watch_audio_env_dir() -> str:
    """Where this plugin keeps its own Python for GPT-Live's WebRTC: one per
    host, beside Hermes' home rather than inside a profile."""
    override = str(os.environ.get(WATCH_AUDIO_ENV_VAR) or "").strip()
    if override:
        return override
    home = str(os.environ.get("HERMES_HOME") or "").strip() or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, "conduit_push", "watch-audio-env")


def _env_python(env_dir: str) -> str:
    if os.name == "nt":
        return os.path.join(env_dir, "Scripts", "python.exe")
    return os.path.join(env_dir, "bin", "python")


class _RuntimeBuildFailed(Exception):
    """Making the helper's Python failed; the message says why, for the user."""


def _last_line(text: Any) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _watch_audio_marker_pin(env_dir: str) -> Optional[str]:
    """The aiortc pin the plugin's environment was made for; None without a readable marker."""
    try:
        with open(os.path.join(env_dir, _WATCH_AUDIO_MARKER), encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return None
    pin = marker.get("requirement") if isinstance(marker, dict) else None
    return pin if isinstance(pin, str) else None


def _watch_audio_marker_current(env_dir: str) -> bool:
    """Whether the plugin's environment was made for this plugin's aiortc pin.

    An upgrade that moves the pin leaves the old environment unready, so
    Conduit's prepare makes it again.
    """
    return _watch_audio_marker_pin(env_dir) == WATCH_AUDIO_AIORTC


def _watch_audio_helper_env(extra: Tuple[str, ...] = ()) -> Dict[str, str]:
    names = frozenset(_WATCH_AUDIO_HELPER_ENV + extra)
    return {name: value for name, value in os.environ.items() if name in names or name.startswith("LC_")}


class _WatchAudioRuntime:
    """The Python that runs watch_audio_helper.py, and making one.

    Any Hermes install works, whatever Python it runs on and however its
    packages got there: the plugin makes a small environment of its own with
    aiortc, from the Python Hermes runs (or uv), and never installs into
    Hermes'. It is made when Conduit asks, never in the middle of a call.
    """

    def __init__(self, env_dir: Optional[Callable[[], str]] = None, run: Optional[Callable[..., Any]] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None,
                 has_module: Optional[Callable[[str], bool]] = None,
                 base_python: Optional[str] = None) -> None:
        self._env_dir = env_dir or _watch_audio_env_dir
        self._run = run or subprocess.run
        self._which = which or shutil.which
        self._has_module = has_module or _has_module
        self._base_python = base_python or sys.executable
        self._lock = threading.Lock()
        self.preparing = False
        self.failure: Optional[str] = None

    def python(self) -> Optional[Tuple[str, Dict[str, str], Tuple[str, ...], str]]:
        """(python, environment, flags, source) for the helper, or None when no Python here has aiortc."""
        env_dir = self._env_dir()
        python = _env_python(env_dir)
        if os.path.isfile(os.path.join(env_dir, _WATCH_AUDIO_MARKER)):
            # The plugin's own environment wins once made; one made for
            # another pin is unready until prepare makes it again.
            if _watch_audio_marker_current(env_dir) and os.path.isfile(python):
                return python, _watch_audio_helper_env(), ("-I",), "plugin"
            return None
        if self._has_module("aiortc") and self._has_module("av"):
            # Some installs add Hermes' packages to sys.path at start, so the
            # helper gets this process's import path. PYTHONHOME too, where
            # a bundled Python needs it to start.
            env = _watch_audio_helper_env(("PYTHONHOME",))
            env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
            return self._base_python, env, (), "hermes"
        return None

    def status(self) -> Dict[str, Any]:
        found = self.python()
        with self._lock:
            preparing, failure = self.preparing, self.failure
        if found is not None:
            return {"runtime": "ready", "source": found[3], "reason": None}
        if preparing:
            return {"runtime": "preparing", "source": None, "reason": None}
        if failure:
            return {"runtime": "failed", "source": None, "reason": failure}
        # The failure snapshot above already said there's none.
        return {"runtime": "missing", "source": None, "reason": self._marker_reason()}

    def missing_reason(self) -> str:
        """Why there's no runtime: a failed prepare's own reason, else whether one was ever made."""
        with self._lock:
            failure = self.failure
        return failure or self._marker_reason()

    def _marker_reason(self) -> str:
        pin = _watch_audio_marker_pin(self._env_dir())
        return WATCH_AUDIO_STALE_RUNTIME if pin is not None and pin != WATCH_AUDIO_AIORTC else WATCH_AUDIO_NEEDS_RUNTIME

    def prepare(self, start: Optional[Callable[[Callable[[], None]], None]] = None) -> Dict[str, Any]:
        """Starts making the environment unless it's there or on its way; returns the status."""
        if self.python() is not None:
            return self.status()
        with self._lock:
            if self.preparing:
                return {"runtime": "preparing", "source": None, "reason": None}
            self.preparing = True
            self.failure = None
        try:
            (start or _start_daemon)(self._prepare)
        except Exception as exc:  # noqa: BLE001 — e.g. no thread to spare
            with self._lock:
                self.preparing = False
                self.failure = f"Couldn't start preparing on the host ({type(exc).__name__})"
        return self.status()

    def _prepare(self) -> None:
        failure: Optional[str] = None
        try:
            self.build()
        except _RuntimeBuildFailed as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 — reported on the status
            failure = f"Preparing failed on the host ({type(exc).__name__})"
        if failure:
            logger.warning("Preparing GPT-Live for the Watch failed: %s", failure)
        else:
            logger.info("GPT-Live for the Watch is ready")
        with self._lock:
            self.preparing = False
            self.failure = failure

    def _step(self, command: list, timeout: float, what: str) -> str:
        # Hermes points PYTHONPATH at its own site-packages, so pip (and uv)
        # would count Hermes' cffi and cryptography as installed and leave
        # them out of an environment the helper runs isolated (#340).
        env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
        try:
            done = self._run(command, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise _RuntimeBuildFailed(f"{what} timed out")
        except OSError as exc:
            raise _RuntimeBuildFailed(f"{what} couldn't run ({type(exc).__name__})")
        if done.returncode != 0:
            detail = _last_line(done.stderr) or _last_line(done.stdout)
            raise _RuntimeBuildFailed(f"{what} failed: {detail}" if detail else f"{what} failed")
        return str(done.stdout or "")

    def build(self) -> None:
        env_dir = self._env_dir()
        python = _env_python(env_dir)
        if os.path.isdir(env_dir):
            # Left by an earlier try: only ever a folder this made.
            if os.listdir(env_dir) and not os.path.isfile(os.path.join(env_dir, "pyvenv.cfg")):
                raise _RuntimeBuildFailed(f"{env_dir} exists and isn't a Python environment")
            shutil.rmtree(env_dir)
        os.makedirs(os.path.dirname(env_dir), exist_ok=True)
        uv = self._which("uv")
        try:
            self._step([self._base_python, "-m", "venv", env_dir], WATCH_AUDIO_VENV_TIMEOUT_S, "Making the environment")
            self._step([python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                        "--no-warn-script-location", WATCH_AUDIO_AIORTC], WATCH_AUDIO_PIP_TIMEOUT_S, "Installing aiortc")
        except _RuntimeBuildFailed as first:
            # A Python without venv or pip (a distribution's own, say): uv
            # brings both, when the host has it.
            if not uv:
                raise _RuntimeBuildFailed(f"{first}. Installing uv on the host lets the plugin try again with it")
            shutil.rmtree(env_dir, ignore_errors=True)
            self._step([uv, "venv", "--python", self._base_python, env_dir], WATCH_AUDIO_VENV_TIMEOUT_S,
                       "Making the environment with uv")
            self._step([uv, "pip", "install", "--python", python, WATCH_AUDIO_AIORTC], WATCH_AUDIO_PIP_TIMEOUT_S,
                       "Installing aiortc with uv")
        version = self._step([python, "-I", "-c", "import aiortc, av; print(aiortc.__version__)"], 60.0,
                             "Checking aiortc").strip()
        with open(os.path.join(env_dir, _WATCH_AUDIO_MARKER), "w", encoding="utf-8") as handle:
            json.dump({"aiortc": version, "requirement": WATCH_AUDIO_AIORTC}, handle)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="conduit-watch-audio-prepare", daemon=True).start()


_watch_audio_runtime = _WatchAudioRuntime()


class _WatchAudioSlots:
    """Engine sessions for Watches on this host, every grant together."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def release(self) -> None:
        with self._lock:
            self.used = max(0, self.used - 1)


_watch_audio_slots = _WatchAudioSlots(WATCH_AUDIO_MAX_SESSIONS)


async def _first_done(timeout: Optional[float], *awaitables: Any) -> set:
    """Waits for the first of ``awaitables`` (futures or coroutines); returns the finished ones."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return done
    finally:
        for task, item in zip(tasks, awaitables):
            # Only what this made: a future it was handed is the caller's.
            if task is not item and not task.done():
                task.cancel()


# --- Sessions -----------------------------------------------------------------


class _WatchAudioSession:
    """One engine session for one Watch stream, from its start to its end."""

    engine = ""

    def __init__(self, bridge: "_WatchAudioBridge", stream: _WatchAudioStream, request: Dict[str, Any]) -> None:
        self.bridge = bridge
        self.stream = stream
        self.request = request
        self.profile = bridge.grant.profile
        self.stopping = asyncio.Event()
        self.reason = "ended"
        # When the provider was sent the oldest event it hasn't answered, and its type.
        self.owed_since: Optional[float] = None
        self.owed_event = ""

    def asked(self, text: str = "") -> None:
        """An event went to the provider: it owes an answer."""
        if self.owed_since is None:
            self.owed_since = self.bridge.clock()
            try:
                kind = json.loads(text).get("type")
            except (ValueError, AttributeError):
                kind = None
            self.owed_event = kind if isinstance(kind, str) and _WATCH_AUDIO_EVENT_TYPE.fullmatch(kind) else "?"

    def answered(self) -> None:
        self.owed_since = None

    async def send_asking(self, upstream: Any, text: str) -> None:
        """Sends a Watch event to the provider, which then owes an answer.
        Armed first: the answer can arrive while the send is still awaited."""
        armed = self.owed_since is None
        self.asked(text)
        try:
            await upstream.send(text)
        except Exception:  # noqa: BLE001 — a dropped provider; its reader ends the session
            if armed:
                self.answered()

    def waiting_on_hermes(self) -> bool:
        """Whether the provider's answer waits on a Hermes turn this host runs."""
        return False

    def overdue(self) -> bool:
        owed = self.owed_since
        return owed is not None and self.bridge.clock() - owed >= WATCH_AUDIO_UPSTREAM_QUIET_S

    async def _hung(self) -> None:
        """Returns once the provider has owed an answer for WATCH_AUDIO_UPSTREAM_QUIET_S."""
        while True:
            if self.owed_since is not None and self.waiting_on_hermes():
                # A slow turn isn't a hung provider: the wait starts over after it.
                self.owed_since = self.bridge.clock()
            if self.overdue():
                return
            await asyncio.sleep(WATCH_AUDIO_UPSTREAM_QUIET_CHECK_S)

    async def _wait_or_hang(self, reader: asyncio.Future, tasks: list) -> bool:
        """Until a stop, the reader's end or a hung provider (True). Each
        watch it starts goes in ``tasks``, for the caller to cancel."""
        while True:
            hung = asyncio.ensure_future(self._hung())
            tasks.append(hung)
            await _first_done(None, self.stopping.wait(), reader, hung)
            if reader.done() or self.stopping.is_set():
                return False
            if self.overdue():
                return True
            # Answered in the moment since it looked: the wait goes on.

    def stop(self, reason: str) -> None:
        if not self.stopping.is_set():
            self.reason = reason
            self.stopping.set()

    async def send(self, kind: int, plain: bytes) -> bool:
        return await self.bridge.send(self.stream, kind, plain)

    async def run(self) -> None:
        raise NotImplementedError

    async def audio(self, pcm: bytes) -> None:
        raise NotImplementedError

    async def event(self, data: bytes) -> None:
        raise NotImplementedError


class _GptLiveHelper:
    """One run of watch_audio_helper.py."""

    def __init__(self, session: "_GptLiveWatchSession") -> None:
        self.session = session
        self.process: Any = None
        self.offer: asyncio.Future = asyncio.get_running_loop().create_future()
        self.connected = asyncio.Event()
        self.ended = asyncio.Event()
        self.reason: Optional[str] = None
        self.lock = asyncio.Lock()
        self.tasks: list = []

    async def start(self, runtime: Tuple[str, Dict[str, str], Tuple[str, ...], str], stun: Tuple[str, ...]) -> None:
        python, env, flags, _ = runtime
        self.process = await asyncio.create_subprocess_exec(
            python, *flags, _WATCH_AUDIO_HELPER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        self.tasks = [asyncio.ensure_future(self._read()), asyncio.ensure_future(self._log_errors())]
        await self.write("C", json.dumps({"input_rate": GPT_LIVE_WATCH_INPUT_RATE,
                                          "output_rate": GPT_LIVE_WATCH_OUTPUT_RATE, "stun": list(stun)}).encode("utf-8"))

    async def write(self, kind: str, payload: bytes) -> None:
        if self.process is None or self.ended.is_set():
            return
        async with self.lock:
            try:
                self.process.stdin.write(kind.encode("ascii") + len(payload).to_bytes(4, "big") + payload)
                await self.process.stdin.drain()
            except Exception:  # noqa: BLE001 — a helper that exited
                self._end("the WebRTC helper stopped")

    def _end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.ended.set()
        if not self.offer.done():
            self.offer.cancel()

    async def _read(self) -> None:
        stdout = self.process.stdout
        try:
            while True:
                header = await stdout.readexactly(5)
                kind, length = chr(header[0]), int.from_bytes(header[1:], "big")
                if kind not in "ORpeX" or length > WATCH_AUDIO_HELPER_MAX_FRAME:
                    self._end("the WebRTC helper sent a bad frame")
                    return
                payload = await stdout.readexactly(length)
                if kind == "O":
                    if not self.offer.done():
                        self.offer.set_result(payload.decode("utf-8", "replace"))
                elif kind == "R":
                    self.connected.set()
                elif kind == "p":
                    await self.session.forward(WATCH_AUDIO_AUDIO, payload)
                elif kind == "e":
                    await self.session.forward(WATCH_AUDIO_EVENT,
                                               _watch_audio_event_text(payload.decode("utf-8", "replace")))
                else:
                    try:
                        reason = json.loads(payload or b"{}").get("reason")
                    except (ValueError, AttributeError):
                        reason = None
                    self._end(str(reason or "GPT-Live ended the call")[:200])
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            self._end("the WebRTC helper exited")

    async def _log_errors(self) -> None:
        # The helper writes one short line per problem, never audio or SDP.
        try:
            async for line in self.process.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    logger.info("GPT-Live Watch helper: %s", text[:200])
        except Exception:  # noqa: BLE001 — only logging
            return

    async def close(self) -> None:
        if self.process is None:
            return
        await self.write("Q", b"")
        with contextlib.suppress(Exception):
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), WATCH_AUDIO_HELPER_EXIT_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            with contextlib.suppress(Exception):
                await self.process.wait()
        self._end(self.reason or "closed")
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class _GptLiveWatchSession(_WatchAudioSession):
    """GPT-Live through a WebRTC helper: the plugin does the SDP exchange, so
    the Codex sign-in never enters the helper."""

    engine = "gpt_live"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper: Optional[_GptLiveHelper] = None
        self.answer: Dict[str, Any] = {}
        self.live = False
        self.held: list = []

    async def forward(self, kind: int, plain: bytes) -> None:
        if not self.live:
            if len(self.held) < WATCH_AUDIO_MAX_HELD:
                self.held.append((kind, plain))
            return
        await self.send(kind, plain)

    async def run(self) -> None:
        runtime = self.bridge.runtime.python()
        if runtime is None:
            raise WatchAudioFailure("unavailable", self.bridge.runtime.missing_reason())
        for stun in ((), WATCH_AUDIO_STUN):
            helper = _GptLiveHelper(self)
            self.helper = helper
            try:
                await helper.start(runtime, stun)
                if await self._connect(helper, retry=not stun):
                    await self._started(helper)
                    await _first_done(None, self.stopping.wait(), helper.ended.wait())
                    if helper.ended.is_set() and not self.stopping.is_set():
                        self.reason = helper.reason or "GPT-Live ended the call"
                    return
                if self.stopping.is_set():
                    return
            finally:
                self.live = False
                await helper.close()
        raise WatchAudioFailure("unreachable", "GPT-Live's audio connection didn't come up from the Hermes host")

    async def _connect(self, helper: _GptLiveHelper, *, retry: bool) -> bool:
        """True once WebRTC is up; False to try again with STUN (or when stopped)."""
        await _first_done(WATCH_AUDIO_OFFER_TIMEOUT_S, helper.offer, self.stopping.wait(), helper.ended.wait())
        if self.stopping.is_set():
            return False
        if not helper.offer.done() or helper.offer.cancelled():
            if helper.ended.is_set():
                raise WatchAudioFailure("failed", f"GPT-Live failed on the host ({helper.reason})")
            raise WatchAudioFailure("failed", "The WebRTC helper didn't make an offer")
        offer = helper.offer.result()
        request = self.request
        profile = self.profile
        try:
            answer = await asyncio.wait_for(core._run_scoped(profile, lambda: gpt_live.create_gpt_live_session(
                offer, request.get("history"), limiter_key=core._limiter_key(profile), voice=request.get("voice"),
                briefing=request.get("briefing"), greeting=request.get("greeting"),
                call_opening=request.get("call_opening")), gpt_live._gpt_live_executor),
                timeout=gpt_live.GPT_LIVE_REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise WatchAudioFailure("unreachable", "GPT-Live timed out starting the call")
        except core.TokenError as exc:
            raise _watch_audio_failure(exc)
        self.answer = answer
        await helper.write("A", answer["transport"]["sdp"].encode("utf-8"))
        await _first_done(WATCH_AUDIO_CONNECT_TIMEOUT_S, helper.connected.wait(), self.stopping.wait(),
                          helper.ended.wait())
        if helper.connected.is_set():
            return True
        if self.stopping.is_set():
            return False
        if helper.ended.is_set():
            raise WatchAudioFailure("failed", f"GPT-Live failed on the host ({helper.reason})")
        if retry:
            logger.info("GPT-Live for the Watch didn't connect with host candidates; trying STUN")
            return False
        raise WatchAudioFailure("unreachable", "GPT-Live's audio connection didn't come up from the Hermes host")

    async def _started(self, helper: _GptLiveHelper) -> None:
        answer = self.answer
        await self.bridge.control(self.stream, {
            "type": "started", "engine": self.engine, "voice": answer.get("voice"),
            "input_rate": GPT_LIVE_WATCH_INPUT_RATE, "output_rate": GPT_LIVE_WATCH_OUTPUT_RATE,
            "briefing_applied": bool(answer.get("briefing_applied")),
            "greeting_applied": bool(answer.get("greeting_applied")),
        })
        # What came while "started" was on its way, in order, then live.
        while self.held:
            kind, plain = self.held.pop(0)
            await self.send(kind, plain)
        self.live = True

    async def audio(self, pcm: bytes) -> None:
        helper = self.helper
        if helper is not None and self.live:
            await helper.write("P", pcm)

    async def event(self, data: bytes) -> None:
        helper = self.helper
        if helper is not None and self.live:
            await helper.write("E", data)


class _WatchAudioPacer:
    """Engine output toward the Watch, in order, with audio at real time.

    Grok streams a reply's audio faster than it plays. The Watch gets at most
    WATCH_AUDIO_LEAD_S ahead; events wait their turn behind the audio before
    them, so the Watch sees them when it would have live. When the user
    speaks over the reply, the audio not yet sent is dropped.
    """

    def __init__(self, send: Callable[[int, bytes], Any], rate: int,
                 clock: Callable[[], float] = time.monotonic, lead_s: Optional[float] = None) -> None:
        self.send = send
        self.rate = rate
        self.clock = clock
        self.lead_s = WATCH_AUDIO_LEAD_S if lead_s is None else lead_s
        self.items: deque = deque()
        self.queued_s = 0.0
        self.queued_event_bytes = 0
        self.playout: Optional[float] = None
        self.wake = asyncio.Event()

    def _seconds(self, pcm: bytes) -> float:
        return len(pcm) / 2 / self.rate

    def audio(self, pcm: bytes) -> None:
        step = max(2, int(self.rate * WATCH_AUDIO_CHUNK_S) * 2)
        for start in range(0, len(pcm) - len(pcm) % 2, step):
            piece = pcm[start:start + step]
            self.items.append((WATCH_AUDIO_AUDIO, piece))
            self.queued_s += self._seconds(piece)
        while self.queued_s > WATCH_AUDIO_MAX_QUEUED_S:
            self._drop_oldest_audio()
        self.wake.set()

    def _drop_oldest(self, wanted: int) -> None:
        for index, (kind, data) in enumerate(self.items):
            if kind == wanted:
                del self.items[index]
                self._forget(kind, data)
                return
        if wanted == WATCH_AUDIO_AUDIO:
            self.queued_s = 0.0
        else:
            self.queued_event_bytes = 0

    def _drop_oldest_audio(self) -> None:
        self._drop_oldest(WATCH_AUDIO_AUDIO)

    def _forget(self, kind: int, data: bytes) -> None:
        if kind == WATCH_AUDIO_AUDIO:
            self.queued_s -= self._seconds(data)
        else:
            self.queued_event_bytes -= len(data)

    def event(self, text: str) -> None:
        data = _watch_audio_event_text(text)
        self.items.append((WATCH_AUDIO_EVENT, data))
        self.queued_event_bytes += len(data)
        while self.queued_event_bytes > WATCH_AUDIO_MAX_QUEUED_EVENT_BYTES:
            self._drop_oldest(WATCH_AUDIO_EVENT)
        self.wake.set()

    def interrupt(self) -> None:
        self.items = deque(item for item in self.items if item[0] != WATCH_AUDIO_AUDIO)
        self.queued_s = 0.0
        self.playout = None
        self.wake.set()

    async def run(self) -> None:
        while True:
            if not self.items:
                self.wake.clear()
                await self.wake.wait()
                continue
            kind, data = self.items[0]
            if kind == WATCH_AUDIO_AUDIO:
                now = self.clock()
                if self.playout is None or self.playout < now:
                    self.playout = now
                wait = self.playout - now - self.lead_s
                if wait > 0:
                    self.wake.clear()
                    # New items or an interruption wake it early.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.wake.wait(), wait)
                    continue
                self.playout += self._seconds(data)
            self._forget(kind, data)
            self.items.popleft()
            if await self.send(kind, data) is False:
                return


class _GrokWatchSession(_WatchAudioSession):
    """Grok on this host's xAI sign-in, the way the phone's Grok relay connects."""

    engine = "grok"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.upstream: Any = None
        self.pacer: Optional[_WatchAudioPacer] = None

    async def run(self) -> None:
        profile = self.profile

        def setup() -> Tuple[str, str, str, str]:
            token, auth, model = grok_live._grok_live_socket_setup(profile)
            return token, auth, model, grok_live.grok_live_model_voice()[1]

        try:
            token, auth, model, voice = await asyncio.wait_for(
                core._run_scoped(profile, setup, grok_live._grok_live_executor), timeout=grok_live.GROK_LIVE_SETUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise WatchAudioFailure("unreachable", "The Hermes host timed out reading its xAI sign-in")
        except core.TokenError as exc:
            raise _watch_audio_failure(exc)
        url = f"{grok_live.GROK_LIVE_URL}?{urllib.parse.urlencode({'model': model})}"
        try:
            upstream = await grok_live._connect_xai(url, {"Authorization": f"Bearer {token}"})
        except grok_live.RealtimeUpstreamRefused as exc:
            code, message = grok_live._grok_refusal(exc.status, auth)
            raise WatchAudioFailure("rate_limited" if code == grok_live.GROK_CLOSE_RATE_LIMITED else
                                    "unreachable" if code == grok_live.GROK_CLOSE_UNREACHABLE else "refused", message)
        except Exception as exc:
            # The type only: the text could quote the request.
            logger.warning("Grok Live for the Watch could not reach xAI: %s", type(exc).__name__)
            raise WatchAudioFailure("unreachable", "Could not reach xAI")
        pacer = self.pacer = _WatchAudioPacer(self.send, GROK_WATCH_RATE)
        tasks: list = []
        # Set first: the Watch answers "started" with its session.update.
        self.upstream = upstream
        try:
            # Before anything of xAI's reaches the Watch: it waits for this.
            await self.bridge.control(self.stream, {
                "type": "started", "engine": self.engine, "model": model, "voice": voice,
                "input_rate": GROK_WATCH_RATE, "output_rate": GROK_WATCH_RATE,
            })
            pacing = asyncio.ensure_future(pacer.run())
            reader = asyncio.ensure_future(self._read(upstream, pacer))
            tasks = [reader, pacing]
            if await self._wait_or_hang(reader, tasks):
                logger.warning("Grok Live for the Watch: xAI left %s unanswered; ending the session", self.owed_event)
                raise WatchAudioFailure("unreachable", "xAI stopped answering")
            if reader.done() and not self.stopping.is_set():
                code, reason = grok_live._forwardable_close(getattr(upstream, "close_code", None),
                                                  str(getattr(upstream, "close_reason", "") or ""))
                self.reason = reason or ("xAI ended the call" if code in (1000, 1001) else "The connection to xAI was lost")
        finally:
            self.upstream = None
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await upstream.close()

    async def _read(self, upstream: Any, pacer: _WatchAudioPacer) -> None:
        with contextlib.suppress(Exception):
            async for message in upstream:
                self.answered()
                if not isinstance(message, str):
                    continue
                try:
                    event = json.loads(message)
                except ValueError:
                    continue
                kind = event.get("type") if isinstance(event, dict) else None
                if kind in _GROK_AUDIO_DELTAS:
                    try:
                        pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
                    except (ValueError, TypeError):
                        continue
                    pacer.audio(pcm)
                    continue
                if kind == "input_audio_buffer.speech_started":
                    pacer.interrupt()
                pacer.event(message)

    async def audio(self, pcm: bytes) -> None:
        upstream = self.upstream
        if upstream is not None and pcm:
            with contextlib.suppress(Exception):
                await upstream.send(json.dumps({"type": "input_audio_buffer.append",
                                                "audio": base64.b64encode(pcm).decode("ascii")}))

    async def event(self, data: bytes) -> None:
        upstream = self.upstream
        if upstream is None or len(data) > grok_live.GROK_LIVE_MAX_CLIENT_FRAME_BYTES:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        await self.send_asking(upstream, text)


# --- The bridge -----------------------------------------------------------------


class WatchAudioRelayRefused(Exception):
    """The relay refused the host's audio socket; ``status`` is its HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _watch_audio_ws_url(relay_url: str, grant_id: str, side: str) -> str:
    base = "wss://" + relay_url[len("https://"):] if relay_url.startswith("https://") else \
        "ws://" + relay_url[len("http://"):]
    return f"{base}/v1/watch-audio/{grant_id}/{side}"


def _watch_audio_client_available() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


async def _connect_watch_audio_relay(url: str, credential: str) -> Any:
    """The host's socket to the relay (the `websockets` package Hermes ships)."""
    headers = {"Authorization": f"Bearer {credential}"}
    # Each client has its own refusal: InvalidStatus from the new one,
    # InvalidStatusCode from the legacy one.
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus as Refused
        kwargs: Dict[str, Any] = {"additional_headers": headers}

        def status_of(exc: Any) -> int:
            return int(getattr(exc.response, "status_code", 0) or 0)
    except ImportError:  # websockets < 13
        from websockets import connect  # type: ignore[no-redef]
        from websockets.exceptions import InvalidStatusCode as Refused  # type: ignore[no-redef]
        kwargs = {"extra_headers": headers}

        def status_of(exc: Any) -> int:
            return int(getattr(exc, "status_code", 0) or 0)
    try:
        return await connect(url, max_size=WATCH_AUDIO_MAX_MESSAGE_BYTES, open_timeout=WATCH_AUDIO_RELAY_TIMEOUT_S,
                             compression=None, **kwargs)
    except Refused as exc:
        raise WatchAudioRelayRefused(status_of(exc))


class _WatchAudioBridge:
    """A grant's audio: this host's socket to the relay, while the grant is open.

    It runs on a thread of its own with its own event loop, reconnecting
    after a drop. One engine session at a time: a new start, a new Watch
    connection or the Watch leaving ends the one before.
    """

    def __init__(self, grant: watch_tools._WatchGrant, secret: bytes, *, runtime: Optional[_WatchAudioRuntime] = None,
                 connect: Optional[Callable[[str, str], Any]] = None,
                 slots: Optional[_WatchAudioSlots] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.grant = grant
        self.secret = secret
        self.runtime = runtime or _watch_audio_runtime
        self.connect = connect or _connect_watch_audio_relay
        self.slots = slots or _watch_audio_slots
        self.clock = clock
        self.stop_requested = threading.Event()
        self.finished = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.wake: Optional[asyncio.Event] = None
        self.send_lock: Optional[asyncio.Lock] = None
        self.socket: Any = None
        self.stream: Optional[_WatchAudioStream] = None
        self.session: Optional[_WatchAudioSession] = None
        self.session_task: Optional[asyncio.Task] = None
        self.sids: set = set()
        self.rejected = 0
        self.refused_hellos = 0
        # Local Live's chats, by the id the Watch resumes them with: they
        # outlive a stream, and end with the grant.
        self.call_chats: Dict[str, "watch_local_live._WatchCallChat"] = {}

    def watch_url(self) -> str:
        return _watch_audio_ws_url(self.grant.relay_url, self.grant.grant_id, "watch")

    def start(self) -> None:
        threading.Thread(target=self._run, name="conduit-watch-audio", daemon=True).start()

    def stop(self) -> None:
        self.stop_requested.set()
        loop, wake = self.loop, self.wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):  # its loop already closed
                loop.call_soon_threadsafe(wake.set)

    def _done(self) -> bool:
        return self.stop_requested.is_set() or self.grant.closed.is_set() or self.clock() >= self.grant.expires_at

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 — the grant's lookups go on without audio
            logger.warning("The Watch audio bridge stopped: %s", type(exc).__name__)
        finally:
            # Each close can wait on Hermes: they go on in the background, so
            # the bridge's end isn't held up by one that hangs.
            for chat in list(self.call_chats.values()):
                try:
                    watch_local_live._local_live_watch_executor.submit(chat.close)
                except RuntimeError:  # the executor already shut down
                    chat.close()
            self.call_chats.clear()
            self.finished.set()

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()
        if self.stop_requested.is_set():
            return
        url = _watch_audio_ws_url(self.grant.relay_url, self.grant.grant_id, "host")
        backoff = 1.0
        while not self._done():
            try:
                socket = await self.connect(url, self.grant.credential)
            except WatchAudioRelayRefused as exc:
                if exc.status in (401, 403, 404, 410):
                    # Closed or expired on the relay, or not an audio grant there.
                    logger.info("The relay closed the Watch audio bridge (HTTP %s)", exc.status)
                    return
                logger.info("The relay refused the Watch audio bridge (HTTP %s); retrying in %ss", exc.status, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, WATCH_AUDIO_RECONNECT_MAX_S)
                continue
            except Exception as exc:  # noqa: BLE001 — retried while the grant is open
                logger.info("The Watch audio bridge couldn't reach the relay (%s); retrying in %ss",
                            type(exc).__name__, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, WATCH_AUDIO_RECONNECT_MAX_S)
                continue
            self.socket = socket
            opened = self.clock()
            try:
                await self._serve(socket)
            finally:
                self.socket = None
                self.stream = None
                await self._end_session("host_offline")
                with contextlib.suppress(Exception):
                    await socket.close()
            if getattr(socket, "close_code", None) == 4010:  # the relay's grant_closed
                return
            if self.clock() - opened >= WATCH_AUDIO_STABLE_S:
                backoff = 1.0
                await self._sleep(0.5)
            else:
                await self._sleep(backoff)
                backoff = min(backoff * 2, WATCH_AUDIO_RECONNECT_MAX_S)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.wake.wait(), seconds)

    async def _serve(self, socket: Any) -> None:
        receiver = asyncio.ensure_future(self._receive(socket))
        remaining = max(0.0, self.grant.expires_at - self.clock())
        done = await _first_done(remaining, receiver, self.wake.wait())
        if receiver not in done:
            receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await receiver
            except Exception as exc:  # noqa: BLE001 — a dropped socket; reconnected
                logger.info("The Watch audio bridge lost the relay: %s", type(exc).__name__)

    async def _receive(self, socket: Any) -> None:
        async for message in socket:
            if isinstance(message, str) or not message:
                continue
            await self._handle(bytes(message))

    async def _handle(self, message: bytes) -> None:
        first = message[0]
        if first == 0:
            if len(message) == 2 and message[1] in (WATCH_AUDIO_NOTICE_CONNECTED, WATCH_AUDIO_NOTICE_GONE):
                # The Watch connection changed: nothing more goes to the old stream.
                self.stream = None
                await self._end_session("watch_left" if message[1] == WATCH_AUDIO_NOTICE_GONE else "watch_rejoined")
            return
        if first == WATCH_AUDIO_HELLO:
            await self._hello(message)
            return
        stream = self.stream
        if stream is None:
            return
        try:
            kind, plain = stream.open(message)
        except WatchAudioError as exc:
            self.rejected += 1
            if self.rejected <= 3:
                logger.info("A Watch audio message didn't open: %s", exc)
            return
        session = self.session
        if kind == WATCH_AUDIO_CONTROL:
            await self._control(stream, plain)
        elif session is not None and session.stream is stream:
            if kind == WATCH_AUDIO_AUDIO:
                await session.audio(plain)
            else:
                await session.event(plain)

    async def _hello(self, message: bytes) -> None:
        if len(message) != 18:
            return
        sid, version = message[1:17], message[17]
        if sid in self.sids:
            # A stream id's keys are spent once: nothing can be sealed to it
            # again, not even a refusal. The Watch never sends one twice.
            self.refused_hellos += 1
            if self.refused_hellos <= 3:
                logger.warning("A Watch audio stream was refused (its id came twice)")
            return
        spent = len(self.sids) >= WATCH_AUDIO_STREAMS_PER_GRANT
        if spent and len(self.sids) >= WATCH_AUDIO_STREAMS_PER_GRANT + WATCH_AUDIO_REFUSALS_PER_GRANT:
            self.refused_hellos += 1
            if self.refused_hellos <= 3:
                logger.warning("A Watch audio stream was refused (too many streams for one call)")
            return
        self.stream = None
        await self._end_session("refused" if spent else "watch_rejoined")
        self.sids.add(sid)
        stream = _WatchAudioStream(self.grant.grant_id, self.secret, sid)
        self.stream = stream
        if spent:
            # A fresh id, so its keys can carry the refusal: the Watch ends
            # the call instead of waiting for a start that never comes.
            logger.warning("A Watch audio stream was refused (too many streams for one call)")
            await self.control(stream, {"type": "error", "code": "streams_spent",
                                        "message": "This call can't reconnect to Hermes again. Start the call again."})
            self.stream = None
        elif version != WATCH_AUDIO_VERSION:
            await self.control(stream, {"type": "error", "code": "version",
                                        "message": f"This Hermes host speaks Watch audio version {WATCH_AUDIO_VERSION}"})
            self.stream = None

    async def _control(self, stream: _WatchAudioStream, plain: bytes) -> None:
        try:
            request = json.loads(plain)
        except ValueError:
            return
        if not isinstance(request, dict):
            return
        kind = request.get("type")
        if kind == "end":
            await self._end_session("ended")
        elif kind == "start":
            await self._start(stream, request)

    async def _start(self, stream: _WatchAudioStream, request: Dict[str, Any]) -> None:
        await self._end_session("restarted")
        engine = request.get("engine")
        if engine not in WATCH_AUDIO_ENGINES:
            await self.control(stream, {"type": "error", "code": "bad_request",
                                        "message": f"engine must be one of {', '.join(WATCH_AUDIO_ENGINES)}"})
            return
        if not self.slots.acquire():
            await self.control(stream, {"type": "error", "code": "busy",
                                        "message": "Too many Watch calls are open on this Hermes host"})
            return
        session = watch_local_live._WATCH_AUDIO_SESSIONS[engine](self, stream, request)
        self.session = session
        self.session_task = asyncio.ensure_future(self._run_session(session))

    async def _run_session(self, session: _WatchAudioSession) -> None:
        try:
            await session.run()
        except WatchAudioFailure as exc:
            session.reason = "error"
            await self.control(session.stream, {"type": "error", "code": exc.code, "message": str(exc)})
        except asyncio.CancelledError:
            session.reason = session.reason if session.stopping.is_set() else "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 — told to the Watch by its type
            logger.warning("A Watch %s session failed: %s", session.engine, type(exc).__name__)
            session.reason = "error"
            await self.control(session.stream, {"type": "error", "code": "failed",
                                                "message": f"The Watch call failed on the host ({type(exc).__name__})"})
        finally:
            self.slots.release()
            if self.session is session:
                self.session = None
                self.session_task = None
            with contextlib.suppress(Exception):
                await self.control(session.stream, {"type": "ended", "engine": session.engine, "reason": session.reason})

    async def _end_session(self, reason: str) -> None:
        session, task = self.session, self.session_task
        if session is None or task is None:
            return
        self.session = None
        self.session_task = None
        session.stop(reason)
        try:
            await asyncio.wait_for(asyncio.shield(task), WATCH_AUDIO_STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # noqa: BLE001 — the session reported its own failure
            pass

    async def control(self, stream: _WatchAudioStream, payload: Dict[str, Any]) -> bool:
        return await self.send(stream, WATCH_AUDIO_CONTROL, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    async def send(self, stream: _WatchAudioStream, kind: int, plain: bytes) -> bool:
        """Seals and sends to the Watch on ``stream``; False once that stream is gone."""
        socket = self.socket
        if socket is None or stream is not self.stream or self.send_lock is None:
            return False
        async with self.send_lock:
            # Sealed under the lock, so counters reach the relay in order.
            if stream is not self.stream:
                return False
            try:
                message = stream.seal(kind, plain)
            except WatchAudioError:
                return True  # one message too large; the stream goes on
            try:
                await socket.send(message)
            except Exception:  # noqa: BLE001 — the socket dropped; the bridge reconnects
                return False
        return True


def watch_audio_status(runtime: Optional[_WatchAudioRuntime] = None, profile: Optional[str] = None,
                       local_live: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Each engine's readiness; local_live's is the profile's (call in its scope)."""
    runtime = runtime or _watch_audio_runtime
    return {"version": WATCH_AUDIO_VERSION,
            "engines": {"gpt_live": runtime.status(), "grok": {"runtime": "ready", "source": "hermes", "reason": None},
                        watch_local_live.LOCAL_LIVE_WATCH_ENGINE: (local_live or watch_local_live.local_live_watch_status)(profile)}}


def _watch_audio_grant_engines(runtime: Optional[_WatchAudioRuntime] = None, profile: Optional[str] = None,
                               local_live: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None) -> list:
    """The engines a grant's bridge can start (local_live's check in the profile's scope)."""
    runtime = runtime or _watch_audio_runtime
    engines = []
    for engine in WATCH_AUDIO_ENGINES:
        if engine == "gpt_live" and runtime.python() is None:
            continue
        if engine == watch_local_live.LOCAL_LIVE_WATCH_ENGINE and (local_live or watch_local_live.local_live_watch_status)(profile).get("runtime") != "ready":
            continue
        engines.append(engine)
    return engines


@router.get("/watch-audio/status")
async def get_watch_audio_status(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    local_live._local_live_start_auth()
    try:
        return {"ok": True, **(await asyncio.wait_for(core._run_scoped(profile, lambda: watch_audio_status(profile=profile)),
                                                      timeout=speech_server.SPEECH_SERVER_SETUP_TIMEOUT_S))}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Watch audio status timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Watch audio")


@router.post("/watch-audio/prepare", dependencies=core._UNLESS_LOCKED)
async def post_watch_audio_prepare(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        _watch_audio_prepare_limiter.acquire("")
        runtime = await asyncio.get_running_loop().run_in_executor(None, _watch_audio_runtime.prepare)
        return {"ok": True, "version": WATCH_AUDIO_VERSION, "engines": {"gpt_live": runtime}}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("prepare", exc, feature="Watch audio")


_watch_audio_prepare_limiter = core._MintLimiter(5, 300.0, message="Preparing GPT-Live for the Watch was asked too often; "
                                                               "try again in a few minutes")
