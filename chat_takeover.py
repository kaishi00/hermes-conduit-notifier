"""Dropping another process's claim on a chat (Hermes' active-session
registry), shared by the dashboard's takeover route (dashboard/api/chats.py,
which explains the rules) and Pulse, which takes over its own chat when Hermes
Desktop holds it (pulse_host.py).

Raises ``TakeoverError`` with an HTTP-style status: 501 for a Hermes that
can't hand a chat over, 503 when the registry can't be read, 504 when the
caller gave up.
"""

from __future__ import annotations

import inspect
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TURN_MARKER_SURFACES = frozenset({"desktop", "tui"})
# The route's workers and Pulse take over in the same process; Hermes' file
# lock may not exclude threads of one process, so they also take this one.
# The dashboard loads this file as conduit_push_dashboard_pkg.chat_takeover
# and Pulse as conduit_push.chat_takeover, so the lock lives on sys, which
# both copies share (dict.setdefault is atomic).
_in_process = sys.__dict__.setdefault("_conduit_chat_takeover_lock", threading.Lock())


def _loaded_version() -> str:
    """plugin.yaml's version as this copy of the module was imported, for the
    log: a process keeps the code it imported until it restarts."""
    try:
        with open(Path(__file__).with_name("plugin.yaml"), encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip() == "version":
                    return value.strip().strip("\"'") or "unknown"
    except (OSError, UnicodeDecodeError):
        pass
    return "unknown"


LOADED_VERSION = _loaded_version()


class TakeoverError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def modules() -> tuple[Any, Any]:
    """Hermes' registry and turn-marker modules, or a clear 501. The route
    leans on private helpers, so their shape is checked, not just their
    presence; and without turn markers a running turn can't be seen, so the
    route refuses rather than risk cutting one off."""
    unsupported = TakeoverError(501, "This Hermes version can't hand a chat over")
    try:
        from hermes_cli import active_sessions as registry
        from tui_gateway import turn_marker
    except ImportError as exc:
        if exc.name not in ("hermes_cli", "hermes_cli.active_sessions", "tui_gateway", "tui_gateway.turn_marker"):
            raise
        raise unsupported
    # Each helper must accept exactly the call this route makes.
    calls = {
        registry: {"_FileLock": ((None,), {}), "_lease_paths": ((), {"registry_home": None}),
                   "_read_entries": ((None,), {"strict": True}), "_write_entries": ((None, None), {}),
                   "_pid_liveness": ((None, None), {})},
        turn_marker: {"read_turn_marker": ((None, None), {}), "marker_writer_state": ((None,), {})},
    }
    for module, helpers in calls.items():
        for name, (args, kwargs) in helpers.items():
            helper = getattr(module, name, None)
            if not callable(helper):
                raise unsupported
            try:
                inspect.signature(helper).bind(*args, **kwargs)
            except (TypeError, ValueError):
                raise unsupported
    return registry, turn_marker


def home() -> Any:
    """Hermes' home directory, or the same clear 501 as a missing registry."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError as exc:
        if exc.name != "hermes_constants":
            raise
        raise TakeoverError(501, "This Hermes version can't hand a chat over")
    if not callable(get_hermes_home):
        raise TakeoverError(501, "This Hermes version can't hand a chat over")
    return get_hermes_home()


def _entry_session_id(entry: dict[str, Any]) -> str:
    value = entry.get("session_id")
    return "" if value is None or isinstance(value, bool) else str(value)


def _owner_pid(entry: dict[str, Any]) -> int:
    pid = entry.get("pid")
    if isinstance(pid, bool):
        return 0
    try:
        return int(pid)
    except (TypeError, ValueError, OverflowError):
        return 0


def _owner_turn_running(turn_marker: Any, home: Any, entry: dict[str, Any], aliases: list,
                        owner_dead: bool = False) -> bool:
    """True while a turn may be in flight on this chat.

    Every Desktop/TUI turn writes a durable marker at start and clears it when
    the turn ends. The marker is looked up under the owner's id and every id
    the caller passed. Any marker counts unless its writer is provably dead
    (crash evidence, not a running turn): the writer need not be the owner's
    pid, since an isolated turn runs in a compute-host child. A marker that
    can't be read counts as running. For an owner that is gone, a marker counts
    unless its writer is provably dead or it names no writer at all (its
    isolated child may outlive it); those are crash leftovers.

    The marker helpers never take the registry lock: liveness is a pid and
    start-time probe (hermes_cli.active_sessions._pid_liveness).

    Keys are looked up in Hermes' marker JSON, never used as a path.
    """
    own_key = _entry_session_id(entry)
    keys = ([own_key] if own_key else []) + [alias for alias in aliases if alias != own_key]
    for key in keys:
        try:
            marker = turn_marker.read_turn_marker(home, key)
        except Exception:  # noqa: BLE001 — can't tell, so never cut a turn off
            return True
        if marker is None:
            continue
        if not isinstance(marker, dict):
            return True  # an unexpected shape can't be read, so it counts as running
        try:
            state = turn_marker.marker_writer_state(marker)
        except Exception:  # noqa: BLE001 — can't tell, so never cut a turn off
            return True
        # A gone owner's marker with no writer identity is a leftover from a
        # build without isolated turns; one naming a writer of unknown
        # liveness may be a child still running, so it counts.
        identified = marker.get("writer_pid") is not None
        if state == "alive" or (state != "dead" and (not owner_dead or identified)):
            return True
    return False


# Hermes' gateway admits work into a durable ledger in the profile's state.db
# and refuses every other surface's turn on that chat while a row is open
# (tui_gateway.session_lifecycle._canonical_ledger_refusal), with the same
# SESSION_NOT_OWNED as a claim but no claim in active_sessions.json. Another
# bot's message (message_agent) is delivered this way, into the bot's Bot Chat,
# and keeps it until its reply ends. No claim can be dropped for it: the route
# answers busy, so Conduit waits and sends once the row closes.
#
# The tables, columns and the 'terminal' status are Hermes' own refusal query
# (_CANONICAL_PENDING_SQL, read at Hermes 9e01b44): a row this reads as open
# is one that refuses the send (a NULL status reads settled in both). Bot deliveries carry request_id "bot:<id>" and
# principal_id "automation:agent:<bot>:…" (tools/bot_mode_dm.py).
_LEDGER_TABLES = (("session_admissions", "target_session_id"), ("worker_executions", "session_id"))
_BOT_MARKS = (("request_id", "bot:%"), ("principal_id", "automation:agent:%"))


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def _ledger_owner(db: sqlite3.Connection, ids: list) -> Optional[str]:
    marks = ",".join("?" * len(ids))
    for table, key in _LEDGER_TABLES:
        columns = _columns(db, table)
        if not {key, "status"} <= columns:
            continue  # this Hermes keeps no such ledger
        bot = [f"{column} LIKE ?" for column, _ in _BOT_MARKS if column in columns]
        patterns = [pattern for column, pattern in _BOT_MARKS if column in columns]
        # Another bot's row first, so it's named even beside other queued work.
        is_bot = f"({' OR '.join(bot)})" if bot else "NULL"
        order = f" ORDER BY {is_bot} DESC" if bot else ""
        row = db.execute(f"SELECT {is_bot} FROM {table} WHERE {key} IN ({marks}) AND status != 'terminal'{order}"
                         " LIMIT 1", patterns + ids + patterns).fetchone()
        if row is not None:
            return "bot" if row[0] else "gateway"
    return None


def gateway_queue_owner(home: Any, session_ids: list) -> Optional[str]:
    """The surface holding the chat through the gateway's ledger: ``bot``
    for another bot's message, ``gateway`` for other queued work, None when
    nothing is open. A ledger that can't be read says nothing (the claim
    rules still apply); Hermes refuses the send itself if it can't read it."""
    ids = [str(value) for value in session_ids if str(value)]
    if not ids or home is None:
        return None
    try:
        path = Path(os.fspath(home), "state.db").resolve()
        if not path.is_file():
            return None
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except (OSError, TypeError, sqlite3.Error) as exc:
        logger.warning("Chat takeover: state.db unreadable, so the gateway ledger isn't checked: %s", exc)
        return None
    try:
        return _ledger_owner(db, ids)
    except sqlite3.Error as exc:
        logger.warning("Chat takeover: gateway ledger unreadable, so it isn't checked: %s", exc)
        return None
    finally:
        db.close()


