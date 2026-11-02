"""Local Live on the Watch."""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import itertools
import json
import re
import secrets
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import core, grok_live, hermes_calls, local_live, local_live_read_back, speech_server, watch_audio, watch_jobs, watch_tools

logger = core.logger


# --- Local Live on the Watch ---------------------------------------------------
#
# A Watch call on Local Live is the phone's call, over the call grant's audio
# bridge instead of /local-live/socket: the same speech server (the profile's
# voice.speech_server), the same session.update, readiness check and greeting
# (the Watch runs the iPhone's own LocalLiveSession), and the same model
# address (/local-live/v1 with the profile's Local Live key). This host relays
# the Watch's audio and events to the server and the server's back, paced to
# real time, as it does for Grok.
#
# Only who answers a turn differs. On the phone the plugin sends conduit.turn
# to Conduit, which runs it in the call's chat. A Watch can't run a Hermes
# turn, so this host does, in process, in a chat of the call's own (Hermes'
# session API, as Watch jobs use): the server's ask for a reply reaches the
# Watch's call through _local_live_route, the words go to the chat as a
# spoken turn, and Hermes' reply streams back to the server as it's written.
# The readiness check's ask (the Watch's marker words) goes to the Watch,
# which answers it, with the greeting or nothing, as the phone does.
#
#   Watch --PCM 24 kHz, its events-->  host  --relayed-->  speech server
#   speech server --/local-live/v1/chat/completions-->  this call's turn
#   host --prompt.submit-->  the call's chat;  message.delta -->  the server
#
# A turn's reply follows LocalLiveTurnRunner's rules: words spoken while a
# reply is still being written correct it (session.redirect), a turn the
# server lets go only stops being spoken, and what's written to be seen (code,
# tables, links) stays in the chat. A rejoining Watch (a new stream on the
# same grant) names the call's chat in its start (resume_session_id) and
# carries on in it; the grant's end closes it, and it stays in Hermes'
# history.

LOCAL_LIVE_WATCH_ENGINE = "local_live"
# The OpenAI Realtime PCM rate both ways, as the phone's Local Live call.
LOCAL_LIVE_WATCH_RATE = 24_000
# What the Watch's readiness check asks with (LocalLiveProtocol.readinessMarker):
# that ask goes to the Watch, never to Hermes.
LOCAL_LIVE_MARKER_PREFIX = "conduit-ready-"
LOCAL_LIVE_WATCH_TITLE = "Watch call"
LOCAL_LIVE_WATCH_CHAT_TIMEOUT_S = 25.0
# Chats one grant may open: a call, and calls after it on the same grant.
LOCAL_LIVE_WATCH_CHATS_PER_GRANT = 8
# As Conduit's spoken turns (AppState.spokenPromptSurface): Hermes answers for
# speech, on its voice model when one is set.
LOCAL_LIVE_WATCH_SURFACE = "voice-live"
# Spoken by the user's speech server instead of a reply (LocalLiveTurnRunner's lines).
LOCAL_LIVE_WATCH_FAILURE_LINE = "Hermes couldn't take that just now."
LOCAL_LIVE_WATCH_ERROR_LINE = "Hermes ran into a problem with that."
LOCAL_LIVE_WATCH_NEEDS_INPUT_LINE = "Hermes needs your answer in the chat before it can go on."
# A Watch that answers approvals itself (its start says approvals: true) is
# shown Hermes' requests, and the call says so once a reply.
LOCAL_LIVE_WATCH_APPROVAL_LINE = "Hermes needs your approval. It's on your Watch."
LOCAL_LIVE_WATCH_APPROVAL_CHOICES = ("once", "deny")
# The command a card shows; a longer one is offered deny only (approving it
# would approve what the card didn't show).
LOCAL_LIVE_WATCH_APPROVAL_COMMAND_CHARS = 2_000
# Hermes denies an approval nobody answers after its own timeout
# (approvals.timeout) without saying so: the Watch's card goes then, this
# much early (the request reaches the plugin a little after Hermes starts
# waiting). This is the timeout when Hermes' can't be read.
LOCAL_LIVE_WATCH_APPROVAL_TIMEOUT_S = 300.0
LOCAL_LIVE_WATCH_APPROVAL_MARGIN_S = 1.0
# While the call waits on an approval, its open turn hears from the plugin
# this often, so the model address doesn't let it go.
LOCAL_LIVE_WATCH_APPROVAL_KEEPALIVE_S = 20.0
LOCAL_LIVE_WATCH_STEERED_LINE = "Hermes added this to the reply already running in the chat."
LOCAL_LIVE_WATCH_QUEUED_LINE = "Hermes queued this in the chat. Its reply will appear there."
LOCAL_LIVE_WATCH_CODE_NOTE = "I've put the code in the chat."
LOCAL_LIVE_WATCH_NO_SESSIONS = ("Local Live on the Watch needs this dashboard to serve Hermes' chats "
                                "(update Hermes, or open a chat in Conduit once)")
# Hermes' notice for a reply a correction cut off (MessageNormalizer).
_LOCAL_LIVE_INTERRUPTION_NOTICE = "[this response was interrupted by a user correction.]"
_LOCAL_LIVE_WATCH_QUESTIONS = ("approval.request", "clarify.request", "sudo.request", "secret.request")
_LOCAL_LIVE_WATCH_WRITING = ("message.start", "reasoning.delta", "message.reasoning", "message.reasoning.delta",
                             "tool.start", "tool.complete")
_LOCAL_LIVE_WATCH_INTERRUPTED = ("message.interrupted", "session.interrupted")
# Sentences, for keeping what's written to be seen out of what's spoken.
_LOCAL_LIVE_WATCH_BOUNDARY = re.compile(r"(?<!\d)[.!?…]+[\"')\]]*(?=\s)|\n")
_LOCAL_LIVE_WATCH_LINK = re.compile(r"(!?)\[([^\]\n]*)\]\([^)\n]*\)")
_LOCAL_LIVE_WATCH_URL = re.compile(r"(?:https?://|www\.)[^\s]*[^\s.,;:!?)\]'\"]")
_LOCAL_LIVE_WATCH_SPACE_BEFORE_MARK = re.compile(r"\s+([.,;:!?])")
_LOCAL_LIVE_WATCH_HTML = re.compile(r"</?[A-Za-z][^>\n]{0,100}>")
_LOCAL_LIVE_WATCH_MARKS = re.compile(r"\*\*|__|~~|`|(?<![\w*])\*(?=\S)|(?<=\S)\*(?![\w*])")
_LOCAL_LIVE_WATCH_LINE_START = re.compile(r"^(?:#{1,6}\s+|>\s?|[-*+•]\s+|\d{1,3}[.)]\s+)+")
_LOCAL_LIVE_WATCH_RULE = re.compile(r"[-=*_|:\s]{3,}")
LOCAL_LIVE_WATCH_MAX_SENTENCE_CHARS = 400

_local_live_watch_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conduit-watch-local-live")


def _hermes_approval_timeout() -> float:
    """Hermes' own approval timeout (approvals.timeout), else its default.
    Call in the profile's scope."""
    try:
        try:
            # Newer Hermes keeps it in tools.approval_context; tools.approval no longer has it.
            from tools.approval_context import _get_approval_timeout
        except ImportError:
            from tools.approval import _get_approval_timeout
        value = float(_get_approval_timeout())
    except Exception:  # noqa: BLE001 — an older or different Hermes
        return LOCAL_LIVE_WATCH_APPROVAL_TIMEOUT_S
    # Zero or less: Hermes denies at once, so no card is worth showing.
    return max(0.0, value)


