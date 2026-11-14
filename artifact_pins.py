"""Artifacts the user saved in Conduit, kept on the host (hermes-conduit#2467).

Conduit's Artifacts list is read fresh from the profile's chats
(artifacts_index.py), so a file scrolls back under everything made since,
and goes from the list when its chat is deleted or pruned. Saving an
artifact keeps it here instead: its row as the list gave it (name, kind,
the chat it came from), a short note the user can add, and, for a file
still on the host, a copy in the profile's ``conduit-saved-artifacts``
folder, so it still opens after the original file, its chat or its session
is gone. A link keeps its row only: there is no file to copy.

Every iPhone and iPad signed in to the profile reads the same set, which
survives a reinstall. Unsaving removes the row and its copy.

A copy is made only of a regular file up to ``MAX_COPY_BYTES``, while all
the copies together stay under ``MAX_TOTAL_COPY_BYTES``; a file past either
is saved without one (``copy`` says why), and still opens while the
original is there.

State lives in the profile's ``conduit-artifact-pins.json``. The dashboard
loads this module by path, so it imports nothing from the plugin package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-artifact-pins.json"
COPY_DIR = "conduit-saved-artifacts"
VERSION = 1

MAX_PINS = 500
MAX_NOTE_CHARS = 200
MAX_NAME_CHARS = 200
MAX_TITLE_CHARS = 200
MAX_PATH_CHARS = 4_096
MAX_URL_CHARS = 2_048
MAX_ID_CHARS = 200
# One file's copy, and every copy together.
MAX_COPY_BYTES = 200 * 1024 * 1024
MAX_TOTAL_COPY_BYTES = 2 * 1024 * 1024 * 1024
# 2100-01-01: later (or inf) is a corrupt time.
MAX_AT = 4_102_444_800

KINDS = ("image", "video", "audio", "page", "document", "file", "link")
SOURCES = ("delivered", "preview", "written", "attached", "link")

_thread_lock = threading.Lock()
# Bytes being copied right now, by host folder: each copy reserves its size
# before it starts, so saves at once can't pass the budget together.
_reserved_lock = threading.Lock()
_reserved: dict = {}
# A temporary copy older than this was cut short (no copy runs this long).
STALE_COPY_SECONDS = 6 * 3600
# The longest copy name, in bytes, leaving room for the temporary suffix
# under a 255-byte file name limit.
MAX_COPY_NAME_BYTES = 150


class PinError(ValueError):
    """A request this store won't keep; ``code`` is a short machine word."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PinStore:
    """``sensitive`` says whether a path is a secret the Folder won't show
    (``.env``, keys, credentials); such a file is saved without a copy, so
    its contents never land under another name."""

    def __init__(self, home: Path, clock: Callable[[], float] = time.time,
                 sensitive: Callable[[str], bool] = lambda path: False) -> None:
        self.home = Path(home)
        self.path = self.home / FILE_NAME
        self.copies = self.home / COPY_DIR
        self.clock = clock
        self.sensitive = sensitive

    def view(self) -> dict[str, Any]:
        """Every saved artifact, the one saved last first, each saying whether
        its original and its copy are still on the host."""
        pins = self._load()["pins"]
        return {"pins": [self._described(pin) for pin in sorted(pins, key=lambda p: -p["pinned_at"])]}

    def save(self, body: Any) -> dict[str, Any]:
        """Saves the artifact ``body["item"]`` names (its path, or a link's
        URL), with ``body["note"]``; saving one already saved changes only
        its note. Returns the saved row."""
        if not isinstance(body, dict):
            raise PinError("invalid", "The body must be an object")
        item = _item(body.get("item"))
        note = _note(body.get("note"))
        key = _key(item)
        existing = next((pin for pin in self._load()["pins"] if _key(pin) == key), None)
        if existing is not None:
            with self._locked() as state:
                for pin in state["pins"]:
                    if _key(pin) == key:
                        pin["note"] = note
                        return {"pin": self._described(pin)}
        if len(self._load()["pins"]) >= MAX_PINS:
            raise PinError("too_many", f"At most {MAX_PINS} artifacts can be saved")
        pin = {**item, "note": note, "pinned_at": self.clock(), "copy": None, "copy_reason": None}
        if "path" in item:
            pin["copy"], pin["copy_reason"] = self._copy(item["path"], key)
        with self._locked() as state:
            same = next((p for p in state["pins"] if _key(p) == key), None)
            if same is not None:
                # Saved by another device while this one copied.
                self._remove_copy(pin)
                same["note"] = note
                return {"pin": self._described(same)}
            if len(state["pins"]) >= MAX_PINS:
                self._remove_copy(pin)
                raise PinError("too_many", f"At most {MAX_PINS} artifacts can be saved")
            state["pins"].append(pin)
            return {"pin": self._described(pin)}

    def remove(self, key: Any) -> dict[str, Any]:
        """Unsaves the artifact by its path or URL, and removes its copy."""
        if not isinstance(key, str) or not key or len(key) > max(MAX_PATH_CHARS, MAX_URL_CHARS):
            raise PinError("invalid", "id must be the artifact's path or URL")
        removed = None
        with self._locked() as state:
            left = []
            for pin in state["pins"]:
                if _key(pin) == key:
                    removed = pin
                else:
                    left.append(pin)
            state["pins"] = left
        if removed is not None:
            self._remove_copy(removed)
        return {"removed": removed is not None}

    # --- Copies ------------------------------------------------------------------

    def _copy(self, path: str, key: str) -> tuple:
        """The copy's path, or None and why there's none. Each copy has a
        folder of its own, so two saves at once never share one."""
        source = os.path.expanduser(path)
        try:
            real = os.path.realpath(source)
            if self.sensitive(source) or self.sensitive(real):
                return None, "sensitive"
            info = os.stat(source)
        except (OSError, ValueError):
            return None, "missing"
        if not stat.S_ISREG(info.st_mode):
            return None, "not_a_file"
        if info.st_size > MAX_COPY_BYTES:
            return None, "too_large"
        budget = str(self.copies)
        with _reserved_lock:
            if self._copies_size() + _reserved.get(budget, 0) + info.st_size > MAX_TOTAL_COPY_BYTES:
                return None, "no_room"
            _reserved[budget] = _reserved.get(budget, 0) + info.st_size
        folder = self.copies / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}-{uuid.uuid4().hex[:12]}"
        name = _safe_name(re.split(r"[/\\]", path.rstrip("/\\"))[-1])
        target = folder / name
        try:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = folder / f".{name}.tmp"
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            return None, "unreadable"
        finally:
            with _reserved_lock:
                left = _reserved.get(budget, 0) - info.st_size
                if left > 0:
                    _reserved[budget] = left
                else:
                    _reserved.pop(budget, None)
        return str(target), None

    def _copies_size(self) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(self.copies):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        info = os.lstat(path)
                        # A copy a crash cut short: never finished, so it
                        # goes rather than counting against the budget.
                        if name.endswith(".tmp") and self.clock() - info.st_mtime > STALE_COPY_SECONDS:
                            shutil.rmtree(root, ignore_errors=True)
                            continue
                        total += info.st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def _remove_copy(self, pin: dict) -> None:
        copy = pin.get("copy")
        if not copy:
            return
        folder = Path(copy).parent
        # Only a folder this store made.
        if folder.parent == self.copies:
            shutil.rmtree(folder, ignore_errors=True)

    def _described(self, pin: dict) -> dict:
        row = {key: value for key, value in pin.items() if value is not None and key != "copy_reason"}
        row.pop("copy", None)
        if "path" in pin:
            size = _file_size(pin["path"])
            row["exists"] = size is not None
            if size is not None:
                row["size"] = size
            copy = pin.get("copy")
            copy_size = _file_size(copy) if copy else None
            if copy and copy_size is not None:
                row["kept_path"] = copy
                row["kept_size"] = copy_size
            elif pin.get("copy_reason"):
                row["not_kept"] = pin["copy_reason"]
            else:
                row["not_kept"] = "missing"
        return row

    # --- The file ----------------------------------------------------------------

    def _load(self, *, for_write: bool = False) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, RecursionError):
            raw = None
        except OSError:
            # Unreadable for now (not missing, not corrupt): a write would
            # replace the rows it couldn't read, so it fails instead.
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


