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

A few times a day it also sends the report (pulse_report): what Hermes would
bring up since the last one, each item its own card on the Pulse screen. A
model call lists the report's candidates first, on a thread of its own, and
code picks what goes in (pulse_candidates); without model access, or when the
list fails, the report turn picks its items itself, as it always did. Those
model calls, memory's and follow-ups' too, run on the model the Pulse chat
runs on unless the user gave Hermes's "Pulse memory" slot one of its own
(``lent_route``).

While Pulse is on, every tool call in the profile also gets a row in the
outcome ledger (pulse_ledger), the record later slices check claims against,
and quiet chats are read into memory items (pulse_memory), on a thread of
their own so a slow model call never holds up delivery.

Follow-ups (pulse_followup) run on that thread after memory: each promise
Hermes made is checked on, and news worth telling goes to the home chat as a
follow-up turn, the one Pulse turn whose reply pushes like any reply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home

from . import pulse_delivery as delivery
from . import pulse_brief as brief
from . import pulse_candidates as candidates
from . import pulse_followup as followup
from . import pulse_hermes as hermes
from . import pulse_host as host
from . import pulse_learning as learning
from . import pulse_ledger as ledger
from . import pulse_memory as memory
from . import pulse_prefs as prefs
from . import pulse_quiet as quiet
from . import pulse_report as report
from . import pulse_store as store

logger = logging.getLogger(__name__)

PROBE = "probe"
REPORT = "report"
FOLLOWUP = followup.FOLLOWUP
BRIEF = brief.BRIEF
# Settings > Pulse's model for Pulse's turns ({"provider", "model"}); unset,
# they run on the Pulse chat's own model.
MODEL = "turn_model"
CARDS_KEEP = 500
# Held cards a brief never took (no home chat for weeks) go too, past CARDS_KEEP.
HELD_DAYS = 30
# A report that didn't get through is tried again after this, up to
# REPORT_TRIES a day, and only this long after its time (a dashboard down at
# 08:00 still sends it at 11:00, but not at midnight).
REPORT_RETRY_S = 1800.0
REPORT_TRIES = 3
REPORT_WINDOW_S = 8 * 3600.0
# The report's candidate list (pulse_candidates): {"id", "state", "started_at",
# ...}. Its model call runs on a thread of its own ("working"); the tick then
# sends its picks ("ready"), or the report as it used to be when the list
# failed ("failed") or was left working by a process that stopped.
REPORT_RUN = "report_run"
RUN_WORKING, RUN_READY, RUN_FAILED, RUN_SENT, RUN_DROPPED = "working", "ready", "failed", "sent", "dropped"
RUN_STALE_S = 2 * memory.CALL_TIMEOUT_S + 60
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
_report_thread: Optional[threading.Thread] = None
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


def wake() -> None:
    """Runs the next tick now instead of after TICK_S (new inbox events)."""
    _wake.set()


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
            return {REPORT: report.REPORT_NOTE, FOLLOWUP: followup.NOTE, BRIEF: brief.NOTE}.get(row["kind"],
                                                                                                 PROBE_NOTE)
    return None


def is_pulse_turn(session_id: str, turn_id: Any) -> bool:
    if not isinstance(turn_id, str) or not turn_id:
        return False
    with _turns_lock:
        return (session_id, turn_id) in _turns


def _turn_attempt(session_id: str, turn_id: Any) -> Optional[dict]:
    """The attempt a Pulse turn is (None when it isn't one, or its store
    can't be read)."""
    if not is_pulse_turn(session_id, turn_id):
        return None
    with _turns_lock:
        tag = _turns.get((session_id, turn_id))
    try:
        return open_store().attempt_by_tag(tag) if tag else None
    except Exception:  # noqa: BLE001 — an unreadable store keeps the turn quiet, never breaks the hook
        logger.debug("conduit_push: Pulse couldn't read the turn's attempt", exc_info=True)
        return None


def _run_picks(pulse_store: store.PulseStore, attempt: dict) -> Optional[list[dict]]:
    """The picks of the candidate list a report attempt says, in order; None
    for a report the turn picked itself (no list behind it)."""
    rows = pulse_store.run_candidates(str(attempt["item_id"])) if attempt["item_id"] else []
    return candidates.picks(rows) if rows else None


def _pushed_lines(attempt: dict, response: Any) -> list[tuple[str, str, Optional[int]]]:
    """A report turn's items that go to the phone: its candidate list's picks
    that came up as a push (pulse_candidates.levels; one journaled before
    levels were counts as one, as every item rode its report's push then),
    else its reply's lines, all of them, as before."""
    try:
        picked = _run_picks(open_store(), attempt)
    except Exception:  # noqa: BLE001 — the reply still says them
        logger.debug("conduit_push: Pulse couldn't read the report's picks", exc_info=True)
        picked = None
    if picked is not None:
        return candidates.lines([pick for pick, level in zip(picked, candidates.levels(picked))
                                 if level in ("push", "")])
    return learning.report_lines(response if isinstance(response, str) else "")