def _watch_chats_can_load() -> bool:
    """Whether this is a dashboard that serves Hermes' chats, its session API
    not imported yet (no chat socket since it started)."""
    web_server = sys.modules.get("hermes_cli.web_server")
    if core._gateway_runner() is not None:
        # The gateway's listener: its chats are the authority's, never this module's.
        return False
    if web_server is None or not getattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True):
        return False
    try:
        return importlib.util.find_spec("tui_gateway.server") is not None
    except (ImportError, ValueError):
        return False


def _watch_chat_session_api() -> Any:
    """Hermes' session API for a Watch call's chat.

    As _hermes_session_api, but a dashboard that hasn't had a chat socket yet
    imports the API now: it's the module the dashboard's /api/ws imports into
    this same process, so there is still one registry of chats. Never call it
    inside a profile's scope: the import reads Hermes' home and .env for the
    whole process.
    """
    server = watch_jobs._hermes_session_api()
    if server is not None or not _watch_chats_can_load():
        return server
    try:
        importlib.import_module("tui_gateway.server")
    except Exception as exc:  # noqa: BLE001 — reported as unavailable
        logger.info("Hermes' session API couldn't be loaded for a Watch call: %s", type(exc).__name__)
        return None
    return watch_jobs._hermes_session_api()


def local_live_watch_status(profile: Optional[str]) -> Dict[str, Any]:
    """Whether a Watch call can run on Local Live (call in the profile's
    scope; it never imports Hermes' session API)."""
    # As local_live_status, without making the profile's key.
    reason = None
    if not local_live._local_live_auth_state.get("ready"):
        reason = str(local_live._local_live_auth_state.get("reason") or local_live.LOCAL_LIVE_UNAVAILABLE)
    elif not local_live._local_live_profile_can_carry_key(profile):
        reason = local_live.LOCAL_LIVE_PROFILE_NAME_REASON
    else:
        server = speech_server.speech_server_status()
        if not server.get("available"):
            reason = server.get("reason") or speech_server.SPEECH_SERVER_NOT_CONFIGURED
    if reason is not None:
        return {"runtime": "missing", "source": None, "reason": reason}
    if watch_jobs._hermes_session_api() is None and not _watch_chats_can_load():
        return {"runtime": "missing", "source": None, "reason": LOCAL_LIVE_WATCH_NO_SESSIONS}
    return {"runtime": "ready", "source": "local_live", "reason": None}


class _SpokenText:
    """A reply's text, as it streams, with what's written to be seen left out:
    Markdown goes (headings, bullets, emphasis; links keep their words), code
    blocks and tables stay in the chat, said once, and URLs aren't read out.
    Text goes on at each sentence's end, so the speech server gets whole
    sentences to speak."""

    def __init__(self) -> None:
        self.raw = ""
        self.in_code = False
        self.code_told = False
        # The text so far ended inside a line that isn't spoken (a table row).
        self.skipping_line = False

    def feed(self, text: str) -> str:
        self.raw += text
        out: List[str] = []
        while True:
            match = _LOCAL_LIVE_WATCH_BOUNDARY.search(self.raw)
            if match is None:
                break
            chunk, self.raw = self.raw[:match.end()], self.raw[match.end():]
            self._take(chunk, out)
        while len(self.raw) > LOCAL_LIVE_WATCH_MAX_SENTENCE_CHARS:
            cut = self.raw.rfind(" ", 0, LOCAL_LIVE_WATCH_MAX_SENTENCE_CHARS)
            cut = cut if cut > 0 else LOCAL_LIVE_WATCH_MAX_SENTENCE_CHARS
            chunk, self.raw = self.raw[:cut], self.raw[cut:]
            self._take(chunk, out)
        return " ".join(out)

    def finish(self) -> str:
        out: List[str] = []
        if self.raw:
            chunk, self.raw = self.raw, ""
            self._take(chunk + "\n", out)
        return " ".join(out)

    def _take(self, chunk: str, out: List[str]) -> None:
        text = self._clean(chunk)
        if text:
            out.append(text)

    def _code(self, entering: bool, parts: List[str]) -> None:
        self.in_code = entering
        if entering and not self.code_told:
            self.code_told = True
            parts.append(LOCAL_LIVE_WATCH_CODE_NOTE)

    def _clean(self, chunk: str) -> str:
        lines = chunk.split("\n")
        parts: List[str] = []
        for index, line in enumerate(lines):
            complete = index < len(lines) - 1
            continuing = index == 0 and self.skipping_line
            if index == 0:
                self.skipping_line = False
            stripped = line.strip()
            if stripped.startswith("~~~") and not continuing:
                self._code(not self.in_code, parts)
                continue
            # Fences, at the start of a line or after text on it.
            pieces = stripped.split("```")
            spoken: List[str] = []
            for number, piece in enumerate(pieces):
                if number > 0:
                    self._code(not self.in_code, parts if not spoken else spoken)
                    if self.in_code:
                        continue
                if not self.in_code:
                    spoken.append(piece)
            stripped = " ".join(piece for piece in spoken if piece.strip()).strip()
            if continuing or not stripped:
                if continuing and not complete:
                    self.skipping_line = True
                continue
            if stripped.startswith("|") or _LOCAL_LIVE_WATCH_RULE.fullmatch(stripped):
                if not complete:
                    self.skipping_line = True
                continue
            text = _LOCAL_LIVE_WATCH_LINE_START.sub("", stripped)
            text = _LOCAL_LIVE_WATCH_LINK.sub(lambda match: "" if match.group(1) else match.group(2), text)
            text = _LOCAL_LIVE_WATCH_URL.sub("", text)
            text = _LOCAL_LIVE_WATCH_HTML.sub("", text)
            text = _LOCAL_LIVE_WATCH_MARKS.sub("", text)
            text = _LOCAL_LIVE_WATCH_SPACE_BEFORE_MARK.sub(r"\1", " ".join(text.split())).lstrip(".,;:!? ")
            if not text:
                continue
            if complete and text[-1] not in ".!?…:;,\"')]":
                # A heading or a bullet ends where its line does.
                text += "."
            parts.append(text)
        return " ".join(parts)


class _WatchChatTransport:
    """Where Hermes writes a Watch call chat's responses and events, as it
    would to the app's WebSocket; ``_closed`` is what Hermes' reaper reads."""

    conduit_client = "conduit"
    # And came from the Watch, spoken (turn_surface.py, hermes-conduit#888).
    conduit_surface = "watch"
    conduit_input = "spoken"

    def __init__(self, chat: "_WatchCallChat") -> None:
        self._chat = chat
        self._closed = False

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False
        try:
            self._chat.frame(obj)
        except Exception as exc:  # noqa: BLE001 — Hermes' writer must never see our failure
            watch_tools._log_watch_failure("A Watch call chat's event couldn't be read", exc)
        return True

    def close(self) -> None:
        self._closed = True
        core._release_authority_link(self)