def _owner_alive(registry: Any, entry: dict[str, Any]) -> Optional[bool]:
    """Hermes' pid + start-time liveness; an error is unknown, which counts as live."""
    try:
        return registry._pid_liveness(_owner_pid(entry), entry.get("process_start_time"))
    except Exception:  # noqa: BLE001
        return None


# A claim this process holds can belong to a runtime nobody is attached to any
# more: Conduit's own earlier connection, left detached when its socket closed
# (hermes-conduit#3125). Hermes reaps such a runtime after a short grace
# (tui_gateway.session_lifecycle._schedule_ws_orphan_reap) and hands its claim
# to a new runtime of the same chat (_take_over_detached_runtime_lease), but a
# runtime kept alive past the grace (a wake-up it drives, another profile path
# for the same chat) slips through both, and its claim then refuses every send
# from the device that left it, with nobody to close it. So the takeover lets
# go of it, the way Hermes' idle release does (session_reaper.
# _release_idle_session_leases): only with no client on it, no turn running or
# marked, nothing waiting on an answer, no delegated work and no write in
# progress, rechecked under the runtime's history_lock; the runtime stays and
# its next turn claims the chat again. A runtime a client is still attached to
# (Conduit on another device, the web chat) keeps its claim.
_DETACHED_REQUIRED = ("_sessions", "_sessions_lock", "_release_active_session_slot", "_transport_is_dead",
                      "_session_pending_kind")