def pushes(session_id: str, turn_id: Any, response: Any = None) -> bool:
    """Whether a Pulse turn's reply goes to the phone like any reply: a
    follow-up or the morning brief does (it's news for the user), and so
    does a report with something that came up as a push (``_pushed_lines``:
    a report whose picks all came up quieter lands as cards only); a probe
    doesn't."""
    attempt = _turn_attempt(session_id, turn_id)
    kind = attempt["kind"] if attempt else ""
    if kind == REPORT:
        return bool(_pushed_lines(attempt, response))
    return kind in (FOLLOWUP, BRIEF)


# What Conduit titles a Pulse push (its `title_label`), and the English
# `title` an app without that label shows.
PUSH_TITLES = {FOLLOWUP: ("pulse.followup", "Pulse · Follow-up"), BRIEF: ("pulse.brief", "Your morning brief")}


def push_copy(session_id: str, turn_id: Any, response: Any) -> dict[str, Any]:
    """What a Pulse turn's push says, as push_event takes it: titled by
    what it is, not by Pulse's hidden chat. A report's body is its items
    that came up as a push (``_pushed_lines``), one after another, without
    the numbers, kinds and remembered-item refs that are for the cards, and
    ``count`` says how many there are (two or more)."""
    attempt = _turn_attempt(session_id, turn_id)
    kind = attempt["kind"] if attempt else ""
    response = response if isinstance(response, str) else ""
    lines = _pushed_lines(attempt, response) if kind == REPORT else []
    if lines:
        copy = {"title_label": "pulse.noticed", "title": "Pulse noticed something",
                "body": " · ".join(" ".join(text.split()) for _, text, _ in lines)}
        if len(lines) > 1:  # one thing is "something", with no count
            copy.update(title=f"Pulse noticed {len(lines)} things", count=len(lines))
        return copy
    label, title = PUSH_TITLES.get(kind, ("response.ready", "Pulse"))
    return {"title_label": label, "title": title, "body": brief.summary_line(response) if kind == BRIEF else response}


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
    a probe or the report if one is due."""
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
    changed = delivery.reconcile(pulse_store, home, process_started_at=_started_at, now=now)
    _turn_cards(pulse_store, home, changed, now)
    # A report's text carries a day of private chat; keep it only while in flight.
    pulse_store.trim_finished(REPORT)
    pulse_store.trim_finished(FOLLOWUP)  # likewise the chat a follow-up quotes
    pulse_store.trim_finished(BRIEF)
    _collect_ratings(pulse_store, home, now)
    # Even with follow-ups turned off: one already sent still lands.
    followup.settle(pulse_store, now, home)
    brief.settle(pulse_store, now)
    # Not now until a time that has come: the card comes back as new (one
    # about something still open after its chat is read, in _memory_run).
    followup.return_rested(pulse_store, now)
    learning.drop_empty_reports(pulse_store)
    pulse_store.prune_cards(CARDS_KEEP, held_before=now - HELD_DAYS * 86400.0)
    pulse_store.prune_candidates(now - candidates.JOURNAL_DAYS * 86400.0)
    pulse_store.prune_signals(now - prefs.KEEP_S)
    _maybe_memory(pulse_store, home, now)
    # One new turn per tick: a probe first, then a follow-up, the brief, then the report.
    if not _maybe_probe(pulse_store, home, now) and not _maybe_followup(pulse_store, home, now) \
            and not _maybe_brief(pulse_store, home, now):
        _maybe_report(pulse_store, home, now)


# Why a report or brief card is on the Pulse screen. A report gives none: a
# card's Why? is a reason of its own (its items' cards carry theirs, when a
# candidate list gave them), never a stock line.
TURN_WHY = {
    REPORT: "",
    BRIEF: "Your morning brief: what's still outstanding, and the news you asked Pulse to hold for the morning.",
}


# How long a delivered report or brief whose card didn't land is tried again.
TURN_CARD_RETRY_S = 600
# The attempts whose cards landed, newest last.
TURN_CARDS_DONE = "turn_cards_done"
TURN_CARDS_DONE_KEEP = 50


def _turn_cards(pulse_store: store.PulseStore, home: Path, changed: list[dict], now: float) -> None:
    """A report or brief that reached the Pulse chat goes on the Pulse screen
    too: a report as one card per item, a brief as Hermes wrote it, linked
    to its message in the chat. Those
    delivered in the last few minutes are tried again, so a card a busy
    state.db kept out still lands (a card is never added twice)."""
    rows = {row["attempt_id"]: row for row in changed}
    for kind in TURN_WHY:
        for row in pulse_store.attempts_since(kind, now - TURN_CARD_RETRY_S - 86400):
            if row["updated_at"] >= now - TURN_CARD_RETRY_S:
                rows.setdefault(row["attempt_id"], row)
    # An attempt whose card landed isn't looked up again: a compaction since
    # would give its reply new ids, and a second card.
    done = pulse_store.get(TURN_CARDS_DONE)
    done = [str(attempt) for attempt in done] if isinstance(done, list) else []
    rows = {attempt: row for attempt, row in rows.items() if row["state"] == store.DELIVERED
            and row["kind"] in TURN_WHY and attempt not in done}
    if not rows:
        return
    chat = home_chat(pulse_store, home)[0]
    title = (chat.title if chat else "") or pulse_store.get("home_title") or "Pulse"
    for attempt, row in rows.items():
        try:
            turn = hermes.find_turn(home, chat_id=row["session_key"], tag=row["tag"],
                                    since=row["created_at"] - delivery.SINCE_SLACK_S)
            picked = _run_picks(pulse_store, row) if turn is not None and row["kind"] == REPORT else None
            if picked is not None:
                # Made from the candidate list: its picks are its cards, whatever the reply says.
                if picked:
                    learning.add_noticed_cards(
                        pulse_store, turn=turn, lines=candidates.lines(picked), chat_id=row["session_key"],
                        chat_title=title, shown={pick["item_id"] for pick in picked if pick["item_id"] is not None},
                        kinds=report.KINDS, item_chat_title=lambda item: followup.chat_title(home, item), now=now,
                        attempt=str(attempt), marks=(TURN_CARDS_DONE, store.SPLIT_REPORTS),
                        keep=TURN_CARDS_DONE_KEEP, links=[pick["id"] for pick in picked],
                        found_in=candidates.found_in(picked), whys=candidates.whys(picked),
                        levels=candidates.levels(picked))
                else:
                    _mark_turn_card(pulse_store, attempt)
                continue
            # A brief comes with a push; a report doesn't.
            if turn is not None and learning.empty_turn(row["kind"], turn.reply):
                # Nothing brought up: no card, and no looking it up again.
                _mark_turn_card(pulse_store, attempt)
                continue
            lines = learning.report_lines(turn.reply) if turn is not None and row["kind"] == REPORT else []
            if lines:
                # Each item its own card; a reply in some other shape stays one
                # card. The cards and the marks that they landed are one write,
                # so a retry never adds them twice.
                learning.add_noticed_cards(
                    pulse_store, turn=turn, lines=lines, chat_id=row["session_key"], chat_title=title,
                    shown=report.shown_items(turn.prompt), kinds=report.KINDS,
                    item_chat_title=lambda item: followup.chat_title(home, item), now=now,
                    attempt=str(attempt), marks=(TURN_CARDS_DONE, store.SPLIT_REPORTS), keep=TURN_CARDS_DONE_KEEP)
                continue
            card = turn is not None and learning.add_turn_card(
                pulse_store, kind=row["kind"], chat_id=row["session_key"], chat_title=title, turn=turn,
                level_name="push" if row["kind"] == BRIEF else "screen", why=TURN_WHY[row["kind"]], now=now,
                brief=str(row["item_id"] or "") if row["kind"] == BRIEF else "")
            if card:
                _mark_turn_card(pulse_store, attempt)
        except (hermes.HermesUnreadable, sqlite3.Error):
            logger.debug("conduit_push: Pulse: no card for %s yet", row["tag"], exc_info=True)


def _mark_turn_card(pulse_store: store.PulseStore, attempt: Any) -> None:
    """Notes that ``attempt``'s card is settled, appended in one write so a
    second process's marks are never written over."""
    pulse_store.add_cards([], attempt=str(attempt), marks=(TURN_CARDS_DONE,), keep=TURN_CARDS_DONE_KEEP)


