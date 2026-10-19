"""Pulse, slice 0: the home chat delivery probe (designs/proactivity.md 6.4-6.4.2, 7.6).

Off unless turned on (``hermes conduit-push pulse on``). When on, the
gateway process sends a short probe turn into the home chat every few hours
(or when ``pulse probe`` asks), and records whether Hermes delivered it.
That answers the hard gate: can the home chat take turns with nobody
watching, after a quiet day, a gateway restart and a reboot.

A Pulse turn is recognised by its tag in ``pre_llm_call``. On such a turn
every tool is blocked in ``pre_tool_call`` (the probe needs none; later
slices allow named reads only), and no push or Live Activity goes out for it.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

from . import pulse_delivery as delivery
from . import pulse_hermes as hermes
from . import pulse_store as store

logger = logging.getLogger(__name__)

PROBE = "probe"
TICK_S = 30.0
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


def register(ctx: Any) -> None:
    """Starts the Pulse loop for this load. It only acts in the process that
    hosts the gateway, and only while Pulse is on."""
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
    """A turn ended: the loop reconciles now instead of at its next tick."""
    if is_pulse_turn(session_id, turn_id):
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


def _gateway_ready() -> bool:
    manager = getattr(_ctx, "_manager", None)
    return bool(getattr(manager, "has_gateway_message_injector", False))


def tick(now: Optional[float] = None) -> None:
    """One pass: reconcile what's in flight, then probe if one is due."""
    if _ctx is None or not _gateway_ready():
        return
    home = get_hermes_home()
    if not (Path(home) / store.DB_NAME).exists():
        return  # never turned on: leave no file behind
    pulse_store = open_store(home)
    if not pulse_store.get("enabled"):
        return
    now = time.time() if now is None else now
    delivery.reconcile(pulse_store, home, plugin_id=_plugin_id(), process_started_at=_started_at, now=now)
    asked = pulse_store.take_probe_request()
    if not asked and not _probe_due(pulse_store, now):
        return
    if any(row["kind"] == PROBE for row in pulse_store.in_flight()):
        if asked:
            logger.info("conduit_push: Pulse: a delivery check is already in flight; not sending another")
        return
    chat, problem = home_chat(pulse_store, home)
    if problem:
        logger.info("conduit_push: Pulse: %s", problem)
    if chat is None:
        return
    delivery.submit(pulse_store, _ctx.inject_message, kind=PROBE, session_key=chat.session_key,
                    text=lambda tag: PROBE_TEXT.format(tag=tag))


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
        key = pulse_store.get("home_session_key")
        if key:
            chat = hermes.chat_for_key(home, key)
            return (chat, "") if chat else (None, f"no chat has the session key {key}")
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


def _plugin_id() -> str:
    return str(getattr(_ctx, "plugin_id", "") or "conduit_push")
