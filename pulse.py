"""Pulse, slice 0: the home chat delivery probe (designs/proactivity.md 6.4-6.4.2, 7.6).

Off unless turned on (``hermes conduit-push pulse on``). When on, the
process that hosts Conduit's chats (the dashboard) sends a short hidden
probe turn into the home chat every few hours (or when ``pulse probe``
asks), and records whether Hermes delivered it. That answers the hard gate:
can the home chat take turns with nobody watching, after a quiet day, a
restart and a reboot.

A Pulse turn is recognised by its tag in ``pre_llm_call``. On such a turn
every tool is blocked in ``pre_tool_call`` (the probe needs none; later
slices allow named reads only), and no push or Live Activity goes out for it.

A few times a day it also sends the shadow report (pulse_report): what Hermes
would have brought up since the last one, for the user to rate in the home chat.

While Pulse is on, every tool call in the profile also gets a row in the
outcome ledger (pulse_ledger), the record later slices check claims against,
and quiet chats are read into memory items (pulse_memory), on a thread of
their own so a slow model call never holds up delivery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

from . import pulse_delivery as delivery
from . import pulse_hermes as hermes
from . import pulse_host as host
from . import pulse_ledger as ledger
from . import pulse_memory as memory
from . import pulse_report as report
from . import pulse_store as store

logger = logging.getLogger(__name__)

PROBE = "probe"
REPORT = "report"
# A report that didn't get through is tried again after this, up to
# REPORT_TRIES a day, and only this long after its time (a dashboard down at
# 08:00 still sends it at 11:00, but not at midnight).
REPORT_RETRY_S = 1800.0
REPORT_TRIES = 3
REPORT_WINDOW_S = 8 * 3600.0
# Ratings are read back from the home chat this often, for reports this old.
RATINGS_EVERY_S = 300.0
RATINGS_DAYS = 3
# Quiet chats are looked for this often (or when `pulse memory now` asks).
MEMORY_EVERY_S = 300.0
TICK_S = 30.0
# `pulse status` calls the loop stopped after this many missed ticks.
STALE_TICK_S = 10 * TICK_S
# One process runs the loop; its lease outlives a few missed ticks.
LEADER_TTL_S = 4 * TICK_S
MAX_TURNS = 256
PROBE_TEXT = "Pulse delivery check {tag}. Nothing to do: reply with one short line."
PROBE_NOTE = ("This turn was started by Pulse, the plugin that lets Hermes speak up on its own, to check that "
              "this chat can take turns while nobody is watching. Reply with one short line saying the check "
              "arrived. Don't use any tools.")
BLOCK_MESSAGE = "Pulse turns can't use tools."

_ctx: Any = None
# Set once pre_llm_call is registered: without it a Pulse turn isn't
# recognized, so it would run with tools and push like the user's own.
turns_marked = False
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_wake = threading.Event()
_started_at = 0.0
# (session id, turn id) -> tag, for the Pulse turns that ran in this process.
_turns: dict[tuple[str, str], str] = {}
_turns_lock = threading.Lock()
# The last problem logged, so a missing home chat isn't logged every tick.
_last_problem = ""
_last_ratings = 0.0
_owner = f"{socket.gethostname()}:{os.getpid()}"
_memory_thread: Optional[threading.Thread] = None
# Whether Hermes took the plugin's auxiliary task, so memory calls can name it.
_memory_task = False


def build_stamp() -> str:
    """The plugin's code as installed on disk now. Every process hosting
    chats loads it once, so after an install the ones started before it run
    the old code until restarted."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        try:
            info = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}:{info.st_mtime_ns}:{info.st_size};".encode())
    return digest.hexdigest()[:12]


_build = build_stamp()
_said_stale = False