class _WatchCallChat:
    """A Watch call's Hermes chat, through Hermes' session API.

    The calls block (as _WatchJobs.rpc): run them off the event loop. Hermes
    writes the chat's events from its own threads; ``listener`` gets them.
    """

    def __init__(self, server: Any, profile: Optional[str]) -> None:
        self.server = server
        self.profile = profile
        self.session_id = ""
        self.stored_session_id = ""
        self.transport = _WatchChatTransport(self)
        self.lock = threading.Lock()
        self._waiters: Dict[str, watch_jobs._RpcWaiter] = {}
        self._rpc_ids = 0
        self.rid_tag = secrets.token_hex(4)
        self.listener: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.voice_turn = True
        self.closed = False
        # Hermes' pending approval requests, oldest first: kept here, not by
        # a stream's session, so one Hermes asks between two streams of the
        # call still counts. Under approvals_lock; on_approvals hears of each
        # change (from Hermes' threads).
        self.approvals: deque = deque()
        self.approvals_lock = threading.Lock()
        self.on_approvals: Optional[Callable[[], None]] = None
        # An answer is on its way to Hermes: Hermes answers its oldest, so
        # no other is taken (or shown) until it's back, for any stream.
        self.answering = False

    @property
    def public_id(self) -> str:
        return (self.stored_session_id or self.session_id)[:128]

    def rpc(self, method: str, params: Dict[str, Any], timeout: float = watch_jobs.WATCH_JOB_RPC_TIMEOUT_S) -> Dict[str, Any]:
        with self.lock:
            self._rpc_ids += 1
            rid = f"conduit-watch-call-{self.rid_tag}-{self._rpc_ids}"
            waiter = self._waiters[rid] = watch_jobs._RpcWaiter()
        try:
            frame = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            if getattr(self.server, "takes_timeout", False):
                # The gateway's own listener answers in the call: hold it to this budget.
                response = self.server.dispatch(frame, self.transport, timeout=timeout)
            else:
                response = self.server.dispatch(frame, self.transport)
            if response is None:
                if not waiter.done.wait(timeout):
                    raise watch_jobs.WatchJobError(f"{method} timed out")
                response = waiter.response
        finally:
            with self.lock:
                self._waiters.pop(rid, None)
        if not isinstance(response, dict):
            raise watch_jobs.WatchJobError(f"{method} gave no answer")
        error = response.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise watch_jobs.WatchJobError(str(message or f"{method} failed")[:300], code=code)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def frame(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        method = obj.get("method")
        if method is None:
            with self.lock:
                waiter = self._waiters.get(obj.get("id")) if isinstance(obj.get("id"), str) else None
            if waiter is not None:
                waiter.response = obj
                waiter.done.set()
            return
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        sid = params.get("session_id") if isinstance(params.get("session_id"), str) else ""
        if method not in ("event", "approval") or not sid or sid not in (self.session_id, self.stored_session_id):
            return
        if method == "approval":
            # A Hermes that asks as a server request (as Watch jobs see it):
            # its fields sit beside session_id, and a request.cancel names
            # it by its id.
            kind, payload = "approval.request", {**params, "server_request_id": watch_jobs._rpc_id(obj.get("id"))}
        else:
            payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
            kind = str(params.get("type") or "")
        if self._track_approval(kind, payload):
            changed = self.on_approvals
            if changed is not None:
                changed()
        listener = self.listener
        if listener is not None:
            listener(kind, payload)

    def _track_approval(self, kind: str, payload: Dict[str, Any]) -> bool:
        """Keeps Hermes' pending approvals; True when they changed."""
        if kind == "approval.request":
            def text(key: str, limit: int) -> str:
                value = payload.get(key)
                return value.strip()[:limit] if isinstance(value, str) else ""

            command = str(payload.get("command") or "").strip()
            with self.approvals_lock:
                self.approvals.append({
                    "id": secrets.token_hex(8), "request_id": text("request_id", 128), "at": time.monotonic(),
                    "server_request_id": text("server_request_id", 128),
                    "command": command[:LOCAL_LIVE_WATCH_APPROVAL_COMMAND_CHARS],
                    "cut": not command or len(command) > LOCAL_LIVE_WATCH_APPROVAL_COMMAND_CHARS,
                    "description": text("description", 500) or "Approval required",
                })
            return True
        if kind == "request.cancel":
            # Hermes withdrew one server request (answered elsewhere, timed
            # out): only that one goes; an event-asked request has no id here.
            wanted = watch_jobs._rpc_id(payload.get("id"))
            with self.approvals_lock:
                kept = [entry for entry in self.approvals if not wanted or entry["server_request_id"] != wanted]
                changed = len(kept) != len(self.approvals)
                if changed:
                    self.approvals = deque(kept)
            return changed
        if kind == "approval.cancelled":
            ids = payload.get("request_ids")
            if ids is not None and not isinstance(ids, list):
                # Names its requests in a way this can't read: none is dropped
                # on a guess (Hermes' timeout and the turn's end still clear them).
                return False
            with self.approvals_lock:
                if ids:
                    kept = [entry for entry in self.approvals if entry["request_id"] not in ids]
                else:
                    kept = []
                changed = len(kept) != len(self.approvals)
                if changed:
                    self.approvals = deque(kept)
            return changed
        if kind in ("message.complete", "error"):
            # The turn ended: none is pending any more.
            with self.approvals_lock:
                had = bool(self.approvals)
                self.approvals.clear()
            return had
        return False

    def respond(self, choice: str, request_id: str) -> Optional[bool]:
        """Whether Hermes resolved it; None when the answer didn't get there."""
        params: Dict[str, Any] = {"session_id": self.session_id, "choice": choice}
        if request_id:
            params["request_id"] = request_id
        try:
            return bool(self.rpc("approval.respond", params).get("resolved"))
        except watch_jobs.WatchJobError as exc:
            logger.info("Answering a Watch call's approval failed: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — Hermes' own failure: not taken, so shown again
            logger.info("Answering a Watch call's approval failed: %s", type(exc).__name__)
            return None

    def open(self) -> None:
        params: Dict[str, Any] = {"cols": 96, "source": "desktop", "title": LOCAL_LIVE_WATCH_TITLE}
        if self.profile:
            params["profile"] = self.profile
        created = self.rpc("session.create", params)
        sid = created.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise watch_jobs.WatchJobError("session.create gave no session")
        stored = created.get("stored_session_id")
        self.session_id = sid
        self.stored_session_id = stored if isinstance(stored, str) else ""

    def submit(self, text: str) -> str:
        """Sends ``text`` as the chat's next spoken turn; Hermes' status for it."""
        params: Dict[str, Any] = {"session_id": self.session_id, "text": text, "surface": LOCAL_LIVE_WATCH_SURFACE}
        if self.voice_turn:
            params["voice_turn"] = True
        try:
            result = self.rpc("prompt.submit", params)
        except watch_jobs.WatchJobError as exc:
            # A Hermes that refuses the marker (Conduit's HermesClient does the same).
            if "voice_turn" not in params or "voice_turn" not in str(exc):
                raise
            self.voice_turn = False
            params.pop("voice_turn")
            result = self.rpc("prompt.submit", params)
        return str(result.get("status") or "").lower()

    def redirect(self, text: str) -> str:
        """Puts ``text`` into the reply running now: redirected, queued or rejected
        (AppState.redirectLiveVoiceFollowUp)."""
        try:
            status = str(self.rpc("session.redirect", {"session_id": self.session_id, "text": text})
                         .get("status") or "").lower()
        except watch_jobs.WatchJobError as exc:
            if not exc.unsupported_redirect:
                raise
            # An older Hermes: steered in instead.
            steered = self.rpc("session.steer", {"session_id": self.session_id, "text": text})
            return "rejected" if str(steered.get("status") or "").lower() == "rejected" else "redirected"
        return status if status in ("redirected", "queued") else "rejected"

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.listener = None
        self.on_approvals = None
        if self.session_id:
            try:
                self.rpc("session.close", {"session_id": self.session_id})
            except Exception as exc:  # noqa: BLE001 — Hermes' reaper closes it anyway
                logger.info("Closing a Watch call's chat failed: %s", type(exc).__name__)
        self.transport.close()


class _LocalLiveWatchReply:
    """The Hermes reply a Watch call follows, from its send until it ends."""

    def __init__(self) -> None:
        # A correction is on its way to Hermes: its marker or the reply's end
        # can arrive before Hermes answers the redirect.
        self.correcting = False
        # The completion that arrived while correcting, until Hermes says what
        # it did with the words (``_NO_COMPLETION`` while none did).
        self.held_completion: Any = _NO_COMPLETION
        # A correction went into it: the marker for the words it cut off
        # isn't its end.
        self.skips_interruption = False
        # Corrections Hermes queued behind it: the current reply's end isn't
        # the one the call waits for.
        self.skips_completions = 0


_NO_COMPLETION = object()


class _LocalLiveWatchTurns:
    """A Watch Local Live call's turns, run in its chat on this host: the
    iPhone's LocalLiveTurnRunner, here.

    One turn at a time, as the model address sends them. A turn that arrives
    while Hermes is still on the last one goes into that reply as a
    correction rather than starting a second turn. A turn the server lets go
    only stops being spoken: Hermes' reply still lands in the chat. Turns
    start on the dashboard's loop, Hermes' events come on its own threads,
    and the chat's calls run one after another on a worker; state is under
    ``lock``.
    """

    def __init__(self, chat: _WatchCallChat) -> None:
        self.chat = chat
        self.lock = threading.Lock()
        # The open turn: its id, how its words are delivered, and whether
        # anything was said for it yet.
        self.turn: Optional[Tuple[str, Callable[[str, str], None]]] = None
        self.spoke = False
        self.reply: Optional[_LocalLiveWatchReply] = None
        self.spoken = _SpokenText()
        self.generation = 0
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conduit-watch-local-live-turn")
        self.closed = False
        # The Watch answers approvals itself: the call says the card is there
        # (once a reply) and keeps the turn open for what Hermes says after.
        self.watch_approvals = False
        self.approval_told = False
        # A call from Hermes about a question or approval in its chat (the
        # grant's answer_sessions): while Hermes waits there, the user's
        # words answer it, and this returns what the call says.
        self.answer_call: Optional[Callable[[str], Optional[str]]] = None

    def start(self, turn_id: str, text: str, deliver: Callable[[str, str], None]) -> None:
        with self.lock:
            if self.closed:
                deliver("done", "")
                return
            # The model address cancels a turn before sending the next:
            # whatever is still open here has nobody waiting for it.
            self.turn = (turn_id, deliver)
            self.spoke = False
            self.approval_told = False
            self.spoken = _SpokenText()
            self.generation += 1
            generation = self.generation
        try:
            # After the last turn's work: one send or correction at a time,
            # in the order the user spoke.
            self.worker.submit(self._run, turn_id, text, generation)
        except RuntimeError:  # closed meanwhile
            deliver("done", "")

    def cancel(self, turn_id: str) -> None:
        with self.lock:
            if self.turn is not None and self.turn[0] == turn_id:
                self.turn = None

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.generation += 1
            self.turn = None
            self.reply = None
        # Turns still queued would only submit to a chat that's closing.
        self.worker.shutdown(wait=False, cancel_futures=True)

    def _current(self, generation: int) -> bool:
        with self.lock:
            return generation == self.generation

    def _run(self, turn_id: str, text: str, generation: int) -> None:
        # A newer turn came while this one waited: the model address let it go.
        if not self._current(generation):
            return
        words = " ".join(text.split())
        if not words:
            with self.lock:
                self._finish(turn_id)
            return
        if self.answer_call is not None:
            try:
                line = self.answer_call(words)
            except Exception as exc:  # noqa: BLE001 — named; the message can carry the answer
                logger.info("A Watch Local Live call's answer didn't reach Hermes: %s", type(exc).__name__)
                line = LOCAL_LIVE_WATCH_FAILURE_LINE
            if line:
                with self.lock:
                    self._say(line, turn_id, generation)
                return
        following: Optional[_LocalLiveWatchReply] = None
        try:
            with self.lock:
                running = self.reply
                if running is not None:
                    running.correcting = True
                    running.skips_interruption = True
            if running is not None:
                try:
                    status = self.chat.redirect(words)
                except Exception:
                    self._settle(running, "rejected")
                    raise
                starts = self._settle(running, status)
                if not starts or not self._current(generation):
                    return
            with self.lock:
                if generation != self.generation:
                    return
                # Its events are this turn's from the send on.
                following = self.reply = _LocalLiveWatchReply()
            status = self.chat.submit(words)
            if status in ("steered", "redirected", "queued"):
                with self.lock:
                    if self.reply is following:
                        self.reply = None
                    self._say(LOCAL_LIVE_WATCH_QUEUED_LINE if status == "queued" else LOCAL_LIVE_WATCH_STEERED_LINE,
                              turn_id, generation)
        except Exception as exc:  # noqa: BLE001 — said on the call; the next turn tries again
            logger.info("A Watch Local Live turn didn't reach Hermes: %s",
                        exc if isinstance(exc, watch_jobs.WatchJobError) else type(exc).__name__)
            with self.lock:
                if following is not None and self.reply is following:
                    self.reply = None
                self._say(LOCAL_LIVE_WATCH_FAILURE_LINE, turn_id, generation)

    def _settle(self, running: _LocalLiveWatchReply, status: str) -> bool:
        """What Hermes did with a correction: True when nothing was running to
        take it, so the words start a turn of their own."""
        with self.lock:
            # The reply ended meanwhile (an error said so): nothing more here.
            if self.reply is not running:
                return False
            held = running.held_completion
            running.correcting = False
            running.held_completion = _NO_COMPLETION
            if status == "redirected":
                # The running reply carries on with the new words; a
                # completion that came first was the cut-off reply's.
                return False
            if status == "queued":
                # Hermes runs the words after the reply it was finishing. If
                # that reply already ended, the next completion is theirs.
                running.skips_interruption = False
                if held is _NO_COMPLETION:
                    running.skips_completions += 1
                return False
            self.reply = None
            return True

    # Hermes' events

    def observe(self, kind: str, payload: Dict[str, Any]) -> None:
        with self.lock:
            current = self.reply
            if current is None:
                return
            if kind in _LOCAL_LIVE_WATCH_WRITING:
                # Hermes is writing again after the correction: a marker
                # after this ends the reply.
                if not current.correcting:
                    current.skips_interruption = False
            elif kind == "message.delta":
                text = payload.get("text")
                # Until Hermes takes the correction, the words are the reply
                # the user talked over: the new turn doesn't say them.
                if current.correcting or not isinstance(text, str):
                    return
                current.skips_interruption = False
                self._speak(text)
            elif kind == "message.complete":
                content = next((payload[key] for key in ("content", "text", "rendered")
                                if isinstance(payload.get(key), str)), None)
                # The notice for a reply a correction cut off is never its answer.
                if content is not None and content.strip().lower() == _LOCAL_LIVE_INTERRUPTION_NOTICE:
                    return
                if current.correcting:
                    current.held_completion = content
                    return
                if current.skips_completions > 0:
                    current.skips_completions -= 1
                    return
                self.reply = None
                if self.turn is not None and not self.spoke and content:
                    self._speak(content)
                if self.turn is not None:
                    self._finish(self.turn[0])
            elif kind in _LOCAL_LIVE_WATCH_INTERRUPTED:
                if current.skips_interruption:
                    current.skips_interruption = False
                    return
                self.reply = None
                if self.turn is not None:
                    self._finish(self.turn[0])
            elif kind == "error":
                self.reply = None
                if self.turn is not None:
                    self._say(LOCAL_LIVE_WATCH_ERROR_LINE, self.turn[0], self.generation)
            elif kind == "approval.request" and self.watch_approvals:
                # Answered on the Watch's card: the reply goes on after it.
                if self.turn is not None and not self.approval_told:
                    self.approval_told = True
                    self._interject(LOCAL_LIVE_WATCH_APPROVAL_LINE)
            elif kind in _LOCAL_LIVE_WATCH_QUESTIONS:
                # Hermes waits on the user: the call says where to answer,
                # and the reply is still followed for the turn after.
                if self.turn is not None:
                    self._say(LOCAL_LIVE_WATCH_NEEDS_INPUT_LINE, self.turn[0], self.generation)

    # Speaking (under lock)

    def _speak(self, text: str) -> None:
        if self.turn is None or not text:
            return
        self.spoke = True
        spoken = self.spoken.feed(text)
        if spoken:
            self.turn[1]("delta", spoken + " ")

    def _interject(self, line: str) -> None:
        """A line of the call's own in the reply, which then goes on. It
        isn't the reply: one that comes whole at its end is still said."""
        if self.turn is None:
            return
        rest = self.spoken.finish()
        self.turn[1]("delta", (rest + " " if rest else "") + line + " ")

    def keepalive(self) -> None:
        """The open turn is still wanted (the call waits on the user)."""
        with self.lock:
            if self.turn is not None:
                self.turn[1]("keepalive", "")

    def _say(self, line: str, turn_id: str, generation: int) -> None:
        """A line of the call's own instead of Hermes' reply."""
        if generation != self.generation or self.turn is None or self.turn[0] != turn_id:
            return
        self.spoke = True
        # What the reply still held ends with it: nothing of it follows the line.
        self.spoken = _SpokenText()
        self.turn[1]("delta", line)
        self._finish(turn_id)

    def _finish(self, turn_id: str) -> None:
        if self.turn is None or self.turn[0] != turn_id:
            return
        deliver = self.turn[1]
        rest = self.spoken.finish()
        if rest:
            deliver("delta", rest)
        self.spoken = _SpokenText()
        self.turn = None
        deliver("done", "")


_LOCAL_LIVE_YES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead", "do it", "approve", "approve it",
    "allow it", "run it", "yes please", "yes go ahead", "yes do it", "yes run it", "yes allow it",
})
_LOCAL_LIVE_NO = frozenset({
    "no", "nope", "nah", "deny", "deny it", "don't", "do not", "don't run it", "don't do it", "no thanks",
    "no thank you", "no don't", "stop", "cancel", "no deny it",
})
_LOCAL_LIVE_ASKS = frozenset({
    "what", "why", "how", "which", "who", "when", "where", "is", "are", "does", "do", "can", "could",
    "will", "would", "should", "explain", "tell",
})