def _gateway() -> Any:
    """This process's TUI gateway as Hermes loaded it, or None (never imported here)."""
    server = sys.modules.get("tui_gateway.server")
    if server is None or not all(getattr(server, name, None) is not None for name in _DETACHED_REQUIRED):
        return None
    return server


def _same_path(a: Any, b: Any) -> bool:
    try:
        return os.path.realpath(os.fspath(a)) == os.path.realpath(os.fspath(b))
    except (TypeError, ValueError, OSError):
        return False


def _detached_claim_check(server: Any, sid: str, session: Any, wanted: set, state_path: Any,
                          turn_marker: Any, home: Any) -> tuple[Any, Optional[str]]:
    """A runtime in this process against this chat's claim: ``(None, None)``
    when it holds no claim on it, ``(lease, None)`` when it holds one with no
    client and nothing running (it can be let go), and ``(lease, why)`` when
    it holds one that stays, ``why`` saying what keeps it for the log."""
    if not isinstance(session, dict):
        return None, None
    lease = session.get("active_session_lease")
    if lease is None or not getattr(lease, "enabled", False) or getattr(lease, "released", True):
        return None, None
    key = str(session.get("session_key") or "")
    if str(getattr(lease, "session_id", "") or "") not in wanted and key not in wanted:
        return None, None
    lease_path = getattr(lease, "state_path", None)
    if lease_path is not None and not _same_path(lease_path, state_path):
        return None, None  # another profile's registry: not the claim this takeover reads
    if session.get("_finalized") or session.get("_closing"):
        return lease, "the runtime is closing"
    if not server._transport_is_dead(session.get("transport")):
        return lease, "a client is attached to it"
    if session.get("running") or session.get("_compute_host_turn_id"):
        return lease, "a turn is running on it"
    ready = session.get("agent_ready")
    if ready is not None and callable(getattr(ready, "is_set", None)) and not ready.is_set():
        return lease, "its agent is still being built"
    pending = server._session_pending_kind(sid)
    if pending:
        return lease, f"it is waiting on an answer ({pending})"
    delegations = getattr(server, "_session_has_active_delegations", None)
    if callable(delegations) and delegations(sid, session):
        return lease, "delegated work is running from it"
    writing = getattr(server, "_session_holds_transcript_claim", None)
    if callable(writing) and writing(session, time.time()):
        return lease, "it is writing its transcript"
    root = session.get("profile_home") or home
    if _owner_turn_running(turn_marker, root, {"session_id": key or getattr(lease, "session_id", "")}, sorted(wanted)):
        return lease, "a turn marker says a turn is running"
    return lease, None


def _detached_idle_claim(server: Any, sid: str, session: Any, wanted: set, state_path: Any,
                         turn_marker: Any, home: Any) -> Any:
    """The claim of a runtime in this process with no client and nothing running, on this chat, or None."""
    lease, why = _detached_claim_check(server, sid, session, wanted, state_path, turn_marker, home)
    return lease if why is None else None