def turn_model(pulse_store: Any) -> Optional[dict[str, str]]:
    """The model Settings > Pulse picked, or None for the chat's own. A stored
    value that isn't one word per field is dropped: it goes into a flag string."""
    model = pulse_store.get(MODEL)
    if not isinstance(model, dict):
        return None
    name, provider = model.get("model"), model.get("provider") or ""
    if not isinstance(name, str) or not isinstance(provider, str) or not host.plain_flag_value(name) \
            or (provider and not host.plain_flag_value(provider)):
        return None
    return {"provider": provider, "model": name}


def chat_route(home: Path, chat_id: str) -> Optional[dict[str, str]]:
    """The model and provider the Pulse chat's turns run on, as Hermes reads
    them back off the chat when it reopens it (a provider no longer set up is
    healed or dropped there, the same here): {"provider", "model"}, with
    "base_url" and "api_mode" when Hermes has them. None when the chat runs on
    Hermes's main model, or outside the process that hosts the chat, which
    can't tell."""
    server = sys.modules.get("tui_gateway.server")
    read_back = getattr(server, "_stored_session_runtime_overrides", None)
    if not chat_id or not callable(read_back):
        return None
    try:
        row = hermes.chat_model_row(home, chat_id)
        with _own_profile_scope():
            overrides = read_back(row) if row else {}
    except Exception:  # noqa: BLE001 — Pulse's model calls stay on Hermes's main model
        logger.warning("conduit_push: Pulse couldn't read its chat's model; its memory, report lists and "
                       "follow-up checks stay on Hermes's main model", exc_info=True)
        return None
    picked = overrides.get("model_override") if isinstance(overrides, dict) else None
    if not isinstance(picked, dict):
        return None
    model, provider = str(picked.get("model") or "").strip(), str(picked.get("provider") or "").strip()
    if not model or not provider:
        return None  # a dropped provider: the chat is on Hermes's main one, as "auto" is
    route = {"provider": provider, "model": model, "base_url": str(picked.get("base_url") or "").strip(),
             "api_mode": str(picked.get("api_mode") or "").strip()}
    return {key: value for key, value in route.items() if value}


