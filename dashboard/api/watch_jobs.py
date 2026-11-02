"""Watch jobs through the relay."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import core, voice_calls, watch_tools

logger = core.logger


# --- Watch jobs through the relay ---------------------------------------------
#
# With jobs in its grant, a Watch call's start_job, list_jobs and cancel_job
# run here instead of on the iPhone, so they work with the wrist down. The
# plugin drives Hermes' own session API in this dashboard process (the
# methods the app calls over its WebSocket) through a transport of its own:
# each job is an ordinary Hermes chat on the grant's profile, filed under
# Voice Jobs like a job the phone started. Two more calls come only from the
# Watch app, never from the model as such: job_news (settled jobs and
# approval requests, held until there is some) and answer_approval (Approve
# or Deny on the Watch, or by voice when the user turned that on, after the
# Watch's own checks). Sealed with the same key, the host can't tell who
# sent a call: the Watch app holds that line, and must never pass the model
# either call as it is. Hermes'
# own approval settings decide what needs approving; the Watch can only
# approve once or deny. (hermes-conduit designs/apple-watch-voice-direct.md,
# "Wrist-down jobs through the relay")
#
# interrupt_job puts the user's words into a job Hermes is still working on
# (a correction, "hold that", "never mind"), as the phone's live calls do
# (Conduit #451/#455): Hermes' session.redirect, which keeps the work so far
# and ends the turn with one completion. One with kind "add" only adds to the
# work, so it is steered in instead (Conduit #1076). It comes with every grant that can
# start or cancel jobs, so the Watch never needs to name it (an older plugin
# would refuse a grant that did); named, it is taken rather than refused.

WATCH_JOB_TOOLS = ("start_job", "list_jobs", "cancel_job")
# The job tools that change jobs: a grant with one also takes corrections.
WATCH_JOB_WRITE_TOOLS = ("start_job", "cancel_job")
WATCH_JOB_CALLS = ("job_news", "answer_approval")
WATCH_JOB_FOLLOW_UP = "interrupt_job"
# Tries and the wait between them while Hermes isn't working on the job
# just then, as the phone (VoiceBackgroundJobSupervisor.followUpAttempts,
# followUpRetryInterval); waits for a job still starting count up to three
# times as many.
WATCH_FOLLOW_UP_ATTEMPTS = 4
WATCH_FOLLOW_UP_RETRY_S = 1.0
# Another follow-up to the same job goes first: this one waits at most this
# long for it. A follow-up is answered within WATCH_FOLLOW_UP_DEADLINE_S in
# all, inside the relay's 25 s wait.
WATCH_FOLLOW_UP_QUEUE_S = 8.0
WATCH_FOLLOW_UP_DEADLINE_S = 22.0
# An end held for words Hermes queued settles the job if the words' turn
# shows no sign of life for this long (the phone's liveness poll does it there).
WATCH_FOLLOW_UP_HOLD_S = 30.0
# Frames from the session renew that wait, but never past this in all: a
# session that only chatters doesn't keep the job open forever.
WATCH_FOLLOW_UP_HOLD_MAX_S = 180.0
# Hermes' marker for a reply a correction cut off, never a result
# (Conduit's MessageNormalizer.isUserCorrectionInterruptionNotice).
WATCH_CORRECTION_NOTICE = "[this response was interrupted by a user correction.]"
# Events that show a turn carrying on (Conduit's StreamEventParser names).
WATCH_TURN_PROGRESS = ("message.start", "message.delta", "reasoning.delta", "message.reasoning",
                       "message.reasoning.delta", "tool.start", "tool_call", "tool.complete", "tool_result")
# Jobs one call may start: the user's setting, capped here.
WATCH_JOBS_DEFAULT = 5
WATCH_JOBS_MAX = 20
# Running at once, as on the phone (VoiceBackgroundJobSupervisor.maximumActiveJobs).
WATCH_JOBS_RUNNING_MAX = 3
# A grant with jobs polls job_news while they run: the relay's own cap.
WATCH_JOB_GRANT_MAX_CALLS = 120
# A job_news call waits this long for news; the relay holds the Watch's
# request 25 s.
WATCH_JOB_NEWS_WAIT_S = 15.0
WATCH_JOB_RPC_TIMEOUT_S = 20.0
# A start_job is answered within this, inside the relay's 25 s wait; a
# start Hermes is still taking is answered "accepted" and goes on, its
# outcome then told as news (as the iPhone's broker answers within 8 s).
WATCH_JOB_START_ANSWER_S = 18.0
# Bytes as JSON, so a task that fits here also fits the sealed call
# (WATCH_MAX_CALL_BYTES); the Watch sends a longer one through the iPhone.
WATCH_JOB_MAX_INSTRUCTION_BYTES = 3_000
# As Conduit's VoiceBackgroundJobSupervisor: maximumResultCharacters and
# maximumTitleCharacters.
WATCH_JOB_RESULT_CHARS = 6_000
WATCH_JOB_TITLE_CHARS = 60
WATCH_JOB_MAX_OPTION_CHARS = 120
# The user's other profiles a call's jobs may run on, as the phone lists
# them ("for Fam, …"): at most this many, each a Hermes profile name.
WATCH_JOB_MAX_PROFILES = 32
_WATCH_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Bytes as JSON. A whole job_news answer, sealed, stays inside the relay's
# bound on an answer (WATCH_MAX_RESULT_CT_CHARS, about 17,980 bytes before
# sealing); more news than fits waits for the next call. A job's texts are
# cut so any one item fits next to every open approval.
WATCH_JOB_ANSWER_BYTES = 17_000
WATCH_JOB_RESULT_BYTES = 11_000
WATCH_JOB_COMMAND_BYTES = 6_000
WATCH_JOB_DETAIL_BYTES = 1_200
# What the Watch may answer an approval with: never "session" or "always".
WATCH_APPROVAL_CHOICES = ("once", "deny")
# Mirrors Conduit's VoiceBackgroundJobSupervisor.jobPrompt.
WATCH_JOB_PROMPT = ("[Background job started from a Conduit voice conversation. Nobody is watching this chat live, "
                    "so work on it on your own. When you are done, end your final message with a plain-language "
                    "summary that can be read aloud: what you found or did, with the key details.]\n\n{instructions}")
# The user's words for a running job, marked as the call's like the
# phone's (Conduit's threadTurnText): a job's chat that only voice used
# leaves Desktop's list once answered (voice_archive).
WATCH_JOB_FOLLOW_UP_PROMPT = "(voice) {words}"
# A later ordinary request of the call, sent into the chat its first one
# made (#1250).
WATCH_JOB_NEXT_PROMPT = ("[The next request from the same Conduit voice conversation. As before, end your final message "
                         "with a plain-language summary that can be read aloud.]\n\n{instructions}")
# How often a request waiting for the call's chat checks it again, besides
# being woken when a job settles.
WATCH_JOB_CHAT_WAIT_S = 2.0
# How long the call's chat waits for a cancelled request's turn to end
# before the next request goes in.
WATCH_JOB_INTERRUPT_END_S = 5.0
# How long after that a cancelled request's end may still come, and is
# told apart from the next request's own. Past it, an end is the next one's.
WATCH_JOB_LATE_END_S = 10.0
# Job ids are unique in this process, not per grant: a renewed grant's jobs
# never share an id with the last grant's, so a cancel can't hit the wrong
# job.
_watch_job_numbers = itertools.count(1)


class WatchJobError(Exception):
    """A Hermes session call for a Watch job that failed; the message is Hermes' own."""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def unsupported_redirect(self) -> bool:
        """Hermes has no session.redirect (Conduit's AppState.isUnsupportedRedirect)."""
        message = str(self).lower()
        return (self.code in (4010, -32601) or "does not support active-turn redirect" in message
                or "method not found" in message or "unknown method" in message)

    @property
    def unsupported_steer(self) -> bool:
        """Hermes has no session.steer (Conduit's AppState.isUnsupportedSteer)."""
        message = str(self).lower()
        return (self.code == -32601 or "method not found" in message
                or ("unknown method" in message and "session.steer" in message))