def release_detached_claims(session_ids: list, *, registry: Any, turn_marker: Any, home: Any,
                            server: Any = None, abandoned: Optional[threading.Event] = None,
                            kept: Optional[list] = None) -> list:
    """Lets go of this process's claims on the chat held by runtimes nobody is
    attached to; the surfaces it let go of. Hermes' lease release takes the
    registry lock itself, so this runs before take_over_session takes it.
    ``kept``, when given, gets ``(runtime id, lease id, why)`` for each runtime
    whose claim on the chat stays, for take_over_session's log."""
    server = _gateway() if server is None else server
    if server is None:
        if kept is not None:
            kept.append((None, None, "Hermes' chat gateway isn't loaded in this process"))
        return []
    wanted = {str(value) for value in session_ids if str(value)}
    state_path = registry._lease_paths(registry_home=home)[0]
    with server._sessions_lock:
        runtimes = list(server._sessions.items())
    surfaces = []
    for sid, session in runtimes:
        try:
            lease, why = _detached_claim_check(server, sid, session, wanted, state_path, turn_marker, home)
            if lease is None:
                continue
            if why is None and session.get("history_lock") is None:
                why = "the runtime has no history lock to release it under"
            if why is not None:
                if kept is not None:
                    kept.append((sid, getattr(lease, "lease_id", None), why))
                continue
            lock = session.get("history_lock")
            # _in_process first, as take_over_session takes it before the registry lock: Hermes' file
            # lock may not exclude this process's other takeover threads from the release's write.
            with _in_process, lock:  # a turn's admission and Hermes' own release take history_lock too
                if abandoned is not None and abandoned.is_set():
                    raise TakeoverError(504, "Chat takeover timed out")
                lease, why = _detached_claim_check(server, sid, session, wanted, state_path, turn_marker, home)
                if lease is not None and why is None and server._sessions.get(sid) is not session:
                    why = "the runtime was replaced while the takeover looked"
                if lease is not None and why is None and not server._release_active_session_slot(session):
                    why = "Hermes refused to release it"
                if lease is None or why is not None:
                    if lease is not None and kept is not None:
                        kept.append((sid, getattr(lease, "lease_id", None), why))
                    continue
                session["_lease_idle_released"] = sid  # off-turn writers re-claim first, as after an idle release
            surfaces.append(str(getattr(lease, "surface", "") or session.get("source") or ""))
            logger.info("Chat takeover: let go of this process's claim on chat %r held by detached runtime %r"
                        " (hermes-conduit#3125)", getattr(lease, "session_id", None), sid)
        except TakeoverError:
            raise
        except Exception as exc:  # noqa: BLE001 — can't tell, so the claim stays
            logger.debug("Chat takeover: detached claim check failed for %r", sid, exc_info=True)
            if kept is not None:
                kept.append((sid, None, f"checking it failed ({type(exc).__name__}: {exc})"))
    return surfaces


def _log_own_claims(own: list, kept: list, own_pid: int) -> None:
    """Says, for each claim this process keeps, what keeps it: the takeover
    answers same_host for it, and Conduit then tells the person the chat is
    open elsewhere (hermes-conduit#3381)."""
    gateway_missing = next((why for sid, _, why in kept if sid is None), None)
    for entry in own:
        live = str((entry.get("metadata") or {}).get("live_session_id") or entry.get("live_session_id") or "")
        lease_id = entry.get("lease_id")
        if _owner_pid(entry) != own_pid:
            why = "the claim names no usable pid"
        else:
            why = gateway_missing or next(
                (why for sid, held, why in kept if (live and sid == live) or (lease_id and held == lease_id)),
                "no runtime in this process holds it any more")
        logger.info("Chat takeover: kept this process's claim on chat %r (live session %r, surface %r): %s"
                    " (plugin %s, pid %s; hermes-conduit#3381)", _entry_session_id(entry), live or None,
                    entry.get("surface"), why, LOADED_VERSION, own_pid)