def register(ctx: Any) -> None:
    """Starts the Pulse loop for this load. It only acts in a process that
    hosts Conduit's chats, and only while Pulse is on."""
    global _ctx, _thread, _stop, _started_at, _memory_task
    _ctx = ctx
    try:
        ctx.register_auxiliary_task(memory.TASK, display_name=memory.TASK_NAME, description=memory.TASK_DESCRIPTION)
        _memory_task = True
    except Exception:  # noqa: BLE001 — an older Hermes: memory uses the main model
        logger.debug("conduit_push: Pulse: no auxiliary task for memory", exc_info=True)
    if _thread is not None and _thread.is_alive() and not _stop.is_set():
        return  # loaded again without an unload: the running loop serves the new context
    _started_at = time.time()
    # Each loop has its own stop, so one still winding down never resumes.
    _stop = threading.Event()
    _thread = threading.Thread(target=_run, args=(_stop,), name="conduit-pulse", daemon=True)
    _thread.start()


def stop(timeout: float = 1.0) -> None:
    _stop.set()
    _wake.set()
    thread = _thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)


def open_store(home: Optional[Path] = None) -> store.PulseStore:
    return store.open_store(home or get_hermes_home())


# Hooks ----------------------------------------------------------------------

def pre_llm_call(session_id: str, turn_id: Any, user_message: Any) -> Optional[str]:
    """The hidden note for a Pulse turn, or None for any other turn. The
    tag must be one this profile's store sent, so a user typing something
    that looks like one gets nothing."""
    if not isinstance(user_message, str) or "Pulse" not in user_message:
        return None
    home = get_hermes_home()
    if not (Path(home) / store.DB_NAME).exists():
        return None
    pulse_store = open_store(home)
    for row in pulse_store.in_flight():
        # The whole injected text, not just its tag: a user quoting a tag
        # doesn't make their turn Pulse's (that would only take their tools).
        if row["text"] in user_message:
            _mark_turn(session_id, turn_id, row["tag"])
            if row["state"] != store.RUNNING:
                pulse_store.update_attempt(row["attempt_id"], state=store.RUNNING,
                                           turn_id=turn_id if isinstance(turn_id, str) else "")
            return report.REPORT_NOTE if row["kind"] == REPORT else PROBE_NOTE
    return None


def is_pulse_turn(session_id: str, turn_id: Any) -> bool:
    if not isinstance(turn_id, str) or not turn_id:
        return False
    with _turns_lock:
        return (session_id, turn_id) in _turns


def pre_tool_call(session_id: str, turn_id: Any) -> Optional[dict[str, str]]:
    """Default deny: a Pulse turn gets no tools at all in slice 0."""
    if is_pulse_turn(session_id, turn_id):
        return {"action": "block", "message": BLOCK_MESSAGE}
    return None


def turn_ended(session_id: str, turn_id: Any) -> None:
    """A Pulse turn ended: noted on its attempt (a reply row should follow at
    once), and the loop reconciles now instead of at its next tick."""
    if not is_pulse_turn(session_id, turn_id):
        return
    with _turns_lock:
        tag = _turns.get((session_id, turn_id))
    pulse_store = open_store() if tag else None
    row = pulse_store.attempt_by_tag(tag) if pulse_store else None
    if row is not None and row["state"] in store.IN_FLIGHT:
        pulse_store.update_attempt(row["attempt_id"], detail=delivery.ENDED)
    _wake.set()


def post_tool_call(**kwargs: Any) -> None:
    """The outcome ledger's row for one tool call, in any session, while
    Pulse is on (pulse_ledger)."""
    ledger.record(get_hermes_home(), **{key: kwargs.get(key) for key in (
        "tool_name", "args", "result", "session_id", "turn_id", "task_id", "tool_call_id", "status",
        "error_type", "error_message")}, duration_ms=kwargs.get("duration_ms") or 0)


def _mark_turn(session_id: str, turn_id: Any, tag: str) -> None:
    if not isinstance(turn_id, str) or not turn_id:
        return
    with _turns_lock:
        _turns.pop((session_id, turn_id), None)
        _turns[(session_id, turn_id)] = tag
        while len(_turns) > MAX_TURNS:
            _turns.pop(next(iter(_turns)))


# The loop -------------------------------------------------------------------

def _run(stopped: threading.Event) -> None:
    while not stopped.is_set():
        # Cleared before the tick, so a turn ending during it wakes the next.
        _wake.clear()
        try:
            tick()
        except Exception:  # noqa: BLE001 — the loop must outlive a bad tick
            logger.warning("conduit_push: Pulse tick failed", exc_info=True)
        _wake.wait(TICK_S)