def _hermes_session_api() -> Any:
    """Hermes' session API module when it serves this process's chats, else None.

    Only a module the dashboard itself loaded counts: importing it here (a
    plugin host process, an older Hermes) would make a second, private
    registry whose chats the app never sees.
    """
    server = sys.modules.get("tui_gateway.server")
    if server is None or not callable(getattr(server, "dispatch", None)):
        return None
    sessions = getattr(server, "_sessions", None)
    return server if sessions is not None and callable(getattr(sessions, "get", None)) else None


def watch_job_title(instructions: str) -> str:
    """Mirrors Conduit's VoiceBackgroundJobSupervisor.title(for:)."""
    collapsed = " ".join(instructions.split())
    if len(collapsed) <= WATCH_JOB_TITLE_CHARS:
        return collapsed
    cut = collapsed[:WATCH_JOB_TITLE_CHARS]
    space = cut.rfind(" ")
    if space > WATCH_JOB_TITLE_CHARS // 2:
        return cut[:space] + "…"
    return cut + "…"


def _clip_job_result(text: str) -> str:
    """At most WATCH_JOB_RESULT_CHARS characters and WATCH_JOB_RESULT_BYTES
    bytes as JSON, marked once when cut."""
    return _json_clip(text[:WATCH_JOB_RESULT_CHARS], WATCH_JOB_RESULT_BYTES,
                      cut=len(text) > WATCH_JOB_RESULT_CHARS)