def take_over_session(session_ids: list, *, registry: Any, turn_marker: Any, home: Any, by: str = "Conduit",
                      own_pid: Optional[int] = None,
                      abandoned: Optional[threading.Event] = None, server: Any = None) -> dict[str, Any]:
    """Drop other processes' claims on the chat. ``session_ids`` are the ids
    of ONE chat (its stored id and live id); a running turn under any of
    them makes the whole request busy. Statuses:

    ``free``: nobody else holds it (send again); ``taken_over``: the claims
    were dropped (send again); ``busy``: an owner is mid-turn, a live
    claim whose surface writes no turn marker holds the chat, or the
    gateway's ledger holds work on it (surface ``bot`` for another bot's
    message, ``gateway`` otherwise; ask again shortly); ``same_host``:
    this dashboard process still holds one of the ids, on a runtime
    release_detached_claims didn't let go: a client on it, work running,
    still building its agent, or closing (other processes' claims may have
    been dropped). Marker reads happen under the registry lock on purpose:
    the decision and the write must be atomic against other claimers; the
    reads are bounded by the caller's ids (at most TAKEOVER_MAX_IDS) per owner.

    A claim whose pid is missing or invalid can't be attributed to another
    process, so it is kept like this dashboard's own. ``abandoned`` is set
    when the request timed out: nothing is written after that.

    First, this process's own claims held by runtimes nobody is attached to
    are let go (release_detached_claims); ``server`` is the TUI gateway to
    look in (default: the one Hermes loaded here). A chat that then has no
    claim left answers ``taken_over``.
    """
    own_pid = os.getpid() if own_pid is None else own_pid
    wanted = set(session_ids)
    # The gateway's ledger refuses the send whoever holds a claim, so no
    # claim is dropped while it holds the chat.
    queued = gateway_queue_owner(home, session_ids)
    if queued is not None:
        logger.info("Chat takeover: Hermes' gateway ledger holds chat %r (surface %r); nothing dropped",
                    session_ids, queued)
        return {"status": "busy", "surface": queued}
    kept: list = []
    released = release_detached_claims(session_ids, registry=registry, turn_marker=turn_marker, home=home,
                                       server=server, abandoned=abandoned, kept=kept)
    state_path, lock_path = registry._lease_paths(registry_home=home)
    def check_abandoned() -> None:
        if abandoned is not None and abandoned.is_set():
            raise TakeoverError(504, "Chat takeover timed out")

    lock = registry._FileLock(lock_path)
    if not (hasattr(lock, "__enter__") and hasattr(lock, "__exit__")):
        raise TakeoverError(501, "This Hermes version can't hand a chat over")
    with _in_process, lock:
        check_abandoned()
        try:
            entries = registry._read_entries(state_path, strict=True)
        except Exception as exc:  # noqa: BLE001 — ActiveSessionRegistryError: never guess ownership
            logger.warning("Chat takeover: active-session registry unreadable: %s", exc)
            raise TakeoverError(503, "Hermes can't read who owns this chat right now")
        # Registry entries are keyed by session_id alone; the caller passes
        # every id the chat goes by so whichever one the owner used matches.
        owners = [entry for entry in entries
                  if isinstance(entry, dict) and _entry_session_id(entry) in wanted]
        if not owners:
            return {"status": "taken_over", "surface": released[0]} if released else {"status": "free"}
        # Kept: this dashboard's own claims, and any claim with no usable pid.
        foreign, own = [], []
        for entry in owners:
            pid = _owner_pid(entry)
            (foreign if pid > 0 and pid != own_pid else own).append(entry)
        if not foreign:
            _log_own_claims(own, kept, own_pid)
            return {"status": "same_host", "surface": str(own[0].get("surface") or "")}
        # A dead owner is dropped like Hermes' own prune would, unless a
        # live writer is still running its turn; unknown liveness counts as live.
        dead = {id(entry) for entry in foreign if _owner_alive(registry, entry) is False}
        live = [entry for entry in foreign if id(entry) not in dead]
        surface = str((live or foreign)[0].get("surface") or "")
        unmarked = [entry for entry in live
                    if str(entry.get("surface") or "").strip().lower() not in TURN_MARKER_SURFACES]
        if unmarked:
            # This dashboard's own claim would refuse the send anyway, so say
            # that rather than have Conduit wait on a worker it can't outlast.
            if own:
                _log_own_claims(own, kept, own_pid)
                return {"status": "same_host", "surface": str(own[0].get("surface") or "")}
            return {"status": "busy", "surface": str(unmarked[0].get("surface") or "")}
        if any(_owner_turn_running(turn_marker, home, entry, session_ids, owner_dead=id(entry) in dead)
               for entry in foreign):
            return {"status": "busy", "surface": surface}
        dropped = {id(entry) for entry in foreign}  # by identity: never let a blank lease id match others
        check_abandoned()
        registry._write_entries(state_path, [entry for entry in entries if id(entry) not in dropped])
    logger.info("Chat takeover: %s took chat %r over (%d claim(s) dropped, surfaces %r)",
                by, session_ids, len(foreign), sorted({str(entry.get("surface") or "") for entry in foreign}))
    if own:
        # This dashboard still holds one of the ids, so a send can still be refused.
        _log_own_claims(own, kept, own_pid)
        return {"status": "same_host", "surface": str(own[0].get("surface") or "")}
    return {"status": "taken_over", "surface": surface}
