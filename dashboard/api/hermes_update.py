"""Update Hermes from Conduit: `hermes update` outlives the dashboard that
started it.

Conduit's Update Hermes (phone and desktop) asks Hermes' own dashboard to run
it (`POST /api/hermes/update`), and Hermes starts it as a child of the
dashboard (``web_server_gateway._spawn_hermes_action``, a new session but the
same cgroup). Under systemd that child dies with the dashboard's unit: the
update stops and restarts the gateway and dashboard units, and a dashboard
unit that requires the gateway is stopped with it. On a real host
(2026-11-20) the update was killed at "Waiting for in-flight turns", before
any code changed, with both units stopped on purpose, so nothing brought
Hermes back until someone started them on the host.

So in the dashboard's process this puts that one command, `hermes update`
spawned by that function, in a transient systemd scope of its own
(`systemd-run --scope`): the same process, log and exit code, outside the
dashboard's cgroup. Only when the dashboard runs in a systemd service, systemd
answers a probe scope, and the scope can be made (root, or a user manager for
the dashboard's user); otherwise Hermes spawns it as before.
"""

from __future__ import annotations

import functools
import itertools
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, List, Optional

from . import core

logger = core.logger

_GUARD_FLAG = "_conduit_update_scope"
_SPAWNER = "_spawn_hermes_action"
PROBE_TIMEOUT_S = 5
_units = itertools.count(1)
_lock = threading.Lock()
_wrapped: Optional[tuple] = None  # (module, Hermes' own runtime_command, our wrapper)


def _in_service_cgroup(path: str = "/proc/self/cgroup") -> bool:
    """Whether this process runs in a systemd service's cgroup (a system or
    user unit): the cgroup its children die with."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return False
    return any(part.endswith(".service") for line in lines for part in line.rsplit(":", 1)[-1].split("/"))


def _manager_args() -> Optional[List[str]]:
    """How to reach a systemd manager that can make a scope: the system one as
    root, else the dashboard user's own, when it has one."""
    if os.geteuid() == 0:
        return []
    runtime = f"/run/user/{os.getuid()}"
    if os.path.exists(f"{runtime}/bus"):
        # A system unit's environment often lacks it, and systemd-run --user needs it.
        return ["env", f"XDG_RUNTIME_DIR={runtime}"]
    return None


def scope_prefix() -> Optional[List[str]]:
    """The words that run a command in a transient scope of its own, or None
    when it should run as Hermes starts it."""
    if not sys.platform.startswith("linux") or not _in_service_cgroup():
        return None
    systemd_run = shutil.which("systemd-run")
    manager = _manager_args()
    if not systemd_run or manager is None:
        return None
    base = [*manager, systemd_run, *(["--user"] if manager else []), "--scope", "--quiet", "--collect"]
    try:
        # A probe, so a systemd that refuses scopes never costs the update itself.
        probe = subprocess.run([*base, "--", "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.info("conduit_push: systemd-run didn't answer; hermes update runs in the dashboard's cgroup")
        return None
    if probe.returncode != 0:
        logger.info("conduit_push: systemd-run refused a scope (%s); hermes update runs in the dashboard's cgroup",
                    probe.returncode)
        return None
    unit = f"hermes-update-{int(time.time())}-{os.getpid()}-{next(_units)}"
    return [*base, f"--unit={unit}", "--"]


def _is_update(args: Any) -> bool:
    try:
        return list(args) == ["update"]
    except TypeError:
        return False


def _scoped(original: Callable[..., List[str]]) -> Callable[..., List[str]]:
    def runtime_command(repo_root: Any, args: Any = (), *rest: Any, **kwargs: Any) -> List[str]:
        command = original(repo_root, args, *rest, **kwargs)
        try:
            caller = sys._getframe(1).f_code.co_name
            if caller != _SPAWNER or rest or kwargs or not _is_update(args):
                return command
            prefix = scope_prefix()
            if prefix is None:
                return command
            logger.info("conduit_push: hermes update runs in its own systemd scope (%s)", prefix[-2])
            return [*prefix, *command]
        except Exception:  # noqa: BLE001 — the update runs as Hermes starts it
            logger.debug("conduit_push: couldn't scope hermes update", exc_info=True)
            return command

    functools.update_wrapper(runtime_command, original)
    setattr(runtime_command, _GUARD_FLAG, True)
    return runtime_command


def install() -> bool:
    """Wraps Hermes' launcher command, once per process. False when this
    Hermes has none to wrap."""
    global _wrapped
    with _lock:
        if _wrapped is not None:
            return True
        try:
            from hermes_cli import _launchers
        except ImportError:
            logger.debug("conduit_push: no hermes_cli._launchers; hermes update runs as Hermes starts it")
            return False
        own = getattr(_launchers, "runtime_command", None)
        # A wrapper an earlier load of the plugin left behind wraps Hermes' own.
        while callable(own) and getattr(own, _GUARD_FLAG, False):
            own = getattr(own, "__wrapped__", None)
        if not callable(own):
            logger.debug("conduit_push: Hermes has no runtime_command; hermes update runs as Hermes starts it")
            return False
        wrapper = _scoped(own)
        _launchers.runtime_command = wrapper
        _wrapped = (_launchers, own, wrapper)
        return True


def uninstall() -> None:
    """Puts Hermes' own function back. A wrapper a later load put on is left
    to that load."""
    global _wrapped
    with _lock:
        wrapped, _wrapped = _wrapped, None
        if wrapped is not None and getattr(wrapped[0], "runtime_command", None) is wrapped[2]:
            wrapped[0].runtime_command = wrapped[1]


if "hermes_cli.web_server" in sys.modules:
    # Loaded by the dashboard (not a test or the CLI).
    try:
        install()
    except Exception:  # noqa: BLE001 — hermes update runs as Hermes starts it
        logger.debug("conduit_push: couldn't wrap Hermes' launcher for hermes update", exc_info=True)
