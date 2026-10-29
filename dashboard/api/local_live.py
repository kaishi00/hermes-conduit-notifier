"""Local Live: a live call through the user's own voice server."""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import itertools
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
import contextlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response, WebSocket

from . import core, local_live_agent, local_live_read_back, pairing, speech_server

logger = core.logger
router = core.router


# --- Local Live: a live call through the user's own voice server -----------
#
# Your own speech server can also run whole live calls: Hugging Face
# speech-to-speech (Hermes Live Voice's local mode) listens, decides when the
# user has finished, and speaks the reply, asking an OpenAI-compatible "model
# address" for the words. For Local Live that model address is this plugin:
#
#   phone -> /local-live/socket -> the speech server   (call audio, relayed
#                                                       as for Classic voice)
#   speech server -> /local-live/v1/chat/completions    (each turn)
#   plugin -> phone: conduit.turn {turn_id, text}       (on the call's socket)
#   phone runs the turn in the call's chat, streams conduit.turn.delta back
#   plugin -> speech server: the reply as chat-completions SSE
#
# So Hermes answers in the chat the call belongs to, with that chat's profile,
# tools and memory, and the turn shows live in Conduit like any other. The
# request's model name is ignored.
#
# The speech server can't sign in to the dashboard, so the two model routes
# take a key instead: the plugin's own, one per profile, shown in Conduit's
# settings to paste into the server (--responses_api_api_key). That uses
# Hermes' token-route seam (hermes_cli.dashboard_auth.token_auth), which only
# ever affects the registered paths. A Hermes without the seam reports Local
# Live unavailable.
#
# One live call per profile at a time: a speech-to-speech server runs one
# pipeline by default, and a turn goes to the profile's newest call.
#
# The same socket with ?mode=speak is Classic voice's reply voice: Conduit
# sends each reply sentence as a user item plus response.create, and the
# model route reads that text back unchanged, so the server speaks it in its
# own voice. Classic then needs one address for listening and speaking.

LOCAL_LIVE_MOUNT = "/api/plugins/conduit_push"
LOCAL_LIVE_MODEL_PATH = "/local-live/v1"
LOCAL_LIVE_TOKEN_PATHS = (LOCAL_LIVE_MOUNT + LOCAL_LIVE_MODEL_PATH + "/chat/completions",
                          LOCAL_LIVE_MOUNT + LOCAL_LIVE_MODEL_PATH + "/models",
                          LOCAL_LIVE_MOUNT + LOCAL_LIVE_MODEL_PATH + "/tools",
                          LOCAL_LIVE_MOUNT + LOCAL_LIVE_MODEL_PATH + "/tools/call")
LOCAL_LIVE_FILE = "conduit-local-live.json"
LOCAL_LIVE_KEY_PREFIX = "cll1"
LOCAL_LIVE_PROVIDER_NAME = "conduit-local-live"
LOCAL_LIVE_MODEL_ID = "conduit"
# Between frames of a turn from the phone. A Hermes turn with tools can be
# slow; the phone sends a short "one moment" first when it is.
LOCAL_LIVE_TURN_IDLE_S = 90.0
LOCAL_LIVE_TURN_MAX_S = 600.0
LOCAL_LIVE_MAX_BODY_BYTES = 512 * 1024
LOCAL_LIVE_MAX_TURN_CHARS = 4_000
LOCAL_LIVE_MAX_DELTA_CHARS = 8_000
# A turn's whole reply: the phone's deltas past this are dropped.
LOCAL_LIVE_MAX_REPLY_CHARS = 32_000
# How long the auth seam waits for the dashboard to start serving.
LOCAL_LIVE_AUTH_WAIT_S = 600.0
LOCAL_LIVE_NO_CALL_REPLY = "Conduit isn't in a Local Live call right now."
LOCAL_LIVE_UNAVAILABLE = ("This Hermes can't take the voice server's key (no dashboard token routes). "
                          "Update Hermes to use Local Live.")
_LOCAL_LIVE_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# A profile whose plugin Hermes runs in a plugin host of its own (Hermes
# 0.22.3+ with several profiles): only the dashboard process serves Local
# Live, and a Conduit that knows ?conduit_profile= reaches it there.
LOCAL_LIVE_PLUGIN_HOST_REASON = ("Hermes runs this profile's plugins apart from the dashboard, where Local Live "
                                 "can't work. Update Conduit, or start a dashboard for this profile.")

_local_live_auth_state: Dict[str, Any] = {"ready": False, "reason": "Local Live is starting", "started": False}
_local_live_auth_lock = threading.Lock()
_local_live_key_lock = threading.Lock()


def _local_live_key_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / LOCAL_LIVE_FILE


def _local_live_profile_part(profile: Optional[str]) -> str:
    # Unfolded: the key is verified in this exact profile's scope, which is
    # case-sensitive.
    name = _local_live_profile_key(profile)
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=") or "-"


