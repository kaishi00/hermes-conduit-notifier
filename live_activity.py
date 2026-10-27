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
turn nobody watches costs a request or two. A relay without the route, or
one that refuses the caller (401/403), turns the whole thing off for a while.

A compaction can move a running turn to a new session id (Hermes does when
it can't compact in place). The phone registered the turn under the ids it
knew when it started, so the turn keeps reporting, and ends, under the id it
started with: an id this reporter doesn't know is looked up in the session
store's compaction lineage before it is taken for a turn of its own.

An end the relay answered nobody is watching (the phone hadn't registered
yet) says how long ago the turn ended, so the relay can still end an activity
registered after it, but not one of the chat's next turn.
"""

from __future__ import annotations

import contextvars
import logging
import math
import threading
import time
from pathlib import Path
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
# The relay refused one turn's update (rate limit, outage): that turn waits
# this long before it is tried again. Other turns carry on.
BACKOFF_S = 60.0
# An end the relay didn't take is tried again this soon: the Lock Screen
# should stop saying "running" quickly.
END_RETRY_S = 10.0
# An end that still hasn't gone out after this long is given up on: the
# activity's stale date has taken over by then.
END_GIVE_UP_S = 15 * 60.0
# The relay has no Live Activity route: try again after this long.
UNSUPPORTED_S = 30 * 60.0
# The relay (or a filter in front of it) refused the credential or the
# caller: every turn waits this long. Retrying an end every 10 s instead kept
# a CrowdSec brute-force ban renewing itself for days. A refused end is
# given up on (END_GIVE_UP_S) before the pause lifts.
REFUSED_S = 30 * 60.0
# How long a sent end keeps a second end of the same turn from going out.
ENDED_MEMORY_S = 120.0
# Turns tracked at once; the oldest is forgotten past this.
MAX_SESSIONS = 64
# Session ids remembered as a compaction of a running turn, or as none.
MAX_LINEAGE_IDS = 1024
# An id the session store couldn't place yet is looked up again after this.
UNSETTLED_RETRY_S = 30.0
# A turn with no hook for this long is forgotten (its end never came).
IDLE_FORGET_S = 2 * 60 * 60.0

PHASES = {"thinking", "tool", "approval", "input"}
# "Leave this field as it is" for ``_update``.
_UNSET: Any = object()
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
    "message_agent": "messages",
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
                 "tracked", "touched_at", "started_at", "retry_at", "ended_at")

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
        # After a failed send, this turn waits until then.
        self.retry_at = 0.0
        # When the end was asked for.
        self.ended_at = 0.0


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
        lineage: Callable[[str, set[str]], str | bool | None] | None = None,
    ) -> None:
        self._post = post or _post_to_relay
        # Which of the running turns' ids a session id continues through
        # compactions: the id, False when none, None when it can't tell yet.
        self._lineage = lineage or (lambda _session_id, _running: False)
        # A running turn's later session ids -> the id it started under.
        self._moved: dict[str, str] = {}
        # Ids that continue no running turn (a session's lineage never
        # changes, so the answer is kept).
        self._not_moved: set[str] = set()
        # Ids the store couldn't place yet -> when to look again.
        self._unsettled: dict[str, float] = {}
        self._clock = clock
        self._start_thread = start_thread
        self._turns: dict[str, _Turn] = {}
        # Sessions whose end went out lately: Hermes ends a turn twice
        # (post_llm_call, then on_session_end), and the second must not
        # send another end.
        self._ended: dict[str, float] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        # The relay has no Live Activity route: nothing is sent until then.
        self._paused_until = 0.0
        self._thread: threading.Thread | None = None
        # stop() (the plugin was unloaded) ends the thread and keeps a new
        # one from starting until start(); a thread from before a stop()
        # exits even if start() ran meanwhile.
        self._stopped = False
        self._generation = 0

    # --- hook-facing ---------------------------------------------------

    def turn_started(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            now = self._clock()
            # The chat's next turn runs under the id it moved to; the phone
            # registers that one now and ends the card of the turn before,
            # so that turn goes too unless it still owes the relay its end.
            moved_from = self._moved.pop(session_id, None)
            before = self._turns.get(moved_from) if moved_from is not None else None
            if before is not None and before.end is None:
                del self._turns[moved_from]
            self._turns.pop(session_id, None)
            self._ended.pop(session_id, None)
            self._turns[session_id] = _Turn(now)
            self._evict(keep=session_id)
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
        except (TypeError, ValueError, OverflowError):
            return
        if not (math.isfinite(used) and math.isfinite(limit)) or limit <= 0 or used < 0:
            return
        # Clamped before rounding: a huge ratio overflows to inf.
        ratio = min(used * 100 / limit, 100.0)
        self._update(session_id, context=max(0, round(ratio)))

    def turn_ended(self, session_id: str, outcome: str) -> None:
        if not session_id or outcome not in ENDS:
            return
        # An end always looks: one that missed its turn leaves the card up.
        session_id = self._turn_id(session_id, recheck=True)
        with self._lock:
            turn = self._turns.get(session_id)
            now = self._clock()
            ended_at = self._ended.get(session_id)
            if turn is None and ended_at is not None and now - ended_at < ENDED_MEMORY_S:
                return
            # A turn this plugin never saw start (a restart mid-turn) still
            # ends: the phone may be showing it.
            if turn is None:
                turn = _Turn(now)
                self._turns[session_id] = turn
                self._evict(keep=session_id)
            if turn.end is not None:
                return
            turn.end = outcome
            turn.dirty = True
            # An earlier update's backoff doesn't hold the end up.
            turn.retry_at = 0.0
            turn.touched_at = turn.ended_at = now
            self._changed()

    # --- delivery --------------------------------------------------------

    def flush(self) -> float | None:
        """Send every update that is due; returns seconds until the next one."""
        due: list[tuple[str, _Turn, dict[str, Any]]] = []
        with self._lock:
            generation = self._generation
            now = self._clock()
            next_in: float | None = None

            def later(at: float) -> None:
                nonlocal next_in
                next_in = at - now if next_in is None else min(next_in, at - now)

            for session_id, turn in list(self._turns.items()):
                if now - turn.touched_at > IDLE_FORGET_S or (
                    turn.end is not None and now - turn.ended_at > END_GIVE_UP_S
                ):
                    del self._turns[session_id]
                    continue
                if not turn.dirty:
                    continue
                if now < self._paused_until:
                    later(self._paused_until)
                    continue
                interval = UNTRACKED_RETRY_S if turn.tracked is False else MIN_INTERVAL_S
                # An end goes out at once.
                if turn.end is not None:
                    ready_at = now
                elif turn.sent_at:
                    ready_at = turn.sent_at + interval
                else:
                    ready_at = turn.started_at + FIRST_DELAY_S
                ready_at = max(ready_at, turn.retry_at)
                if ready_at > now:
                    later(ready_at)
                    continue
                # Marked sent now so the next hook's change waits its turn;
                # a failed send marks it dirty again.
                turn.dirty = False
                turn.sent_at = now
                due.append((session_id, turn, _payload(session_id, turn, now)))
            for session_id, ended_at in list(self._ended.items()):
                if now - ended_at >= ENDED_MEMORY_S:
                    del self._ended[session_id]
        for session_id, turn, payload in due:
            with self._lock:
                # A refusal earlier in this batch paused everything: the rest
                # wait too instead of reaching the relay back to back.
                if self._clock() < self._paused_until:
                    if self._turns.get(session_id) is turn:
                        turn.dirty = True
                    next_in = self._paused_until - self._clock()
                    continue
            self._deliver(session_id, turn, payload, generation)
        return next_in

    def _deliver(self, session_id: str, turn: _Turn, payload: dict[str, Any], generation: int | None = None) -> None:
        try:
            answer = self._post(payload)
        except Exception as error:  # noqa: BLE001 — never into the turn
            status = error.status if isinstance(error, client.RelayRejected) else None
            paused_for = 0.0
            with self._lock:
                # A send still in flight across a stop() (the plugin reloaded,
                # after pairing again, say) mustn't pause the new reporter.
                if generation is not None and generation != self._generation:
                    return
                now = self._clock()
                if status == 400:
                    # The relay will never take this report: retrying can't
                    # help, so the turn is dropped (and a second end of it
                    # isn't sent either).
                    if self._turns.get(session_id) is turn:
                        del self._turns[session_id]
                    if payload.get("end") is not None:
                        self._ended[session_id] = now
                else:
                    if status == 404:
                        # The relay has no route at all: every turn waits.
                        paused_for = UNSUPPORTED_S
                    elif status in (401, 403):
                        paused_for = REFUSED_S
                    if paused_for:
                        self._paused_until = now + paused_for
                    else:
                        turn.retry_at = now + (END_RETRY_S if payload.get("end") is not None else BACKOFF_S)
                    # The state (an end included) goes out on the next try,
                    # unless a newer turn of the session replaced this one.
                    if self._turns.get(session_id) is turn:
                        turn.dirty = True
                        self._changed()
            if status == 400:
                logger.info("Conduit Live Activity report refused: %s", error)
            elif paused_for:
                logger.warning("Conduit Live Activity paused for %d minutes: the relay answered %s",
                               paused_for // 60, status)
            else:
                logger.info("Conduit Live Activity update not delivered: %s", error)
            return
        tracked = bool(isinstance(answer, dict) and answer.get("tracked"))
        with self._lock:
            turn.tracked = tracked
            turn.retry_at = 0.0
            if payload.get("end") is not None and self._turns.get(session_id) is turn:
                del self._turns[session_id]
                self._ended[session_id] = self._clock()

    def _turn_id(self, session_id: str, recheck: bool = False) -> str:
        """The id a running turn started under, for one a compaction moved
        it to; any other id as it is."""
        with self._lock:
            if session_id in self._moved:
                return self._moved[session_id]
            if session_id in self._turns or session_id in self._not_moved:
                return session_id
            if not recheck and self._clock() < self._unsettled.get(session_id, 0.0):
                return session_id
            running = {key for key, turn in self._turns.items() if turn.end is None}
        if not running:
            return session_id
        try:
            found = self._lineage(session_id, running)
        except Exception:  # noqa: BLE001 — never into the turn
            logger.debug("Conduit Live Activity lineage lookup failed", exc_info=True)
            return session_id
        with self._lock:
            if isinstance(found, str) and found:
                if len(self._moved) >= MAX_LINEAGE_IDS:
                    self._moved.pop(next(iter(self._moved)))
                self._moved[session_id] = found
                return found
            if found is False:
                self._unsettled.pop(session_id, None)
                if len(self._not_moved) >= MAX_LINEAGE_IDS:
                    self._not_moved.clear()
                self._not_moved.add(session_id)
            else:
                # Not stored yet (a compaction being written) or never
                # stored at all: not every hook reads the store for it.
                if len(self._unsettled) >= MAX_LINEAGE_IDS:
                    self._unsettled.pop(next(iter(self._unsettled)))
                self._unsettled[session_id] = self._clock() + UNSETTLED_RETRY_S
        return session_id

    def _update(self, session_id: str, *, phase: str | None = None, tool: str | None | object = _UNSET,
                context: int | None = None, count_tool: bool = False) -> None:
        if not session_id:
            return
        session_id = self._turn_id(session_id)
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.end is not None:
                return
            before = (turn.phase, turn.tool, turn.tools, turn.context)
            if phase is not None:
                turn.phase = phase
            if tool is not _UNSET:
                turn.tool = tool  # type: ignore[assignment]
            if count_tool:
                turn.tools += 1
            if context is not None:
                turn.context = context
            turn.touched_at = self._clock()
            if (turn.phase, turn.tool, turn.tools, turn.context) != before:
                turn.dirty = True
                self._changed()

    def _evict(self, keep: str) -> None:
        # Called with the lock held. The oldest turn goes, unless it still
        # owes the phone its end; `keep` (just added) never goes.
        while len(self._turns) > MAX_SESSIONS:
            victim = next((key for key, old in self._turns.items()
                           if old.end is None and key != keep), None)
            if victim is None:
                victim = next(key for key in self._turns if key != keep)
            self._turns.pop(victim)

    def start(self) -> None:
        with self._lock:
            self._stopped = False

    def stop(self, timeout: float = 1.0) -> None:
        """Forgets every turn and ends the thread, waiting up to ``timeout``
        for an update in flight."""
        with self._lock:
            self._stopped = True
            self._generation += 1
            self._turns.clear()
            self._ended.clear()
            self._moved.clear()
            self._not_moved.clear()
            self._unsettled.clear()
            # A reload (after pairing again, say) starts unpaused.
            self._paused_until = 0.0
            thread, self._thread = self._thread, None
            self._wake.notify_all()
        if thread is not None:
            thread.join(timeout)

    def _changed(self) -> None:
        # Called with the lock held.
        if not self._start_thread or self._stopped:
            return
        if self._thread is None:
            # Under the profile whose turn started it (see client.enqueue):
            # each profile's plugin has its own reporter.
            self._thread = threading.Thread(target=contextvars.copy_context().run, args=(self._run, self._generation),
                                            name="conduit-live-activity", daemon=True)
            self._thread.start()
        self._wake.notify()

    def _run(self, generation: int = 0) -> None:
        while generation == self._generation:
            try:
                next_in = self.flush()
            except Exception:  # noqa: BLE001
                logger.debug("Conduit Live Activity flush failed", exc_info=True)
                next_in = BACKOFF_S
            with self._lock:
                if generation != self._generation:
                    return
                if not any(turn.dirty for turn in self._turns.values()):
                    self._wake.wait(timeout=IDLE_FORGET_S)
                elif next_in is not None:
                    self._wake.wait(timeout=max(0.05, next_in))


def _payload(session_id: str, turn: _Turn, now: float) -> dict[str, Any]:
    # The relay takes at most 10,000.
    state: dict[str, Any] = {"phase": turn.phase, "tools": min(turn.tools, 10_000)}
    if turn.tool and turn.phase == "tool":
        state["tool"] = turn.tool
    if turn.context is not None:
        state["context"] = turn.context
    payload: dict[str, Any] = {"session_id": session_id, "state": state}
    if turn.end is not None:
        payload["end"] = turn.end
        # A retried end still says when the turn ended.
        payload["ended_ago"] = round(max(0.0, now - turn.ended_at), 1)
    return payload


def _post_to_relay(payload: dict[str, Any]) -> dict[str, Any]:
    state = client.load_state()
    if not state:
        return {"tracked": False}
    if state.get("redact_content"):
        payload = {**payload, "state": {k: v for k, v in payload["state"].items() if k != "tool"}}
    try:
        answer = client.request_json(
            f"{state['relay_url'].rstrip('/')}/v1/live-activity",
            method="POST",
            credential=state["credential"],
            payload=payload,
            timeout=10.0,
        )
    except client.RelayRejected as error:
        # The same "pair again" / "blocked" signals notifications raise (and
        # clear), for a host whose only relay traffic is Live Activities.
        _mark_refused(client.record_relay_refusal, state, error)
        raise
    # Also ends a notification pause: the relay takes this pairing again.
    _mark_refused(client.record_relay_accepted, state)
    return answer


def _mark_refused(mark: Any, *args: Any) -> None:
    # A failure here must not replace the relay's answer: that would turn a
    # refusal into a per-turn retry, or a delivered update into a resend.
    # The arguments come in bound, not closed over: Python unbinds an
    # `except ... as error` name when the block ends.
    try:
        mark(*args)
    except Exception:  # noqa: BLE001
        logger.warning("Conduit could not record the relay's refusal", exc_info=True)


# Parents followed up a compaction lineage before giving up.
_LINEAGE_HOPS = 32


def compacted_from(session_id: str, running: set[str]) -> str | bool | None:
    """The running turn's id ``session_id`` continues through compactions,
    read from Hermes' session store: False when it continues none, None
    when that can't be settled yet (not stored yet, or unreadable)."""
    import sqlite3

    from hermes_constants import get_hermes_home

    try:
        path = Path(get_hermes_home()) / "state.db"
        if not path.exists():
            return None
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    except Exception:  # noqa: BLE001 — a hook must never break the turn
        return None
    try:
        current, seen = session_id, {session_id}
        for _ in range(_LINEAGE_HOPS):
            row = db.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            if row is None:
                return None if current == session_id else False
            parent = row[0]
            if not parent:
                return False
            ended = db.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
            # Only a compaction carries the turn on; a branch or a delegated
            # child is a conversation of its own.
            if not ended or ended[0] != "compression":
                return False
            if parent in running:
                return parent
            if parent in seen:
                return False  # a loop in the store: no turn continues through it
            seen.add(parent)
            current = parent
        return None
    except sqlite3.Error:
        return None
    finally:
        db.close()


reporter = LiveActivityReporter(lineage=compacted_from)