def lent_route(home: Path, chat_id: str) -> Optional[dict[str, str]]:
    """The route Pulse's calls outside its chat borrow: the Pulse chat's
    (``chat_route``), unless Hermes's "Pulse memory" slot names a model of its
    own, which then runs them without one."""
    return None if memory_slot() else chat_route(home, chat_id)


def chat_model(home: Path, chat_id: str) -> Optional[dict[str, str]]:
    """The model the Pulse chat is set to, as stored: {"provider", "model"}.
    None when it runs on Hermes's main model, or state.db can't say."""
    try:
        row = hermes.chat_model_row(home, chat_id)
    except hermes.HermesUnreadable:
        return None
    if not row:
        return None
    try:
        config = json.loads(row.get("model_config") or "{}")
    except (TypeError, ValueError):
        config = {}
    config = config if isinstance(config, dict) else {}
    runtime = config.get("gateway_runtime") if isinstance(config.get("gateway_runtime"), dict) else {}
    billed = str(row.get("billing_provider") or "").strip()
    model = str(row.get("model") or config.get("model") or "").strip()
    # Where Hermes reads the provider from, in its order; "auto" and "custom" there name none.
    provider = str(runtime.get("provider") or config.get("provider")
                   or (billed if billed.lower() not in ("auto", "custom") else "")).strip()
    return {"provider": provider, "model": model} if model else None


def memory_slot() -> Optional[dict[str, str]]:
    """The model the user gave Hermes's "Pulse memory" slot: {"provider",
    "model"}. None while it's on "auto", which is the Pulse chat's model, or
    the config can't be read."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception:  # noqa: BLE001 — can't tell: "auto", as it starts out
        return None
    auxiliary = config.get("auxiliary") if isinstance(config, dict) else None
    slot = auxiliary.get(memory.TASK) if isinstance(auxiliary, dict) else None
    if not isinstance(slot, dict):
        return None
    provider, model = slot.get("provider") or "", slot.get("model") or ""
    if not isinstance(provider, str) or not isinstance(model, str):
        return None  # hand-edited into something Hermes can't route either
    provider, model = provider.strip(), model.strip()
    if not provider or provider.lower() == "auto":
        return None
    return {"provider": provider, "model": model}


def _sender(pulse_store: store.PulseStore) -> Callable[[str, str], host.Sent]:
    """Sends a Pulse turn on this profile, on the model Settings > Pulse picked."""
    profile = str(getattr(_ctx, "profile_name", "") or "")
    model = turn_model(pulse_store)
    return lambda chat_id, body: host.send(chat_id, body, profile=profile, model=model)


def _send(pulse_store: store.PulseStore, *, kind: str, chat_id: str, text: Any) -> dict:
    return delivery.submit(pulse_store, _sender(pulse_store), kind=kind,
                           chat_id=chat_id, text=text)


def _quiet(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    """Whether Pulse keeps to itself now (pulse_quiet): nothing goes on its
    own, only what the user asked for."""
    return bool(quiet.why(pulse_store, report_zone(pulse_store, home, now), now))


def _maybe_probe(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    """Sends a probe if one was asked for, or is due and Pulse isn't quiet; True when one went."""
    asked = pulse_store.take_probe_request()
    if not asked and (_quiet(pulse_store, home, now) or not _probe_due(pulse_store, now)):
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
    """Sends the report if asked for or due; True when it went. With model
    access its candidate list comes first, on a thread of its own, and a
    later tick sends what the list picked (``_send_report``)."""
    asked = pulse_store.take_request("report_requested")
    stored = pulse_store.get(REPORT_RUN)
    run = stored if isinstance(stored, dict) and isinstance(stored.get("id"), str) and stored["id"] else {}
    started = run.get("started_at")
    started = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else 0.0
    if run.get("state") == RUN_WORKING and now - started < RUN_STALE_S:
        if asked:
            logger.info("conduit_push: Pulse: a report is already on its way; not sending another")
        return False
    pending = run.get("state") in (RUN_WORKING, RUN_READY, RUN_FAILED)
    if pending and now - started > REPORT_WINDOW_S:
        # Too late to send, like a report long past its time.
        dropped = {**run, "state": RUN_DROPPED}
        if pulse_store.swap(REPORT_RUN, stored, dropped):
            stored = dropped  # so a run asked for now starts from it
        pending = False
    if pending and not asked and not run.get("asked") and _quiet(pulse_store, home, now):
        # Pulse went quiet while a scheduled report was on its way: it doesn't
        # go. One asked for by hand still does.
        dropped = {**run, "state": RUN_DROPPED}
        if pulse_store.swap(REPORT_RUN, stored, dropped):
            stored = dropped
        pending = False
    if not pending and not asked and (_quiet(pulse_store, home, now) or not _report_due(pulse_store, home, now)):
        return False  # a quiet day: the next scheduled one covers it
    if any(row["kind"] == REPORT for row in pulse_store.in_flight()):
        if asked:
            logger.info("conduit_push: Pulse: a report is already in flight; not sending another")
        return False
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        if asked:
            logger.info("conduit_push: Pulse: the requested report wasn't sent; run `pulse report` again "
                        "once the home chat is there")
        return False
    if pending:
        return _send_report(pulse_store, home, run, chat.chat_id, now)
    if getattr(_ctx, "llm", None) is not None:
        return _start_report_run(pulse_store, home, chat.chat_id, now, stored, asked=asked)
    _send_turn_report(pulse_store, home, chat.chat_id, now)
    return True


