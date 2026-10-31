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


def _hermes_update_steps() -> Tuple[Callable[[], Dict[str, Any]], Callable[[], int]]:
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
        return dashboard_update_user_plugin(core.PLUGIN_NAME)

    def restart_gateway() -> int:
        proc, _reused = _spawn_gateway_restart(None, **restart_kwargs)
        try:
            return proc.wait(timeout=SELF_UPDATE_GATEWAY_LIMIT_S)
        except subprocess.TimeoutExpired:
            # Failed is failed: a restart finishing later mustn't contradict the job.
            with contextlib.suppress(OSError):
                proc.kill()
            raise

    return pull, restart_gateway


def _self_update_failed(job: Dict[str, Any], detail: str, code: Optional[str] = None) -> None:
    # failed_step says how far it got: Conduit runs the steps itself only
    # when the pull itself failed.
    job.update(failed_step=job.get("step"), step="failed", detail=detail, finished_at=time.time())
    if code:
        job["code"] = code


def _run_self_update(job: Dict[str, Any], path: Path, pull: Callable[[], Dict[str, Any]],
                     restart_gateway: Callable[[], int], schedule: Callable[[float, Callable[[], None]], Any],
                     homes: Callable[[], List[Path]], notify: Callable[[Dict[str, Any]], None],
                     sleep: Callable[[float], None] = time.sleep,
                     point_profiles: Callable[[], Any] = lambda: _point_profiles()) -> None:
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
        # Before the restart, so every profile's turns load the new version with it.
        point_profiles()
        _write_self_update(path, job)
        try:
            exit_code = restart_gateway()
        except subprocess.TimeoutExpired:
            exit_code = None
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
                      steps: Optional[Tuple[Callable[[], Dict[str, Any]], Callable[[], int]]] = None,
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
            and not job.get("code")
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
    if job.get("version") is not None and capabilities.LOADED_VERSION == job["version"]:
        for key in ("failed_step", "detail", "code"):
            job.pop(key, None)
        job.update(step="done", loaded_version=capabilities.LOADED_VERSION, finished_at=time.time())
    else:
        _self_update_failed(job, "The dashboard restarted before the update finished")
    _write_self_update(path, job)
    timer = threading.Timer(delay, notify or _notify_self_update, args=(dict(job),))
    timer.daemon = True
    timer.start()
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
