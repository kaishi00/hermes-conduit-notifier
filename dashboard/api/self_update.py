"""Self restart and self update."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import types
import contextlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Response

from . import capabilities, chats, core, desktop_views

logger = core.logger
router = core.router


# --- Every profile on the main install (profile_copies.py, #1538) -------------
#
# A profile's chat turns load only its own plugins/ folder, so the dashboard
# points each profile that turns the plugin on at this install: when it starts,
# and after an update pulls a new version, before the gateway restarts.


def _point_profiles() -> List[str]:
    """profile_copies.sweep over this host's profiles; [] when it can't run."""
    path = os.path.join(core._plugin_root(), "profile_copies.py")
    try:
        from hermes_constants import get_default_hermes_root
    except (ImportError, AttributeError):  # an older Hermes: one home, no profiles to point
        return []
    try:
        spec = importlib.util.spec_from_file_location("conduit_push_dashboard_profile_copies", path)
        if spec is None or spec.loader is None or not os.path.isfile(path):
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.sweep_quietly(Path(get_default_hermes_root()), Path(core._plugin_root()))
    except Exception:  # noqa: BLE001 — a fault here mustn't stop the dashboard
        logger.warning("Conduit plugin: couldn't point profiles at the main install", exc_info=True)
        return []


threading.Thread(target=_point_profiles, name="conduit-profile-copies", daemon=True).start()


# --- Self restart -------------------------------------------------------------
#
# After an update, Hermes' gateway restart reloads the plugin's hooks and tools
# for the gateway, but this dashboard keeps serving the routes (and the hooks of
# the chats it runs) it imported at start. Conduit calls this last: the process
# re-executes itself with the command line it started with, keeping its pid, so
# systemd, launchd and a terminal launch all carry on. Refused while a chat turn
# this process runs is still going, for the Desktop app's own backend (it
# supervises and re-ports its child), inside a plugin host, and on Windows,
# where exec spawns a new process instead of replacing this one.

SELF_RESTART_DELAY_S = 1.0
SELF_RESTART_RECHECK_S = 2.0
_self_restart_lock = threading.Lock()
_self_restart_scheduled = False


def _hermes_homes() -> List[Path]:
    """Every profile home this host has: the root and each profiles/<name>."""
    try:
        from hermes_constants import get_default_hermes_root
    except (ImportError, AttributeError):  # an older Hermes: one home
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home())
    else:
        # A root that fails to resolve raises: guessing could miss a turn.
        root = Path(get_default_hermes_root())
    homes = [root]
    with contextlib.suppress(OSError):
        homes.extend(sorted(p for p in (root / "profiles").iterdir() if p.is_dir()))
    return homes


def _turn_marker_file(home: Path) -> Path:
    try:
        from tui_gateway import turn_marker
    except ImportError:  # a Hermes without turn markers: the path they used
        return home / "desktop" / "interrupted_turns.json"
    # A Hermes that moved its markers raises here: guessing could miss a turn.
    return Path(turn_marker._marker_path(home))


def _own_start_time() -> Optional[float]:
    try:
        from hermes_cli.active_sessions import _own_start_time as own
        start = own()
        return None if start is None else float(start)
    except Exception:  # noqa: BLE001
        return None


def _descends_from(pid: int, ancestor: int) -> bool:
    """Whether `pid` is a child (at any depth) of `ancestor`: an isolated turn's compute host."""
    if pid <= 0:
        return False
    try:
        import psutil  # a Hermes dependency
        return any(parent.pid == ancestor for parent in psutil.Process(pid).parents())
    except Exception:  # noqa: BLE001 — gone, or no psutil: not a child we can see
        return False


