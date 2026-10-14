"""Lock Screen progress for a running turn (Conduit #605).

Conduit starts a Live Activity when the user sends a turn and registers its
push token with the relay under the chat's session ids. While the turn runs,
these hooks tell the relay what the agent is doing (thinking, which kind of
tool, waiting on the user), how many tools it ran and how full its context
is, and the relay updates the Live Activity through APNs.

What goes out is deliberately coarse: a phase, a tool *category* from a fixed
list (never a tool's name, arguments or output), two numbers and the session
id. Live Activity payloads are shown by iOS itself, so they can't be sealed
end to end like notifications; nothing here says what the turn is about. A
profile with content redaction on sends no tool category at all.

Sends are coalesced per session on one background thread: at most one every
``MIN_INTERVAL_S`` while the phone is watching, and the end of a turn at
once. The relay answers whether the phone registered an activity for the
session; one that didn't (yet) is asked again only every
``UNTRACKED_RETRY_S`` (Conduit registers a moment after the turn starts), so a
turn nobody watches costs a request or two. A relay without the route turns
the whole thing off for a while.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from . import client


logger = logging.getLogger("hermes.plugins.conduit_push")

# Seconds between updates of one watched turn; the relay holds the same line.
MIN_INTERVAL_S = 5.0
# A turn the phone isn't watching (yet) is asked about again this often.
UNTRACKED_RETRY_S = 15.0
# The first update of a turn waits this long, so the activity Conduit starts
# as it sends the turn is registered by the time the relay is asked.
FIRST_DELAY_S = 2.0
# The relay refused (rate limit, outage): wait this long before the next try.
BACKOFF_S = 60.0
# The relay has no Live Activity route: try again after this long.
UNSUPPORTED_S = 30 * 60.0
# How long a sent end keeps a second end of the same turn from going out.
ENDED_MEMORY_S = 120.0
# Turns tracked at once; the oldest is forgotten past this.
MAX_SESSIONS = 64
# A turn with no hook for this long is forgotten (its end never came).
IDLE_FORGET_S = 2 * 60 * 60.0

PHASES = {"thinking", "tool", "approval", "input"}
ENDS = {"done", "failed", "stopped"}

# Tool name -> category Conduit names on the Lock Screen. Exact names first,
# then prefixes; anything else is just "tool".
_TOOL_CATEGORIES = {
    "terminal": "terminal",
    "process": "terminal",
    "execute_code": "code",
    "read_file": "files",
    "write_file": "files",
    "patch": "files",
    "search_files": "files",
    "web_search": "web",
    "web_extract": "web",
    "web_crawl": "web",
    "delegate_task": "agents",
    "memory": "memory",
    "session_search": "memory",
    "vision_analyze": "images",
    "image_generate": "images",
    "text_to_speech": "audio",
    "todo": "planning",
    "cronjob": "schedule",
    "send_message": "messages",
    "skill_view": "skills",
    "skills_list": "skills",
    "skill_manage": "skills",
}
_TOOL_PREFIXES = (
    ("browser_", "browser"),
    ("mcp_", "connector"),
    ("conduit_", "phone"),
    ("ha_", "home"),
    ("web_", "web"),
    ("skill", "skills"),
)


def tool_category(name: Any) -> str:
    """The Lock Screen category for a tool name (never the name itself)."""
    value = str(name or "").strip().lower()
    if value in _TOOL_CATEGORIES:
        return _TOOL_CATEGORIES[value]
    for prefix, category in _TOOL_PREFIXES:
        if value.startswith(prefix):
            return category
    return "tool"


class _Turn:
    __slots__ = ("phase", "tool", "tools", "context", "end", "dirty", "sent_at",
                 "tracked", "touched_at", "started_at")

    def __init__(self, now: float) -> None:
        self.phase = "thinking"
        self.tool: str | None = None
        self.tools = 0
        self.context: int | None = None
        self.end: str | None = None
        self.dirty = True
        self.sent_at = 0.0
        # None: not asked yet; False: the phone wasn't watching when asked.
        self.tracked: bool | None = None
        self.touched_at = now
        self.started_at = now


class LiveActivityReporter:
    """Per-session turn state and the thread that reports it to the relay.

    ``post`` sends one update and returns the relay's answer; tests pass a
    fake and a fake clock and call ``flush`` instead of running the thread.
    """

    def __init__(
        self,
        post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        start_thread: bool = True,
    ) -> None:
        self._post = post or _post_to_relay
        self._clock = clock
        self._start_thread = start_thread
        self._turns: dict[str, _Turn] = {}
        # Sessions whose end went out lately: Hermes ends a turn twice
        # (post_llm_call, then on_session_end), and the second must not
        # send another end.
        self._ended: dict[str, float] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._paused_until = 0.0
        self._thread: threading.Thread | None = None

    # --- hook-facing ---------------------------------------------------

    def turn_started(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            now = self._clock()
            self._turns.pop(session_id, None)
            self._ended.pop(session_id, None)
            self._turns[session_id] = _Turn(now)
            while len(self._turns) > MAX_SESSIONS:
                self._turns.pop(next(iter(self._turns)))
            self._changed()

    def thinking(self, session_id: str) -> None:
        self._update(session_id, phase="thinking", tool=None)

    def tool_started(self, session_id: str, tool_name: Any) -> None:
        name = str(tool_name or "")
        if name == "clarify":
            self._update(session_id, phase="input", tool=None)
            return
        self._update(session_id, phase="tool", tool=tool_category(name), count_tool=True)

    def waiting(self, session_id: str, kind: str) -> None:
        self._update(session_id, phase="approval" if kind == "approval" else "input", tool=None)

    def context_used(self, session_id: str, prompt_tokens: Any, context_length: Any) -> None:
        try:
            used = float(prompt_tokens)
            limit = float(context_length)
        except (TypeError, ValueError):
            return
        if limit <= 0 or used < 0:
            return
        self._update(session_id, context=max(0, min(100, round(used * 100 / limit))))

    def turn_ended(self, session_id: str, outcome: str) -> None:
        if not session_id or outcome not in ENDS:
            return
        with self._lock:
            turn = self._turns.get(session_id)
            now = self._clock()
            ended_at = self._ended.get(session_id)
            if turn is None and ended_at is not None and now - ended_at < ENDED_MEMORY_S:
                return
            # A turn this plugin never saw start (a restart mid-turn) still
            # ends: the phone may be showing it.
            if turn is None:
                turn = _Turn(self._clock())
                self._turns[session_id] = turn
            if turn.end is not None:
                return
            turn.end = outcome
            turn.dirty = True
            turn.touched_at = self._clock()
            self._changed()

    # --- delivery --------------------------------------------------------

    def flush(self) -> float | None:
        """Send every update that is due; returns seconds until the next one."""
        due: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            now = self._clock()
            next_in: float | None = None
            for session_id, turn in list(self._turns.items()):
                if now - turn.touched_at > IDLE_FORGET_S:
                    del self._turns[session_id]
                    continue
                if not turn.dirty:
                    continue
                if now < self._paused_until:
                    wait = self._paused_until - now
                    next_in = wait if next_in is None else min(next_in, wait)
                    continue
                interval = UNTRACKED_RETRY_S if turn.tracked is False else MIN_INTERVAL_S
                # An end goes out at once.
                if turn.end is not None:
                    ready_at = now
                elif turn.sent_at:
                    ready_at = turn.sent_at + interval
                else:
                    ready_at = turn.started_at + FIRST_DELAY_S
                if ready_at > now:
                    next_in = ready_at - now if next_in is None else min(next_in, ready_at - now)
                    continue
                turn.dirty = False
                turn.sent_at = now
                due.append((session_id, _payload(session_id, turn)))
                if turn.end is not None:
                    del self._turns[session_id]
                    self._ended[session_id] = now
            for session_id, ended_at in list(self._ended.items()):
                if now - ended_at >= ENDED_MEMORY_S:
                    del self._ended[session_id]
        for session_id, payload in due:
            self._deliver(session_id, payload)
        return next_in

    def _deliver(self, session_id: str, payload: dict[str, Any]) -> None:
        try:
            answer = self._post(payload)
        except client.RelayRejected as error:
            with self._lock:
                self._paused_until = self._clock() + (UNSUPPORTED_S if error.status == 404 else BACKOFF_S)
            logger.info("Conduit Live Activity update not taken: %s", error)
            return
        except Exception as error:  # noqa: BLE001 — never into the turn
            with self._lock:
                self._paused_until = self._clock() + BACKOFF_S
            logger.info("Conduit Live Activity update failed: %s", error)
            return
        tracked = bool(isinstance(answer, dict) and answer.get("tracked"))
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is not None:
                turn.tracked = tracked

    def _update(self, session_id: str, *, phase: str | None = None, tool: str | None | type = ...,
                context: int | None = None, count_tool: bool = False) -> None:
        if not session_id:
            return
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.end is not None:
                return
            before = (turn.phase, turn.tool, turn.tools, turn.context)
            if phase is not None:
                turn.phase = phase
            if tool is not ...:
                turn.tool = tool  # type: ignore[assignment]
            if count_tool:
                turn.tools += 1
            if context is not None:
                turn.context = context
            turn.touched_at = self._clock()
            if (turn.phase, turn.tool, turn.tools, turn.context) != before:
                turn.dirty = True
                self._changed()

    def _changed(self) -> None:
        # Called with the lock held.
        if not self._start_thread:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="conduit-live-activity", daemon=True)
            self._thread.start()
        self._wake.notify()

    def _run(self) -> None:
        while True:
            try:
                next_in = self.flush()
            except Exception:  # noqa: BLE001
                logger.debug("Conduit Live Activity flush failed", exc_info=True)
                next_in = BACKOFF_S
            with self._lock:
                if not any(turn.dirty for turn in self._turns.values()):
                    self._wake.wait(timeout=IDLE_FORGET_S)
                elif next_in is not None:
                    self._wake.wait(timeout=max(0.05, next_in))


def _payload(session_id: str, turn: _Turn) -> dict[str, Any]:
    state: dict[str, Any] = {"phase": turn.phase, "tools": turn.tools}
    if turn.tool and turn.phase == "tool":
        state["tool"] = turn.tool
    if turn.context is not None:
        state["context"] = turn.context
    payload: dict[str, Any] = {"session_id": session_id, "state": state}
    if turn.end is not None:
        payload["end"] = turn.end
    return payload


def _post_to_relay(payload: dict[str, Any]) -> dict[str, Any]:
    state = client.load_state()
    if not state:
        return {"tracked": False}
    if state.get("redact_content"):
        payload = {**payload, "state": {k: v for k, v in payload["state"].items() if k != "tool"}}
    return client.request_json(
        f"{state['relay_url'].rstrip('/')}/v1/live-activity",
        method="POST",
        credential=state["credential"],
        payload=payload,
        timeout=10.0,
    )


reporter = LiveActivityReporter()
