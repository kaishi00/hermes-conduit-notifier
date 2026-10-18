"""Cron jobs whose runs don't notify: the user's per-job choice in Conduit's
Cron tab.

A cron run ends like any other turn, so the plugin's turn-end hooks would push
"response ready" (or "turn failed") for every run. The user can turn that off
for one job: its id is kept here, and the hooks skip the push for that job's
runs. A job that isn't listed notifies, so a new job, or a host this file was
never written on, behaves as before.

Hermes owns the jobs themselves (``cron/jobs.json``); this file only holds
the ids the user muted, so it needs no Hermes change and survives job edits.
A deleted job's id just stays until the list is trimmed.

State lives in the profile's ``conduit-cron.json`` beside the pairing state.
The hooks (agent process) and the dashboard routes (dashboard process) both
use this module: the dashboard loads it by path, so it imports nothing from
the plugin package.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-cron.json"
VERSION = 1
# More muted jobs than anyone keeps; the oldest choices go first.
MAX_MUTED = 500

# Hermes mints uuid4().hex[:12] ids; user and legacy ids ("job-1") are real
# too (hermes_state_ids), so allow the same characters.
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_thread_lock = threading.Lock()


def valid_job_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _JOB_ID.match(value) else None


class CronStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def muted(self) -> list[str]:
        """The muted job ids, oldest choice first."""
        return list(self._load()["muted"])

    def is_muted(self, job_id: Any) -> bool:
        clean = valid_job_id(job_id)
        return clean is not None and clean in self._load()["muted"]

    def set_notify(self, job_id: Any, notify: Any) -> list[str]:
        """Turns one job's notifications on or off; returns the muted ids."""
        clean = valid_job_id(job_id)
        if clean is None:
            raise ValueError("job_id must be a cron job id")
        if not isinstance(notify, bool):
            raise ValueError("notify must be true or false")
        with self._locked() as state:
            muted = state["muted"]
            muted.pop(clean, None)
            if not notify:
                muted[clean] = self.clock()
                while len(muted) > MAX_MUTED:
                    muted.pop(next(iter(muted)))
            return list(muted)

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the choices it couldn't read, so it fails instead.
            if for_write:
                raise
            raw = None
        return _normalized(raw)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save: all under the file lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _thread_lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            state = self._load(for_write=True)
            before = _encoded(state)
            yield state
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _normalized(raw: Any) -> dict[str, Any]:
    muted: dict[str, float] = {}
    entries = raw.get("muted") if isinstance(raw, dict) else None
    if isinstance(entries, dict):
        for key, at in entries.items():
            if valid_job_id(key) and isinstance(at, (int, float)) and not isinstance(at, bool):
                muted[key] = float(at)
    # Oldest choice first, so trimming drops the oldest.
    ordered = dict(sorted(muted.items(), key=lambda item: item[1])[-MAX_MUTED:])
    return {"version": VERSION, "muted": ordered}


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":")) + "\n"


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as client.state_file_lock: flock where there is one,
    # a first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):  # about a minute
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