def _running_turns(pid: int, homes: List[Path], start_time: Optional[float] = None) -> int:
    """Turns this process has started and not finished, from Hermes' turn markers.

    Every Desktop/TUI/dashboard chat turn writes a marker with its writer's pid
    (and start time, tui_gateway.turn_marker) at start and clears it at the
    end. A marker naming this pid, or a child of it (an isolated turn's
    compute host), is a turn still running here, unless its start time shows
    an earlier process that had this pid. A missing file is none; one that
    can't be read raises, and Hermes rewrites an unparseable one from scratch.
    """
    running = 0
    for home in homes:
        try:
            entries = json.loads(_turn_marker_file(home).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            continue
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            writer = chats._owner_pid({"pid": entry.get("writer_pid")})
            if writer != pid:
                running += 1 if _descends_from(writer, pid) else 0
                continue
            written = entry.get("writer_start_time")
            if start_time is not None and isinstance(written, (int, float)) and not isinstance(written, bool) \
                    and abs(float(written) - start_time) > 1.0:
                continue
            running += 1
    return running


def _self_restart_refusal() -> Optional[Tuple[int, str]]:
    if sys.platform == "win32":
        return 501, "Restart the Hermes dashboard on this host to finish the update"
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return 501, "This plugin runs in a Hermes plugin host; restart Hermes to finish the update"
    try:
        from hermes_cli.anon_auth import on_local_desktop_backend
        desktop = on_local_desktop_backend()
    except Exception:
        desktop = bool(os.environ.get("HERMES_DESKTOP_CHILD_PID"))
    if desktop:
        return 501, "This dashboard belongs to Hermes Desktop; restart Hermes Desktop to finish the update"
    if not getattr(sys, "orig_argv", None):
        return 501, "Restart the Hermes dashboard on this host to finish the update"
    return None


def _reexec() -> None:
    logger.warning("Conduit plugin: restarting this dashboard to load plugin %s", capabilities._plugin_version())
    # exec skips atexit: save what this module would have saved on the way out.
    with contextlib.suppress(Exception):
        desktop_views._desktop_view_store.flush()
    for handler in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            handler.flush()
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def _restart_when_idle(schedule: Callable[[float, Callable[[], None]], Any],
                       homes: Callable[[], List[Path]]) -> None:
    """Re-execs once no chat turn runs here: one may have started since the request."""
    global _self_restart_scheduled
    try:
        if _running_turns(os.getpid(), homes(), _own_start_time()):
            schedule(SELF_RESTART_RECHECK_S, lambda: _restart_when_idle(schedule, homes))
            return
        _reexec()
    except Exception:
        logger.exception("Conduit plugin: the dashboard restart failed; it can be asked again")
        with _self_restart_lock:
            _self_restart_scheduled = False


def self_restart(schedule: Callable[[float, Callable[[], None]], Any],
                 homes: Optional[Callable[[], List[Path]]] = None) -> Dict[str, Any]:
    global _self_restart_scheduled
    refusal = _self_restart_refusal()
    if refusal is not None:
        raise core.TokenError(*refusal)
    running = _running_turns(os.getpid(), (homes or _hermes_homes)(), _own_start_time())
    if running:
        raise core.TokenError(409, f"{running} chat turn{'s' if running != 1 else ''} still running on this dashboard")
    with _self_restart_lock:
        if not _self_restart_scheduled:
            _self_restart_scheduled = True
            try:
                schedule(SELF_RESTART_DELAY_S, lambda: _restart_when_idle(schedule, homes or _hermes_homes))
            except BaseException:
                _self_restart_scheduled = False
                raise
    return {"restarting": True, "boot_id": capabilities.BOOT_ID, "loaded_version": capabilities.LOADED_VERSION,
            "version": capabilities._plugin_version()}


@router.post("/self/restart")
async def post_self_restart(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    loop = asyncio.get_running_loop()
    try:
        # The answer goes out first; the exec follows a moment later.
        return {"ok": True, **self_restart(loop.call_later)}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise core._unexpected("restart", exc, feature="Restart")


# --- Self update --------------------------------------------------------------
#
# The whole update on the host, so Conduit can hand it over and be closed:
# Hermes pulls the plugin (the call behind its agent-plugins update route), the
# gateway restarts (the spawn behind /api/gateway/restart) and this dashboard
# re-execs once no chat turn runs here, as /self/restart does. The job lives in
# a file in the Hermes root, so the restarted dashboard can finish it: it
# records how the update ended and notifies the profile that asked, through
# that profile's relay pairing. A Hermes without those calls, or a dashboard
# that can't restart itself, answers 501 and Conduit runs the steps itself.

SELF_UPDATE_FILE = "conduit-self-update.json"
SELF_UPDATE_GATEWAY_LIMIT_S = 180.0
# A gateway still letting running work finish after the limit is waited on in
# steps of the limit, up to this (its own cap, agent.restart_after_turn_timeout,
# is 30 minutes by default).
SELF_UPDATE_DRAIN_LIMIT_S = 7200.0
# How long the update waits for the gateway's running work before it restarts
# anyway (a wedged turn is left out, as the gateway leaves it out). Short: past
# it the gateway's restart waits for the same work again, up to its own cap
# (agent.restart_after_turn_timeout), so a long one here only adds to that.
# Conduit names the work and can stop it (Restart now, /self/gateway-work).
SELF_UPDATE_GATEWAY_WAIT_S = 600.0
SELF_UPDATE_CHATS_RECHECK_S = 2.0
# A turn marker left behind can't hold the update forever.
SELF_UPDATE_CHATS_LIMIT_S = 7200.0
# The restarted dashboard notifies once it has had a moment to come up.
SELF_UPDATE_NOTIFY_DELAY_S = 5.0
# A job that hasn't moved for this long is over, however it was left.
SELF_UPDATE_STALE_S = 3600.0
# A gateway restart that fails this shortly before a new dashboard starts
# took the dashboard down with it (a service manager restarting both).
SELF_UPDATE_TAKEN_DOWN_S = 300.0
# The pull in a fresh Hermes process (below): git, the install scan and any dependencies.
SELF_UPDATE_FRESH_PULL_LIMIT_S = 900.0
_FRESH_PULL_MARK = "conduit-self-update-result:"
# A process's start time is only roughly the wall clock (Linux counts it from boot), so a
# file written as the dashboard started could read as newer than it.
_HERMES_CHANGED_MARGIN_S = 5.0
SELF_UPDATE_RUNNING = ("downloading", "restarting_gateway", "waiting_for_chats", "restarting_dashboard")
# Reentrant: a start reads the status under it too.
_self_update_lock = threading.RLock()
_self_update_thread: Optional[threading.Thread] = None
# The job this process started, for when its file can't be read back.
_self_update_job: Optional[Dict[str, Any]] = None
# Each start pulls and restarts the gateway: a few per hour is plenty.
_self_update_limiter = core._MintLimiter(3, 600.0, message="Too many plugin updates; try again in a few minutes")


def _self_update_path() -> Path:
    return _hermes_homes()[0] / SELF_UPDATE_FILE


def _read_self_update(path: Path) -> Optional[Dict[str, Any]]:
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return job if isinstance(job, dict) and isinstance(job.get("id"), str) else None


def _write_self_update(path: Path, job: Dict[str, Any]) -> None:
    job["updated_at"] = time.time()
    handle, temp = tempfile.mkstemp(prefix=".conduit-self-update.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(job, out)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


# --- The gateway's running work (issue 546) ------------------------------------
#
# A restart sent while the gateway runs a turn makes it refuse new work at once
# and wait for that turn (up to agent.restart_after_turn_timeout, 30 minutes by
# default) before it stops; --interrupt only skips the CLI's question. Conduit
# can't reach a gateway that refuses new work, so the update waits for the work
# first, while the gateway still serves, and a restart that still meets some
# (a turn started meanwhile) is waited on rather than reported as failed.


def _gateway_homes() -> List[Path]:
    """The homes whose gateway `hermes gateway restart` from here takes down, as Hermes' own
    guard picks them (a host multiplexer's included), or the root on a Hermes that can't say;
    none when even the root can't be found (the callers then read "no gateway")."""
    try:
        from hermes_cli.gateway_lifecycle_guards import _candidate_homes
        homes = [Path(home) for home in _candidate_homes()]
    except Exception:  # noqa: BLE001 — the root's gateway is the one to ask then
        logger.debug("Conduit plugin: couldn't list the gateway's homes", exc_info=True)
        homes = []
    if homes:
        return homes
    try:
        return _hermes_homes()[:1]
    except Exception:  # noqa: BLE001
        logger.debug("Conduit plugin: couldn't find the Hermes home", exc_info=True)
        return []


def _gateway_live_work() -> Tuple[List[Dict[str, Any]], int]:
    """The work units the gateway's restart would wait on (wedged turns and restart-safe cron
    runs left out, as the gateway leaves them out), and how many: an older gateway gives only
    the count. ([], 0) when no gateway answers or this Hermes can't say."""
    try:
        from hermes_cli.gateway_lifecycle_guards import live_work
    except Exception:  # noqa: BLE001 — a broken import reads as one that's missing
        return [], 0
    for home in _gateway_homes():
        try:
            work = live_work(home)
        except Exception:  # noqa: BLE001 — not knowing is no reason to hold the update
            logger.debug("Conduit plugin: couldn't read the gateway's running work", exc_info=True)
            return [], 0
        if not isinstance(work, dict):
            continue
        units = [unit for unit in work.get("active_work") or [] if isinstance(unit, dict)]
        if units:
            awaited = [unit for unit in units if not unit.get("wedged") and not unit.get("restart_safe")]
            return awaited, len(awaited)
        count = work.get("active_agents")
        return [], count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0
    return [], 0


def _gateway_work() -> int:
    """How many work units the gateway's restart would wait on (_gateway_live_work)."""
    return _gateway_live_work()[1]


def _profile_home(profile: str) -> Optional[Path]:
    """A profile's Hermes home: the root for the default one, else profiles/<name>."""
    try:
        root = _hermes_homes()[0]
    except Exception:  # noqa: BLE001 — no home: nothing to read the chat from
        return None
    return root if profile == "default" else root / "profiles" / profile


def _chat_of(key: str) -> Tuple[str, Optional[str], Optional[str]]:
    """``(profile, platform, chat id)`` from a gateway session key (``agent:<ns>:<platform>:
    <chat type>[:<chat id>...]``, gateway/session.py build_session_key): ``agent:main`` is the
    default profile and ``agent:main~`` one named main."""
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != "agent":
        return "default", None, None
    profile = {"main": "default", "main~": "main"}.get(parts[1], parts[1])
    return profile, parts[2], ":".join(parts[4:]) or None


def _session_row(home: Optional[Path], key: str) -> Dict[str, Any]:
    """The chat's latest session row for a gateway session key ({} when it can't be read)."""
    if home is None:
        return {}
    try:
        from hermes_state import SessionDB
        db = SessionDB(db_path=home / "state.db")
    except Exception:  # noqa: BLE001 — a name is never worth failing the answer
        logger.debug("Conduit plugin: couldn't open a profile's sessions", exc_info=True)
        return {}
    try:
        rows = db.list_sessions_rich(session_key=key, limit=1, order_by_last_active=True)
        return rows[0] if rows and isinstance(rows[0], dict) else {}
    except Exception:  # noqa: BLE001
        logger.debug("Conduit plugin: couldn't read a chat's session", exc_info=True)
        return {}
    finally:
        with contextlib.suppress(Exception):
            db.close()


def _named_work_unit(unit: Dict[str, Any]) -> Dict[str, Any]:
    """One unit the restart waits on, as Conduit names it: whose chat, its title and session
    (to stop it), or the cron job; a kind Hermes names no further keeps only its kind."""
    named: Dict[str, Any] = {"kind": str(unit.get("kind") or "work")}
    elapsed = unit.get("elapsed_s")
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
        named["elapsed_s"] = round(float(elapsed))
    if named["kind"] == "chat" and isinstance(unit.get("session"), str):
        key = unit["session"]
        profile, platform, chat_id = _chat_of(key)
        home = _profile_home(profile)
        row = _session_row(home, key)
        session_id = row.get("id") or (chat_id if platform == "local" else None)
        title = row.get("title") or row.get("preview") or ""
        named.update(profile=profile, platform=platform or "", session_id=session_id or None,
                     title=" ".join(str(title).split())[:120])
        if home is not None:
            with contextlib.suppress(Exception):
                titles = importlib.import_module(f"{_plugin_client().__package__}.session_titles")
                named["bot"] = titles._bot_name(home)
    elif named["kind"] == "cron":
        job_id = str(unit.get("job_id") or "")
        named["job_id"] = job_id
        try:
            from hermes_cli.update_cmd_drain_report import _cron_job_name
        except ImportError:
            _cron_job_name = None
        if _cron_job_name is not None:
            with contextlib.suppress(Exception):
                named["title"] = _cron_job_name(job_id, _gateway_homes()[0]) or ""
    return named


def _restart_timeout_s() -> Optional[float]:
    """The gateway's own cap on a restart's wait (agent.restart_after_turn_timeout), or None."""
    try:
        from gateway.restart import DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT, parse_restart_after_turn_timeout
        from hermes_cli.config import load_config
        raw = (load_config().get("agent") or {}).get("restart_after_turn_timeout")
        return float(parse_restart_after_turn_timeout(raw) if raw is not None
                     else DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT)
    except Exception:  # noqa: BLE001 — Conduit then says no time
        logger.debug("Conduit plugin: couldn't read the gateway's restart cap", exc_info=True)
        return None


# When this dashboard first saw the gateway draining, for a restart it didn't spawn itself.
_draining_seen_at: Optional[float] = None


def _restart_spawned_at() -> Optional[float]:
    """When this dashboard spawned the gateway restart still running (Hermes' own
    /api/gateway/restart and the host-run update alike), as wall-clock time, or None."""
    try:
        web_server = sys.modules.get("hermes_cli.web_server")
        spawned_at, proc, _command = getattr(web_server, "_LAST_GATEWAY_RESTART")
        if proc.poll() is not None:
            return None
        return time.time() - (time.monotonic() - float(spawned_at))
    except Exception:  # noqa: BLE001 — none, or a Hermes that keeps it otherwise
        return None


def _draining_since(draining: bool) -> Optional[float]:
    global _draining_seen_at
    if not draining:
        _draining_seen_at = None
        return None
    # A sighting from a drain this dashboard stopped watching before it ended is no drain's start.
    if _draining_seen_at is None or time.time() - _draining_seen_at > SELF_UPDATE_DRAIN_LIMIT_S:
        _draining_seen_at = time.time()
    spawned = _restart_spawned_at()
    return min(spawned, _draining_seen_at) if spawned is not None else _draining_seen_at


def gateway_work_report() -> Dict[str, Any]:
    """What a gateway restart waits on now (GET /self/gateway-work): each unit named, how many
    (an older gateway names none), whether the gateway is already draining, its cap, and when
    it restarts anyway (the drain's start, as near as this dashboard knows it, plus the cap)."""
    units, count = _gateway_live_work()
    draining = _gateway_draining()
    timeout = _restart_timeout_s()
    since = _draining_since(draining)
    return {"units": [_named_work_unit(unit) for unit in units], "count": count, "draining": draining,
            "restart_timeout_s": timeout,
            "restarts_by": since + timeout if since is not None and timeout is not None else None}


# Restart now: the update's wait for the gateway's work ends here; Conduit stops the chats
# themselves (session.interrupt), which a draining gateway still takes.
_restart_now = threading.Event()
# When Restart now last forced a restart: the update's own restart child, cut short by it, may
# exit non-zero although the gateway came back under systemd.
_forced_restart_at: Optional[float] = None


def _forget_restart_now() -> None:
    """A new update starts with no Restart now and no forced restart of its own."""
    global _forced_restart_at
    _restart_now.clear()
    _forced_restart_at = None


def force_gateway_restart() -> None:
    """Restarts the gateway through its service manager, which doesn't wait for running work:
    the gateway's stop gives it agent.restart_drain_timeout (60 s by default), then ends it. A
    drain already under way can't be cut any other way (a second `hermes gateway restart` joins
    it). systemd only: queued with --no-block, so it lands even when the restart takes this
    dashboard down with the gateway (a unit that Requires= it)."""
    try:
        from hermes_cli import gateway as gw
        systemd = gw.supports_systemd_services() and gw._installed_service_kind_for(lambda: False) == "systemd"
    except Exception:  # noqa: BLE001 — a Hermes that can't say: no force
        logger.debug("Conduit plugin: couldn't tell how the gateway is run", exc_info=True)
        systemd = False
    if not systemd:
        raise core.TokenError(501, "Conduit can force a restart only for a gateway systemd runs. "
                                   "Run hermes gateway stop, then hermes gateway start, on the host.")
    global _forced_restart_at
    command = gw._systemctl_cmd(gw._select_systemd_scope(False)) + ["restart", "--no-block", gw.get_service_name()]
    logger.warning("Conduit plugin: forcing a gateway restart (%s)", " ".join(command))
    done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                          timeout=30, start_new_session=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[-300:]
        raise core.TokenError(502, f"systemd didn't take the restart: {detail}" if detail
                              else "systemd didn't take the restart")
    _forced_restart_at = time.time()


def _gateway_draining() -> bool:
    """Whether the gateway is letting running work finish before its restart stops it."""
    try:
        from gateway.status import read_runtime_status, runtime_status_is_stale
    except Exception:  # noqa: BLE001 — a broken import reads as one that's missing
        return False
    for home in _gateway_homes():
        try:
            record = read_runtime_status(home / "gateway_state.json")
            if record and not runtime_status_is_stale(record):
                return record.get("gateway_state") == "draining"
        except Exception:  # noqa: BLE001 — unknown reads as not draining: the old limit applies
            logger.debug("Conduit plugin: couldn't read the gateway's state", exc_info=True)
            return False
    return False


def _hermes_update_steps() -> Tuple[Callable[[], Dict[str, Any]], Callable[..., int]]:
    """Hermes' own plugin pull and gateway restart, or 501 when this Hermes lacks them."""
    try:
        plugins_cmd = importlib.import_module("hermes_cli.plugins_cmd")
        web_server = importlib.import_module("hermes_cli.web_server")
    except ImportError as exc:
        # Only a Hermes without these modules; a broken import inside Hermes is a real failure.
        if exc.name not in ("hermes_cli", "hermes_cli.plugins_cmd", "hermes_cli.web_server"):
            raise
        raise core.TokenError(501, "This Hermes can't run the plugin update on its own")
    dashboard_update_user_plugin = getattr(plugins_cmd, "dashboard_update_user_plugin", None)
    _spawn_gateway_restart = getattr(web_server, "_spawn_gateway_restart", None)
    if not callable(dashboard_update_user_plugin) or not callable(_spawn_gateway_restart):
        raise core.TokenError(501, "This Hermes can't run the plugin update on its own")
    # interrupt arrived with the drain refusal (Hermes 2026-10-20); before it
    # nothing refused a restart, so an older Hermes restarts without it.
    try:
        inspect.signature(_spawn_gateway_restart).bind(None, interrupt=True)
    except (TypeError, ValueError):
        restart_kwargs: Dict[str, Any] = {}
    else:
        restart_kwargs = {"interrupt": True}

    def pull() -> Dict[str, Any]:
        # A dashboard still running the Hermes it started with, after Hermes was updated
        # under it, mixes the two in one process: what it loaded is old, what the pull
        # imports now is new, and the new code calls into the old ("'PythonDeclaration'
        # object has no attribute 'installs_packages'", a dashboard started on Hermes 0.22.3
        # updated to 0.22.4). A fresh process runs the Hermes on disk, whole; the dashboard
        # restart at the end of the update then loads it here too.
        if _hermes_changed_on_disk(sys.modules.get("hermes_cli")):
            return _pull_in_fresh_process()
        return dashboard_update_user_plugin(core.PLUGIN_NAME)

    def restart_gateway(draining: Callable[[], None] = lambda: None) -> int:
        proc, _reused = _spawn_gateway_restart(None, **restart_kwargs)
        waited = 0.0
        while True:
            try:
                return proc.wait(timeout=SELF_UPDATE_GATEWAY_LIMIT_S)
            except subprocess.TimeoutExpired:
                waited += SELF_UPDATE_GATEWAY_LIMIT_S
                # Hermes' restart waits out the drain, then forces it: still going, not failed.
                if waited < SELF_UPDATE_DRAIN_LIMIT_S and _gateway_draining():
                    draining()
                    continue
                # Failed is failed: a restart finishing later mustn't contradict the job.
                with contextlib.suppress(OSError):
                    proc.kill()
                # Reaped, so it doesn't linger as a zombie of this dashboard.
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                raise

    return pull, restart_gateway


# When this module loaded: after a re-exec (_reexec keeps the pid, so its creation time) it
# loads again, with the Hermes on disk then.
_MODULE_LOADED_AT = time.time()


def _process_start_time() -> Optional[float]:
    """When this process loaded the Hermes it runs: its start, or its last re-exec."""
    try:
        import psutil  # a Hermes dependency
        return max(float(psutil.Process(os.getpid()).create_time()), _MODULE_LOADED_AT)
    except Exception:  # noqa: BLE001 — not knowing reads as unchanged: the pull runs here, as before
        return None


def _hermes_root(hermes_cli: Any) -> Optional[str]:
    """The Hermes checkout this process runs: the folder holding hermes_cli."""
    file = getattr(hermes_cli, "__file__", None)
    return os.path.dirname(os.path.dirname(os.path.realpath(file))) if file else None


def _hermes_changed_on_disk(hermes_cli: Any, started: Optional[float] = None) -> bool:
    """Whether Hermes' code this process loaded changed on disk after it started (a
    `hermes update` without a dashboard restart)."""
    root = _hermes_root(hermes_cli)
    started = _process_start_time() if started is None else started
    if root is None or started is None:
        return False
    prefix = root + os.sep
    plugin = os.path.realpath(core._plugin_root()) + os.sep
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if not isinstance(file, str):
            continue
        file = os.path.realpath(file)
        if not file.startswith(prefix) or file.startswith(plugin):
            continue
        try:
            if os.stat(file).st_mtime > started + _HERMES_CHANGED_MARGIN_S:
                return True
        except OSError:  # gone from disk: changed too
            return True
    return False


def _pull_in_fresh_process() -> Dict[str, Any]:
    """Hermes' plugin pull in a new process of the Hermes on disk, for this dashboard's home."""
    env = dict(os.environ)
    with contextlib.suppress(Exception):
        from hermes_constants import get_hermes_home
        env["HERMES_HOME"] = str(get_hermes_home())
    script = ("import json, sys\n"
              "from hermes_cli.plugins_cmd import dashboard_update_user_plugin\n"
              "result = dashboard_update_user_plugin(sys.argv[1])\n"
              f"print({_FRESH_PULL_MARK!r} + json.dumps(result, default=str), flush=True)\n")
    restart = ("Hermes was updated on this host after the dashboard started. Restart the Hermes "
               "dashboard, then update the plugin again.")
    try:
        done = subprocess.run([sys.executable, "-c", script, core.PLUGIN_NAME], env=env,
                              cwd=_hermes_root(sys.modules.get("hermes_cli")) or None,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=SELF_UPDATE_FRESH_PULL_LIMIT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        logger.warning("Conduit plugin: the update's pull in a fresh Hermes process timed out")
        return {"ok": False, "error": f"The update didn't finish on the host within "
                                      f"{int(SELF_UPDATE_FRESH_PULL_LIMIT_S // 60)} minutes. {restart}"}
    except OSError:
        logger.warning("Conduit plugin: couldn't start a fresh Hermes process for the update's pull", exc_info=True)
        return {"ok": False, "error": restart}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith(_FRESH_PULL_MARK):
            with contextlib.suppress(ValueError):
                result = json.loads(line[len(_FRESH_PULL_MARK):])
                if isinstance(result, dict):
                    return result
    logger.warning("Conduit plugin: the update's pull in a fresh Hermes process failed (exit %s): %s",
                   done.returncode, (done.stderr or "")[-2000:])
    return {"ok": False, "error": restart}


def _self_update_failed(job: Dict[str, Any], detail: str, code: Optional[str] = None) -> None:
    # failed_step says how far it got: Conduit runs the steps itself only
    # when the pull itself failed.
    # The gateway's stage belonged to the step that ended.
    for key in ("gateway_stage", "running_chats", "gateway_restart_at"):
        job.pop(key, None)
    job.update(failed_step=job.get("step"), step="failed", detail=detail, finished_at=time.time())
    if code:
        job["code"] = code


def _run_self_update(job: Dict[str, Any], path: Path, pull: Callable[[], Dict[str, Any]],
                     restart_gateway: Callable[..., int], schedule: Callable[[float, Callable[[], None]], Any],
                     homes: Callable[[], List[Path]], notify: Callable[[Dict[str, Any]], None],
                     sleep: Callable[[float], None] = time.sleep,
                     point_profiles: Callable[[], Any] = lambda: _point_profiles(),
                     gateway_work: Callable[[], int] = lambda: _gateway_work()) -> None:
    global _self_restart_scheduled
    claimed_restart = False
    try:
        result = pull()
        if not result.get("ok"):
            # Not found, no owned checkout, a re-pin needing consent, a blocked
            # scan: Hermes' own words, which Conduit acts on.
            code = next((flag for flag in ("consent_required", "scan_blocked") if result.get(flag)), None)
            _self_update_failed(job, str(result.get("error") or "Hermes didn't apply the update"), code)
            return
        job.update(step="restarting_gateway", version=capabilities._plugin_version())
        if job["version"] is None:
            _self_update_failed(job, "The update left a plugin.yaml without a readable version on the host")
            return
        # Before the restart, so the profiles it restarts load the new version with it. A profile
        # running its own gateway loads it when that gateway restarts.
        point_profiles()
        # Until the restart is sent: a dashboard restarted before then (during the wait below)
        # loads the new plugin, but the gateway still runs the old one.
        job["gateway_pending"] = True
        _write_self_update(path, job)
        # The gateway's running work first, while it still serves (issue 546). Past the limit
        # the restart goes ahead: the gateway then waits for the work itself, up to its own cap.
        waited = 0.0
        while waited < SELF_UPDATE_GATEWAY_WAIT_S and not _restart_now.is_set() and (running := gateway_work()):
            if job.get("gateway_stage") != "waiting" or job.get("running_chats") != running:
                job.update(gateway_stage="waiting", running_chats=running)
                _write_self_update(path, job)
            sleep(SELF_UPDATE_CHATS_RECHECK_S)
            waited += SELF_UPDATE_CHATS_RECHECK_S
        # Restarting now, no longer waiting.
        for key in ("gateway_pending", "gateway_stage", "running_chats"):
            job.pop(key, None)
        try:
            _write_self_update(path, job)
        except BaseException:
            # The restart wasn't sent: a failure here must not read as taken down with it.
            job["gateway_pending"] = True
            raise

        def draining() -> None:
            if job.get("gateway_stage") != "draining":
                job.update(gateway_stage="draining")
                job.pop("running_chats", None)
                # A status read misses the stage at worst; the restart goes on.
                with contextlib.suppress(OSError):
                    _write_self_update(path, job)

        # When the gateway's own wait began, for Conduit's "restarts by".
        job["gateway_restart_at"] = time.time()
        try:
            exit_code = restart_gateway(draining)
        except subprocess.TimeoutExpired:
            exit_code = None
        forced = _forced_restart_at
        if exit_code != 0 and forced is not None and forced >= job["gateway_restart_at"]:
            # Restart now forced it through systemd while this restart waited: that restart is the
            # one that happened, and the dashboard goes on as if this one had.
            logger.info("Conduit plugin: the update's gateway restart was forced through systemd")
            exit_code = 0
        for key in ("gateway_stage", "running_chats", "gateway_restart_at"):
            job.pop(key, None)
        if exit_code != 0:
            _self_update_failed(job, "The plugin is updated, but the gateway didn't restart. "
                                     "Run hermes gateway status on the host.")
            return
        if capabilities.LOADED_VERSION is not None and job["version"] == capabilities.LOADED_VERSION:
            # Nothing new on disk for this dashboard to load.
            job.update(step="done", loaded_version=capabilities.LOADED_VERSION, finished_at=time.time())
            return
        waited = 0.0
        while running := _running_turns(os.getpid(), homes(), _own_start_time()):
            if waited >= SELF_UPDATE_CHATS_LIMIT_S:
                _self_update_failed(job, "Chats were still running after two hours. Restart the Hermes "
                                         "dashboard on this host to finish the update", "restart_needed")
                return
            if job.get("running_chats") != running:
                job.update(step="waiting_for_chats", running_chats=running)
                _write_self_update(path, job)
            sleep(SELF_UPDATE_CHATS_RECHECK_S)
            waited += SELF_UPDATE_CHATS_RECHECK_S
        with _self_restart_lock:
            # Taken before the step is written, so a status read never sees
            # the restart step without a restart on its way.
            already = _self_restart_scheduled
            _self_restart_scheduled = True
        claimed_restart = not already
        job.update(step="restarting_dashboard", running_chats=0)
        _write_self_update(path, job)
        if already:
            return
        # It checks again for a turn started meanwhile.
        schedule(SELF_RESTART_DELAY_S, lambda: _restart_when_idle(schedule, homes))
        claimed_restart = False
        return
    except Exception as exc:  # noqa: BLE001 — the job says how it ended, whatever ended it
        if claimed_restart:
            # Nothing was scheduled: a later restart must not read as already on its way.
            with _self_restart_lock:
                _self_restart_scheduled = False
        logger.exception("Conduit plugin: the update on the host failed")
        _self_update_failed(job, f"The update failed on the host ({type(exc).__name__})")
    finally:
        if job["step"] in ("done", "failed"):
            with contextlib.suppress(OSError):
                _write_self_update(path, job)
            notify(job)


def start_self_update(profile: Optional[str], schedule: Callable[[float, Callable[[], None]], Any], *,
                      path: Optional[Path] = None,
                      steps: Optional[Tuple[Callable[[], Dict[str, Any]], Callable[..., int]]] = None,
                      homes: Optional[Callable[[], List[Path]]] = None,
                      notify: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Starts the update on the host, or returns the one still under way."""
    global _self_update_thread, _self_update_job
    refusal = _self_restart_refusal()
    if refusal is not None:
        raise core.TokenError(*refusal)
    pull, restart_gateway = steps or _hermes_update_steps()
    path = path or _self_update_path()
    with _self_update_lock:
        if _self_update_thread is not None and _self_update_thread.is_alive():
            return _read_self_update(path) or dict(_self_update_job or {"id": "", "step": "downloading"})
        # A job whose thread ended can still be under way: the dashboard
        # restart it handed over hasn't happened yet.
        current = self_update_status(path, notify=notify)
        if current is not None and current.get("step") in SELF_UPDATE_RUNNING:
            return current
        mint = _self_update_limiter.acquire("")
        job: Dict[str, Any] = {
            "id": uuid.uuid4().hex, "step": "downloading", "from_version": capabilities.LOADED_VERSION,
            "boot_id": capabilities.BOOT_ID, "pid": os.getpid(), "profile": profile or "", "started_at": time.time(),
        }
        try:
            _write_self_update(path, job)
            # A Restart now from an earlier update isn't this one's.
            _forget_restart_now()
            thread = threading.Thread(
                target=_run_self_update, name="conduit-self-update", daemon=True,
                args=(job, path, pull, restart_gateway, schedule, homes or _hermes_homes, notify or _notify_self_update))
            thread.start()
        except BaseException:
            # Nothing started: the attempt doesn't count against the limit.
            _self_update_limiter.release("", mint)
            raise
        _self_update_job, _self_update_thread = job, thread
        return dict(job)


def self_update_status(path: Optional[Path] = None, now: Optional[float] = None,
                       notify: Optional[Callable[[Dict[str, Any]], None]] = None) -> Optional[Dict[str, Any]]:
    """The latest job, with one this dashboard can no longer finish marked failed."""
    path = path or _self_update_path()
    with _self_update_lock:
        return _self_update_status(path, now, notify or _notify_self_update)


def _job_time(value: Any) -> float:
    """A job file's timestamp; an unreadable one counts as long ago."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _self_update_status(path: Path, now: Optional[float],
                        notify: Callable[[Dict[str, Any]], None]) -> Optional[Dict[str, Any]]:
    job = _read_self_update(path)
    if job is None or job.get("step") not in SELF_UPDATE_RUNNING:
        return job
    alive = _self_update_thread is not None and _self_update_thread.is_alive()
    if alive and job.get("boot_id") == capabilities.BOOT_ID:
        # Its thread is still on it, however long the chats take.
        return job
    if job.get("boot_id") == capabilities.BOOT_ID and job["step"] == "restarting_dashboard" and _self_restart_scheduled:
        # The restart waits for the chats; it finishes the job when it lands.
        return job
    if job.get("boot_id") == capabilities.BOOT_ID and job["step"] != "restarting_dashboard" and not alive:
        _self_update_failed(job, "The update stopped on the host before it finished")
    elif job.get("boot_id") == capabilities.BOOT_ID and job["step"] == "restarting_dashboard" and not _self_restart_scheduled:
        # The re-exec failed (and was logged): the new plugin waits for a restart.
        _self_update_failed(job, "Restart the Hermes dashboard on this host to finish the update",
                            "restart_needed")
    elif (now or time.time()) - _job_time(job.get("updated_at")) > SELF_UPDATE_STALE_S:
        _self_update_failed(job, "The update didn't finish on the host")
    else:
        return job
    with contextlib.suppress(OSError):
        _write_self_update(path, job)
    # Conduit may be closed: the asking profile hears it ended.
    timer = threading.Timer(0, notify, args=(dict(job),))
    timer.daemon = True
    timer.start()
    return job


def _windows_pid_running(pid: int) -> bool:
    """Windows has no signal 0: os.kill(pid, 0) there terminates the process.
    Open it for a query instead; one this process may not open counts as
    running."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process
    try:
        code = wintypes.DWORD()
        return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _another_live_process(pid: Any) -> bool:
    """Whether `pid` is a running process other than this one. The dashboard's
    re-exec keeps its pid; a dashboard restarted any other way has a new one."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 2**31 - 1 or pid == os.getpid():
        return False
    if os.name == "nt":
        return _windows_pid_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _taken_down_by_gateway_restart(job: Dict[str, Any], now: Optional[float] = None) -> bool:
    """A job its dashboard marked failed as it went down with the gateway restart.

    A service manager can restart the dashboard together with the gateway, so
    the restart command it was waiting on dies with it and reads as failed.
    The new plugin is what this process loaded: that update finished.
    """
    return (job.get("step") == "failed" and job.get("failed_step") == "restarting_gateway"
            and not job.get("code") and not job.get("gateway_pending")
            and (now or time.time()) - _job_time(job.get("finished_at")) <= SELF_UPDATE_TAKEN_DOWN_S)


def finish_self_update_after_restart(path: Optional[Path] = None,
                                     notify: Optional[Callable[[Dict[str, Any]], None]] = None,
                                     delay: float = SELF_UPDATE_NOTIFY_DELAY_S) -> Optional[Dict[str, Any]]:
    """In a new dashboard process: ends a job the previous process left running,
    or one it marked failed as the gateway restart took it down."""
    path = path or _self_update_path()
    job = _read_self_update(path)
    if job is None or job.get("boot_id") == capabilities.BOOT_ID:
        return None
    taken_down = _taken_down_by_gateway_restart(job)
    if job.get("step") not in SELF_UPDATE_RUNNING and not (taken_down and capabilities.LOADED_VERSION == job.get("version")):
        return None
    if _another_live_process(job.get("pid")):
        # Another process loading this file (a probe), while the dashboard runs the job.
        return None
    # Pulled (it has a version) and this process loaded it: done, whatever
    # restarted the dashboard (an update's own re-exec or a /self/restart).
    if job.get("gateway_pending"):
        if _self_restart_refusal() is not None:
            # A plugin host (or another process that mustn't restart Hermes) leaves it to the
            # dashboard: a gateway restarted from here would come back with this process's env.
            return None
        # It went down before it restarted the gateway, which still runs the old plugin.
        return _resume_gateway_restart(path, job, notify or _notify_self_update)
    if job.get("version") is not None and capabilities.LOADED_VERSION == job["version"]:
        for key in ("failed_step", "detail", "code", "gateway_stage", "running_chats", "gateway_restart_at"):
            job.pop(key, None)
        job.update(step="done", loaded_version=capabilities.LOADED_VERSION, finished_at=time.time())
    else:
        _self_update_failed(job, "The dashboard restarted before the update finished")
    _write_self_update(path, job)
    timer = threading.Timer(delay, notify or _notify_self_update, args=(dict(job),))
    timer.daemon = True
    timer.start()
    return job


def _resume_gateway_restart(path: Path, job: Dict[str, Any],
                            notify: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Carries on a job whose dashboard restarted before the gateway did (during the wait for
    its running work): this process waits and restarts it. It already runs the new plugin, so
    the job ends there."""
    global _self_update_thread, _self_update_job

    def give_up() -> Dict[str, Any]:
        # At import nobody hears a raise: the job says it at once, and the asking profile hears it.
        logger.warning("Conduit plugin: couldn't carry on the update's gateway restart", exc_info=True)
        _self_update_failed(job, "The dashboard restarted before the gateway did. Restart the gateway "
                                 "on this host (hermes gateway restart) to finish the update")
        with contextlib.suppress(OSError):
            _write_self_update(path, job)
        timer = threading.Timer(0, notify, args=(dict(job),))
        timer.daemon = True
        timer.start()
        return job

    try:
        pull, restart_gateway = _hermes_update_steps()
    except Exception:  # noqa: BLE001
        return give_up()
    # From the pull again (a no-op once pulled), so a failure there reads as the pull's.
    job.update(boot_id=capabilities.BOOT_ID, pid=os.getpid(), step="downloading")

    def schedule(delay: float, fn: Callable[[], None]) -> None:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()

    with _self_update_lock:
        try:
            _write_self_update(path, job)
            # A Restart now from an earlier update isn't this one's.
            _forget_restart_now()
            thread = threading.Thread(target=_run_self_update, name="conduit-self-update", daemon=True,
                                      args=(job, path, pull, restart_gateway, schedule, _hermes_homes, notify))
            thread.start()
        except Exception:  # noqa: BLE001 — a resume that can't begin ends the job, not an hour later
            return give_up()
        _self_update_job, _self_update_thread = job, thread
    return job


_plugin_package_lock = threading.Lock()


def _plugin_client() -> Any:
    """client.py, imported as part of a package of this plugin's own files.

    This module is loaded by path, outside the plugin's package, and client.py
    imports its siblings relatively; the package here holds only what it
    imports, and never runs the plugin's __init__ (which registers hooks).
    """
    name = "conduit_push_dashboard_package"
    with _plugin_package_lock:
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [core._plugin_root()]
            sys.modules[name] = package
        return importlib.import_module(f"{name}.client")


def _notify_self_update(job: Dict[str, Any]) -> None:
    """Tells the profile that asked how the update ended, when it is paired."""
    profile = job.get("profile") or None
    if job.get("step") == "done":
        title = "Conduit plugin updated"
        body = f"Version {job.get('loaded_version') or job.get('version')} is running."
    else:
        title = "Conduit plugin update didn't finish"
        body = str(job.get("detail") or "Open Conduit to see what happened.")
    try:
        with core._profile_scope(profile):
            client = _plugin_client()
            if not client.load_state():
                return
            events = sys.modules[f"{client.__package__}.events"]
            # Per outcome: a failure the restarted dashboard corrects isn't a
            # duplicate of the push the dying one sent.
            event = events.push_event("background_task.finished", identifier=events.event_id("plugin-update", job["id"], job.get("step")),
                                      profile=profile or "default", profile_name=_bot_name(client), title=title, body=body)
            client.send_now(event)
    except Exception as exc:  # noqa: BLE001 — a notification is a courtesy; the job file has the outcome
        logger.warning("Conduit plugin: couldn't send the update notification (%s)", type(exc).__name__)


def _bot_name(client: Any) -> str:
    """The profile's bot name for a push (session_titles.bot_name); "" when it
    can't be read, so the push still goes out with the profile id."""
    try:
        return importlib.import_module(f"{client.__package__}.session_titles").bot_name()
    except Exception:  # noqa: BLE001 — a name is never worth the notification
        logger.debug("Conduit plugin: no bot name for the update notification", exc_info=True)
        return ""


@router.post("/self/update")
async def post_self_update(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    loop = asyncio.get_running_loop()

    def schedule(delay: float, fn: Callable[[], None]) -> None:
        # From the job's thread: the restart runs on the dashboard's own loop.
        loop.call_soon_threadsafe(loop.call_later, delay, fn)

    try:
        return {"ok": True, "job": await asyncio.to_thread(start_self_update, profile, schedule)}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise core._unexpected("update", exc, feature="Update")


@router.get("/self/gateway-work")
async def get_gateway_work(response: Response) -> Dict[str, Any]:
    """What a gateway restart waits on now, named, for the update's "Letting running tasks
    finish" (the host's update and one Conduit runs itself alike)."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, **await asyncio.to_thread(gateway_work_report)}
    except Exception as exc:
        raise core._unexpected("running work", exc, feature="Update")


@router.post("/self/gateway-work/restart-now")
async def post_gateway_restart_now(response: Response, force: bool = False) -> Dict[str, Any]:
    """Restart now: the update stops waiting for the gateway's work and restarts it. Conduit
    stops the chats it names itself; ``force`` restarts the gateway through its service manager
    for work it can't stop (force_gateway_restart)."""
    response.headers["Cache-Control"] = "no-store"
    _restart_now.set()
    if not force:
        return {"ok": True, "forced": False}
    try:
        await asyncio.to_thread(force_gateway_restart)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("forced restart", exc, feature="Update")
    return {"ok": True, "forced": True}


@router.get("/self/update")
async def get_self_update(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"ok": True, "job": await asyncio.to_thread(self_update_status), "boot_id": capabilities.BOOT_ID,
                "loaded_version": capabilities.LOADED_VERSION}
    except Exception as exc:
        raise core._unexpected("update status", exc, feature="Update")


with contextlib.suppress(Exception):
    # A job the previous dashboard process handed over at its restart.
    finish_self_update_after_restart()
