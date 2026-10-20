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

Once a day it also sends the shadow report (pulse_report): what Hermes would
have told Eric today, for him to rate in the home chat.

While Pulse is on, every tool call in the profile also gets a row in the
outcome ledger (pulse_ledger), the record later slices check claims against.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

from . import pulse_delivery as delivery
from . import pulse_hermes as hermes
from . import pulse_host as host
from . import pulse_ledger as ledger
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


def register(ctx: Any) -> None:
    """Starts the Pulse loop for this load. It only acts in a process that
    hosts Conduit's chats, and only while Pulse is on."""
    global _ctx, _thread, _stop, _started_at
    _ctx = ctx
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
    row = open_store().attempt_by_tag(tag) if tag else None
    if row is not None and row["state"] in store.IN_FLIGHT:
        open_store().update_attempt(row["attempt_id"], detail=delivery.ENDED)
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
    a probe or the daily report if one is due."""
    if _ctx is None or not _hosts_chats():
        return
    home = get_hermes_home()
    if not (Path(home) / store.DB_NAME).exists():
        return  # never turned on: leave no file behind
    pulse_store = open_store(home)
    if not pulse_store.get("enabled"):
        return
    now = time.time() if now is None else now
    if not pulse_store.claim_leader(_owner, LEADER_TTL_S, now):
        return  # another process on this Hermes home runs the loop
    # The loop's heartbeat: `pulse status` warns when nothing runs the loop.
    pulse_store.set("last_tick_at", now)
    delivery.reconcile(pulse_store, home, process_started_at=_started_at, now=now)
    _collect_ratings(pulse_store, home, now)
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
    """Sends the daily shadow report if asked for or due; True when it went."""
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
    context = report.build(home, pulse_store, home_chat_id=chat.chat_id, now=now)
    _send(pulse_store, kind=REPORT, chat_id=chat.chat_id, text=lambda tag: report.report_text(tag, context))
    return True


def report_zone(pulse_store: store.PulseStore, home: Path) -> Any:
    return report.zone_for(pulse_store.get("time_zone") or report.phone_zone(home))


def _report_due(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    if not pulse_store.get("daily_report"):
        return False
    at = report.due_today(now, report_zone(pulse_store, home), pulse_store.get("report_time"))
    if at is None or now - at > REPORT_WINDOW_S:
        return False
    tries = pulse_store.attempts_since(REPORT, at)
    if any(row["state"] not in (store.REFUSED, store.NOT_ADMITTED, store.FAILED) for row in tries):
        return False  # delivered, or still on its way
    return len(tries) < REPORT_TRIES and (not tries or now - tries[-1]["updated_at"] >= REPORT_RETRY_S)


def _collect_ratings(pulse_store: store.PulseStore, home: Path, now: float) -> None:
    """Reads Eric's ratings of recent reports back from the home chat."""
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
            return
        if items:
            pulse_store.set_ratings(row["attempt_id"], json.dumps(items), report.ratings_json(ratings))


def rating_counts(pulse_store: store.PulseStore) -> dict[str, tuple[int, int]]:
    """Kind -> (rated useful, rated not), over every rated report."""
    counts: dict[str, list[int]] = {}
    for row in pulse_store.all_ratings():
        for rating in report.ratings_from_json(row["ratings_json"]):
            pair = counts.setdefault(rating.kind, [0, 0])
            pair[0 if rating.useful else 1] += 1
    return {kind: (yes, no) for kind, (yes, no) in sorted(counts.items())}


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