def _key(pin: dict) -> str:
    """A link, or a picture on the web, by its URL; a file by its path."""
    return pin["url"] if pin["source"] == "link" else pin["path"]


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _note(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PinError("invalid", "note must be text")
    text = " ".join(value.split())
    if len(text) > MAX_NOTE_CHARS:
        raise PinError("invalid", f"A note is at most {MAX_NOTE_CHARS} characters")
    return text or None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0 <= value <= MAX_AT else None


def _item(raw: Any) -> dict:
    """The artifact row to keep, as Conduit's list gave it, or PinError."""
    if not isinstance(raw, dict):
        raise PinError("invalid", "item must be the artifact's row")
    kind, source = raw.get("kind"), raw.get("source")
    if kind not in KINDS or source not in SOURCES:
        raise PinError("invalid", "item has no artifact kind or source")
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > MAX_ID_CHARS:
        raise PinError("invalid", "item has no chat")
    message_id = raw.get("message_id")
    timestamp = _number(raw.get("timestamp"))
    if isinstance(message_id, bool) or not isinstance(message_id, int) or timestamp is None:
        raise PinError("invalid", "item has no message")
    item: dict = {
        "kind": kind,
        "source": source,
        "session_id": session_id,
        "session_title": _clip(raw.get("session_title"), MAX_TITLE_CHARS),
        "message_id": message_id,
        "timestamp": timestamp,
    }
    if source == "link":
        # A web link, or a picture a tool gave by URL (kind image).
        url = raw.get("url")
        if not isinstance(url, str) or len(url) > MAX_URL_CHARS:
            raise PinError("invalid", "A link needs its URL")
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            raise PinError("invalid", "A link needs its URL")
        if parsed.scheme not in ("http", "https") or not host or "@" in parsed.netloc:
            raise PinError("invalid", "A link needs its web address")
        item["url"] = url
        item["host"] = host
        item["name"] = _clip(raw.get("name") or host, MAX_NAME_CHARS)
        return item
    path = raw.get("path")
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or "\x00" in path:
        raise PinError("invalid", "A file needs its path")
    if not (path.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:[/\\]", path)):
        raise PinError("invalid", "A file's path must be absolute")
    item["path"] = path
    item["name"] = _clip(raw.get("name") or re.split(r"[/\\]", path.rstrip("/\\"))[-1], MAX_NAME_CHARS)
    return item


def _safe_name(name: str) -> str:
    """A file name safe on every host, its extension kept, within
    ``MAX_COPY_NAME_BYTES`` encoded."""
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", name).strip(" ") or "file"
    if name.startswith("."):
        name = "_" + name
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext.encode("utf-8")) > 20:
        stem, ext = name, ""
    room = MAX_COPY_NAME_BYTES - (len(ext.encode("utf-8")) + 1 if ext else 0)
    stem = stem.encode("utf-8")[:room].decode("utf-8", "ignore") or "file"
    return f"{stem}.{ext}" if ext else stem


def _file_size(path: str) -> Optional[int]:
    try:
        info = os.stat(os.path.expanduser(path))
    except (OSError, ValueError):
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _normalized(raw: Any) -> dict[str, Any]:
    pins = []
    seen = set()
    for value in (raw or {}).get("pins", []) if isinstance(raw, dict) else []:
        try:
            pin = _item(value)
        except PinError:
            continue
        key = _key(pin)
        if key in seen:
            continue
        seen.add(key)
        pin["note"] = value.get("note") if isinstance(value.get("note"), str) else None
        pin["pinned_at"] = _number(value.get("pinned_at")) or 0.0
        copy = value.get("copy")
        pin["copy"] = copy if isinstance(copy, str) and copy else None
        reason = value.get("copy_reason")
        pin["copy_reason"] = reason if isinstance(reason, str) else None
        pins.append(pin)
    return {"version": VERSION, "pins": pins}


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as prefs_store: flock where there is one, a
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
