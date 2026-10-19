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
"""

from __future__ import annotations

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
from . import pulse_store as store

logger = logging.getLogger(__name__)

PROBE = "probe"
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
            return PROBE_NOTE
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
    """One pass: reconcile what's in flight, then probe if one is due."""
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
    asked = pulse_store.take_probe_request()
    if not asked and not _probe_due(pulse_store, now):
        return
    if any(row["kind"] == PROBE for row in pulse_store.in_flight()):
        if asked:
            logger.info("conduit_push: Pulse: a delivery check is already in flight; not sending another")
        return
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        if asked:
            logger.info("conduit_push: Pulse: the requested delivery check wasn't sent; run `pulse probe` again "
                        "once the home chat is there")
        return
    profile = str(getattr(_ctx, "profile_name", "") or "")
    delivery.submit(pulse_store, lambda chat_id, text: host.send(chat_id, text, profile=profile), kind=PROBE,
                    chat_id=chat.chat_id, text=lambda tag: PROBE_TEXT.format(tag=tag))


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

