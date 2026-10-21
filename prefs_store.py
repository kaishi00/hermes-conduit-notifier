"""Conduit's preferences, kept on the host so every iPhone and iPad signed in
to this profile shares them (hermes-conduit#1089).

Conduit decides which preferences it syncs and what each value means: this
file keeps a flat map of key to JSON value with the time the host took it and
the device that sent it. Credentials, sign-in state and per-device consent are
never sent. A change wins over the one before it (last write wins), timed by
this host's clock when the change arrives, so devices with skewed clocks
can't fight.

A key set to null is cleared: the device that sent it went back to Conduit's
default, and the other devices follow.

Moving to another host: ``export()`` writes the whole map as one small JSON
file and ``import_()`` takes it on another host's plugin, either merged
(imported keys win) or replacing what was there. Conduit can also upload its
own copy to a new host from Settings.

State lives in the profile's ``conduit-prefs.json``. The dashboard route
(dashboard process) and the CLI (``hermes conduit-push prefs``) both use this
module: the dashboard loads it by path, so it imports nothing from the plugin
package.
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

FILE_NAME = "conduit-prefs.json"
VERSION = 1
# The export file's marker and shape version.
EXPORT_KIND = "conduit-prefs"

MAX_KEYS = 200
MAX_CHANGES = 200
# One value, encoded: room for a voice settings blob or a list of dashboards.
MAX_VALUE_BYTES = 16 * 1024
# The whole map, encoded.
MAX_TOTAL_BYTES = 512 * 1024
MAX_DEVICE_NAME_CHARS = 60
# 2100-01-01: later (or inf) is a corrupt time.
MAX_AT = 4_102_444_800

_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_INSTALLATION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_thread_lock = threading.Lock()


class PrefsError(ValueError):
    """A request this store won't keep; ``code`` is a short machine word."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PrefsStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def view(self) -> dict[str, Any]:
        """Every synced value, with when the host took it and from which device."""
        return _public(self._load())

    def sync(self, body: Any) -> dict[str, Any]:
        """A device's changes since its last sync (key to value, null clears).
        Returns every value afterwards."""
        if not isinstance(body, dict):
            raise PrefsError("invalid", "Expected a JSON object")
        installation = body.get("installation_id")
        if not isinstance(installation, str) or not _INSTALLATION.match(installation):
            raise PrefsError("invalid", "installation_id must be a plain id")
        changes = _changes(body.get("changes", {}))
        device = _line(body.get("device"), MAX_DEVICE_NAME_CHARS) or "Conduit"
        with self._locked() as state:
            _apply(state, changes, self.clock(), {"id": installation, "name": device})
            return _public(state)

    def export(self) -> dict[str, Any]:
        """The whole map as a file another host's plugin can import."""
        state = self._load()
        return {
            "kind": EXPORT_KIND,
            "version": VERSION,
            "exported_at": self.clock(),
            "values": {key: entry["value"] for key, entry in state["values"].items()},
        }

    def import_(self, document: Any, *, replace: bool = False) -> dict[str, Any]:
        """Takes an export. Imported keys win; with ``replace`` the keys the
        export lacks are dropped too. Returns every value afterwards."""
        if not isinstance(document, dict) or document.get("kind") != EXPORT_KIND:
            raise PrefsError("invalid", "This isn't a Conduit preferences export")
        version = document.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version > VERSION:
            raise PrefsError("invalid", "This export is from a newer plugin; update this one first")
        values = document.get("values")
        if not isinstance(values, dict):
            raise PrefsError("invalid", "The export has no values")
        # An import never clears a key by null: an export lists only set values.
        changes = _changes({key: value for key, value in values.items() if value is not None})
        with self._locked() as state:
            if replace and set(state["values"]) - set(changes):
                state["values"] = {}
                state["revision"] += 1
            _apply(state, changes, self.clock(), {"id": "import", "name": "Import"})
            return _public(state)

    # --- Storage -------------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the values it couldn't read, so it fails instead.
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
            if after != before:
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


# --- Helpers ---------------------------------------------------------------------


def _apply(state: dict[str, Any], changes: dict[str, Any], now: float, device: dict[str, str]) -> None:
    """Writes ``changes`` into ``state`` or raises PrefsError, leaving it as it was."""
    values = dict(state["values"])
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = {"value": value, "at": now, "by": device["id"], "by_name": device["name"]}
    if len(values) > MAX_KEYS:
        raise PrefsError("too_many", f"At most {MAX_KEYS} preferences are kept")
    if len(_encoded({"values": values})) > MAX_TOTAL_BYTES:
        raise PrefsError("too_large", "The preferences are too large to keep")
    state["values"] = values
    if changes:
        state["revision"] += 1


def _changes(value: Any) -> dict[str, Any]:
    """Well-formed changes (key to JSON value or null), or PrefsError."""
    if not isinstance(value, dict) or len(value) > MAX_CHANGES:
        raise PrefsError("invalid", f"changes must be an object of up to {MAX_CHANGES} keys")
    kept: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not _KEY.match(key):
            raise PrefsError("invalid", "Each key is lowercase letters, digits, dots, dashes or underscores")
        if raw is not None:
            try:
                size = len(json.dumps(raw, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, RecursionError):
                raise PrefsError("invalid", f"{key} isn't a JSON value")
            if size > MAX_VALUE_BYTES:
                raise PrefsError("too_large", f"{key} is over {MAX_VALUE_BYTES // 1024} KB")
        kept[key] = raw
    return kept


def _is_json(value: Any) -> bool:
    """A value the routes can send back (a hand-edited NaN can't be)."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _public(state: dict[str, Any]) -> dict[str, Any]:
    return {"revision": state["revision"], "values": dict(state["values"])}


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False) + "\n"


def _line(value: Any, limit: int) -> str:
    """One line of printable text, trimmed; empty when ``value`` isn't text."""
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return text[:limit]


def _normalized(raw: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"version": VERSION, "revision": 0, "values": {}}
    if not isinstance(raw, dict):
        return state
    revision = raw.get("revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        state["revision"] = revision
    values = raw.get("values")
    if isinstance(values, dict):
        for key, entry in values.items():
            if not isinstance(key, str) or not _KEY.match(key) or not isinstance(entry, dict) or "value" not in entry:
                continue
            at = entry.get("at")
            # A time a hand-edited file could make unreadable is dropped.
            if (entry["value"] is None or not isinstance(at, (int, float)) or isinstance(at, bool)
                    or not 0 <= at < MAX_AT or not _is_json(entry["value"])):
                continue
            state["values"][key] = {
                "value": entry["value"],
                "at": float(at),
                "by": _line(entry.get("by"), 64),
                "by_name": _line(entry.get("by_name"), MAX_DEVICE_NAME_CHARS),
            }
    return state


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as settings_store: flock where there is one, a
    # first-byte lock on Windows.
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