def _start_report_run(pulse_store: store.PulseStore, home: Path, chat_id: str, now: float, stored: Any, *,
                      asked: bool = False) -> bool:
    """Starts the report's candidate list on a thread of its own, so a slow
    model call never holds up the loop; True when it started. A run the user
    asked for says so: it goes even if Pulse goes quiet meanwhile."""
    global _report_thread
    run = {"id": secrets.token_hex(8), "state": RUN_WORKING, "started_at": now, "owner": _owner}
    if asked:
        run["asked"] = True
    if not pulse_store.swap(REPORT_RUN, stored, run):
        return False  # another process started one
    _report_thread = threading.Thread(target=_report_run, args=(home, chat_id, run), name="conduit-pulse-report",
                                      daemon=True)
    _report_thread.start()
    return True


def _report_run(home: Path, chat_id: str, run: dict) -> None:
    """Makes the report's candidate list and journals it (pulse_candidates);
    the tick then sends the report. A list that fails leaves the report to
    go out as it used to."""
    pulse_store = None
    try:
        pulse_store = open_store(home)
        now = float(run["started_at"])
        zone = report_zone(pulse_store, home, now)
        # Ratings reach the list through learning (pulse_prefs, in decide), not as text.
        gathered = report.gather(home, pulse_store, home_chat_id=chat_id, now=now, room=candidates.CONTEXT_BUDGET,
                                 extra=candidates.recent_section(pulse_store, zone, now), keyed=True, ratings=False)
        route = lent_route(home, chat_id)
        lessons = candidates.lessons_block(seen=learning.report_lessons(pulse_store, learning.SEEN),
                                           sooner=learning.report_lessons(pulse_store, learning.SOONER_LESSON))
        rows, tokens = candidates.ask(lambda instructions, text: _memory_llm(
            instructions, text, purpose="pulse report", max_tokens=candidates.REPLY_TOKENS, route=route), gathered,
            nudge=learning.nudge(pulse_store), note=report.note(pulse_store), lessons=lessons)
        candidates.decide(pulse_store, rows, now=now)
        ready = {**run, "state": RUN_READY, "since": gathered.since, "tokens": tokens}
        if not pulse_store.finish_run(REPORT_RUN, run, rows, ready, now=now):
            logger.info("conduit_push: Pulse: the report's candidate list took too long; the report went without it")
    except BaseException as error:  # noqa: BLE001 — the report goes out as it used to
        logger.warning("conduit_push: Pulse couldn't list the report's candidates; it goes out as before",
                       exc_info=True)
        try:
            if pulse_store is not None:
                pulse_store.swap(REPORT_RUN, run, {**run, "state": RUN_FAILED, "error": type(error).__name__})
        except Exception:  # noqa: BLE001 — left working, it goes out as before once stale
            logger.debug("conduit_push: Pulse couldn't mark the report's candidate list failed", exc_info=True)
        if not isinstance(error, Exception):
            raise  # an interpreter exit or the like: marked, and on its way
    finally:
        _wake.set()  # the next tick sends it now, not a whole TICK_S later