def _hosts_chats() -> bool:
    """Whether this process hosts Conduit's chats: the dashboard (or `hermes
    serve`) publishes a session host to plugins, and its method table is
    loaded. The messaging gateway and the CLI have neither."""
    manager = getattr(_ctx, "_manager", None)
    return bool(getattr(manager, "has_tui_message_injector", False)) and host.available()


def tick(now: Optional[float] = None) -> None:
    """One pass: reconcile what's in flight, read report ratings, then send
    a probe or the shadow report if one is due."""
    if _ctx is None or not turns_marked or not _hosts_chats():
        return
    home = get_hermes_home()
    if not (Path(home) / store.DB_NAME).exists():
        return  # never turned on: leave no file behind
    pulse_store = open_store(home)
    if not pulse_store.get("enabled"):
        return
    now = time.time() if now is None else now
    if build_stamp() != _build:
        # Installed over since this process started: leave the loop to a
        # process running the new code (the lease runs out on its own).
        global _said_stale
        if not _said_stale:
            logger.warning("conduit_push: Pulse: a newer build of the plugin is installed; this process (%s) "
                           "stopped running Pulse until it's restarted", _owner)
            _said_stale = True
        return
    if not pulse_store.claim_leader(_owner, LEADER_TTL_S, now, build=_build):
        return  # another process on this Hermes home runs the loop
    # The loop's heartbeat: `pulse status` warns when nothing runs the loop.
    pulse_store.set("last_tick_at", now)
    delivery.reconcile(pulse_store, home, process_started_at=_started_at, now=now)
    # A report's text carries a day of private chat; keep it only while in flight.
    pulse_store.trim_finished(REPORT)
    _collect_ratings(pulse_store, home, now)
    _maybe_memory(pulse_store, home, now)
    # One new turn per tick: a probe first, the report on a later tick.
    if not _maybe_probe(pulse_store, home, now):
        _maybe_report(pulse_store, home, now)


def _send(pulse_store: store.PulseStore, *, kind: str, chat_id: str, text: Any) -> None:
    profile = str(getattr(_ctx, "profile_name", "") or "")
    delivery.submit(pulse_store, lambda chat_id, body: host.send(chat_id, body, profile=profile), kind=kind,
                    chat_id=chat_id, text=text)