def _local_live_yes_no(words: str) -> Optional[bool]:
    """A plain spoken yes (True) or no (False) to an approval; None for
    anything else, which approves nothing."""
    plain = " ".join(re.sub(r"[^\w\s']", " ", words.lower()).split())
    if plain in _LOCAL_LIVE_YES:
        return True
    if plain in _LOCAL_LIVE_NO:
        return False
    return None


def _local_live_asks(words: str) -> bool:
    """Words that ask about the choice rather than make it."""
    first = words.lower().split()[0].strip(",.!?") if words.split() else ""
    return words.rstrip().endswith("?") or first in _LOCAL_LIVE_ASKS


def _local_live_question_line(question: Dict[str, Any]) -> str:
    choices = question.get("choices") or []
    line = f"Next: {question.get('question', '')}"
    if choices:
        line += " " + " ".join(f"{number}: {choice}." for number, choice in enumerate(choices, 1))
        line += " Say the number of your choice, or answer in your own words."
    else:
        line += " Answer in your own words."
    if question.get("may_leave_blank"):
        line += " Or say skip to leave it blank."
    return line


def _local_live_call_answer(grant: watch_tools._WatchGrant, words: str) -> Optional[str]:
    """A Watch Local Live call that answers a call from Hermes: the user's
    words answer what Hermes waits on in the call's chat (answer_call_waiting,
    as answer_call does for the other modes), and this is what the call says
    after. A question takes any words, by number, label or "the recommended
    one", "skip" leaves one of a form blank, and words that ask go as a
    request to explain first. An approval
    takes only a plain yes or no, and only when the user lets the Watch
    answer approvals by voice; a question about it denies it with the
    question, so Hermes explains and asks again. None: nothing waits, or the
    words don't answer it, and they go to the call's own chat."""
    store_module = hermes_calls._calls_store()
    with core._profile_scope(grant.profile):
        store = store_module.CallStore(core._calls_home())
        for session_id in grant.answer_sessions:
            waiting = store.waiting_for(session_id)
            if not waiting:
                continue
            if waiting["kind"] == "question":
                if all(question["answered"] for question in waiting["questions"]):
                    continue
                lead = store_module.ASKS_FIRST_LEAD
                answer = f"{lead} {words}" if _local_live_asks(words) and not words.casefold().startswith(lead.casefold()) else words
                result = store.answer_waiting(session_id, answer=answer)
                if result.get("status") != "answered" or "answer" not in result:
                    return result.get("message") or None
                if answer.casefold().startswith(lead.casefold()):
                    return "I've asked Hermes. It will explain, then ask again."
                line = ("Okay, I'll leave that blank." if result.get("skipped")
                        else f"Got it: {result['answer']}. Hermes has your answer.")
                if isinstance(result.get("next"), dict):
                    line += " " + _local_live_question_line(result["next"])
                return line
            if waiting.get("answered"):
                continue
            said = _local_live_yes_no(words)
            asks = said is None and _local_live_asks(words)
            if said is None and not asks:
                return None
            if not grant.answer_approvals:
                return "Hermes is waiting on your approval in that chat. Answer it in Conduit on your iPhone."
            if asks:
                result = hermes_calls._approval_delivered(store, store.answer_waiting(
                    session_id, choice="deny", reason=f"{store_module.ASKS_FIRST_LEAD} {words}"))
                return ("I've asked Hermes. It will explain, then ask again."
                        if result.get("status") == "answered" else _local_live_undelivered(result))
            result = hermes_calls._approval_delivered(store, store.answer_waiting(session_id, choice="once" if said else "deny"))
            if result.get("status") != "answered":
                return _local_live_undelivered(result)
            return "Approved once. Hermes runs it now." if result.get("choice") == "once" else "Denied. Hermes won't run it."
    return None