def _send_report(pulse_store: store.PulseStore, home: Path, run: dict, chat_id: str, now: float) -> bool:
    """Sends the report a candidate list was started for: its picks once the
    list is ready, else the report as it used to be (the list failed, or a
    process that stopped left it working). Marked sent first, so it goes
    once."""
    sent = {**run, "state": RUN_SENT}
    if run["state"] != RUN_READY:
        sent["error"] = run.get("error") or "left working"  # what `pulse candidates` says
    if not pulse_store.swap(REPORT_RUN, run, sent):
        return False  # another process got to it
    try:
        if run["state"] != RUN_READY:
            attempt = _send_turn_report(pulse_store, home, chat_id, now)
        else:
            picked = candidates.picks(pulse_store.run_candidates(run["id"]))
            since = str(run.get("since") or "")
            # The attempt's item id is the run's, so its turn finds its picks (_run_picks).
            attempt = delivery.submit(pulse_store, _sender(pulse_store), kind=REPORT, chat_id=chat_id,
                                      text=lambda tag: candidates.report_text(tag, picked, since),
                                      item_id=run["id"])
    except BaseException:
        # Nothing recorded (the store refused the attempt): back as it was, so
        # a later tick sends it. Should this write fail too, the run stays sent
        # and the next scheduled report makes a new list.
        pulse_store.swap(REPORT_RUN, sent, run)
        raise
    if attempt.get("state") in (store.REFUSED, store.NOT_ADMITTED):
        # Hermes didn't take it: `pulse candidates` says so, and the report is
        # tried again as any report is (_report_due), from a new list.
        pulse_store.swap(REPORT_RUN, sent, {**sent, "refused": attempt.get("detail") or attempt["state"]})
    return True


def _send_turn_report(pulse_store: store.PulseStore, home: Path, chat_id: str, now: float) -> dict:
    """The report as it was before candidate lists: one turn that reads the
    day and picks its items itself; its attempt."""
    # What Pulse brought up lately, as the candidate list reads it: the turn
    # sees the Pulse chat, but its earlier turns are no evidence of what's true now.
    recent = candidates.recent_section(pulse_store, report_zone(pulse_store, home, now), now)
    context, since = report.build(home, pulse_store, home_chat_id=chat_id, now=now, extra=recent)
    return _send(pulse_store, kind=REPORT, chat_id=chat_id, text=lambda tag: report.report_text(tag, context, since,
                                                                                    learning.nudge(pulse_store),
                                                                                    report.note(pulse_store)))


