"""Moving an old Bot Chat under its profile's gateway (hermes-conduit FEATURE_MAP "Bots messaging bots").

A Bot Chat written before Hermes' gateway owned sessions (main's CLI, TUI or
Desktop; source tui, chat_id unset) keeps no gateway receipt until a native
client on the host resumes it once. Hermes' bot relay (``message_agent``)
does that move on the first message to the chat, but only while nothing
holds it: when a client has it open (Conduit's or Desktop's session on the
dashboard holds its active-session lease from the first turn on), the move
is refused, and with it the message ("held by another session writer ...
safe to resend after that session closes"). A bot whose Bot Chat is open
somewhere is then out of reach of every other bot, every time.

Conduit reaches the chat through the dashboard, never natively, so it never
makes that first move itself. This does it instead, the way Hermes' own
``hermes sessions adopt`` (newer Hermes) does for each chat: a native
``session.resume`` of the Bot Chat on the profile's running gateway, then
``session.detach``. Only Bot Chats, only while no live process holds one,
never starting a gateway. One sweep runs in this host's long-lived process
that holds the lock: soon after it starts, then every SWEEP_S, and a profile
whose move was refused waits longer each time.

A dashboard session keeps its lease until the session closes (its client
goes away and Hermes reaps it), so an open chat can stay held for hours.
The first pass runs a minute after this process starts: after a restart no
session holds a lease until a client sends its first turn again, which is
when the move reliably finds the chat free.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

TITLE = "Bot Chat"
POLICY_PREFIX = "gateway.local_policy.v1:"
LEGACY_PREFIX = "gateway.local_legacy.v1:"
# Sources Hermes adopts (hermes_state_local_migration.LEGACY_LOCAL_SOURCES).
LEGACY_SOURCES = frozenset({"cli", "tui", "gui", "desktop", "oneshot", "tool", "acp"})
FIRST_DELAY_S = 60.0
SWEEP_S = 600.0
RETRY_MAX_S = 6 * 3600.0
CHILD_TIMEOUT_S = 120.0
LOCK_FILE = "conduit-bot-chat-adopt.lock"

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock_handle: Any = None
# Per profile home: when to try again, and the wait after the next refusal.
_backoff: dict[str, tuple[float, float]] = {}


UNREADABLE = "?"  # legacy_bot_chat couldn't tell


def legacy_bot_chat(home: Path) -> Optional[str]:
    """The id of the profile's Bot Chat when it still waits for its move,
    None when there's nothing to move (no Bot Chat, or already the
    gateway's), or UNREADABLE. Read-only."""
    path = Path(home).resolve() / "state.db"
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return UNREADABLE
    try:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if not {"id", "title", "source"} <= columns:
            return None
        # The exact title, as Hermes' relay finds the chat it delivers to
        # (SessionDB.get_session_by_title): that's the chat a refusal names.
        row = db.execute("SELECT * FROM sessions WHERE title = ? ORDER BY rowid DESC LIMIT 1",
                         (TITLE,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        if row.get("source") not in LEGACY_SOURCES:
            return None
        if any(row.get(key) for key in ("chat_id", "session_key", "origin_json")):
            return None  # Hermes' own rule: a row with an identity isn't old local history
        bound = db.execute("SELECT 1 FROM state_meta WHERE key IN (?, ?)",
                           (POLICY_PREFIX + row["id"], LEGACY_PREFIX + row["id"])).fetchone()
        return None if bound else str(row["id"])
    except sqlite3.Error:
        return UNREADABLE
    finally:
        db.close()


def held_by_live_process(home: Path, session_id: str, alive: Callable[[int], bool] = None) -> bool:
    """Whether a running process holds the session's active-session lease
    (``<home>/runtime/active_sessions.json``). A lease whose process is gone
    doesn't count: Hermes drops it at its next check."""
    alive = alive or _pid_alive
    try:
        entries = json.loads((Path(home) / "runtime" / "active_sessions.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # unreadable: never guessed free (as chat_takeover reads it)
    entries = entries.get("entries") if isinstance(entries, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("session_id") != session_id:
            continue
        try:
            if alive(int(entry.get("pid"))):
                return True
        except (TypeError, ValueError):
            return True  # can't tell: leave it to the next sweep
    return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# Run with the profile's HERMES_HOME, so the gateway client mints its native
# ticket for that profile. Prints one JSON line: {"status": ..., ...}.
CHILD = r"""
import asyncio, inspect, json, sys
from pathlib import Path

home, session_id = Path(sys.argv[1]), sys.argv[2]

def say(status, **extra):
    print(json.dumps({"status": status, **extra}))
    sys.exit(0)

try:
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_cli.gateway_client import connect_gateway
except Exception as error:
    say("unsupported", detail=type(error).__name__)

try:
    discovery = discover_gateway_endpoint(home, timeout=5)
except Exception as error:
    say("failed", detail="discovery: " + type(error).__name__)
if getattr(discovery, "state", None) != "ready" or getattr(discovery, "endpoint", None) is None:
    say("no_gateway", detail=str(getattr(discovery, "state", "")))

async def adopt():
    kwargs = {}
    if "max_size" in inspect.signature(connect_gateway).parameters:
        kwargs["max_size"] = 64 * 1024 * 1024  # the answer carries the whole history
    async with connect_gateway(discovery.endpoint, **kwargs) as client:
        snapshot = await client.rpc("session.resume", session_id=session_id)
        try:
            await client.rpc("session.detach", session_id=snapshot.get("session_id") or session_id,
                             subscription_id=snapshot["subscription_id"])
        except Exception:
            pass  # closing the socket detaches it too
        return snapshot.get("session_id") or session_id

try:
    say("resumed", session_id=asyncio.run(adopt()))
except Exception as error:
    reason = str(error)[:120] if type(error).__name__ == "GatewayRPCError" else type(error).__name__
    say("refused", detail=reason)
"""


def adopt(home: Path, session_id: str, run: Callable[..., Any] = subprocess.run) -> str:
    """Moves one Bot Chat over; returns "adopted", or why not. Checked from
    the store afterwards, not from the answer: a long chat's answer can
    outgrow the client's frame after the gateway has already moved it."""
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    try:
        done = run([sys.executable, "-c", CHILD, str(home), session_id], env=env, capture_output=True,
                   text=True, timeout=CHILD_TIMEOUT_S, check=False)
        lines = (done.stdout or "").strip().splitlines()
        if not lines:
            last = ((getattr(done, "stderr", "") or "").strip().splitlines() or ["no answer"])[-1]
            raise RuntimeError(last[:200])
        answer = json.loads(lines[-1])
    except Exception as error:  # noqa: BLE001 — a reason, never a crash
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        answer = {"status": "failed", "detail": detail}
    if not isinstance(answer, dict):
        answer = {"status": "failed", "detail": "answer"}
    after = legacy_bot_chat(home)
    if after is None:
        return "adopted"
    if after == UNREADABLE:
        return "unknown: the store couldn't be read afterwards"
    return f"{answer.get('status', 'failed')}: {answer.get('detail', '')}".rstrip(": ")


def homes(root: Path) -> list[Path]:
    """The main home and each profile under it."""
    found = [Path(root)]
    try:
        found += sorted(path for path in (Path(root) / "profiles").iterdir() if path.is_dir())
    except OSError:
        pass
    return found


def sweep(root: Path, now: Optional[float] = None, adopt_one: Callable[[Path, str], str] = None,
          stop: Optional[threading.Event] = None) -> dict[str, str]:
    """One pass over the host's profiles: {home: outcome} for each Bot Chat it tried."""
    now = time.time() if now is None else now
    adopt_one = adopt_one or adopt
    tried: dict[str, str] = {}
    for home in homes(root):
        if stop is not None and stop.is_set():
            break
        try:
            _sweep_one(home, now, adopt_one, tried)
        except Exception:  # noqa: BLE001 — one profile never stops the others
            logger.warning("conduit_push: Bot Chat move failed for %s", home.name, exc_info=True)
    return tried


def _sweep_one(home: Path, now: float, adopt_one: Callable[[Path, str], str], tried: dict[str, str]) -> None:
    key = str(home)
    session_id = legacy_bot_chat(home)
    if session_id is None:
        _backoff.pop(key, None)
        return
    if session_id == UNREADABLE:
        return
    due, wait = _backoff.get(key, (0.0, SWEEP_S))
    if now < due or held_by_live_process(home, session_id):
        return
    outcome = adopt_one(home, session_id)
    tried[key] = outcome
    if outcome == "adopted":
        _backoff.pop(key, None)
        logger.info("conduit_push: moved %s's Bot Chat under its gateway, so other bots can reach it",
                    home.name)
    elif outcome.startswith("no_gateway"):
        _backoff[key] = (now + SWEEP_S, wait)  # nothing refused: try again at the next pass
    else:
        _backoff[key] = (now + wait, min(wait * 2, RETRY_MAX_S))
        logger.info("conduit_push: %s's Bot Chat isn't under its gateway yet (%s)", home.name, outcome)


def _can_lock() -> bool:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False  # Windows hosts: Hermes' own relay still moves the chat when nothing holds it
    return True


def _take_lock(root: Path) -> bool:
    """One sweeper per host: the first long-lived process to take this lock."""
    global _lock_handle
    try:
        import fcntl
    except ImportError:
        return False  # Windows hosts: Hermes' own relay still moves the chat when nothing holds it
    try:
        handle = open(Path(root) / LOCK_FILE, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_handle = handle
    return True


def _run(root: Path, stop: threading.Event) -> None:
    """Waits for the lock (another process may hold it, then exit), then
    sweeps; the lock goes when this thread ends."""
    if stop.wait(FIRST_DELAY_S) or not _can_lock():
        return
    while not _take_lock(root):
        if stop.wait(SWEEP_S):
            return
    try:
        while True:
            try:
                sweep(root, stop=stop)
            except Exception:  # noqa: BLE001 — the next pass tries again
                logger.warning("conduit_push: Bot Chat move sweep failed", exc_info=True)
            if stop.wait(SWEEP_S):
                return
    finally:
        _release_lock()


def _release_lock() -> None:
    global _lock_handle
    handle, _lock_handle = _lock_handle, None
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def start(root: Path) -> None:
    """Starts the sweep for this load (once per process)."""
    global _thread, _stop
    if _thread is not None and _thread.is_alive() and not _stop.is_set():
        return
    _stop = threading.Event()
    _thread = threading.Thread(target=_run, args=(Path(root), _stop), name="conduit-bot-chat-adopt", daemon=True)
    _thread.start()


def stop(timeout: float = 1.0) -> None:
    """Ends the sweep; its thread lets the lock go when it finishes (after
    a child still running, at most CHILD_TIMEOUT_S)."""
    _stop.set()
    thread = _thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