def _local_live_undelivered(result: Dict[str, Any]) -> Optional[str]:
    """What a Watch Local Live call says when its approval answer didn't
    reach Hermes (_approval_delivered), to the user rather than a model."""
    if result.get("status") == "too_late":
        return "That didn't go through: Hermes wasn't waiting on it any more. Check the chat in Conduit."
    if result.get("status") == "failed":
        return "That didn't go through, so nothing changed. Answer it in Conduit on your iPhone."
    if result.get("status") == "unconfirmed":
        return "Hermes hasn't confirmed it yet. Check the chat in Conduit."
    if result.get("status") == "refused":
        return "I can't approve that one by voice. Say no to deny it, or approve it in Conduit on your iPhone."
    return result.get("message") or None


class _LocalLiveWatchCall:
    """A Watch's Local Live call, where the model address sends its turns
    (_local_live_calls, beside the phone's _LocalLiveCall).

    Turns start on the dashboard's loop, where the model address waits on
    their frames; the call itself runs on the Watch bridge's loop. Frames
    cross back with call_soon_threadsafe.
    """

    speaks = False

    def __init__(self, session: "_LocalLiveWatchSession", profile: str) -> None:
        self.session = session
        self.profile = profile
        self.turns: Dict[str, Tuple[local_live._LocalLiveTurn, asyncio.AbstractEventLoop, bool]] = {}
        self.numbers = itertools.count(1)
        self.closed = False
        # As _LocalLiveCall's: turns never overlap. Used on the loop the
        # turns start on: the dashboard's for the model address, the
        # bridge's for a read-back server (which never calls the model
        # address), never both for one call.
        self.turn_lock = asyncio.Lock()

    def take_words(self, text: str) -> bool:
        return False

    async def start_turn(self, text: str) -> Optional[local_live._LocalLiveTurn]:
        async with self.turn_lock:
            return await self._start_turn(text)

    async def _start_turn(self, text: str) -> Optional[local_live._LocalLiveTurn]:
        if self.closed:
            return None
        for old, _, _ in list(self.turns.values()):
            await self.cancel_turn(old)
        loop = asyncio.get_running_loop()
        turn = local_live._LocalLiveTurn(f"w{next(self.numbers)}")
        asks_watch = text.strip().startswith(LOCAL_LIVE_MARKER_PREFIX)
        self.turns[turn.turn_id] = (turn, loop, asks_watch)
        if asks_watch:
            # The Watch's readiness check: the Watch answers it.
            if not await self.session.to_watch({"type": "conduit.turn", "turn_id": turn.turn_id, "text": text}):
                self.turns.pop(turn.turn_id, None)
                return None
            return turn
        runner = self.session.runner
        if runner is None:
            self.turns.pop(turn.turn_id, None)
            return None
        runner.start(turn.turn_id, text, lambda kind, words: self.deliver(turn.turn_id, kind, words))
        return turn

    async def cancel_turn(self, turn: local_live._LocalLiveTurn) -> None:
        entry = self.turns.pop(turn.turn_id, None)
        if entry is None:
            return
        turn.frames.put_nowait(("done", ""))
        if entry[2]:
            await self.session.to_watch({"type": "conduit.turn.cancel", "turn_id": turn.turn_id})
        elif self.session.runner is not None:
            self.session.runner.cancel(turn.turn_id)

    def deliver(self, turn_id: str, kind: str, text: str) -> None:
        """Part of a turn's reply, or its end, from any thread."""
        entry = self.turns.get(turn_id)
        if entry is None:
            return
        with contextlib.suppress(RuntimeError):  # the dashboard's loop already closed
            entry[1].call_soon_threadsafe(self._deliver, turn_id, kind, text)

    def _deliver(self, turn_id: str, kind: str, text: str) -> None:
        entry = self.turns.get(turn_id)
        if entry is None:
            return
        turn = entry[0]
        if kind == "delta":
            if text and turn.chars < local_live.LOCAL_LIVE_MAX_REPLY_CHARS:
                text = text[:min(local_live.LOCAL_LIVE_MAX_DELTA_CHARS, local_live.LOCAL_LIVE_MAX_REPLY_CHARS - turn.chars)]
                turn.chars += len(text)
                turn.frames.put_nowait(("delta", text))
        elif kind == "keepalive":
            turn.frames.put_nowait(("keepalive", ""))
        else:
            self.turns.pop(turn_id, None)
            turn.frames.put_nowait(("done", ""))

    def watch_frame(self, frame: Dict[str, Any]) -> None:
        """The Watch's answer to a turn it was sent (the readiness check)."""
        turn_id = str(frame.get("turn_id") or "")
        entry = self.turns.get(turn_id)
        if entry is None or not entry[2]:
            return
        if frame.get("type") == "conduit.turn.delta":
            text = frame.get("text")
            if isinstance(text, str) and text:
                self.deliver(turn_id, "delta", text)
        elif frame.get("type") in ("conduit.turn.done", "conduit.turn.error"):
            self.deliver(turn_id, "done", "")

    def close(self) -> None:
        self.closed = True
        for turn_id in list(self.turns):
            self.deliver(turn_id, "done", "")