def _local_live_profile_from_part(part: str) -> Optional[str]:
    """The profile a key names, "" for the dashboard's own; None when malformed."""
    if part == "-":
        return ""
    try:
        name = base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return name if _LOCAL_LIVE_PROFILE_NAME.fullmatch(name) else None


def _local_live_read_key() -> str:
    """This profile's key (inside its scope), or "" when none was made."""
    try:
        data = json.loads(_local_live_key_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    key = data.get("api_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.startswith(LOCAL_LIVE_KEY_PREFIX + "."):
        raise ValueError("Invalid Local Live key store")
    return key


def local_live_key(profile: Optional[str], rotate: bool = False) -> str:
    """This profile's key, made on first use or when ``rotate`` (inside its scope)."""
    with _local_live_key_lock:
        key = "" if rotate else _local_live_read_key()
        if key:
            return key
        path = _local_live_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Another dashboard process on this home (`hermes dashboard` beside the
        # desktop's `hermes serve`) may be making the first key too: only the
        # last one written verifies, so the second takes the first's.
        with pairing._pairing_state_lock(path):
            key = "" if rotate else _local_live_read_key()
            if key:
                return key
            key = ".".join((LOCAL_LIVE_KEY_PREFIX, _local_live_profile_part(profile), secrets.token_urlsafe(32)))
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".conduit-local-live-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "api_key": key, "created_at": int(time.time())}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                if os.name == "posix":
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        # After the replace, so a verify that read the old file can't cache it
        # again (the generation it started under is gone).
        global _local_live_key_generation
        _local_live_key_generation += 1
        named = _local_live_profile_key(profile)
        for token in [t for t, (p, _) in _local_live_verified.items() if _local_live_profile_key(p) == named]:
            del _local_live_verified[token]
        _local_live_known.pop(named, None)
        return key


# Keys verified lately, so a call's turns don't each reread the key file
# and the profile's config on the event loop (verify_token is synchronous).
# A profile's entries go whenever its key is made or rotated.
_local_live_verified: Dict[str, Tuple[str, float]] = {}
_local_live_key_generation = 0
LOCAL_LIVE_VERIFIED_TTL_S = 30.0
# Well-formed keys checked against the files a minute, per profile the key
# names and in all: past this, an unknown key is refused without reading
# anything, so a flood of wrong keys can't keep the event loop reading files.
# Malformed keys never count, and keys verified lately still work.
# Per profile as many as the one shared budget allowed before, so junk keys
# naming one profile cost no less than they did.
LOCAL_LIVE_KEY_CHECK_LIMIT = 60
LOCAL_LIVE_MAX_KEY_CHARS = 512
LOCAL_LIVE_KEY_CHECK_TOTAL_LIMIT = 300
# Its keys are bounded by the total budget below (a refused check gives its
# slot back), so a flood of made-up profile names never fills it.
_local_live_key_check_limiter = core._MintLimiter(LOCAL_LIVE_KEY_CHECK_LIMIT, 60.0, message="Too many Local Live key checks",
                                             max_keys=LOCAL_LIVE_KEY_CHECK_TOTAL_LIMIT + 1)
_local_live_key_check_total_limiter = core._MintLimiter(LOCAL_LIVE_KEY_CHECK_TOTAL_LIMIT, 60.0,
                                                   message="Too many Local Live key checks")
# The key each profile last verified with, until its key file changes: that
# key gets in without the check budget, so wrong keys naming the profile can't
# spend the budget and lock its voice server out once the cache above expires.
# Kept as a digest beside the key file's stat, so a rotation by another
# dashboard process on this home (which replaces the file) retires it too.
_local_live_known: Dict[str, Tuple[bytes, Tuple[int, int, int, int]]] = {}


def local_live_profile_for_key(token: str) -> Optional[str]:
    """The profile whose key ``token`` is ("" for the dashboard's own), else None."""
    now = time.monotonic()
    with _local_live_key_lock:
        cached = _local_live_verified.get(token)
    if cached is not None and now < cached[1]:
        return cached[0]
    named = _local_live_key_profile(token)
    if named is None:
        return None
    bucket = _local_live_profile_key(named)
    with _local_live_key_lock:
        generation = _local_live_key_generation
    if _local_live_is_known(token, named):
        with _local_live_key_lock:
            # A rotation since the stat retired this key: it takes the budgeted check below.
            if generation == _local_live_key_generation:
                _local_live_remember_verified(token, named, now)
                return named
    try:
        mint = _local_live_key_check_limiter.acquire(bucket)
    except core.TokenError:
        return None
    try:
        total = _local_live_key_check_total_limiter.acquire("")
    except core.TokenError:
        _local_live_key_check_limiter.release(bucket, mint)
        return None
    with _local_live_key_lock:
        generation = _local_live_key_generation
    # Stat before and after the read: a file replaced in between can't be
    # remembered against the key read from the one before it.
    before = _local_live_key_stamp(named)
    profile = _local_live_check_key(token)
    if profile is not None:
        # A right key doesn't spend the budget wrong ones share.
        _local_live_key_check_limiter.release(bucket, mint)
        _local_live_key_check_total_limiter.release("", total)
        after = _local_live_key_stamp(named) if before is not None else None
        with _local_live_key_lock:
            if generation == _local_live_key_generation:
                _local_live_remember_verified(token, profile, now)
                if after is not None and after == before:
                    _local_live_known[bucket] = (_local_live_digest(token), after)
    return profile


def _local_live_remember_verified(token: str, profile: str, now: float) -> None:
    """Caches a verified key, dropping expired ones (under the key lock)."""
    for stale in [k for k, (_, expiry) in _local_live_verified.items() if expiry <= now]:
        del _local_live_verified[stale]
    _local_live_verified[token] = (profile, now + LOCAL_LIVE_VERIFIED_TTL_S)


def _local_live_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _local_live_key_stamp(profile: str) -> Optional[Tuple[int, int, int, int]]:
    """The profile's key file as it stands on disk (inode, mtime, ctime, size), else None."""
    try:
        with core._profile_scope(profile or None):
            stat = os.stat(_local_live_key_path())
    except Exception:  # noqa: BLE001 — no stamp just means the key is checked again
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


def _local_live_is_known(token: str, profile: str) -> bool:
    """Whether ``token`` is the key ``profile`` last verified with and its file is unchanged."""
    bucket = _local_live_profile_key(profile)
    with _local_live_key_lock:
        known = _local_live_known.get(bucket)
    if known is None or not hmac.compare_digest(known[0], _local_live_digest(token)):
        return False
    if _local_live_key_stamp(profile) == known[1]:
        return True
    with _local_live_key_lock:
        if _local_live_known.get(bucket) == known:
            del _local_live_known[bucket]
    return False


def _local_live_key_profile(token: str) -> Optional[str]:
    """The profile a well-formed key names, without reading anything; else None."""
    # A real key is well under this; longer ones are refused before any work.
    if not isinstance(token, str) or len(token) > LOCAL_LIVE_MAX_KEY_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != LOCAL_LIVE_KEY_PREFIX or not parts[2]:
        return None
    return _local_live_profile_from_part(parts[1])


def _local_live_check_key(token: str) -> Optional[str]:
    profile = _local_live_key_profile(token)
    if profile is None:
        return None
    try:
        with core._profile_scope(profile or None):
            stored = _local_live_read_key()
    except Exception as exc:  # noqa: BLE001 — a bad profile is a wrong key, never a crash in auth
        logger.warning("Local Live could not read a profile's key: %s", type(exc).__name__)
        return None
    if stored and hmac.compare_digest(stored.encode("utf-8"), token.encode("utf-8")):
        return profile
    return None


def _local_live_provider() -> Any:
    from hermes_cli.dashboard_auth.base import DashboardAuthProvider, TokenPrincipal

    class LocalLiveKeyProvider(DashboardAuthProvider):
        """The voice server's key for Local Live's model routes; nothing else."""

        name = LOCAL_LIVE_PROVIDER_NAME
        display_name = "Conduit Local Live (voice server key)"
        supports_token = True
        supports_session = False

        def verify_token(self, *, token: str) -> Any:
            profile = local_live_profile_for_key(token or "")
            if profile is None:
                return None
            return TokenPrincipal(principal=f"local-live:{profile}", provider=self.name, scopes=("local-live",))

        def start_login(self, *, redirect_uri: str) -> Any:
            raise NotImplementedError("Local Live's key is for the voice server, not for signing in")

        def complete_login(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("Local Live's key is for the voice server, not for signing in")

        def verify_session(self, *args: Any, **kwargs: Any) -> Any:
            return None

        def refresh_session(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("Local Live's key is for the voice server, not for signing in")

        def revoke_session(self, *args: Any, **kwargs: Any) -> None:
            return None

    return LocalLiveKeyProvider()


def _local_live_register_auth() -> None:
    """Registers the key provider and the model routes once the dashboard serves.

    Waits for start_server to decide its auth gate: registered before that, a
    token-only provider would count as the "auth provider" a public bind must
    have. With the gate on and nothing else to sign in with, it stays off.
    """
    try:
        from hermes_cli.dashboard_auth import token_auth
        from hermes_cli.dashboard_auth import registry
    except ImportError:
        _local_live_auth_state.update(ready=False, reason=LOCAL_LIVE_UNAVAILABLE)
        return
    web_server = sys.modules.get("hermes_cli.web_server")
    if web_server is None:
        # Gone since the starter looked (a failed import): no gate to wait for.
        _local_live_auth_state.update(ready=False, reason="Local Live runs in the Hermes dashboard")
        _local_live_retry_auth()
        return
    app = getattr(web_server, "app", None)
    deadline = time.monotonic() + LOCAL_LIVE_AUTH_WAIT_S
    # Mid-import the dashboard may not have its app yet: wait for that too.
    while app is None or getattr(app.state, "auth_required", None) is None:
        if time.monotonic() > deadline:
            _local_live_auth_state.update(ready=False, reason="The dashboard hasn't started serving")
            _local_live_retry_auth()
            return
        time.sleep(0.5)
        app = getattr(web_server, "app", None)
    if app is not None and getattr(app.state, "auth_required", False):
        others = [p for p in registry.list_providers() if p.name != LOCAL_LIVE_PROVIDER_NAME]
        if not others:
            _local_live_auth_state.update(ready=False, reason="The dashboard has no sign-in provider")
            return
    try:
        if registry.get_provider(LOCAL_LIVE_PROVIDER_NAME) is None:
            registry.register_provider(_local_live_provider())
        for path in LOCAL_LIVE_TOKEN_PATHS:
            token_auth.register_token_route(path)
    except Exception as exc:  # noqa: BLE001 — Local Live off, never the dashboard
        logger.warning("Local Live could not register its key: %s", exc)
        _local_live_auth_state.update(ready=False, reason=f"Hermes refused Local Live's key ({type(exc).__name__})")
        _local_live_retry_auth()
        return
    _local_live_auth_state.update(ready=True, reason=None)


def _local_live_retry_auth() -> None:
    """After a failure that may pass (a slow start, a refused registration),
    the next status or socket call tries again, once this attempt's thread
    has returned."""
    _local_live_auth_state["retry"] = True


def _local_live_register_auth_thread() -> None:
    _local_live_auth_state["retry"] = False
    try:
        _local_live_register_auth()
    except Exception as exc:  # noqa: BLE001 — Local Live off, never the dashboard
        # Anything unforeseen (a provider registry that raises) gets a true
        # reason and another try, not "starting" for good.
        logger.warning("Local Live could not start its key check: %s", exc)
        _local_live_auth_state.update(ready=False, reason=f"Local Live couldn't start ({type(exc).__name__})")
        _local_live_retry_auth()
    finally:
        if _local_live_auth_state.get("retry"):
            with _local_live_auth_lock:
                _local_live_auth_state["started"] = False


def _local_live_start_auth() -> None:
    with _local_live_auth_lock:
        if _local_live_auth_state["started"]:
            return
        if "hermes_cli.web_server" not in sys.modules:
            # Not the dashboard yet (tests, a plugin host process, or imported
            # before it): a later status or socket call tries again.
            _local_live_auth_state.update(ready=False, reason=(
                LOCAL_LIVE_PLUGIN_HOST_REASON if "hermes_cli.plugin_host_child" in sys.modules
                else "Local Live runs in the Hermes dashboard"))
            return
        _local_live_auth_state["started"] = True
    try:
        threading.Thread(target=_local_live_register_auth_thread, name="conduit-local-live-auth", daemon=True).start()
    except RuntimeError as exc:  # no thread to spare: try again on the next call
        logger.warning("Local Live could not start its key registration: %s", exc)
        _local_live_auth_state.update(ready=False, reason="Local Live couldn't start; it will try again")
        with _local_live_auth_lock:
            _local_live_auth_state["started"] = False


# The voice server's warmup asks for a turn as soon as it starts, maybe
# before Conduit has opened anything: the key works from the dashboard's start.
_local_live_start_auth()


class _LocalLiveTurn:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.frames: "asyncio.Queue[Tuple[str, str]]" = asyncio.Queue()
        # Characters queued so far; bounds the queue with the reply.
        self.chars = 0


class _LocalLiveCall:
    """A live call's relay socket: the phone end, where turns are sent."""

    def __init__(self, ws: WebSocket, profile: str, speaks: bool = False) -> None:
        self.ws = ws
        self.profile = profile
        # Classic voice's reply voice (?mode=speak): Conduit already has the
        # words, sends them as the user item, and the server reads them back.
        self.speaks = speaks
        # On a speak socket, the words Conduit sent that the server hasn't
        # asked to have read back yet: how its requests are told from a
        # live call's on the same profile.
        self.to_read: "collections.deque[str]" = collections.deque(maxlen=64)
        # Words read back lately, with when: a server retrying a request whose
        # answer it lost still gets the read-back, not a live turn.
        self.read_lately: "collections.deque[Tuple[str, float]]" = collections.deque(maxlen=16)
        self.send_lock = asyncio.Lock()
        # Held across cancel-old, make-new and send, so turns never overlap.
        self.turn_lock = asyncio.Lock()
        self.turns: Dict[str, _LocalLiveTurn] = {}
        self.numbers = itertools.count(1)
        self.closed = False
        # Agent mode (conduit.agent from the phone): a fast front model
        # answers the call and hands the work to Hermes through the call's
        # tools. None keeps one Hermes turn per utterance.
        self.agent: Optional[local_live_agent._LocalLiveAgent] = None

    async def send(self, payload: Dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            async with self.send_lock:
                await self.ws.send_text(json.dumps(payload))
            return True
        except Exception:  # noqa: BLE001 — a gone phone ends the turn, not the request
            return False

    async def start_turn(self, text: str) -> Optional[_LocalLiveTurn]:
        # One turn at a time: the server has moved on from any earlier one.
        async with self.turn_lock:
            for old in list(self.turns.values()):
                await self.cancel_turn(old)
            turn = _LocalLiveTurn(f"t{next(self.numbers)}")
            self.turns[turn.turn_id] = turn
            if not await self.send({"type": "conduit.turn", "turn_id": turn.turn_id, "text": text}):
                self.turns.pop(turn.turn_id, None)
                return None
            return turn

    async def cancel_turn(self, turn: _LocalLiveTurn) -> None:
        if self.turns.pop(turn.turn_id, None) is not None:
            turn.frames.put_nowait(("done", ""))
            await self.send({"type": "conduit.turn.cancel", "turn_id": turn.turn_id})

    async def intercept(self, text: str) -> bool:
        """A frame from the phone: True for turn frames, which stay here."""
        if self.speaks and '"conversation.item.create"' in text:
            self._note_words(text)
        if '"conduit.' not in text:
            return False
        try:
            frame = json.loads(text)
        except ValueError:
            return False
        kind = frame.get("type") if isinstance(frame, dict) else None
        if not isinstance(kind, str) or not kind.startswith("conduit."):
            return False
        if kind.startswith(("conduit.agent", "conduit.tool.")):
            await self._agent_frame(kind, frame)
            return True
        turn = self.turns.get(str(frame.get("turn_id") or ""))
        if turn is None:
            return True
        if kind == "conduit.turn.delta":
            delta = frame.get("text")
            if isinstance(delta, str) and delta and turn.chars < LOCAL_LIVE_MAX_REPLY_CHARS:
                delta = delta[:min(LOCAL_LIVE_MAX_DELTA_CHARS, LOCAL_LIVE_MAX_REPLY_CHARS - turn.chars)]
                turn.chars += len(delta)
                turn.frames.put_nowait(("delta", delta))
        elif kind in ("conduit.turn.done", "conduit.turn.error"):
            self.turns.pop(turn.turn_id, None)
            turn.frames.put_nowait(("done", ""))
        return True

    def _note_words(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        item = frame.get("item") if isinstance(frame, dict) and frame.get("type") == "conversation.item.create" else None
        content = item.get("content") if isinstance(item, dict) else None
        # Joined the way _local_live_user_text joins a request's parts.
        parts = [part["text"] for part in content if isinstance(part, dict)
                 and part.get("type") in ("text", "input_text")
                 and isinstance(part.get("text"), str)] if isinstance(content, list) else []
        words = " ".join(" ".join(parts).split())[:LOCAL_LIVE_MAX_TURN_CHARS]
        if words:
            self.to_read.append(words)

    def take_words(self, text: str) -> bool:
        """Whether ``text`` is words this speak socket sent (now read back)."""
        # Spaced the way _local_live_user_text gives the request's words.
        wanted = " ".join(text.split())
        now = time.monotonic()
        if wanted in self.to_read:
            self.to_read.remove(wanted)
            self.read_lately.append((wanted, now))
            return True
        return any(words == wanted and now - at < LOCAL_LIVE_RETRY_WINDOW_S for words, at in self.read_lately)

    async def _agent_frame(self, kind: str, frame: Dict[str, Any]) -> None:
        if kind == "conduit.agent":
            # A connection's setup, sent again on each reconnect: the
            # conversation so far stays.
            agent = local_live_agent._LocalLiveAgent.configured(frame, previous=self.agent)
            self.agent = agent
            await self.send({"type": "conduit.agent.ready", "tools": len(agent.tools)})
        elif self.agent is None:
            return
        elif kind == "conduit.agent.note":
            self.agent.note(frame.get("text"))
        elif kind == "conduit.tool.result":
            call_id = str(frame.get("call_id") or "")
            output = frame.get("output")
            output = output if isinstance(output, str) else json.dumps(output if output is not None else {})
            if not self.agent.resolve(call_id, output) and frame.get("speak") is True and call_id:
                # Nothing waits for it any more (the front model moved on or
                # gave up): the phone says it as an update of its own.
                await self.send({"type": "conduit.tool.late", "call_id": call_id})

    def close(self) -> None:
        self.closed = True
        for turn in list(self.turns.values()):
            turn.frames.put_nowait(("done", ""))
        self.turns.clear()
        if self.agent is not None:
            self.agent.close()


_local_live_calls: Dict[str, List[Any]] = {}
# Registered from the dashboard's loop (the phone's sockets) and from Watch
# bridges' threads (_LocalLiveWatchCall); read anywhere.
_local_live_calls_lock = threading.Lock()


def _local_live_register(call: Any, may_ignore: Optional[Callable[[Any], bool]] = None) -> bool:
    """Registers atomically; a profile may have only one live call. A call
    ``may_ignore`` says is still winding down (a Watch's earlier call on the
    same bridge) doesn't count."""
    with _local_live_calls_lock:
        calls = _local_live_calls.get(call.profile, [])
        if not call.speaks and any(not other.closed and not other.speaks
                                   and not (may_ignore is not None and may_ignore(other)) for other in calls):
            return False
        _local_live_calls.setdefault(call.profile, calls).append(call)
        return True


def _local_live_unregister(call: Any) -> None:
    with _local_live_calls_lock:
        calls = _local_live_calls.get(call.profile)
        if calls is None:
            return
        with contextlib.suppress(ValueError):
            calls.remove(call)
        if not calls:
            _local_live_calls.pop(call.profile, None)


# How long a read-back sentence can be asked for again (a server's retry).
LOCAL_LIVE_RETRY_WINDOW_S = 30.0
# Requests per profile a minute: a speak socket asks once a sentence, a
# call once a turn, so this only stops a runaway client.
LOCAL_LIVE_REQUEST_LIMIT = 120
_local_live_limiter = core._MintLimiter(LOCAL_LIVE_REQUEST_LIMIT, 60.0,
                                   message="Too many Local Live requests; try again shortly")
# When each profile's voice server last asked for a turn: Conduit's setup
# shows whether the server is pointed at this plugin.
_local_live_last_request: Dict[str, float] = {}


_local_live_tasks: set = set()


def _local_live_spawn(coroutine: Any) -> None:
    task = asyncio.ensure_future(coroutine)
    _local_live_tasks.add(task)
    task.add_done_callback(_local_live_task_done)


def _local_live_task_done(task: "asyncio.Task[Any]") -> None:
    _local_live_tasks.discard(task)
    error = None if task.cancelled() else task.exception()
    if error is not None:
        logger.warning("Local Live background task failed: %r", error, exc_info=error)


def _local_live_profile_key(profile: Optional[str]) -> str:
    """The call registry's key: the profile as its Local Live key names it
    (unfolded, so Work and work are two profiles)."""
    name = (profile or "").strip()
    return "" if name.lower() in ("", "current") else name


# The phone's _LocalLiveCall or a Watch's _LocalLiveWatchCall: the same
# take_words / start_turn / cancel_turn / speaks / closed surface.
_LocalLiveAnyCall = Any


def _local_live_open_calls(profile: str) -> List[_LocalLiveAnyCall]:
    # A copy taken under the lock: Watch bridges register from their own threads.
    with _local_live_calls_lock:
        calls = list(_local_live_calls.get(_local_live_profile_key(profile), ()))
    return [call for call in calls if not call.closed]


def _local_live_active_call(profile: str) -> Optional[_LocalLiveAnyCall]:
    """The newest live call (not a speak socket), where turns go."""
    calls = [call for call in _local_live_open_calls(profile) if not call.speaks]
    return calls[-1] if calls else None


def _local_live_route(profile: str, text: str) -> Tuple[Optional[_LocalLiveAnyCall], bool]:
    """Who answers a request: (call, read back). Words a speak socket sent are
    read back; anything else is a live call's turn. With no live call but a
    speak socket open, the request is read back too."""
    calls = _local_live_open_calls(profile)
    for call in reversed(calls):
        if call.speaks and text and call.take_words(text):
            return call, True
    live = _local_live_active_call(profile)
    if live is not None:
        return live, False
    speaking = [call for call in calls if call.speaks]
    return (speaking[-1], True) if speaking else (None, False)


LOCAL_LIVE_PROFILE_NAME_REASON = "Local Live needs a profile name of letters, digits, dots, dashes or underscores"


def _local_live_profile_can_carry_key(profile: Optional[str]) -> bool:
    """Whether a key can name this profile: letters, digits, . _ - only, up to 64."""
    name = (profile or "").strip()
    return name.lower() in ("", "current") or bool(_LOCAL_LIVE_PROFILE_NAME.fullmatch(name))


def local_live_status(
    profile: Optional[str], in_call: Optional[bool] = None, last_request_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Inside the profile's scope: what Conduit shows in Local Live's settings.

    The route reads ``in_call`` and ``last_request_at`` on the event loop,
    which owns the call registry, and passes them in."""
    server = speech_server.speech_server_status()
    auth_ready = bool(_local_live_auth_state.get("ready"))
    reason = None if auth_ready else str(_local_live_auth_state.get("reason") or LOCAL_LIVE_UNAVAILABLE)
    if auth_ready and not _local_live_profile_can_carry_key(profile):
        auth_ready = False
        reason = LOCAL_LIVE_PROFILE_NAME_REASON
    if reason is None and not server.get("available"):
        reason = server.get("reason")
    return {
        "available": auth_ready and bool(server.get("available")),
        "reason": reason,
        "model_path": LOCAL_LIVE_MOUNT + LOCAL_LIVE_MODEL_PATH,
        "model_name": LOCAL_LIVE_MODEL_ID,
        "api_key": local_live_key(profile) if auth_ready else None,
        "speech_server": server,
        # Agent mode's fast model; without one, Hermes answers each turn.
        "front_model": local_live_agent.local_live_front_model_status(),
        "in_call": (_local_live_active_call(profile or "") is not None) if in_call is None else in_call,
        "last_request_at": (_local_live_last_request.get(_local_live_profile_key(profile))
                            if last_request_at is None else last_request_at),
    }


@router.get("/local-live/status")
async def get_local_live_status(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    _local_live_start_auth()
    in_call = _local_live_active_call(profile or "") is not None
    last_request_at = _local_live_last_request.get(_local_live_profile_key(profile))
    try:
        return {"ok": True, **(await asyncio.wait_for(
            core._run_scoped(profile, lambda: local_live_status(profile, in_call, last_request_at), speech_server._speech_server_executor),
            timeout=speech_server.SPEECH_SERVER_SETUP_TIMEOUT_S))}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Local Live status timed out", headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except core.TokenError as exc:
        # A profile that can't be scoped: its own status, not a host failure.
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("status", exc, feature="Local Live")


@router.post("/local-live/key")
async def post_local_live_key(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    """A new key for the voice server. This process drops the old one at once;
    other dashboard processes accept it for up to 30 s (their verified cache)."""
    response.headers["Cache-Control"] = "no-store"
    profile = await core._conduit_route_profile_no_store(request, profile)
    _local_live_start_auth()
    if not _local_live_auth_state.get("ready"):
        raise HTTPException(status_code=503, detail=str(_local_live_auth_state.get("reason") or LOCAL_LIVE_UNAVAILABLE),
                            headers={"Cache-Control": "no-store"})
    if not _local_live_profile_can_carry_key(profile):
        raise HTTPException(status_code=400, detail=LOCAL_LIVE_PROFILE_NAME_REASON, headers={"Cache-Control": "no-store"})
    try:
        key = await asyncio.wait_for(
            core._run_scoped(profile, lambda: local_live_key(profile, rotate=True), speech_server._speech_server_executor),
            timeout=speech_server.SPEECH_SERVER_SETUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        # The worker can't be stopped: the rotation may still land, which a
        # retry (or the status route's key) then picks up.
        raise HTTPException(status_code=504, detail="Local Live key rotation timed out", headers={"Cache-Control": "no-store"})
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        raise core._unexpected("key", exc, feature="Local Live")
    return {"ok": True, "api_key": key}


@router.websocket("/local-live/socket")
async def local_live_socket(ws: WebSocket) -> None:
    """A Local Live call: relayed to the speech server like Classic voice, plus its turns."""

    async def relay(refuse: Callable[[int, str], Any], url: str, key: str, profile: Optional[str]) -> None:
        call = _LocalLiveCall(ws, _local_live_profile_key(profile), speaks=ws.query_params.get("mode") == "speak")

        async def to_phone(text: str) -> None:
            async with call.send_lock:
                await ws.send_text(text)

        async def fail(reason: str) -> None:
            await refuse(speech_server.SPEECH_SERVER_CLOSE_REFUSED, reason)

        # A live call's turns, should its server only read text back.
        read_back = None if call.speaks else local_live_read_back._LocalLiveReadBack(call, to_phone, fail)
        if not _local_live_register(call):
            call.close()
            await fail("A Local Live call is already running on this Hermes profile")
            return
        try:
            await speech_server._speech_server_relay(ws, refuse, url, key, intercept=call.intercept, send_lock=call.send_lock,
                                       read_back=read_back)
        finally:
            call.close()
            _local_live_unregister(call)

    _local_live_start_auth()
    await speech_server._speech_server_socket(ws, relay)


def _local_live_request_profile(request: Request) -> str:
    principal = getattr(request.state, "token_principal", None)
    name = str(getattr(principal, "principal", "") or "")
    if getattr(principal, "provider", None) != LOCAL_LIVE_PROVIDER_NAME or not name.startswith("local-live:"):
        raise HTTPException(status_code=401, detail="The voice server's Local Live key is required",
                            headers={"Cache-Control": "no-store"})
    return name[len("local-live:"):]


def _local_live_acquire(profile: str) -> None:
    """Spends one of the profile's requests this minute, or answers 429."""
    try:
        _local_live_limiter.acquire(_local_live_profile_key(profile))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})


def _local_live_user_text(body: Any) -> str:
    """The newest user message's text: the turn. Hermes keeps the history."""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="Missing or invalid 'messages' field",
                            headers={"Cache-Control": "no-store"})
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part["text"] for part in content
                               if isinstance(part, dict) and part.get("type") in ("text", "input_text")
                               and isinstance(part.get("text"), str))
        if not isinstance(content, str):
            return ""
        return " ".join(content.split())[:LOCAL_LIVE_MAX_TURN_CHARS]
    return ""


def _local_live_chunk(completion_id: str, created: int, delta: Dict[str, Any], finish: Optional[str] = None) -> str:
    chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": LOCAL_LIVE_MODEL_ID,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(chunk)}\n\n"


@router.get("/local-live/v1/models")
async def get_local_live_models(request: Request, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _local_live_start_auth()
    profile = _local_live_request_profile(request)
    _local_live_acquire(profile)
    return {"object": "list", "data": [{"id": LOCAL_LIVE_MODEL_ID, "object": "model", "owned_by": "conduit"}]}


@router.post("/local-live/v1/chat/completions")
async def post_local_live_chat_completions(request: Request) -> Any:
    """One turn of a Local Live call, asked by the user's voice server."""
    from fastapi.responses import JSONResponse, StreamingResponse

    _local_live_start_auth()
    profile = _local_live_request_profile(request)
    body = await core._read_json_body(request, LOCAL_LIVE_MAX_BODY_BYTES)
    text = _local_live_user_text(body)
    # After validation, so a malformed request doesn't spend the budget.
    _local_live_acquire(profile)
    _local_live_last_request[_local_live_profile_key(profile)] = time.time()
    stream = isinstance(body, dict) and body.get("stream") is True
    completion_id = f"chatcmpl-conduit-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    call, speaks = _local_live_route(profile, text)
    # Agent mode: the call's front model answers, unless it isn't set up
    # (then Hermes answers each turn, as before). The readiness check is
    # always the phone's own turn.
    agent = getattr(call, "agent", None) if call is not None and not speaks else None
    target = await local_live_agent._local_live_front_target(profile) \
        if agent is not None and text and not text.startswith(local_live_agent.LOCAL_LIVE_READY_PREFIX) else None
    turn = await call.start_turn(text) if call is not None and text and not speaks and target is None else None

    async def turn_words() -> Any:
        """The phone's turn reply as it streams."""
        deadline = time.monotonic() + LOCAL_LIVE_TURN_MAX_S
        while True:
            remaining = min(LOCAL_LIVE_TURN_IDLE_S, deadline - time.monotonic())
            if remaining <= 0:
                return
            try:
                kind, delta = await asyncio.wait_for(turn.frames.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            if kind == "done":
                return
            # A keepalive (the call waits on the user) says nothing.
            yield "" if kind == "keepalive" else delta

    async def words() -> Any:
        """The reply's text as the phone streams it; a fixed line with no call."""
        nonlocal turn
        if speaks:
            # Read back word for word: the words are the reply already.
            if text:
                yield text
            return
        try:
            if target is not None:
                try:
                    async with contextlib.aclosing(agent.reply(call, text, target)) as replies:
                        async for delta in replies:
                            yield delta
                    return
                except local_live_agent._LocalLiveFrontUnavailable:
                    # The fast model is down: this utterance goes to Hermes.
                    yield local_live_agent.LOCAL_LIVE_FRONT_UNREACHABLE_REPLY + " "
                    turn = await call.start_turn(text)
            if turn is None:
                # With a call open, an empty request (a warmup mid-call) gets
                # nothing to say, not the no-call line.
                if call is None:
                    yield LOCAL_LIVE_NO_CALL_REPLY
                return
            async with contextlib.aclosing(turn_words()) as deltas:
                async for delta in deltas:
                    yield delta
        finally:
            # The server stopped listening (the user talked over the reply)
            # or the reply ended: either way the phone hears it's over.
            # Never awaited here: a closing generator can't suspend.
            if call is not None and turn is not None:
                _local_live_spawn(call.cancel_turn(turn))

    if not stream:
        reply = "".join([delta async for delta in words()])
        return JSONResponse({
            "id": completion_id, "object": "chat.completion", "created": created, "model": LOCAL_LIVE_MODEL_ID,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        }, headers={"Cache-Control": "no-store"})

    async def events() -> Any:
        try:
            yield _local_live_chunk(completion_id, created, {"role": "assistant"})
            async with contextlib.aclosing(words()) as deltas:
                async for delta in deltas:
                    # An SSE comment keeps a stream that's waiting on the user open.
                    yield _local_live_chunk(completion_id, created, {"content": delta}) if delta else ": keepalive\n\n"
            yield _local_live_chunk(completion_id, created, {}, finish="stop")
            yield "data: [DONE]\n\n"
        finally:
            # A dropped stream ends the phone's turn now, not when words() is
            # finalized. Cancelling a finished turn does nothing.
            if call is not None and turn is not None:
                _local_live_spawn(call.cancel_turn(turn))

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
