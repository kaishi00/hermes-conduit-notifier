"""Starts the Hermes dashboard again on Windows, once the one it replaces has exited.

Windows can't replace a running process with a new program (os.execv there
starts a second process and ends the first), so the dashboard's self restart
(dashboard/api/self_update.py, issue notifier 774) hands its command line to
this script and exits. The script runs on its own, detached from the
dashboard and whatever started it (a terminal, a Scheduled Task):

    python -I windows_relaunch.py <pid> <command as JSON> <log file>

It waits for that pid to exit, so the new dashboard can take its port, and
starts the command in the background, its output going to the log file. A
dashboard something else started again in the meantime (a launcher script that
restarts it in a loop) is left to run, so two never fight over the port.

Standard library only (psutil, a Hermes dependency, when it's there): Hermes
runs it with -I, so nothing from the working directory loads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, List, Optional

EXIT_WAIT_S = 120.0
# How long a supervisor gets to start the dashboard again itself.
OTHER_START_GRACE_S = 3.0

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# Detached from the dashboard's console and its Ctrl+C, with a hidden console of its own that its
# children inherit (so nothing flashes), and out of the job object a Scheduled Task or a terminal
# puts its processes in, when that job allows it.
DETACHED = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
DETACHED_BREAKAWAY = DETACHED | CREATE_BREAKAWAY_FROM_JOB


def wait_for_exit(pid: int, timeout: float = EXIT_WAIT_S) -> bool:
    """Whether `pid` exited within `timeout`. One that can't be opened has already exited."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0  # WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def _same_command(cmdline: List[str], command: List[str]) -> bool:
    """Whether `cmdline` ends with what `command` runs: the interpreter, and anything a launcher
    puts before the dashboard's own arguments (a flag, its own name), can differ."""
    tail = [os.path.normcase(a) for a in command[1:]]
    return bool(tail) and len(cmdline) >= len(tail) and \
        [os.path.normcase(a) for a in cmdline[-len(tail):]] == tail


def started_by_someone_else(command: List[str], own_pid: Optional[int] = None) -> bool:
    """Whether another process now runs this command: the dashboard, started again by its supervisor."""
    try:
        import psutil
    except ImportError:
        print("windows_relaunch: no psutil, so a dashboard started again by something else can't be seen",
              file=sys.stderr)
        return False
    own_pid = os.getpid() if own_pid is None else own_pid
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or []
            if process.info.get("pid") != own_pid and _same_command(list(cmdline), command):
                return True
        except Exception:  # noqa: BLE001 — a process that went away or can't be read isn't it
            continue
    return False


def _open_log(log_path: str) -> Any:
    """The restart log, or nowhere when it can't be opened: the dashboard starts either way."""
    log = None
    try:
        log = open(log_path, "ab", buffering=0)
        log.write(f"\n=== Conduit plugin: dashboard restarted {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        return log
    except OSError:
        if log is not None:
            log.close()
        return open(os.devnull, "wb")


def start(command: List[str], log_path: str, popen: Callable[..., Any] = subprocess.Popen) -> int:
    """Starts `command` in the background, its output appended to `log_path`; returns its pid."""
    with _open_log(log_path) as log:
        kwargs = dict(cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
        try:
            return popen(command, creationflags=DETACHED_BREAKAWAY, **kwargs).pid
        except OSError:
            # A job that forbids breakaway refuses the flag; the hidden console alone still detaches.
            return popen(command, creationflags=DETACHED, **kwargs).pid


def main(argv: List[str]) -> int:
    pid, command, log_path = int(argv[1]), json.loads(argv[2]), argv[3]
    if not wait_for_exit(pid):
        return 1
    deadline = time.monotonic() + OTHER_START_GRACE_S
    while time.monotonic() < deadline:
        if started_by_someone_else(command):
            return 0
        time.sleep(0.5)
    start(command, log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