class _LocalLiveWatchSession(watch_audio._WatchAudioSession):
    """Local Live on the Watch: the profile's speech server, relayed as for
    Grok, with Hermes answering its turns here."""

    engine = LOCAL_LIVE_WATCH_ENGINE

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.upstream: Any = None
        self.pacer: Optional[watch_audio._WatchAudioPacer] = None
        self.call: Optional[_LocalLiveWatchCall] = None
        self.runner: Optional[_LocalLiveWatchTurns] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # The Watch answers Hermes' approvals itself (approvals: true in its
        # start): the oldest pending one is on its card, by id and choices.
        self.watch_approvals = self.request.get("approvals") is True
        self.shown_approval: Optional[Tuple[str, Tuple[str, ...]]] = None
        self.approval_timeout = LOCAL_LIVE_WATCH_APPROVAL_TIMEOUT_S
        self.approval_timer: Optional[asyncio.TimerHandle] = None
        self.approval_show_lock = asyncio.Lock()
        self.chat: Optional[_WatchCallChat] = None
        # run() is over: its cards and answers are done too.
        self.finished = False
        # This call shows Hermes' requests on the Watch's card: the Watch
        # asked, and Hermes waits for an answer (a timeout above 0).
        self.cards = False
        # The call's turns, should its server only read text back.
        self.read_back: Optional[local_live_read_back._LocalLiveReadBack] = None
        # Why read-back ended the call: a refusal, so the Watch doesn't redial.
        self.read_back_failure: Optional[watch_audio.WatchAudioFailure] = None

    async def run(self) -> None:
        profile = self.profile
        loop = self.loop = asyncio.get_running_loop()

        def setup() -> Tuple[str, str]:
            status = local_live_watch_status(profile)
            if status["runtime"] != "ready" and status["reason"] != LOCAL_LIVE_WATCH_NO_SESSIONS:
                raise core.TokenError(503, str(status["reason"]))
            return speech_server._speech_server_setup(profile)

        try:
            url, key = await asyncio.wait_for(core._run_scoped(profile, setup, speech_server._speech_server_executor),
                                              timeout=speech_server.SPEECH_SERVER_SETUP_TIMEOUT_S)
            # Outside any profile's scope: importing it reads Hermes' home.
            server = await asyncio.wait_for(loop.run_in_executor(_local_live_watch_executor, _watch_chat_session_api),
                                            timeout=speech_server.SPEECH_SERVER_SETUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise watch_audio.WatchAudioFailure("unreachable", "The Hermes host timed out reading the speech server settings")
        except core.TokenError as exc:
            raise watch_audio._watch_audio_failure(exc)
        if server is None:
            raise watch_audio.WatchAudioFailure("unavailable", LOCAL_LIVE_WATCH_NO_SESSIONS)
        # The speech server's turns go to the profile's newest live call: one
        # running elsewhere (the phone, another Watch grant) would take them.
        if any(not call.speaks and not self._rejoins(call) for call in local_live._local_live_open_calls(profile)):
            raise watch_audio.WatchAudioFailure("busy", "A Local Live call is already running on this Hermes profile")
        open_key = core._limiter_key(profile)
        if not speech_server._speech_server_take_open(open_key):
            raise watch_audio.WatchAudioFailure("busy", "Too many speech server connections are open on this host")
        upstream: Any = None
        call: Optional[_LocalLiveWatchCall] = None
        tasks: list = []
        try:
            upstream = await self._dial(url, key)
            # After the server answered: a call that can't reach it opens no chat.
            chat, resumed = await self._chat(server)
            runner = self.runner = _LocalLiveWatchTurns(chat)
            self.chat = chat
            grant = self.bridge.grant
            if getattr(grant, "answer_sessions", ()):
                runner.answer_call = lambda words: _local_live_call_answer(grant, words)
            if self.watch_approvals:
                with contextlib.suppress(Exception):
                    self.approval_timeout = await asyncio.wait_for(
                        core._run_scoped(profile, _hermes_approval_timeout, speech_server._speech_server_executor),
                        timeout=speech_server.SPEECH_SERVER_SETUP_TIMEOUT_S)
                # A Hermes that denies at once (a timeout of 0) has no card
                # to show, nor a line to point to one.
                self.cards = self.approval_timeout > 0
                runner.watch_approvals = self.cards
                if self.cards:
                    chat.on_approvals = self._approvals_changed
            # Last: a resumed chat's events reach a runner that already knows
            # where its approvals go.
            chat.listener = runner.observe
            call = self.call = _LocalLiveWatchCall(self, local_live._local_live_profile_key(profile))
            if not local_live._local_live_register(call, self._rejoins):
                raise watch_audio.WatchAudioFailure("busy", "A Local Live call is already running on this Hermes profile")
            pacer = self.pacer = watch_audio._WatchAudioPacer(self.send, LOCAL_LIVE_WATCH_RATE)
            read_back = self.read_back = local_live_read_back._LocalLiveReadBack(call, self._to_watch_event, self._fail)
            read_back.attach(upstream.send)
            # Set first: the Watch answers "started" with its session.update.
            self.upstream = upstream
            await self.bridge.control(self.stream, {
                "type": "started", "engine": self.engine, "voice": None,
                "input_rate": LOCAL_LIVE_WATCH_RATE, "output_rate": LOCAL_LIVE_WATCH_RATE,
                "session_id": chat.public_id, "resumed": resumed,
            })
            # A rejoin while Hermes waits on an approval: shown again.
            await self._show_approval()
            pacing = asyncio.ensure_future(pacer.run())
            reader = asyncio.ensure_future(self._read(upstream, pacer))
            tasks = [reader, pacing]
            hung = await self._wait_or_hang(reader, tasks)
            if self.read_back_failure is not None:
                raise self.read_back_failure
            if hung:
                logger.warning("A Watch Local Live call: the speech server left %s unanswered; ending the session",
                               self.owed_event)
                raise watch_audio.WatchAudioFailure("unreachable", "The speech server stopped answering")
            if reader.done() and not self.stopping.is_set():
                code, reason = grok_live._forwardable_close(getattr(upstream, "close_code", None),
                                                  str(getattr(upstream, "close_reason", "") or ""))
                self.reason = reason or ("The speech server ended the call" if code in (1000, 1001)
                                         else "The connection to the speech server was lost")
        finally:
            self.upstream = None
            # No more cards from here, however this ended.
            self.finished = True
            if self.chat is not None and self.chat.on_approvals == self._approvals_changed:
                self.chat.on_approvals = None
            if self.approval_timer is not None:
                self.approval_timer.cancel()
                self.approval_timer = None
            if self.read_back is not None:
                self.read_back.close()
            if call is not None:
                local_live._local_live_unregister(call)
                call.close()
            if self.runner is not None:
                if self.runner.chat.listener == self.runner.observe:
                    self.runner.chat.listener = None
                self.runner.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if upstream is not None:
                with contextlib.suppress(Exception):
                    await upstream.close()
            speech_server._speech_server_give_open(open_key)

    async def _chat(self, server: Any) -> Tuple[_WatchCallChat, bool]:
        """The call's chat: the one a rejoining Watch names, else a new one."""
        chats = self.bridge.call_chats
        wanted = self.request.get("resume_session_id")
        if isinstance(wanted, str) and wanted:
            chat = chats.get(wanted)
            if chat is not None and not chat.closed:
                return chat, True
        if len(chats) >= LOCAL_LIVE_WATCH_CHATS_PER_GRANT:
            raise watch_audio.WatchAudioFailure("busy", "This call has started as many chats as it may")
        chat = _WatchCallChat(server, self.profile)
        opening = asyncio.get_running_loop().run_in_executor(_local_live_watch_executor, chat.open)
        try:
            await asyncio.wait_for(asyncio.shield(opening), LOCAL_LIVE_WATCH_CHAT_TIMEOUT_S)
        except watch_jobs.WatchJobError as exc:
            raise watch_audio.WatchAudioFailure("failed", f"Hermes couldn't open the call's chat: {exc}")
        except BaseException as exc:
            # Timed out or cancelled while Hermes opens it: closed once it
            # has, so no chat is left without a call.
            opening.add_done_callback(lambda _: _local_live_watch_executor.submit(chat.close))
            if isinstance(exc, asyncio.TimeoutError):
                raise watch_audio.WatchAudioFailure("unreachable", "Hermes took too long to open the call's chat")
            raise
        replaced = chats.get(chat.public_id)
        if replaced is not None and replaced is not chat:
            # Hermes named a chat this grant already had: the old one closes.
            _local_live_watch_executor.submit(replaced.close)
        chats[chat.public_id] = chat
        return chat, False

    async def _dial(self, url: str, key: str) -> Any:
        try:
            return await speech_server._connect_speech_server(url, speech_server._speech_server_headers(key))
        except grok_live.RealtimeUpstreamRefused as exc:
            code, message = speech_server._speech_server_refusal(exc.status)
            raise watch_audio.WatchAudioFailure("refused" if code == speech_server.SPEECH_SERVER_CLOSE_REFUSED else "unreachable", message)
        except Exception as exc:
            # The type only: the text could quote the address.
            logger.warning("A Watch Local Live call couldn't reach the speech server: %s", type(exc).__name__)
            raise watch_audio.WatchAudioFailure("unreachable", "Could not reach the speech server from the Hermes host")

    async def _read(self, upstream: Any, pacer: watch_audio._WatchAudioPacer) -> None:
        with contextlib.suppress(Exception):
            async for message in upstream:
                self.answered()
                if not isinstance(message, str):
                    continue
                if self.read_back is not None:
                    message = await self.read_back.from_server(message)
                    if message is None:
                        continue
                try:
                    event = json.loads(message)
                except ValueError:
                    continue
                kind = event.get("type") if isinstance(event, dict) else None
                if kind in watch_audio._GROK_AUDIO_DELTAS:
                    try:
                        pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
                    except (ValueError, TypeError):
                        continue
                    pacer.audio(pcm)
                    continue
                if kind == "input_audio_buffer.speech_started":
                    pacer.interrupt()
                pacer.event(message)

    # Approvals on the Watch


    def waiting_on_hermes(self) -> bool:
        # The speech server says nothing until the call's Hermes turn answers it.
        # Read without the runner's lock: a stale read moves the clock one check.
        runner = self.runner
        return runner is not None and runner.turn is not None

    def _rejoins(self, other: Any) -> bool:
        """This Watch's earlier call, still winding down: a rejoin may start."""
        return isinstance(other, _LocalLiveWatchCall) and other.session.bridge is self.bridge
    def _approvals_changed(self) -> None:
        """From Hermes' threads: the chat's pending approvals changed."""
        loop = self.loop
        if loop is None or self.stopping.is_set() or self.finished:
            return
        with contextlib.suppress(RuntimeError):  # the bridge's loop already closed
            loop.call_soon_threadsafe(lambda: local_live._local_live_spawn(self._show_approval()))

    def _approval_choices(self, approvals: List[Dict[str, Any]]) -> Tuple[str, ...]:
        """Once only while exactly one is pending and shown whole: Hermes
        approves its oldest, and its queue can drop one the plugin never hears
        of (answered in Conduit), so with more the card might not be it."""
        if len(approvals) == 1 and not approvals[0].get("cut"):
            return LOCAL_LIVE_WATCH_APPROVAL_CHOICES
        return ("deny",)

    def _approval_margin(self) -> float:
        return min(LOCAL_LIVE_WATCH_APPROVAL_MARGIN_S, self.approval_timeout / 10)

    def _purge_expired_approvals(self) -> None:
        """Drops the requests past Hermes' timeout (before an answer is checked)."""
        self._pending_approvals()

    def _pending_approvals(self) -> List[Dict[str, Any]]:
        """The chat's pending approvals, those past Hermes' timeout dropped."""
        chat = self.chat
        if chat is None:
            return []
        now = time.monotonic()
        limit = self.approval_timeout - self._approval_margin()
        with chat.approvals_lock:
            while chat.approvals and not chat.answering and now - chat.approvals[0]["at"] > limit:
                chat.approvals.popleft()
            return list(chat.approvals)

    async def _show_approval(self) -> None:
        """The oldest pending approval on the Watch's card, or the card gone.
        One look at a time: a look that waited on the Watch never sends a
        card another has already taken away."""
        async with self.approval_show_lock:
            await self._show_approval_now()

    async def _show_approval_now(self) -> None:
        if not self.cards or self.stopping.is_set() or self.finished or self.chat is None:
            return
        approvals = self._pending_approvals()
        if self.approval_timer is not None:
            self.approval_timer.cancel()
            self.approval_timer = None
        # What the Watch was told is only recorded once it's sent: a send
        # that didn't go is sent again at the next look.
        if not approvals:
            if self.shown_approval is not None and await self.to_watch(
                    {"type": "conduit.approval.gone", "id": self.shown_approval[0]}):
                self.shown_approval = None
            return
        # The turn waits on the user: kept open while it does.
        if self.runner is not None:
            self.runner.keepalive()
        oldest = approvals[0]
        # Looked at again when Hermes gives up on it, and to keep the turn
        # open. One already past it waits on its answer (in flight): no
        # sooner than the keepalive then.
        expires = self.approval_timeout - self._approval_margin() - (time.monotonic() - oldest["at"])
        wait = (min(expires + 0.05, LOCAL_LIVE_WATCH_APPROVAL_KEEPALIVE_S) if expires > 0
                else LOCAL_LIVE_WATCH_APPROVAL_KEEPALIVE_S)
        if self.chat.answering:
            # Hermes is taking an answer: the next card waits for it, and
            # the answer's end looks again.
            return
        if not self.finished:
            self.approval_timer = asyncio.get_running_loop().call_later(
                wait, lambda: local_live._local_live_spawn(self._show_approval()))
        choices = self._approval_choices(approvals)
        card = (oldest["id"], choices)
        if self.shown_approval == card:
            return
        if self.shown_approval is not None and self.shown_approval[0] != oldest["id"]:
            if not await self.to_watch({"type": "conduit.approval.gone", "id": self.shown_approval[0]}):
                return
            self.shown_approval = None
        sent = await self.to_watch({"type": "conduit.approval", "id": oldest["id"], "command": oldest["command"],
                                    "description": oldest["description"], "choices": list(choices),
                                    "pending": len(approvals), "cut": bool(oldest.get("cut"))})
        if sent:
            # Not otherwise: the Watch still holds the card it had, so the
            # next look sends this again, or takes that one away.
            self.shown_approval = card

    async def _answer_approval(self, frame: Dict[str, Any]) -> None:
        """The Watch's answer: only to the card it was shown (the oldest, the
        one Hermes answers), and only with a choice that card offered."""
        chat = self.chat
        if chat is None or not self.cards or self.stopping.is_set() or self.finished:
            return
        self._purge_expired_approvals()
        choice = frame.get("choice")
        with chat.approvals_lock:
            approvals = list(chat.approvals)
            oldest = approvals[0] if approvals else None
            # The same card's answer again while Hermes takes the first (a
            # second tap): nothing to say, the first one's done follows.
            repeat = chat.answering and oldest is not None and frame.get("id") == oldest["id"]
            # One answer at a time, to the card shown: a second tap or a
            # rejoined stream's answer would reach Hermes' next request.
            taken = (oldest is not None and not chat.answering and frame.get("id") == oldest["id"]
                     and choice in self._approval_choices(approvals))
            if taken:
                chat.answering = True
        if repeat:
            return
        if not taken:
            await self.to_watch({"type": "conduit.approval.done", "id": str(frame.get("id") or "")[:64],
                                 "resolved": False})
            await self._show_approval()
            return
        try:
            resolved = await asyncio.get_running_loop().run_in_executor(
                _local_live_watch_executor, chat.respond, choice, oldest["request_id"])
        except Exception as exc:  # noqa: BLE001 — not taken: the Watch hears so, and is shown it again
            logger.info("A Watch call's approval answer didn't reach Hermes: %s", type(exc).__name__)
            resolved = None
        finally:
            with chat.approvals_lock:
                chat.answering = False
        if resolved is not None:
            # Answered, or gone in Hermes (timed out): either way not pending.
            with chat.approvals_lock:
                if chat.approvals and chat.approvals[0] is oldest:
                    chat.approvals.popleft()
        self.shown_approval = None
        if self.finished:
            # A stream that took over meanwhile shows the next card now.
            notify = chat.on_approvals
            if notify is not None:
                notify()
            return
        await self.to_watch({"type": "conduit.approval.done", "id": oldest["id"], "resolved": bool(resolved)})
        # The next one, or this one again when the answer didn't get there.
        await self._show_approval()

    async def _to_watch_event(self, text: str) -> None:
        """A server event (or one the read-back turns make) for the Watch, as
        _read passes them on."""
        pacer = self.pacer
        if pacer is None:
            return
        if '"input_audio_buffer.speech_started"' in text:
            pacer.interrupt()
        pacer.event(text)

    async def _fail(self, reason: str) -> None:
        self.read_back_failure = watch_audio.WatchAudioFailure("refused", reason)
        self.stop(reason)

    async def to_watch(self, payload: Dict[str, Any]) -> bool:
        """An event for the Watch, from any loop; False once it can't go."""
        loop = self.loop
        if loop is None or self.stopping.is_set() or self.finished:
            return False
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            if asyncio.get_running_loop() is loop:
                return await self.send(watch_audio.WATCH_AUDIO_EVENT, data)
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.send(watch_audio.WATCH_AUDIO_EVENT, data), loop))
        except RuntimeError:  # the bridge's loop already closed
            return False

    async def audio(self, pcm: bytes) -> None:
        upstream = self.upstream
        if upstream is not None and pcm:
            with contextlib.suppress(Exception):
                await upstream.send(json.dumps({"type": "input_audio_buffer.append",
                                                "audio": base64.b64encode(pcm).decode("ascii")}))

    async def event(self, data: bytes) -> None:
        upstream = self.upstream
        if upstream is None or len(data) > speech_server.SPEECH_SERVER_MAX_CLIENT_FRAME_BYTES:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        if '"conduit.' in text:
            # The call's own frames stay here, as on /local-live/socket: the
            # Watch's answers to the turns it was sent, and its keepalive.
            try:
                frame = json.loads(text)
            except ValueError:
                return
            kind = frame.get("type") if isinstance(frame, dict) else None
            if isinstance(kind, str) and kind.startswith("conduit."):
                if kind.startswith("conduit.turn.") and self.call is not None:
                    self.call.watch_frame(frame)
                elif kind == "conduit.approval.answer" and self.cards and not self.finished:
                    local_live._local_live_spawn(self._answer_approval(frame))
                return
        if self.read_back is not None and await self.read_back.from_client(text):
            return
        await self.send_asking(upstream, text)


_WATCH_AUDIO_SESSIONS = {"gpt_live": watch_audio._GptLiveWatchSession, "grok": watch_audio._GrokWatchSession,
                         LOCAL_LIVE_WATCH_ENGINE: _LocalLiveWatchSession}