def _rpc_id(value: Any) -> str:
    """A JSON-RPC id as a string: ids can be numbers, 0 among them."""
    return "" if value is None else str(value)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _json_clip(text: str, limit: int, cut: bool = False) -> str:
    """``text`` cut so it takes at most ``limit`` bytes as a JSON string
    (quotes and escapes included), marked when cut here or already (``cut``)."""
    if not cut and _json_bytes(text) <= limit:
        return text
    marker = "\n[…]"
    room = limit - (_json_bytes(marker) - 2)
    if room < 2:
        # Too small for the marker: as much of the text as fits.
        marker, room = "", limit
    if _json_bytes(text) <= room:
        return text + marker
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_bytes(text[:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return text[:low] + marker


def _clean_job_options(value: Any) -> Dict[str, str]:
    """The phone's voice-job model settings for this profile, as it would send them."""
    if not isinstance(value, dict):
        return {}
    options = {key: value[key].strip() for key in ("model", "provider", "reasoning_effort")
               if isinstance(value.get(key), str) and 0 < len(value[key].strip()) <= WATCH_JOB_MAX_OPTION_CHARS}
    # A provider only goes with its model (HermesClient.createSession).
    if "model" not in options:
        options.pop("provider", None)
    return options


def _check_ticket_home(ticket_home: str, profile: Optional[str], what: str) -> None:
    """Refuses (403) a native ticket's request for a profile other than its home."""
    if not ticket_home:
        logger.warning("Watch tool %s refused: the native ticket's home is unknown", what)
        raise core.TokenError(403, "This ticket's profile isn't known here")
    name = (profile or "").strip()
    if name.lower() in ("", "current"):
        # The launch profile, the home this request runs in.
        try:
            from hermes_constants import get_hermes_home
            on_home = Path(get_hermes_home()).resolve() == Path(ticket_home).resolve()
        except Exception:  # noqa: BLE001 — an unresolvable home is on no home
            on_home = False
    else:
        on_home = core._job_profile_on_home(name, ticket_home)
    if not on_home:
        logger.warning("Watch tool %s refused: the request's profile isn't the native ticket's", what)
        raise core.TokenError(403, "This ticket can't reach another profile")


def _clean_job_profiles(value: Any, *, own: Optional[str],
                        ticket_home: Optional[str] = None) -> Tuple[str, ...]:
    """The user's other profiles a Watch call's jobs may run on, as the
    phone names them; the grant's own profile needs no naming. With a native
    ticket's ``ticket_home``, only the profiles on that very home stay."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > WATCH_JOB_MAX_PROFILES:
        raise core.TokenError(400, f"job_profiles must be a list of at most {WATCH_JOB_MAX_PROFILES} profile names")
    if not all(isinstance(name, str) and _WATCH_PROFILE_NAME.fullmatch(name) for name in value):
        raise core.TokenError(400, "Each job profile starts with a letter or digit, then up to 63 letters, digits, '.', '_' or '-'")
    names: List[str] = []
    seen = {core._limiter_key(own)}
    for name in value:
        # As Hermes' scope folds them: "current" is the grant's own profile.
        key = core._limiter_key(name)
        if key and key not in seen:
            seen.add(key)
            names.append(name)
    if ticket_home is None:
        return tuple(names)
    if not ticket_home:
        # A ticket whose home this Hermes didn't say: nothing may be named.
        return ()
    return tuple(name for name in names if core._job_profile_on_home(name, ticket_home))


def _keep_in_registry(profile: Optional[str], session_id: str, title: str, instructions: str) -> None:
    """The job kept in its bot's work registry (Bot workstreams M5): the
    Work panel lists it as a Watch call's, and a restart leaves it for the
    user. Best effort: the job runs as before whatever happens here."""
    try:
        home = core._profile_dir(profile) if profile else core._calls_home()
        tool = core._plugin_package_module("work_tool")
        tool = tool.copy_for(home) or tool
        tool.adopt(home, session_id, title, via="watch", goal=instructions[:2000])
    except Exception:  # noqa: BLE001
        logger.warning("Keeping a Watch job in its bot's work registry failed", exc_info=True)


def _registry_start_failed(profile: Optional[str], session_id: str, error: str) -> None:
    """A kept job whose prompt didn't go in: its registry row says it failed,
    as the model was told, rather than staying a start. Best effort."""
    if not session_id:
        return
    try:
        home = core._profile_dir(profile) if profile else core._calls_home()
        tool = core._plugin_package_module("work_tool")
        tool = tool.copy_for(home) or tool
        tool.call_start_failed(home, session_id, error)
    except Exception:  # noqa: BLE001
        logger.warning("Settling a Watch job's failed start in its bot's work registry failed", exc_info=True)


class _WatchJob:
    def __init__(self, job_id: str, title: str, profile: Optional[str] = None) -> None:
        self.job_id = job_id
        self.title = title
        # Another of the user's profiles it runs on; None: the grant's.
        self.profile = profile
        self.session_id = ""
        self.stored_session_id = ""
        self.status = "starting"
        self.result = ""
        self.error = ""
        # The pending approval: Hermes' request id, its server request's id
        # (for request.cancel), the redacted command and its description.
        self.approval: Optional[Dict[str, Any]] = None
        # Told to the Watch: the outcome, and the approval request last told
        # (None before any request has been told).
        self.outcome_told = False
        self.approval_told: Optional[str] = None
        # An answer to the pending approval is on its way to Hermes: a
        # second one would go to Hermes' next request when it has no id.
        self.approval_answering = False
        self.closed = False
        # The start's own answer: pending until the start settles or is
        # answered "accepted"; its outcome isn't news meanwhile.
        self.answer_pending = True
        self.answered_late = False
        self.start_answer: Optional[Dict[str, Any]] = None
        # While a cancel's interrupt is on its way: the end of the turn,
        # kept in case Hermes refuses the interrupt (cancel).
        self.cancelling = False
        self.held_end: Optional[Tuple[str, Dict[str, Any]]] = None
        # Its prompt went in and Hermes hasn't ended that turn yet; after a
        # cancel, the call's chat waits for that end until quiet_by (#1250).
        self.turn_open = False
        self.quiet_by = 0.0
        # A follow-up going into the running turn (interrupt_job): None,
        # "sending", "accepted" (Hermes took it into the turn) or "queued"
        # (Hermes runs it as the next turn). Until the turn carries on, an
        # end of turn may not be the last: it is held here.
        self.follow_up: Optional[str] = None
        self.held_completion: Optional[Tuple[str, Dict[str, Any]]] = None
        self.held_at = 0.0
        self.held_since = 0.0
        # Follow-ups to one job go one at a time, in the order they came.
        self.follow_up_lock = threading.Lock()
        # An ordinary request (not background, quick or another profile's):
        # the call's first one makes the chat its later ones go to (#1250).
        self.joins_call = False

    @property
    def active(self) -> bool:
        return self.status in ("starting", "running", "needs_approval")

    def news(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {"job_id": self.job_id, "title": self.title, "status": self.status,
                                "session_id": (self.stored_session_id or self.session_id)[:128],
                                **({"profile": self.profile} if self.profile else {})}
        if self.status == "finished":
            item["result"] = _clip_job_result(self.result)
        if self.error:
            item["error"] = _json_clip(self.error, WATCH_JOB_DETAIL_BYTES)
        if self.approval and self.status == "needs_approval":
            item["approval"] = {
                "request_id": self.approval["request_id"],
                "command": self.approval["command"],
                "cut": self.approval["cut"],
                "description": _json_clip(self.approval["description"], WATCH_JOB_DETAIL_BYTES),
                "fingerprint": self.approval["fingerprint"],
            }
        return item

    @property
    def approval_key(self) -> str:
        """Which approval request is pending, for telling each one once: its
        fingerprint, which holds its ids, so a request changed under the
        same id is news again too."""
        if not self.approval:
            return ""
        return self.approval["fingerprint"]


def _watch_approval_fingerprint(approval: Dict[str, Any], command: str) -> str:
    """Names the request as the Watch shows it: its command and description,
    and Hermes' ids for it. hermes-conduit-notifier#232 (N7-3). The whole
    ``command`` counts too, so two long ones cut alike still differ."""
    shown = [command, approval["command"], approval["cut"], approval["description"],
             approval["request_id"], approval["server_request_id"]]
    return hashlib.sha256(json.dumps(shown, ensure_ascii=False).encode()).hexdigest()[:24]


class _WatchJobTransport:
    """This plugin's end of its jobs' sessions: Hermes writes their responses
    and events here, as it would to the app's WebSocket. ``_closed`` is what
    Hermes' session reaper reads."""

    # A Watch job's reply is a Conduit one (reply_sources.py, #115).
    conduit_client = "conduit"
    # And came from the Watch, spoken (turn_surface.py, hermes-conduit#888).
    conduit_surface = "watch"
    conduit_input = "spoken"

    def __init__(self, jobs: "_WatchJobs") -> None:
        self._jobs = jobs
        self._closed = False

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False
        try:
            self._jobs.frame(obj)
        except Exception as exc:  # noqa: BLE001 — Hermes' writer must never see our failure
            watch_tools._log_watch_failure("A Watch job event couldn't be read", exc)
        return True

    def close(self) -> None:
        self._closed = True


class _RpcWaiter:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class _WatchJobs:
    """One grant's jobs: started, followed and ended through Hermes' session API."""

    def __init__(self, grant: "watch_tools._WatchGrant", *, max_jobs: int, options: Dict[str, str], server: Any,
                 profiles: Tuple[str, ...] = ()) -> None:
        self.grant = grant
        self.max_jobs = max_jobs
        self.options = options
        self.profiles = profiles
        self.server = server
        self.jobs: Dict[str, _WatchJob] = {}
        self.started = 0
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.transport = _WatchJobTransport(self)
        self._waiters: Dict[str, _RpcWaiter] = {}
        self._rpc_ids = 0
        self.ended = False
        # Follow-ups between their 410 check and their answer: the hold
        # reaper keeps watching while any is in flight.
        self.follow_ups_in_flight = 0
        self.reaping = False
        # The job whose chat the call's later ordinary requests run in: its
        # first ordinary one that started (#1250).
        self.call_chat: Optional[_WatchJob] = None
        # The first ordinary request while its chat is still being made: one
        # that starts meanwhile waits for that chat rather than making its own.
        self.call_chat_claim: Optional[_WatchJob] = None

    # Hermes' session API

    def rpc(self, method: str, params: Dict[str, Any], timeout: float = WATCH_JOB_RPC_TIMEOUT_S) -> Dict[str, Any]:
        with self.lock:
            self._rpc_ids += 1
            rid = f"conduit-watch-{self.grant.grant_id[:8]}-{self._rpc_ids}"
            waiter = self._waiters[rid] = _RpcWaiter()
        try:
            response = self.server.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                                            self.transport)
            if response is None:
                # A slow method answers from Hermes' worker pool.
                if not waiter.done.wait(timeout):
                    raise WatchJobError(f"{method} timed out")
                response = waiter.response
        finally:
            with self.lock:
                self._waiters.pop(rid, None)
        if not isinstance(response, dict):
            raise WatchJobError(f"{method} gave no answer")
        error = response.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise WatchJobError(str(message or f"{method} failed")[:300], code=code)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def frame(self, obj: Any) -> None:
        """A frame Hermes wrote to this transport."""
        if not isinstance(obj, dict):
            return
        method = obj.get("method")
        if method is None:
            # A response to one of our calls.
            with self.lock:
                waiter = self._waiters.get(obj.get("id")) if isinstance(obj.get("id"), str) else None
            if waiter is not None:
                waiter.response = obj
                waiter.done.set()
            return
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        sid = params.get("session_id") if isinstance(params.get("session_id"), str) else ""
        if method == "event":
            payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
            kind = str(params.get("type") or "")
            if kind == "approval.request":
                # The event form: the same fields, in the payload, with no
                # server request for a request.cancel to name.
                self._approval_requested(sid, "", payload)
            else:
                self._event(kind, sid, payload)
        elif method == "approval":
            # A server request: its fields sit beside session_id.
            self._approval_requested(sid, _rpc_id(obj.get("id")), params)

    def _job_for(self, sid: str) -> Optional[_WatchJob]:
        if not sid:
            return None
        # The call's chat carries one request at a time (#1250): its frames
        # are the running one's, never a settled earlier one's. Late frames
        # with no running owner go to the earliest, whose handlers drop them.
        owners = [job for job in self.jobs.values() if sid in (job.session_id, job.stored_session_id)]
        return next((job for job in owners if job.active or job.cancelling),
                    next((job for job in owners if job.turn_open), owners[0] if owners else None))

    def _superseded_turn(self, job: _WatchJob, sid: str) -> Optional[_WatchJob]:
        """The call's chat (#1250): a cancelled turn on the running job's
        session whose end may still come. Once that end is overdue, the
        turn is taken as over and the frames are the running job's."""
        if not job.active:
            return None
        now = time.monotonic()
        for other in self.jobs.values():
            if (other is job or not other.turn_open or other.status != "cancelled"
                    or sid not in (other.session_id, other.stored_session_id)):
                continue
            if now < other.quiet_by + WATCH_JOB_LATE_END_S:
                return other
            other.turn_open = False
        return None

    def _event(self, kind: str, sid: str, payload: Dict[str, Any]) -> None:
        settled = False
        with self.changed:
            job = self._job_for(sid)
            if job is None:
                return
            superseded = self._superseded_turn(job, sid)
            if superseded is not None:
                # The call's chat (#1250): a session's turns run in order. An
                # end before the next request's turn shows any sign of life is
                # the cancelled turn's: however it ended while the next one
                # waits, and only as an interrupted completion once the next
                # went in after the hold, when its own end can come first (an
                # early error).
                # Anything else means that turn is over and its end isn't coming.
                if kind in ("message.complete", "error") and (
                        not job.turn_open or self._reads_as_interrupt(kind, payload)):
                    job = superseded
                else:
                    superseded.turn_open = False
            if kind in ("message.complete", "error") and job.turn_open:
                # Its turn is over: the call's chat can take the next one.
                job.turn_open = False
                self.changed.notify_all()
            if job.held_completion is not None and kind not in ("message.complete", "error"):
                # Hermes is still at work on the session: the end it held
                # waits on, so a quiet stretch isn't read as no turn coming.
                job.held_at = time.monotonic()
            if kind == "request.cancel":
                # Answered elsewhere (the app, the phone), timed out, or the
                # turn ended. The id is compared as a string, as it was
                # stored: JSON-RPC ids can be numbers.
                server_id = job.approval["server_request_id"] if job.approval else ""
                if server_id and _rpc_id(payload.get("id")) == server_id:
                    self._approval_gone(job)
            elif kind == "approval.cancelled":
                ids = payload.get("request_ids")
                if job.approval and (not isinstance(ids, list) or not ids or job.approval["request_id"] in ids):
                    self._approval_gone(job)
            elif kind in WATCH_TURN_PROGRESS:
                # Hermes carries on with the follow-up it took: the end it
                # held was the step it was finishing.
                if job.active and job.follow_up == "accepted":
                    job.follow_up, job.held_completion = None, None
                return
            elif kind in ("message.complete", "error") and (job.active or job.cancelling):
                if not job.active:
                    # Cancelled meanwhile: this end reads as the cancel,
                    # unless Hermes refuses the interrupt.
                    job.held_end = (kind, payload)
                    return
                if kind == "message.complete" and self._cut_by_correction(job, payload):
                    return
                if kind == "message.complete" and job.follow_up is not None and job.follow_up != "accepted":
                    # Maybe not the last: held until Hermes takes the words.
                    job.held_completion, job.held_at = (kind, payload), time.monotonic()
                    job.held_since = job.held_at
                    if job.follow_up == "queued":
                        job.follow_up = "accepted"
                    if self.ended:
                        # No news poll reaps it once the call is gone.
                        self._reap_holds_later()
                    return
                self._settle(job, kind, payload)
                settled = True
            else:
                return
            self.changed.notify_all()
            ended = self.ended
        if settled and ended:
            self._close_session_later(job)

    @staticmethod
    def _cut_by_correction(job: _WatchJob, payload: Dict[str, Any]) -> bool:
        """The end of a reply a follow-up cut off: never the job's result."""
        text = payload.get("text")
        if job.follow_up is None:
            # No correction went in: whatever this says is the job's own.
            return False
        if isinstance(text, str) and text.strip().lower() == WATCH_CORRECTION_NOTICE:
            return True
        return payload.get("status") == "interrupted"

    @staticmethod
    def _reads_as_interrupt(kind: str, payload: Dict[str, Any]) -> bool:
        """An end that says its turn was interrupted: Hermes ends an
        interrupted turn with a completion marked so (as _settle reads it),
        while an error is a turn failing, so it is the running one's."""
        return kind == "message.complete" and payload.get("status") == "interrupted"

    @staticmethod
    def _settle(job: _WatchJob, kind: str, payload: Dict[str, Any]) -> None:
        """The job's turn ended: ``message.complete`` or ``error``."""
        job.approval = None
        job.follow_up, job.held_completion = None, None
        if kind == "error":
            message = payload.get("message")
            job.status = "failed"
            job.error = (message if isinstance(message, str) else "").strip()[:300] or "Hermes reported an error"
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            text = payload.get("rendered") if isinstance(payload.get("rendered"), str) else ""
        status = payload.get("status")
        if status == "interrupted":
            job.status = "cancelled"
        elif status == "error":
            detail = payload.get("error") if isinstance(payload.get("error"), str) else text
            job.status, job.error = "failed", (detail or "").strip()[:300] or "Hermes reported an error"
        else:
            # News clips it; one more character marks the cut.
            job.status, job.result = "finished", text.strip()[:WATCH_JOB_RESULT_CHARS + 1]

    @staticmethod
    def _approval_gone(job: _WatchJob) -> None:
        job.approval = None
        # A later request, even under the same id, is news again.
        job.approval_told = None
        if job.status == "needs_approval":
            job.status = "running"

    def _approval_requested(self, sid: str, server_request_id: str, params: Dict[str, Any]) -> None:
        def text(key: str, limit: int) -> str:
            value = params.get(key)
            return value.strip()[:limit] if isinstance(value, str) else ""

        with self.changed:
            job = self._job_for(sid)
            if job is None or not job.active:
                return
            if not job.turn_open and self._superseded_turn(job, sid) is not None:
                # The call's chat (#1250): the cancelled turn is still going
                # and the next request hasn't gone in, so the ask is the
                # cancelled one's, which its interrupt ends.
                return
            if job.held_completion is not None:
                # Still at work, as any frame for the session says.
                job.held_at = time.monotonic()
            command = params.get("command")
            command = command.strip() if isinstance(command, str) else ""
            shown_command = _json_clip(command[:2_000], WATCH_JOB_COMMAND_BYTES)
            job.approval = {
                "request_id": text("request_id", 128),
                "server_request_id": server_request_id,
                "command": shown_command,
                # No command to show is never shown whole: it can only be denied.
                "cut": not command or shown_command != command,
                "description": text("description", 500) or "Approval required",
            }
            job.approval["fingerprint"] = _watch_approval_fingerprint(job.approval, command)
            job.status = "needs_approval"
            self.changed.notify_all()

    # The Watch's calls

    def start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        instructions = str(args.get("instructions") or "").strip()
        # "Quick:" only routes work on the phone; it isn't part of the task.
        if instructions[:6].lower() == "quick:":
            instructions = instructions[6:].strip()
        if not instructions:
            return {"ok": False, "status": 400, "detail": "instructions is required"}
        named = str(args.get("profile") or "").strip()
        if _json_bytes(instructions) > WATCH_JOB_MAX_INSTRUCTION_BYTES:
            return {"ok": False, "status": 413, "detail": "The task is too long for a Watch job"}
        with self.lock:
            if self.ended:
                return {"ok": False, "status": 410, "detail": "This call's Watch jobs have ended"}
            profile: Optional[str] = None
            if named and core._limiter_key(named) not in ("", core._limiter_key(self.grant.profile)):
                # Only a profile the phone listed: a name is never guessed at.
                profile = next((name for name in self.profiles if core._limiter_key(name) == core._limiter_key(named)), None)
                if profile is None:
                    return {"ok": True, "status": "not_started",
                            "message": f"I don't know a profile or bot called {named[:64]}, so I didn't start the job."}
            if self.started >= self.max_jobs:
                return {"ok": True, "status": "not_started",
                        "message": f"This call has started {self.max_jobs} jobs, the most the user allows per call. "
                                   "Tell them; they can raise it in Conduit's Watch settings or start more from "
                                   "their iPhone."}
            running = sum(1 for job in self.jobs.values() if job.active)
            if running >= WATCH_JOBS_RUNNING_MAX:
                return {"ok": True, "status": "not_started",
                        "message": f"You already have {running} background jobs running. Cancel them before "
                                   "starting another."}
            self.started += 1
            job = _WatchJob(f"watch-{next(_watch_job_numbers)}", watch_job_title(instructions), profile)
            # The Watch says whether the user asked to keep it apart; a job on
            # another profile always is.
            job.joins_call = args.get("joins_call") is True and profile is None
            chat: Optional[_WatchJob] = None
            if job.joins_call:
                claim = self.call_chat_claim if self.call_chat_claim is not None and self.call_chat_claim.active else None
                chat = self.call_chat or claim
                if chat is None:
                    self.call_chat_claim = job
            self.jobs[job.job_id] = job
        done = threading.Event()

        def run() -> None:
            try:
                if chat is not None:
                    answer = self._continue_session(job, instructions, chat)
                else:
                    answer = self._start_session(job, instructions)
            except Exception as exc:  # noqa: BLE001 — _start_session catches its own
                watch_tools._log_watch_failure("Starting a Watch job failed", exc)
                answer = {"ok": True, "status": "not_started", "title": job.title,
                          "message": f"Hermes couldn't start the job: {type(exc).__name__}"}
                with self.changed:
                    if job.active:
                        job.status, job.error = "failed", type(exc).__name__
            with self.changed:
                job.answer_pending = False
                if self.call_chat_claim is job:
                    # Made the call's chat, or never will.
                    self.call_chat_claim = None
                if answer.get("status") == "not_started":
                    # Nothing ran: it doesn't spend one of the call's jobs.
                    self.started -= 1
                if not job.answered_late:
                    job.start_answer = answer
                    if answer.get("status") == "not_started":
                        job.outcome_told = True  # this answer tells it
                # Answered late: a job that didn't start is news.
                self.changed.notify_all()
            done.set()

        try:
            threading.Thread(target=run, name="conduit-watch-job-start", daemon=True).start()
        except RuntimeError as exc:
            # No thread to spare: nothing reached Hermes, so the job never was.
            logger.warning("Couldn't start a Watch job (%s)", type(exc).__name__)
            with self.changed:
                self.jobs.pop(job.job_id, None)
                self.started -= 1
                if self.call_chat_claim is job:
                    self.call_chat_claim = None
                self.changed.notify_all()
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": "Hermes couldn't start the job: the host is too busy right now."}
        done.wait(WATCH_JOB_START_ANSWER_S)
        with self.changed:
            answer = job.start_answer
            if answer is None:
                job.answered_late = True
                job.answer_pending = False
                self.changed.notify_all()
        if answer is None:
            return {"ok": True, "status": "accepted", "job_id": job.job_id, "title": job.title,
                    "message": "Hermes is starting the job. Its result will arrive later as a message; don't wait "
                               "for it."}
        undelivered = watch_tools._watch_undelivered.get()
        if undelivered is not None and answer.get("status") == "not_started":
            # Lost on the way, the failure or cancel goes out as news instead.
            undelivered.append(lambda: self._untake([(job, "outcome_told", False)]))
        return answer

    def _start_session(self, job: "_WatchJob", instructions: str) -> Dict[str, Any]:
        """Creates the job's Hermes chat and submits its task; the start's answer."""
        kept = False  # in its bot's work registry, whose row a failure then settles
        profile: Optional[str] = None
        try:
            # A job on another profile runs on that profile's own model, as
            # the phone's does: the voice-job model is this profile's.
            params: Dict[str, Any] = {"cols": 96, "source": "desktop", "title": job.title,
                                      **(self.options if job.profile is None else {})}
            profile = job.profile or self.grant.profile
            if profile:
                params["profile"] = profile
            created = self.rpc("session.create", params)
            sid = created.get("session_id")
            if not isinstance(sid, str) or not sid:
                raise WatchJobError("session.create gave no session")
            landed = created.get("profile")
            if job.profile is not None and isinstance(landed, str) and core._limiter_key(landed) != core._limiter_key(job.profile):
                # As the phone refuses it (AppState's createSession): only a
                # profile Hermes names, as a Hermes that names none is trusted
                # with the one asked for.
                with self.lock:
                    job.session_id = sid
                raise WatchJobError(f"Hermes put it on {landed[:64]} instead of {job.profile}")
            stored = created.get("stored_session_id")
            with self.lock:
                job.session_id = sid
                job.stored_session_id = stored if isinstance(stored, str) else ""
                cancelled = job.status == "cancelled"
            if not cancelled:
                # Before its turn, so the turn's hooks find its row.
                _keep_in_registry(profile, job.stored_session_id or sid, job.title, instructions)
                kept = True
                self.rpc("prompt.submit", {"session_id": sid, "text": WATCH_JOB_PROMPT.format(instructions=instructions)})
                with self.lock:
                    job.turn_open = True
                    cancelled = job.status == "cancelled"
                if cancelled:
                    # Cancelled while its prompt went in: stop that turn.
                    try:
                        self.rpc("session.interrupt", {"session_id": sid})
                    except WatchJobError as exc:
                        logger.info("Cancelling a starting Watch job failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 — the job didn't start; the model is told
            with self.changed:
                if job.active:
                    job.status = "failed"
                    job.error = str(exc)[:300] if isinstance(exc, WatchJobError) else type(exc).__name__
                self.changed.notify_all()
            if not isinstance(exc, WatchJobError):
                watch_tools._log_watch_failure("Starting a Watch job failed", exc)
            if kept:
                _registry_start_failed(profile, job.stored_session_id or job.session_id,
                                       job.error or type(exc).__name__)
            self._close_session(job)
            if job.status == "cancelled":
                return {"ok": True, "status": "not_started", "title": job.title,
                        "message": f"{job.title} was cancelled before it started."}
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"Hermes couldn't start the job: {job.error}"}
        if cancelled:
            # cancel_job got there while it started, and said so.
            self._close_session(job)
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"{job.title} was cancelled before it started."}
        with self.changed:
            if job.status == "starting":
                job.status = "running"
            # A cancel between the prompt and here interrupted the turn
            # itself and told the Watch.
            cancelled = job.status == "cancelled"
            if not cancelled and job.joins_call and job.profile is None and self.call_chat is None:
                self.call_chat = job
            if self.call_chat_claim is job:
                self.call_chat_claim = None
            self.changed.notify_all()
        if cancelled:
            self._close_session(job)
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"{job.title} was cancelled before it started."}
        with self.lock:
            # The chat the call took is the call's, not a job, as on the phone.
            call_chat = self.call_chat is job
        if not call_chat:
            self._tag(job)
        where = f" on {job.profile}" if job.profile else ""
        return {"ok": True, "status": "started", "job_id": job.job_id, "title": job.title,
                "session_id": job.stored_session_id or sid,
                **({"profile": job.profile} if job.profile else {}),
                "message": f"The job is running on Hermes{where}. Its result will arrive later as a message; "
                           "don't wait for it."}

    def _chat_waits(self, chat: _WatchJob, job: _WatchJob) -> bool:
        """Whether a request waits on in the call's chat: for the chat to be
        made, or for the request running there. Called with the lock held."""
        if self.call_chat is not chat:
            return chat.active and self.call_chat_claim is chat
        return self._chat_busy(chat, job)

    def _chat_busy(self, chat: _WatchJob, job: _WatchJob) -> bool:
        """Whether another request still runs in the call's chat. Called with
        the lock held."""
        now = time.monotonic()
        return any(other is not job and other.session_id == chat.session_id
                   and (other.active or other.cancelling or (other.turn_open and now < other.quiet_by))
                   for other in self.jobs.values())

    def _continue_session(self, job: "_WatchJob", instructions: str, chat: "_WatchJob") -> Dict[str, Any]:
        """Sends the task as the next turn of the call's chat (#1250), once
        nothing else runs there, as the phone sends a call's requests to its
        chat; the start's answer."""
        with self.changed:
            own = False
            while True:
                while not self.ended and job.status == "starting" and self._chat_waits(chat, job):
                    self.changed.wait(WATCH_JOB_CHAT_WAIT_S)
                ended, cancelled = self.ended, job.status == "cancelled"
                if ended or cancelled or self.call_chat is chat:
                    break
                # The chat waited for never got made: wait for the one made
                # since, or for another request making one, or make it.
                claim = self.call_chat_claim if self.call_chat_claim is not None and self.call_chat_claim.active else None
                if self.call_chat is not None or (claim is not None and claim is not job):
                    chat = self.call_chat or claim
                    continue
                self.call_chat_claim, own = job, True
                break
            if not ended and not cancelled and not own:
                job.session_id, job.stored_session_id = chat.session_id, chat.stored_session_id
        if own:
            return self._start_session(job, instructions)
        if cancelled:
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"{job.title} was cancelled before it started."}
        if ended:
            with self.changed:
                if job.active:
                    job.status, job.error = "failed", "The call ended before Hermes took it"
                self.changed.notify_all()
            # It was the last thing holding the call's transport open.
            self._close_transport_if_idle()
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": "The call ended before Hermes could take this request."}
        try:
            with self.lock:
                cancelled = job.status == "cancelled"
            if not cancelled:
                self.rpc("prompt.submit", {"session_id": job.session_id,
                                           "text": WATCH_JOB_NEXT_PROMPT.format(instructions=instructions)})
                with self.lock:
                    job.turn_open = True
                    cancelled = job.status == "cancelled"
                if cancelled:
                    # Cancelled while its prompt went in: stop that turn, as
                    # _start_session does. Nothing else ran in the chat.
                    try:
                        self.rpc("session.interrupt", {"session_id": job.session_id})
                    except WatchJobError as exc:
                        logger.info("Cancelling a starting Watch job failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 — the request didn't go; the model is told
            with self.changed:
                if job.active:
                    job.status = "failed"
                    job.error = str(exc)[:300] if isinstance(exc, WatchJobError) else type(exc).__name__
                self.changed.notify_all()
            if not isinstance(exc, WatchJobError):
                watch_tools._log_watch_failure("Continuing a Watch call's chat failed", exc)
            # The chat stays open while the call runs: it is the call's, and
            # its earlier jobs'.
            if self.ended:
                self._close_session(job)
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"Hermes couldn't take the request: {job.error}"}
        with self.changed:
            if job.status == "starting":
                job.status = "running"
            cancelled = job.status == "cancelled"
            self.changed.notify_all()
        if cancelled:
            return {"ok": True, "status": "not_started", "title": job.title,
                    "message": f"{job.title} was cancelled before it started."}
        return {"ok": True, "status": "started", "job_id": job.job_id, "title": job.title,
                "session_id": job.stored_session_id or job.session_id,
                "message": "Hermes is working on this in the call's chat, after the earlier requests there. "
                           "Its result will arrive later as a message; don't wait for it."}

    def list(self) -> Dict[str, Any]:
        """Mirrors the phone's list_jobs answer (GeminiLiveToolBridge.listResult)."""
        with self.lock:
            visible = [job for job in self.jobs.values() if job.active or not job.outcome_told]
            lines = {
                "starting": "{} is still running.", "running": "{} is still running.",
                "needs_approval": "{} is waiting for your approval.", "finished": "{} has finished.",
                "failed": "{} failed.", "cancelled": "{} was cancelled.",
            }
            result: Dict[str, Any] = {
                "ok": True,
                "summary": " ".join(lines[job.status].format(job.title) for job in visible)
                or "No background jobs are running.",
            }
            for index, job in enumerate(visible, 1):
                result[f"job_{index}"] = f"id={job.job_id}; title={job.title}; status={job.status}" + (
                    f"; profile={job.profile}" if job.profile else "")
        return result

    def cancel(self, args: Dict[str, Any]) -> Dict[str, Any]:
        wanted = str(args.get("job_id") or "").strip()
        with self.changed:
            targets = [job for job in self.jobs.values() if job.active and (not wanted or job.job_id == wanted)]
            if not targets:
                return {"ok": True, "message": "There are no background jobs to cancel."}
            # Marked first, as on the phone: the interrupt's own end of turn
            # reads as this cancel, which the user hears in this answer.
            before = {job.job_id: (job.status, job.approval) for job in targets}
            for job in targets:
                job.status = "cancelled"
                job.approval = None
                job.outcome_told = True
                job.cancelling = bool(job.session_id)
            self.changed.notify_all()
        failed = []
        for job in targets:
            if not job.session_id:
                continue  # still starting: its start sees the cancel
            try:
                self.rpc("session.interrupt", {"session_id": job.session_id})
            except Exception as exc:  # noqa: BLE001 — it stays followed
                logger.info("Cancelling a Watch job failed: %s", type(exc).__name__)
                with self.changed:
                    if job.status == "cancelled":
                        job.status, job.approval = before[job.job_id]
                        job.outcome_told = False
                        if job.held_end is not None:
                            # Its turn ended meanwhile: that end stands.
                            self._settle(job, *job.held_end)
                    job.cancelling, job.held_end = False, None
                    self.changed.notify_all()
                failed.append(job)
                if self.ended and not job.active:
                    self._close_session_later(job)
            else:
                with self.changed:
                    job.cancelling, job.held_end = False, None
                    # Hermes ends the interrupted turn on its own time: the
                    # call's chat waits a little for that end.
                    job.quiet_by = time.monotonic() + WATCH_JOB_INTERRUPT_END_S
                    self.changed.notify_all()
        cancelled = [job for job in targets if job not in failed]
        undelivered = watch_tools._watch_undelivered.get()
        if undelivered is not None and cancelled:
            # Lost on the way, the cancel goes out as news instead.
            undelivered.append(lambda: self._untake([(job, "outcome_told", False) for job in cancelled]))
        if self.ended:
            for job in cancelled:
                self._close_session_later(job)
        if wanted:
            title = targets[0].title
            return {"ok": True, "message": f"Couldn't cancel {title}. It may still be running." if failed
                    else f"{title} was cancelled."}
        count = len(targets) - len(failed)
        parts = [f"Cancelled {count} background jobs."] if count else []
        parts += [f"Couldn't cancel {job.title}. It may still be running." for job in failed]
        return {"ok": True, "message": " ".join(parts)}

    def interrupt(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Puts the user's words into a job Hermes is still working on.

        The answer is the outcome, as the phone's VoiceFollowUpOutcome:
        "interrupted" (taken into the running turn), "queued" (Hermes takes
        it right after the step it is finishing), "finished" (too late),
        "failed" (with ``error``) or "unknown_job". The Watch words it for
        its model.
        """
        wanted = str(args.get("job_id") or "").strip()
        words = str(args.get("message") or "").strip()
        # kind "add" only adds to the work: steered, not redirected (Conduit #1076).
        adds = str(args.get("kind") or "").strip().lower() == "add"
        if not wanted:
            return {"ok": False, "status": 400, "detail": "job_id is required"}
        if not words:
            return {"ok": False, "status": 400, "detail": "message is required"}
        if _json_bytes(words) > WATCH_JOB_MAX_INSTRUCTION_BYTES:
            return {"ok": False, "status": 413, "detail": "The words are too long for a Watch job"}
        deadline = time.monotonic() + WATCH_FOLLOW_UP_DEADLINE_S
        with self.lock:
            if self.ended:
                return {"ok": False, "status": 410, "detail": "This call's Watch jobs have ended"}
            job = self.jobs.get(wanted)
            if job is None:
                return {"ok": True, "outcome": "unknown_job"}
            self.follow_ups_in_flight += 1
        try:
            if not job.follow_up_lock.acquire(timeout=WATCH_FOLLOW_UP_QUEUE_S):
                return {"ok": True, "outcome": "failed", "title": job.title,
                        "error": "Hermes is still taking the user's last words for this job."}
            try:
                outcome = self._follow_up(job, words, deadline, adds=adds)
            finally:
                job.follow_up_lock.release()
        finally:
            with self.lock:
                self.follow_ups_in_flight -= 1
        return {"ok": True, "title": job.title, **outcome}

    def _follow_up(self, job: _WatchJob, words: str, deadline: float, adds: bool = False) -> Dict[str, Any]:
        attempts = waits = 0
        while True:
            with self.lock:
                # Ended: the call is gone, so no more words go in.
                if not job.active or self.ended:
                    return {"outcome": "finished"}
                sid = job.session_id
                # Words Hermes already runs as its next turn keep this
                # turn's end from ending the job, whatever comes now.
                was_queued = job.follow_up == "queued"
                if sid and not was_queued:
                    job.follow_up = "sending"
            if sid:
                attempts += 1
                try:
                    result = self._redirect(sid, WATCH_JOB_FOLLOW_UP_PROMPT.format(words=words),
                                            timeout=max(1.0, deadline - time.monotonic()), adds=adds)
                except WatchJobError as exc:
                    if not was_queued:
                        self._end_follow_up(job)
                    return {"outcome": "failed", "error": str(exc)[:300]}
                except Exception as exc:  # noqa: BLE001 — named; the words stay out of the log
                    watch_tools._log_watch_failure("A Watch job follow-up failed", exc)
                    if not was_queued:
                        self._end_follow_up(job)
                    return {"outcome": "failed", "error": type(exc).__name__}
                if result in ("redirected", "queued"):
                    with self.changed:
                        # Settled meanwhile (an error ended the turn): the
                        # words came too late. A call that ended meanwhile
                        # still hears they went in: they were sent during it.
                        if not job.active:
                            return {"outcome": "finished"}
                        if job.follow_up == "sending":
                            # A queued turn's end held meanwhile was the step
                            # Hermes was finishing; the words' own turn ends the job.
                            job.follow_up = ("accepted" if result == "redirected" or job.held_completion is not None
                                             else "queued")
                    return {"outcome": "interrupted" if result == "redirected" else "queued"}
                if not was_queued:
                    self._end_follow_up(job)
            # Not running: the turn just ended (its end settles the job), or
            # Hermes hasn't started it yet. Asked again shortly while open.
            with self.lock:
                if not job.active or self.ended:
                    return {"outcome": "finished"}
            waits += 1
            if (attempts >= WATCH_FOLLOW_UP_ATTEMPTS or waits >= WATCH_FOLLOW_UP_ATTEMPTS * 3
                    or time.monotonic() + WATCH_FOLLOW_UP_RETRY_S >= deadline):
                return {"outcome": "failed", "error": f"Hermes wasn't working on \"{job.title}\" just then."}
            time.sleep(WATCH_FOLLOW_UP_RETRY_S)

    def _redirect(self, sid: str, words: str, timeout: float = WATCH_JOB_RPC_TIMEOUT_S, adds: bool = False) -> str:
        """Hermes' answer: "redirected", "queued" or "not_running". A Hermes
        without redirect gets a steer, as the phone sends it. An addition
        (``adds``) is steered into the turn, keeping its direction; a Hermes
        that refuses the steer, or has none, gets the redirect instead, as on
        the phone (VoiceBackgroundJobSupervisor, AppState.isUnsupportedSteer)."""
        if adds:
            started = time.monotonic()
            try:
                status = self.rpc("session.steer", {"session_id": sid, "text": words}, timeout=timeout).get("status")
            except WatchJobError as exc:
                if not exc.unsupported_steer:
                    raise
            else:
                if str(status or "").lower() != "rejected":
                    return "redirected"
            # The redirect gets what is left of the follow-up's time, not a second budget.
            timeout = max(1.0, timeout - (time.monotonic() - started))
        try:
            status = self.rpc("session.redirect", {"session_id": sid, "text": words}, timeout=timeout).get("status")
        except WatchJobError as exc:
            if not exc.unsupported_redirect:
                raise
            status = self.rpc("session.steer", {"session_id": sid, "text": words}, timeout=timeout).get("status")
            return "not_running" if status == "rejected" else "redirected"
        status = status.lower() if isinstance(status, str) else ""
        return status if status in ("redirected", "queued") else "not_running"

    def _end_follow_up(self, job: _WatchJob) -> None:
        """The follow-up didn't take: an end held meanwhile was the turn's last after all."""
        with self.changed:
            held = job.held_completion
            job.follow_up, job.held_completion = None, None
            if held is None or not job.active:
                return
            self._settle(job, *held)
            self.changed.notify_all()
            ended = self.ended
        if ended:
            self._close_session_later(job)

    def news(self, args: Dict[str, Any], caller: Optional["watch_tools._WatchGrant"] = None) -> Dict[str, Any]:
        """Settled jobs and approval requests not yet told, waiting up to
        ``wait_s`` (at most WATCH_JOB_NEWS_WAIT_S) for some while jobs run.
        The wait also ends with ``caller``, the grant asking: after a carry,
        the call's previous grant still answers until it closes."""
        wait = args.get("wait_s")
        if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait >= 0:
            wait = float(min(wait, WATCH_JOB_NEWS_WAIT_S))
        else:
            wait = WATCH_JOB_NEWS_WAIT_S
        deadline = time.monotonic() + wait
        marks: list = []
        reaped: list = []
        with self.changed:
            while True:
                reaped += self._settle_stale_holds()
                running = sum(1 for job in self.jobs.values() if job.active)
                # Every request still open, so the Watch drops a card
                # answered elsewhere or timed out.
                approvals = [{"job_id": job.job_id, "request_id": job.approval["request_id"],
                              "fingerprint": job.approval["fingerprint"]}
                             for job in self.jobs.values() if job.approval and job.status == "needs_approval"]
                answer = {"ok": True, "news": [], "running": running, "more": True, "approvals": approvals}
                news = self._take_news(max(0, WATCH_JOB_ANSWER_BYTES - _json_bytes(answer)), marks)
                remaining = deadline - time.monotonic()
                closed = self.grant.closed.is_set() or (caller is not None and caller.closed.is_set())
                if news or not running or self.ended or closed or remaining <= 0:
                    break
                self.changed.wait(min(remaining, 1.0))
            answer["news"] = news
            answer["more"] = any(self._untold(job) is not False for job in self.jobs.values())
            ended = self.ended
        if ended:
            for job in reaped:
                self._close_session_later(job)
        undelivered = watch_tools._watch_undelivered.get()
        if undelivered is not None and marks:
            undelivered.append(lambda: self._untake(marks))
        return answer

    def _settle_stale_holds(self) -> list:
        """An end held for queued words whose turn never showed: it was the
        job's last after all. Called with the lock held; the jobs settled."""
        now = time.monotonic()
        settled = []
        for job in self.jobs.values():
            if (job.active and job.held_completion is not None and job.follow_up == "accepted"
                    and (now - job.held_at >= WATCH_FOLLOW_UP_HOLD_S
                         or now - job.held_since >= WATCH_FOLLOW_UP_HOLD_MAX_S)):
                self._settle(job, *job.held_completion)
                settled.append(job)
        return settled

    def _untake(self, marks: list) -> None:
        """Marks news as not told again: its answer never reached the Watch."""
        with self.changed:
            for job, field, previous in marks:
                setattr(job, field, previous)
            self.changed.notify_all()

    @staticmethod
    def _untold(job: "_WatchJob") -> Any:
        """False when the Watch has heard all of ``job``'s news; else None
        for its outcome or the approval request's key."""
        if job.answer_pending:
            return False
        if not job.active and not job.outcome_told:
            return None
        if job.approval and job.status == "needs_approval" and job.approval_told != job.approval_key:
            return job.approval_key
        return False

    def _take_news(self, budget: int, marks: list) -> list:
        """Untold news within ``budget`` bytes, each mark it moves added to
        ``marks``; called with the lock held."""
        news: list = []
        used = 0
        for job in self.jobs.values():
            told = self._untold(job)
            if told is False:
                continue
            item = job.news()
            size = _json_bytes(item) + 1
            if used + size > budget:
                if news:
                    break  # the rest goes with the next call
                # The clips above make any one item fit; should one not,
                # its status still goes rather than holding up all news.
                item = {key: item[key] for key in ("job_id", "title", "status")}
                size = _json_bytes(item) + 1
            used += size
            news.append(item)
            if told is None:
                marks.append((job, "outcome_told", job.outcome_told))
                job.outcome_told = True
            else:
                marks.append((job, "approval_told", job.approval_told))
                job.approval_told = told
        return news

    def answer_approval(self, args: Dict[str, Any]) -> Dict[str, Any]:
        choice = str(args.get("choice") or "")
        if choice not in WATCH_APPROVAL_CHOICES:
            return {"ok": False, "status": 400, "detail": "choice must be once or deny"}
        with self.lock:
            job = self.jobs.get(str(args.get("job_id") or ""))
            approval = dict(job.approval) if job and job.approval and job.status == "needs_approval" else None
        if job is None or approval is None:
            return {"ok": True, "status": "not_pending", "message": "That job isn't waiting for an approval any more."}
        wanted = str(args.get("request_id") or "")
        if wanted != approval["request_id"]:
            # Only the request the Watch showed: a newer one needs its own answer.
            return {"ok": True, "status": "not_pending", "message": "That approval request has been replaced."}
        shown = str(args.get("fingerprint") or "")
        if not approval["request_id"] and not shown:
            # Without an id Hermes answers its oldest pending request, so
            # only the card's fingerprint says which one the user read.
            return {"ok": False, "status": 400,
                    "detail": "Update Conduit to answer this request on the Watch, or answer it in Conduit on your iPhone"}
        if shown and shown != approval["fingerprint"]:
            # The request changed under the same (or no) id since the Watch
            # showed it: the new one is news again, for its own answer.
            with self.changed:
                if job.approval and job.approval["fingerprint"] == approval["fingerprint"]:
                    job.approval_told = None
                self.changed.notify_all()
            return {"ok": True, "status": "changed",
                    "message": "The request changed before the answer reached Hermes. Show the user the new one and ask again."}
        if choice == "once" and approval.get("cut"):
            # The Watch showed only part of the command: approving it would
            # approve what the user never saw.
            return {"ok": False, "status": 400,
                    "detail": "The command could not be shown whole, so it can only be denied"}
        params: Dict[str, Any] = {"session_id": job.session_id, "choice": choice}
        if approval["request_id"]:
            params["request_id"] = approval["request_id"]
        with self.lock:
            if job.approval_answering:
                return {"ok": True, "status": "not_pending", "message": "That request is already being answered."}
            if not job.approval or job.approval["fingerprint"] != approval["fingerprint"]:
                return {"ok": True, "status": "changed",
                        "message": "The request changed before the answer reached Hermes. Show the user the new one and ask again."}
            job.approval_answering = True
        try:
            resolved = self.rpc("approval.respond", params).get("resolved")
        except WatchJobError as exc:
            return {"ok": True, "status": "failed", "message": f"Hermes didn't take the answer: {exc}"}
        finally:
            with self.lock:
                job.approval_answering = False
        with self.changed:
            if job.approval and job.approval["fingerprint"] == approval["fingerprint"]:
                self._approval_gone(job)
            self.changed.notify_all()
        # Hermes answers how many approvals the decision unblocked.
        if not isinstance(resolved, int) or resolved <= 0:
            return {"ok": True, "status": "not_pending", "message": "That job isn't waiting for an approval any more."}
        return {"ok": True, "status": "approved" if choice == "once" else "denied", "job_id": job.job_id}

    # Ending

    def end(self) -> None:
        """The grant ended: settled jobs' sessions close now, running ones'
        once they settle. A job still running keeps running in Hermes."""
        with self.changed:
            if self.ended:
                return
            self.ended = True
            settled = [job for job in self.jobs.values() if not job.active]
            if self._holding():
                # No news poll reaps a held end once the call is gone.
                self._reap_holds_later()
            self.changed.notify_all()
        for job in settled:
            self._close_session_later(job)
        self._close_transport_if_idle()

    def _reap_holds_later(self) -> None:
        """Starts the hold reaper unless it runs. Called with the lock held."""
        if self.reaping:
            return
        try:
            threading.Thread(target=self._reap_holds_after_end, name="conduit-watch-job-holds", daemon=True).start()
        except RuntimeError as exc:
            logger.warning("Couldn't start the Watch job hold reaper (%s)", type(exc).__name__)
            return
        self.reaping = True

    def _holding(self) -> bool:
        """A follow-up in flight or an end held for one. Called with the lock
        held. Words Hermes queued with no end held yet need nothing: their
        job runs on, and its next end starts the reaper again."""
        return self.follow_ups_in_flight > 0 or any(
            job.active and job.held_completion is not None for job in self.jobs.values())

    def _reap_holds_after_end(self) -> None:
        """Settles ends held for follow-up words that never ran, after the
        call ended, so their sessions and the transport close. Runs while
        anything holds; a hold taken later starts it again."""
        holding = True
        try:
            while holding:
                with self.changed:
                    reaped = self._settle_stale_holds()
                    if reaped:
                        self.changed.notify_all()
                    holding = self._holding()
                    if not holding:
                        self.reaping = False
                for job in reaped:
                    self._close_session_later(job)
                self._close_transport_if_idle()
                if holding:
                    time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 — a later hold starts a new reaper
            watch_tools._log_watch_failure("The Watch job hold reaper failed", exc)
        finally:
            if holding:
                # Left early: a later hold starts a new reaper.
                with self.changed:
                    self.reaping = False

    def _close_session_later(self, job: _WatchJob) -> None:
        try:
            threading.Thread(target=self._close_session, args=(job,), name="conduit-watch-job-close",
                             daemon=True).start()
        except RuntimeError:
            self._close_session(job)

    def _close_session(self, job: _WatchJob) -> None:
        """Closes the job's live session, unless the app has joined it; its
        chat stays in history either way."""
        with self.lock:
            if job.closed or not job.session_id:
                return
            # The call's chat closes once the last request in it is done.
            sharing = [other for other in self.jobs.values() if other.session_id == job.session_id]
            if any(other.active or other.cancelling for other in sharing if other is not job):
                return
            for other in sharing:
                other.closed = True
            job.closed = True
        record = self.server._sessions.get(job.session_id)
        if isinstance(record, dict) and record.get("transport") is self.transport:
            try:
                self.rpc("session.close", {"session_id": job.session_id})
            except Exception as exc:  # noqa: BLE001 — Hermes' reaper closes it once our transport closes
                watch_tools._log_watch_failure("Closing a Watch job's session failed", exc)
        self._close_transport_if_idle()

    def _close_transport_if_idle(self) -> None:
        # Once the call has ended and nothing runs: a session the app joined
        # then lives or ends with the app alone.
        with self.lock:
            if self.ended and not any(job.active for job in self.jobs.values()):
                self.transport.close()

    def _tag(self, job: _WatchJob) -> None:
        """Files the job under Voice Jobs in Conduit, as a phone job is."""
        session_id = job.stored_session_id or job.session_id
        try:
            with core._profile_scope(job.profile or self.grant.profile):
                with voice_calls._voice_lock():
                    db = voice_calls._open_voice_db(voice_calls._VOICE_WRITE)
                    try:
                        if voice_calls._voice_tags(db).get(session_id, {}).get("kind") != "call":
                            voice_calls._set_voice_tag(db, session_id, {"kind": "job"})
                            # Its turn may have ended before the tag landed.
                            voice_calls._archive_settled_job(db, session_id)
                    finally:
                        db.close()
        except Exception as exc:  # noqa: BLE001 — the job runs untagged
            watch_tools._log_watch_failure("Tagging a Watch job failed", exc)


def run_watch_job_call(grant: "watch_tools._WatchGrant", tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    jobs = grant.jobs
    if jobs is None:
        return {"ok": False, "status": 403, "detail": "This tool isn't available to the Watch"}
    try:
        if tool == "start_job":
            return jobs.start(args)
        if tool == "list_jobs":
            return jobs.list()
        if tool == "cancel_job":
            return jobs.cancel(args)
        if tool == WATCH_JOB_FOLLOW_UP:
            return jobs.interrupt(args)
        if tool == "job_news":
            return jobs.news(args, caller=grant)
        return jobs.answer_approval(args)
    except Exception as exc:  # noqa: BLE001 — named; the message can carry the task
        watch_tools._log_watch_failure(f"A Watch {tool} call failed", exc)
        return {"ok": False, "status": 500, "detail": f"The job call failed on the host ({type(exc).__name__})"}