def _maybe_probe(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    """Sends a probe if one was asked for or is due; True when one went."""
    asked = pulse_store.take_probe_request()
    if not asked and not _probe_due(pulse_store, now):
        return False
    if any(row["kind"] == PROBE for row in pulse_store.in_flight()):
        if asked:
            logger.info("conduit_push: Pulse: a delivery check is already in flight; not sending another")
        return False
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        if asked:
            logger.info("conduit_push: Pulse: the requested delivery check wasn't sent; run `pulse probe` again "
                        "once the home chat is there")
        return False
    _send(pulse_store, kind=PROBE, chat_id=chat.chat_id, text=lambda tag: PROBE_TEXT.format(tag=tag))
    return True


def _maybe_report(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    """Sends the shadow report if asked for or due; True when it went."""
    asked = pulse_store.take_request("report_requested")
    if not asked and not _report_due(pulse_store, home, now):
        return False
    if any(row["kind"] == REPORT for row in pulse_store.in_flight()):
        if asked:
            logger.info("conduit_push: Pulse: a shadow report is already in flight; not sending another")
        return False
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        if asked:
            logger.info("conduit_push: Pulse: the requested shadow report wasn't sent; run `pulse report` again "
                        "once the home chat is there")
        return False
    context, since = report.build(home, pulse_store, home_chat_id=chat.chat_id, now=now)
    _send(pulse_store, kind=REPORT, chat_id=chat.chat_id, text=lambda tag: report.report_text(tag, context, since))
    return True


def _maybe_memory(pulse_store: store.PulseStore, home: Path, now: float) -> None:
    """Starts a memory run on its own thread when one is due and none is
    running. Memory starts from the first time it runs: older chats wait
    for a backfill."""
    global _memory_thread
    if _memory_thread is not None and _memory_thread.is_alive():
        return
    asked = pulse_store.take_request("memory_requested")
    if not pulse_store.get("memory"):
        return  # an ask made while memory was off doesn't fire when it's turned on
    last = pulse_store.get("memory_run_at")
    if not asked and isinstance(last, (int, float)) and now - last < MEMORY_EVERY_S:
        return
    since = pulse_store.get("memory_since")
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        pulse_store.swap("memory_since", since, now)  # unless `pulse memory backfill start` just set it
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        if asked:
            logger.info("conduit_push: Pulse: memory didn't read chats; it needs the home chat first, so its own "
                        "reports aren't read as chats")
        return
    pulse_store.set("memory_run_at", now)
    _memory_thread = threading.Thread(target=_memory_run, args=(home, chat.chat_id, now, _stop), name="conduit-pulse-memory",
                                      daemon=True)
    _memory_thread.start()


def memory_sources(pulse_store: store.PulseStore, home: Path, home_chat_id: str,
                   profiles: Optional[str] = None) -> list[tuple[str, Path, str]]:
    """What memory reads: (profile, home, home chat to leave out), this
    profile first, then the others when the report reads every profile
    (``profiles``, else `report_profiles`, is "all")."""
    sources = [("", Path(home), home_chat_id)]
    if (pulse_store.get("report_profiles") if profiles is None else profiles) == "all":
        sources += [(name, path, report.their_home_chat(path)) for name, path in report.profile_homes(home)]
    return sources


def _memory_run(home: Path, home_chat_id: str, now: float, stopped: threading.Event) -> None:
    try:
        now = max(now, time.time())  # the thread may start a little after the tick
        pulse_store = open_store(home)
        zone = report_zone(pulse_store, home)
        day = datetime.fromtimestamp(now, zone).strftime("%Y-%m-%d")
        today = pulse_store.get("memory_today")
        used = int(today.get("tokens", 0) or 0) if isinstance(today, dict) and today.get("day") == day else 0
        try:
            cap = float(pulse_store.get("memory_daily_tokens"))
        except (TypeError, ValueError):
            cap = float(store.DEFAULTS["memory_daily_tokens"])
        if used >= cap:
            return  # `pulse status` says so
        profiles = pulse_store.get("report_profiles")
        sources = memory_sources(pulse_store, home, home_chat_id, profiles)
        done: dict[str, int] = {}
        try:
            memory.run(pulse_store, sources, _memory_llm, zone=zone, now=now,
                       reads="all" if profiles == "all" else "this",
                       still_leader=lambda: not stopped.is_set() and _leads(pulse_store), budget=cap - used, done=done)
        finally:
            # A run stopped by an unreadable state.db still spent its tokens.
            if done.get("tokens"):
                pulse_store.set("memory_today", {"day": day, "tokens": used + done["tokens"]})
            if done.get("chunks") or done.get("failed") or done.get("tokens"):
                usage = pulse_store.get("memory_usage")
                usage = usage if isinstance(usage, dict) else {}
                pulse_store.set("memory_usage", {key: int(usage.get(key, 0) or 0) + done.get(key, 0)
                                                for key in ("chunks", "tokens", "failed", "skipped")})
    except Exception:  # noqa: BLE001 — the next run tries again
        logger.warning("conduit_push: Pulse memory run failed", exc_info=True)


def _leads(pulse_store: store.PulseStore) -> bool:
    """Whether this process still runs the loop (a memory run stops when not)."""
    lease = pulse_store.get(store.LEADER)
    return (isinstance(lease, dict) and lease.get("owner") == _owner
            and isinstance(lease.get("until"), (int, float)) and lease["until"] > time.time() and pulse_store.get("enabled")
            and bool(pulse_store.get("memory")))


def _memory_llm(instructions: str, text: str) -> tuple[Any, int]:
    llm = getattr(_ctx, "llm", None)
    if llm is None:
        raise RuntimeError("this Hermes gives plugins no model access")
    result = llm.complete_structured(
        instructions=instructions, input=[{"type": "text", "text": text}], json_mode=True,
        task=memory.TASK if _memory_task else None, purpose="pulse memory", timeout=memory.CALL_TIMEOUT_S,
        max_tokens=memory.MAX_TOKENS)
    usage = getattr(result, "usage", None)
    return getattr(result, "parsed", None), int(getattr(usage, "total_tokens", 0) or 0)


# One Hermes home per process, like _ctx, so the cache needs no key.
_zone_cache: tuple[float, Any] = (0.0, None)
ZONE_TTL_S = 3600.0


def report_zone(pulse_store: store.PulseStore, home: Path, now: Optional[float] = None) -> Any:
    """The report's time zone: the one set with `pulse report`, else the
    phone's (read at most hourly), else the host's."""
    global _zone_cache
    if pulse_store.get("time_zone"):
        return report.zone_for(pulse_store.get("time_zone"))
    now = time.time() if now is None else now
    if _zone_cache[1] is None or now - _zone_cache[0] >= ZONE_TTL_S:
        _zone_cache = (now, report.zone_for(report.phone_zone(home)))
    return _zone_cache[1]


def _report_due(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    if not pulse_store.get("daily_report"):
        return False
    zone = report_zone(pulse_store, home, now)
    at, previous = report.last_slot(now, zone, pulse_store.get("report_time"))
    if now - at > REPORT_WINDOW_S:
        return False
    # Any report delivered or on its way in the second half of the gap before
    # this time, an early `pulse report` too, stands in for the scheduled one.
    if any(row["state"] not in (store.REFUSED, store.NOT_ADMITTED, store.FAILED)
           for row in pulse_store.attempts_since(REPORT, at - (at - previous) / 2)):
        return False
    tries = pulse_store.attempts_since(REPORT, at)
    return len(tries) < REPORT_TRIES and (not tries or now - tries[-1]["updated_at"] >= REPORT_RETRY_S)


def _collect_ratings(pulse_store: store.PulseStore, home: Path, now: float) -> None:
    """Reads the user's ratings of recent reports back from the home chat."""
    global _last_ratings
    if now - _last_ratings < RATINGS_EVERY_S:
        return
    _last_ratings = now
    for row in pulse_store.attempts_since(REPORT, now - RATINGS_DAYS * 86400):
        if row["state"] != store.DELIVERED:
            continue
        try:
            items, ratings = report.ratings_after(home, chat_id=row["session_key"], tag=row["tag"],
                                                  since=row["created_at"] - delivery.SINCE_SLACK_S)
        except hermes.HermesUnreadable:
            continue
        if items:
            pulse_store.set_ratings(row["attempt_id"], json.dumps(items), report.ratings_json(ratings))


def rating_counts(pulse_store: store.PulseStore) -> dict[str, tuple[int, int]]:
    """Kind -> (rated useful, rated not), over every rated report."""
    return report.rating_counts(pulse_store)


def _log_problem(problem: str) -> None:
    global _last_problem
    if problem and problem != _last_problem:
        logger.info("conduit_push: Pulse: %s", problem)
    _last_problem = problem


def _probe_due(pulse_store: store.PulseStore, now: float) -> bool:
    last = pulse_store.last_attempt_at(PROBE)
    try:
        interval = float(pulse_store.get("probe_interval_hours")) * 3600.0
    except (TypeError, ValueError):
        interval = store.DEFAULTS["probe_interval_hours"] * 3600.0
    return last is None or now - last >= max(interval, 600.0)


def home_chat(pulse_store: store.PulseStore, home: Path) -> tuple[Optional[hermes.HomeChat], str]:
    """The chat Pulse speaks in, and what's wrong when there's none."""
    try:
        chat_id = pulse_store.get("home_session_key")
        if chat_id:
            chat = hermes.chat_by_id(home, chat_id)
            return (chat, "") if chat else (None, f"no open chat has the id {chat_id}")
        title = pulse_store.get("home_title") or "Pulse"
        chats = hermes.find_home_chat(home, title)
    except hermes.HermesUnreadable as error:
        return None, f"can't read Hermes's state.db: {error}"
    if not chats:
        return None, f'no chat is titled "{title}"; create one and pin it'
    problem = ""
    if len(chats) > 1:
        problem = f'{len(chats)} chats are titled "{title}"; using the {"pinned" if chats[0].pinned else "latest"} one'
    elif not chats[0].pinned:
        problem = f'pin the "{title}" chat so pruning never removes it'
    return chats[0], problem