def _maybe_followup(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    """Sends the next follow-up whose check said to tell the user; True when
    one went."""
    if not pulse_store.get("followups"):
        return False
    picked = followup.next_send(pulse_store, now, home, quiet=_quiet(pulse_store, home, now))
    if picked is None:
        return False
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        return False
    item, row, attempt = picked
    zone = report_zone(pulse_store, home, now)
    text = followup.text_for(item, row, title=followup.chat_title(home, item), zone=zone)
    delivery.submit(pulse_store, _sender(pulse_store), kind=FOLLOWUP,
                    chat_id=chat.chat_id, text=text, item_id=followup.attempt_item_id(item["id"]), attempt=attempt)
    return True


def _maybe_brief(pulse_store: store.PulseStore, home: Path, now: float) -> bool:
    """Sends the morning brief when it's time (not on a quiet day: the first
    one after carries what's held), with what's still open and what's held
    for it; True when it went."""
    zone = report_zone(pulse_store, home, now)
    time_value = pulse_store.get("brief_time")
    slot, _ = report.last_slot(now, zone, time_value or brief.DEFAULT_TIME)
    if quiet.why(pulse_store, zone, now) or quiet.why(pulse_store, zone, slot):
        return False  # quiet now, or at its time: not sent late once it isn't (at midnight, say)
    planned = brief.next_attempt(pulse_store, now, zone, time_value)
    if planned is None:
        return False
    chat, problem = home_chat(pulse_store, home)
    _log_problem(problem)
    if chat is None:
        return False
    item_id, attempt = planned
    cards = pulse_store.held_cards(brief.MAX_CARDS)
    context = brief.context(home, pulse_store, cards, home_chat_id=chat.chat_id, now=now, zone=zone)
    pulse_store.set(brief.PENDING, {"item_id": item_id, "ids": [card["id"] for card in cards]})
    delivery.submit(pulse_store, _sender(pulse_store), kind=BRIEF,
                    chat_id=chat.chat_id, text=brief.text_for(context, report.note(pulse_store)), item_id=item_id,
                    attempt=attempt)
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
        used, cap = _spent_today(pulse_store, "memory", day)
        follow_used, follow_cap = _spent_today(pulse_store, "followup", day)
        follows = bool(pulse_store.get("followups")) and follow_used < follow_cap
        # Do it is the user's own ask: its chat is read whatever follow-ups'
        # switch or budget (at most a few reads per tap, MAX_CHECKS a run).
        acts = bool(pulse_store.due_acts(now))
        # Cards held for the morning brief are read again before it, on the
        # follow-up budget, whatever follow-ups' switch: they hold report
        # cards too, and a stale card shouldn't reach the brief either way.
        holds = follow_used < follow_cap and bool(pulse_store.held_cards(1))
        # Likewise a card swiped away until now, about something still open:
        # with the budget spent it comes back unread rather than late.
        rests = any(card["item_id"] is not None for card in pulse_store.rested_cards(now))
        if used >= cap and not follows and not acts and not holds and not rests:
            return  # `pulse status` says so
        profiles = pulse_store.get("report_profiles")
        sources = memory_sources(pulse_store, home, home_chat_id, profiles)
        route = lent_route(home, home_chat_id)

        def ask(instructions: str, text: str, purpose: str = "pulse memory") -> tuple[Any, int]:
            return _memory_llm(instructions, text, purpose=purpose, route=route)

        done: dict[str, int] = {}
        checked: dict[str, int] = {}
        acted: dict[str, int] = {}
        try:
            try:
                if used < cap:
                    memory.run(pulse_store, sources, ask, zone=zone, now=now,
                               reads="all" if profiles == "all" else "this",
                               still_leader=lambda: not stopped.is_set() and _leads(pulse_store), budget=cap - used,
                               done=done)
            except Exception:  # noqa: BLE001 — follow-ups on what memory already has still run
                logger.warning("conduit_push: Pulse memory run failed", exc_info=True)
            if acts:
                try:
                    followup.check_acts(pulse_store, home, lambda instructions, text: ask(
                                            instructions, text, purpose="pulse follow-up"), zone=zone,
                                        now=max(now, time.time()),
                                        still_leader=lambda: not stopped.is_set() and _leads(pulse_store),
                                        budget=float("inf"), done=acted)
                finally:
                    _count_checks(pulse_store, acted)  # in the totals, not the follow-up day's budget
            if holds or rests:
                try:
                    if holds:
                        followup.check_held(pulse_store, home, lambda instructions, text: ask(
                                                instructions, text, purpose="pulse follow-up"), zone=zone,
                                            now=max(now, time.time()),
                                            still_leader=lambda: not stopped.is_set() and _leads(pulse_store),
                                            budget=follow_cap - follow_used, done=checked)
                    if rests:
                        followup.check_rested(pulse_store, home, lambda instructions, text: ask(
                                                  instructions, text, purpose="pulse follow-up"), zone=zone,
                                              now=max(now, time.time()),
                                              still_leader=lambda: not stopped.is_set() and _leads(pulse_store),
                                              budget=follow_cap - follow_used, done=checked)
                except Exception:  # noqa: BLE001 — the brief tells them as they were; a rested card waits
                    logger.warning("conduit_push: Pulse held or rested card checks failed", exc_info=True)
                finally:
                    if not follows:
                        _count_checks(pulse_store, checked)  # else the follow-up run counts them with its own
            if follows:
                # After memory, so a promise it just read is scheduled now.
                now = max(now, time.time())
                # Memory expires week-old items as it reads; on a day it
                # doesn't, they still mustn't be reminded of.
                pulse_store.expire_items(due_before=now - memory.EXPIRE_S, now=now)
                followup.schedule(pulse_store, now)
                followup.check_all_now(pulse_store, now)
                followup.check_watches_now(pulse_store, now)
                try:
                    followup.run(pulse_store, home, lambda instructions, text: ask(
                                     instructions, text, purpose="pulse follow-up"), zone=zone, now=now,
                                 still_leader=lambda: (not stopped.is_set() and _leads(pulse_store)
                                                       and bool(pulse_store.get("followups"))),
                                 budget=follow_cap - follow_used, done=checked)
                finally:
                    _count_checks(pulse_store, checked)
        finally:
            # A run stopped by an unreadable state.db still spent its tokens.
            if done.get("tokens"):
                pulse_store.set("memory_today", {"day": day, "tokens": used + done["tokens"]})
            if checked.get("tokens"):
                pulse_store.set("followup_today", {"day": day, "tokens": follow_used + checked["tokens"]})
            if done.get("chunks") or done.get("failed") or done.get("tokens"):
                usage = pulse_store.get("memory_usage")
                usage = usage if isinstance(usage, dict) else {}
                pulse_store.set("memory_usage", {key: int(usage.get(key, 0) or 0) + done.get(key, 0)
                                                for key in ("chunks", "tokens", "failed", "skipped")})
    except Exception:  # noqa: BLE001 — the next run tries again
        logger.warning("conduit_push: Pulse memory run failed", exc_info=True)


def _count_checks(pulse_store: store.PulseStore, checked: dict[str, int]) -> None:
    if checked.get("checks") or checked.get("failed"):
        usage = pulse_store.get("followup_usage")
        usage = usage if isinstance(usage, dict) else {}
        pulse_store.set("followup_usage", {key: int(usage.get(key, 0) or 0) + checked.get(key, 0)
                                          for key in ("checks", "tokens", "failed")})


def _spent_today(pulse_store: store.PulseStore, name: str, day: str) -> tuple[int, int]:
    """Tokens ``name`` ("memory" or "followup") used on ``day``, and its daily cap."""
    today = pulse_store.get(f"{name}_today")
    try:
        used = int(today.get("tokens", 0) or 0) if isinstance(today, dict) and today.get("day") == day else 0
    except (TypeError, ValueError):
        used = 0
    cap = pulse_store.get(f"{name}_daily_tokens")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap < 1:
        cap = (learning.nudging(pulse_store)["followup_tokens"] if name == "followup"
               else store.DEFAULTS[f"{name}_daily_tokens"])
    return used, int(cap)


def _leads(pulse_store: store.PulseStore) -> bool:
    """Whether this process still runs the loop (a memory run stops when not)."""
    lease = pulse_store.get(store.LEADER)
    return (isinstance(lease, dict) and lease.get("owner") == _owner
            and isinstance(lease.get("until"), (int, float)) and lease["until"] > time.time() and pulse_store.get("enabled")
            and bool(pulse_store.get("memory")))


def _memory_llm(instructions: str, text: str, purpose: str = "pulse memory",
                max_tokens: int = memory.MAX_TOKENS, route: Optional[dict[str, str]] = None) -> tuple[Any, int]:
    """One Pulse model call, on Hermes's "Pulse memory" slot. ``route`` is
    the Pulse chat's model (``lent_route``), which that slot left on "auto"
    then means, as Settings > Pulse's "Same as the Pulse chat" says."""
    llm = getattr(_ctx, "llm", None)
    if llm is None:
        raise RuntimeError("this Hermes gives plugins no model access")
    with _own_profile_scope(), _on_route(route):
        result = llm.complete_structured(
            instructions=instructions, input=[{"type": "text", "text": text}], json_mode=True,
            task=memory.TASK if _memory_task else None, purpose=purpose, timeout=memory.CALL_TIMEOUT_S,
            max_tokens=max_tokens)
    usage = getattr(result, "usage", None)
    return getattr(result, "parsed", None), int(getattr(usage, "total_tokens", 0) or 0)


def _on_route(route: Optional[dict[str, str]]) -> Any:
    """Makes ``route`` the main model for one call, so a slot on "auto"
    resolves to it, the way a chat's own side calls resolve to its model
    (Hermes's llm.oneshot lends a chat's model the same way). No route, or a
    Hermes without the binding, leaves Hermes's main model."""
    if not route:
        return nullcontext()
    try:
        from agent.auxiliary_client import scoped_runtime_main
    except ImportError:
        return nullcontext()
    return scoped_runtime_main(route)


def _own_profile_scope() -> Any:
    """This profile's secret scope for a model call off any request.

    Once the dashboard has served another profile (Conduit's ``?profile=``)
    it hosts several, and a key read with no scope raises
    UnscopedSecretError instead of reading this profile's .env.
    """
    try:
        from hermes_cli.web_server_profiles import _config_profile_scope
    except ImportError as exc:
        # Only a Hermes without profile hosting has nothing to scope.
        if exc.name not in ("hermes_cli", "hermes_cli.web_server_profiles"):
            raise
        return nullcontext()
    return _config_profile_scope(None)


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
    if quiet.why(pulse_store, zone, at):
        return False  # its time fell while Pulse was quiet: skipped, not sent late once it isn't
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
    # A report split into cards is rated on them, not by typing.
    split = pulse_store.get(store.SPLIT_REPORTS)
    split = {str(attempt) for attempt in split} if isinstance(split, list) else set()
    for row in pulse_store.attempts_since(REPORT, now - RATINGS_DAYS * 86400):
        if row["state"] != store.DELIVERED or str(row["attempt_id"]) in split:
            continue
        picked = _run_picks(pulse_store, row)
        shown = None if picked is None else {pick["item_id"] for pick in picked if pick["item_id"] is not None}
        try:
            items, ratings = report.ratings_after(home, chat_id=row["session_key"], tag=row["tag"],
                                                  since=row["created_at"] - delivery.SINCE_SLACK_S, shown=shown)
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

