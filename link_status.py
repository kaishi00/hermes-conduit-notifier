"""When Conduit last reached this host, and how its last push went.

The dashboard's Sessions page shows a Conduit card from this (the plugin's
``sessions:top`` slot): Hermes' own Connected Platforms card lists only the
gateway's messaging adapters, and Conduit is a dashboard client, not one.

Two processes write it, per Hermes home, in ``conduit-link-status.json``:
the dashboard notes Conduit's requests and sockets (``app_seen_at``), and the
hooks note each relay delivery (``push_ok_at``, or ``push_failed_at`` with a
short reason). Times are seconds since the epoch. Writes are throttled, so a
busy app or a long turn costs one small write a minute; a push that starts or
stops failing always writes at once. Anything unreadable reads as nothing
recorded, so a fault here only makes the card say less.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

FILE_NAME = "conduit-link-status.json"
# One write a minute per home and kind at most; the card counts in minutes.
WRITE_EVERY_S = 60.0
MAX_REASON_CHARS = 200

_lock = threading.Lock()
# (home, kind) -> when this process last wrote it.
_written: dict[tuple[str, str], float] = {}
# home -> whether the last push this process recorded there worked.
_last_push_ok: dict[str, bool] = {}


def _key(home: Any) -> str:
    return os.path.realpath(str(home))


def _due(key: str, kind: str, now: float, force: bool = False) -> bool:
    with _lock:
        last = _written.get((key, kind))
        if not force and last is not None and 0 <= now - last < WRITE_EVERY_S:
            return False
        _written[(key, kind)] = now
        return True


def app_seen(home: Any, *, now: float | None = None) -> bool:
    """Notes that Conduit reached this host. True when it was written."""
    now = time.time() if now is None else now
    key = _key(home)
    if not _due(key, "app", now):
        return False
    return _merge(key, {"app_seen_at": now}, ("app",))


def push_result(home: Any, ok: bool, reason: str = "", *, now: float | None = None) -> bool:
    """Notes how a relay delivery went. True when it was written."""
    now = time.time() if now is None else now
    key = _key(home)
    with _lock:
        flipped = _last_push_ok.get(key) is not ok
        _last_push_ok[key] = ok
    if not _due(key, "push_ok" if ok else "push_failed", now, force=flipped):
        return False
    if ok:
        return _merge(key, {"push_ok_at": now}, ("push_ok",))
    return _merge(key, {"push_failed_at": now, "push_failed_reason": str(reason or "")[:MAX_REASON_CHARS]},
                  ("push_failed",))


def read(home: Any) -> dict[str, Any]:
    """The recorded times (and the last failure's reason), each only when valid."""
    raw = _load(Path(_key(home)) / FILE_NAME)
    clean: dict[str, Any] = {}
    for field in ("app_seen_at", "push_ok_at", "push_failed_at"):
        if _valid_time(raw.get(field)):
            clean[field] = float(raw[field])
    reason = raw.get("push_failed_reason")
    if "push_failed_at" in clean and isinstance(reason, str):
        clean["push_failed_reason"] = reason[:MAX_REASON_CHARS]
    return clean


def _valid_time(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:  # an int too large for a float
        return False


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serializes the dashboard's and the hooks' writes (flock where there is one)."""
    if fcntl is None:
        yield
        return
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.with_name(f".{path.name}.lock"), flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _merge(key: str, fields: dict[str, Any], kinds: tuple[str, ...]) -> bool:
    path = Path(key) / FILE_NAME
    try:
        with _file_lock(path):
            current = _load(path)
            stamp = next(name for name in fields if name.endswith("_at"))
            if _valid_time(current.get(stamp)) and current[stamp] >= fields[stamp]:
                return False  # a newer one is there already (another process, or an older sighting)
            current.update(fields)
            # A fresh, private (0600) temp file of its own, never one already there.
            descriptor, temporary = tempfile.mkstemp(dir=key, prefix=f".{FILE_NAME}.", suffix=".tmp")
            try:
                try:
                    handle = os.fdopen(descriptor, "w", encoding="utf-8")
                except BaseException:
                    os.close(descriptor)
                    raise
                with handle:
                    json.dump(current, handle, separators=(",", ":"))
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
    except OSError:
        # Let the next event try again instead of waiting out the throttle.
        with _lock:
            for kind in kinds:
                _written.pop((key, kind), None)
        return False
    return True
