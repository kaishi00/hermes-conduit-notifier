"""Room files, room pictures and the room planner."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import contextlib
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core, linked_rooms, pairing

logger = core.logger
router = core.router


# --- Room files ----------------------------------------------------------------
#
# A group chat room isn't a session, so Hermes' attach RPCs (file.attach and
# friends), which stage into a session's workspace, can't take a file for it.
# Conduit uploads the file here instead and puts an `@file:` reference to the
# stored path in the room message; the room's members run on this host and read
# it from there. Files live under <hermes home>/conduit/room-files/<id>/ and are
# swept a month after their upload.

ROOM_FILE_MAX_BYTES = 50 * 1024 * 1024
# base64 grows a file by a third; the rest is the JSON around it.
ROOM_FILE_MAX_BODY_BYTES = ROOM_FILE_MAX_BYTES * 4 // 3 + 64 * 1024
ROOM_FILE_RETENTION_S = 30 * 24 * 3600
# All uploads together; the oldest go first past it.
ROOM_FILE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
# Never fill the disk: an upload leaves at least this much free.
ROOM_FILE_FREE_FLOOR_BYTES = 1024 * 1024 * 1024
ROOM_FILE_SWEEP_INTERVAL_S = 3600.0
ROOM_FILE_LIMIT = 60
ROOM_FILE_WINDOW_S = 600.0
_room_file_limiter = core._MintLimiter(ROOM_FILE_LIMIT, ROOM_FILE_WINDOW_S,
                                  message="Too many file uploads; try again shortly")
_ROOM_FILE_UNSAFE = re.compile(r"[\\/\x00-\x1f\x7f\x85\u2028\u2029:*?\"'`<>|\u200b-\u200f\u202a-\u202e\u2066-\u2069]+")
# Filesystems cap one path component at 255 bytes; leave room for UTF-8.
ROOM_FILE_NAME_MAX_BYTES = 180


def _room_file_name(raw: Any) -> str:
    """The upload's own name, made safe to use as one path component."""
    # Only the first part of an absurd name is worth looking at.
    raw = raw if isinstance(raw, str) else ""
    # Lone surrogates (valid JSON escapes) can't be encoded: drop them.
    raw = raw[:1024].encode("utf-8", "ignore").decode("utf-8")
    name = _ROOM_FILE_UNSAFE.sub("_", raw.strip()).strip(" .")
    if not name:
        return "file"
    if len(name.encode("utf-8")) <= ROOM_FILE_NAME_MAX_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    if not (dot and stem and 0 < len(ext.encode("utf-8")) <= 16):
        stem, ext = name, ""
    suffix = "." + ext if ext else ""
    budget = ROOM_FILE_NAME_MAX_BYTES - len(suffix.encode("utf-8"))
    # Cut whole characters, never a UTF-8 sequence in half.
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return stem + suffix


def _room_files_root() -> str:
    from hermes_constants import get_hermes_home

    return os.path.join(str(get_hermes_home()), "conduit", "room-files")


_room_file_last_sweep: Dict[str, float] = {}
# Uploads wait on one another (the lock below), so they get their own
# threads rather than the default pool other routes share.
_room_file_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-room-files")
# Sweeps, the free-space check and the write run one at a time, so two
# uploads can't both pass the check and then fill the disk together.
_room_file_lock = threading.Lock()


def _sweep_room_files(root: str, now: float, incoming: int = 0) -> None:
    """Deletes uploads past retention, then the oldest until the rest and
    ``incoming`` fit the total cap. Runs at most hourly per root, and again
    whenever the free-disk floor would be passed, so the cap can be overrun
    for up to an hour; the floor guards the disk at all times."""
    try:
        with os.scandir(root) as it:
            entries = [entry for entry in it if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return
    folders = []
    for entry in entries:
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
            if now - mtime > ROOM_FILE_RETENTION_S:
                shutil.rmtree(entry.path, ignore_errors=True)
                continue
            with os.scandir(entry.path) as files:
                size = sum(f.stat(follow_symlinks=False).st_size for f in files
                           if f.is_file(follow_symlinks=False))
            folders.append((mtime, size, entry.path))
        except OSError:
            continue
    total = sum(size for _, size, _ in folders) + incoming
    for _, size, path in sorted(folders):
        if total <= ROOM_FILE_TOTAL_BYTES:
            break
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            total -= size
    _room_file_last_sweep[root] = now


def _room_file_free(root: str) -> int:
    """Free bytes on the room-files disk; an unreadable disk counts as full."""
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return 0


def store_room_file(root: str, body: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Writes one uploaded file under ``root``; returns where it went and the
    `@file:` reference Hermes reads."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers={"Cache-Control": "no-store"})
    encoded = body.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise HTTPException(status_code=400, detail="data_base64 is required", headers={"Cache-Control": "no-store"})
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="data_base64 is not base64", headers={"Cache-Control": "no-store"})
    if len(data) > ROOM_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The file is too large", headers={"Cache-Control": "no-store"})
    now = time.time() if now is None else now
    name = _room_file_name(body.get("name"))
    with _room_file_lock:
        if now - _room_file_last_sweep.get(root, float("-inf")) >= ROOM_FILE_SWEEP_INTERVAL_S:
            _sweep_room_files(root, now, len(data))
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise HTTPException(status_code=507, detail="The host is low on disk space",
                                    headers={"Cache-Control": "no-store"})
            raise
        try:
            # makedirs leaves an existing folder's mode alone.
            os.chmod(root, 0o700)
        except OSError:
            pass
        if _room_file_free(root) - len(data) < ROOM_FILE_FREE_FLOOR_BYTES:
            # The cap may be what's full: make room once more before
            # refusing, unless the hourly sweep just did.
            if _room_file_last_sweep.get(root) != now:
                _sweep_room_files(root, now, len(data))
            if _room_file_free(root) - len(data) < ROOM_FILE_FREE_FLOOR_BYTES:
                raise HTTPException(status_code=507, detail="The host is low on disk space",
                                    headers={"Cache-Control": "no-store"})
        folder = os.path.join(root, time.strftime("%Y%m%d", time.gmtime(now)) + "-" + uuid.uuid4().hex)
        path = os.path.join(folder, name)
        try:
            os.makedirs(folder, mode=0o700, exist_ok=False)
            # Readable by the host's own user only, whatever the umask.
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(data)
        except OSError as exc:
            # A folder that was already there is another upload's: keep it.
            if exc.errno != errno.EEXIST:
                shutil.rmtree(folder, ignore_errors=True)
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise HTTPException(status_code=507, detail="The host is low on disk space",
                                    headers={"Cache-Control": "no-store"})
            raise
    return {"path": path, "name": name, "size": len(data), "ref_text": "@file:" + _room_file_reference(path)}


def _room_file_reference(path: str) -> str:
    """Quotes a path holding whitespace, brackets or quotes, as Hermes'
    format_reference_value does, so the reference reads back whole."""
    if not re.search(r"[\s\[\](){}'\"`]", path):
        return path
    for quote in ("`", '"', "'"):
        if quote not in path:
            return quote + path + quote
    return path


@router.post("/room-files")
async def post_room_file(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    key = core._limiter_key(profile)
    try:
        mint = _room_file_limiter.acquire(key)
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    try:
        body = await core._read_json_body(request, ROOM_FILE_MAX_BODY_BYTES)
        stored = await core._run_scoped(profile, lambda: store_room_file(_room_files_root(), body), _room_file_executor)
    except core.TokenError as exc:
        # The profile couldn't be scoped; nothing was stored.
        _room_file_limiter.release(key, mint)
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except HTTPException as exc:
        # Refused before anything was stored (507 included): no slot spent.
        if exc.status_code < 500 or exc.status_code == 507:
            _room_file_limiter.release(key, mint)
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        # Nothing was stored: a failed upload costs no slot.
        _room_file_limiter.release(key, mint)
        raise core._unexpected("upload", exc, feature="Room files")
    return {"ok": True, **stored}


# --- Room pictures -------------------------------------------------------------
#
# Hermes' gateway-hosted rooms (groups.*) carry no picture, so Conduit keeps a
# room's custom picture here, under <hermes home>/conduit/room-images/: per
# room, a small `<hash>.json` record (room id, revision, type) and the
# picture's bytes in `<hash>.img`, named by a hash of the room id. GET
# /room-images lists each room's revision from the records alone, so Conduit
# fetches only pictures it hasn't cached; GET /room-images/{room_id} returns
# one; PUT sets it and DELETE clears it. Conduit sends a downscaled JPEG or
# PNG. Anyone who can reach these routes is signed in to the dashboard, the
# same trust every other route here assumes.

ROOM_IMAGE_MAX_BYTES = 512 * 1024
ROOM_IMAGE_MAX_BODY_BYTES = ROOM_IMAGE_MAX_BYTES * 4 // 3 + 16 * 1024
# Records beyond this many are never listed; the oldest go first on a write.
ROOM_IMAGE_MAX_ROOMS = 500
# A record is a few hundred bytes; anything larger isn't one.
ROOM_IMAGE_RECORD_MAX_BYTES = 4096
# Temp files a crashed write left behind go after this long.
ROOM_IMAGE_STALE_TEMP_S = 3600.0
# Taken by every process that reads or writes the folder's pictures.
ROOM_IMAGE_LOCK_FILE = ".room-images.lock"
ROOM_IMAGE_LIMIT = 60
ROOM_IMAGE_WINDOW_S = 600.0
_room_image_limiter = core._MintLimiter(ROOM_IMAGE_LIMIT, ROOM_IMAGE_WINDOW_S,
                                   message="Too many picture changes; try again shortly")
_ROOM_IMAGE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}\Z")
_room_image_lock = threading.Lock()
# Writes wait on one another (the locks below), so they get their own threads
# rather than the default pool other routes share.
_room_image_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-room-images")


def _room_images_root() -> str:
    from hermes_constants import get_hermes_home

    return os.path.join(str(get_hermes_home()), "conduit", "room-images")


def _room_image_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _ROOM_IMAGE_ID.match(raw):
        raise HTTPException(status_code=400, detail="Invalid room id", headers={"Cache-Control": "no-store"})
    return raw


def _room_image_stem(root: str, room_id: str) -> str:
    return os.path.join(root, hashlib.sha256(room_id.encode("utf-8")).hexdigest())


def _room_image_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _read_no_follow(path: str, limit: int) -> Optional[bytes]:
    """A regular file's bytes up to ``limit``, never through a symlink
    (where the platform has O_NOFOLLOW); None when missing, larger, or not
    a plain file."""
    try:
        # Non-blocking, so a FIFO planted in its place can't hang the read.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
        except OSError:
            return None
        data = handle.read(limit + 1)
    return data if len(data) <= limit else None


def _read_room_record(path: str) -> Optional[Dict[str, Any]]:
    raw = _read_no_follow(path, ROOM_IMAGE_RECORD_MAX_BYTES)
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("room_id"), str) \
            or not isinstance(record.get("rev"), str) \
            or record.get("mime", "image/jpeg") not in ("image/jpeg", "image/png"):
        return None
    return record


@contextlib.contextmanager
def _room_images_locked(root: str, *, create: bool = False):
    """A picture's bytes and its record change and are read as a pair: under
    a lock file in the folder, which every process and thread takes through
    its own handle, then this process's lock. Without ``create``, a folder
    that isn't there (or a lock file that can't be opened) has nothing to
    pair, so only the thread lock is taken."""
    if create:
        os.makedirs(root, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            # makedirs leaves an existing folder's mode alone.
            os.chmod(root, 0o700)
    lock_path = os.path.join(root, ROOM_IMAGE_LOCK_FILE)
    try:
        handle = pairing._open_private_lock_file(lock_path)
    except OSError:
        if create:
            raise
        with _room_image_lock:
            yield
        return
    with handle, pairing._locked_handle(handle), _room_image_lock:
        yield


def _write_atomic(root: str, path: str, data: bytes) -> None:
    # A fresh, exclusively created 0600 name, so nothing planted in the
    # folder can redirect the write.
    fd, temp = tempfile.mkstemp(dir=root, prefix=".room-image.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def list_room_images(root: str) -> Dict[str, str]:
    """Each stored room's picture revision, keyed by room id."""
    try:
        with os.scandir(root) as it:
            names = sorted(entry.path for entry in it
                           if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False))
    except OSError:
        return {}
    revisions: Dict[str, str] = {}
    for path in names[:ROOM_IMAGE_MAX_ROOMS]:
        record = _read_room_record(path)
        if record is not None:
            revisions[record["room_id"]] = record["rev"]
    return revisions


def read_room_image(root: str, room_id: str) -> Optional[Dict[str, Any]]:
    stem = _room_image_stem(root, room_id)
    # Under the write lock, so a record and its bytes are read as a pair.
    with _room_images_locked(root):
        record = _read_room_record(stem + ".json")
        if record is None or record["room_id"] != room_id:
            return None
        data = _read_no_follow(stem + ".img", ROOM_IMAGE_MAX_BYTES)
    if not data:
        return None
    return {"room_id": room_id, "rev": record["rev"], "mime": record.get("mime") or "image/jpeg",
            "data_base64": base64.b64encode(data).decode("ascii")}


def _remove_room_image_files(stem: str) -> None:
    for suffix in (".json", ".img"):
        with contextlib.suppress(OSError):
            os.unlink(stem + suffix)


def _prune_room_images(root: str, keep: str, now: float) -> None:
    """Drops the oldest rooms past the room cap (never ``keep``), and temp
    files a crashed write left behind."""
    records = []
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    names = {entry.name for entry in entries}
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        # Leftovers of a crashed write: a temp file, or bytes whose record
        # was never written.
        leftover = (entry.name.startswith(".room-image.") and entry.name.endswith(".tmp")) \
            or (entry.name.endswith(".img") and entry.name[:-len(".img")] + ".json" not in names)
        if leftover:
            if now - mtime > ROOM_IMAGE_STALE_TEMP_S:
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
        elif entry.name.endswith(".json"):
            records.append((mtime, entry.path[:-len(".json")]))
    excess = len(records) - ROOM_IMAGE_MAX_ROOMS
    for _, stem in sorted(records):
        if excess <= 0:
            break
        if stem == keep:
            continue
        _remove_room_image_files(stem)
        excess -= 1


def store_room_image(root: str, room_id: str, body: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Sets ``room_id``'s picture from ``body["data_base64"]``; returns its revision."""
    no_store = {"Cache-Control": "no-store"}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object", headers=no_store)
    encoded = body.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise HTTPException(status_code=400, detail="data_base64 is required", headers=no_store)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="data_base64 is not base64", headers=no_store)
    if len(data) > ROOM_IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The picture is too large", headers=no_store)
    mime = _room_image_mime(data)
    if mime is None:
        raise HTTPException(status_code=415, detail="The picture must be a JPEG or PNG", headers=no_store)
    now = time.time() if now is None else now
    rev = f"{int(now * 1000)}-{hashlib.sha256(data).hexdigest()[:12]}"
    record = json.dumps({"version": 1, "room_id": room_id, "rev": rev, "mime": mime},
                        separators=(",", ":")).encode("utf-8")
    stem = _room_image_stem(root, room_id)
    try:
        with _room_images_locked(root, create=True):
            # Bytes first: a record always names a picture that's there.
            _write_atomic(root, stem + ".img", data)
            try:
                _write_atomic(root, stem + ".json", record)
            except OSError:
                # The old record would now label the new bytes with its
                # revision: drop the room instead, so clients refetch.
                _remove_room_image_files(stem)
                raise
            _prune_room_images(root, stem, now)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(status_code=507, detail="The host is low on disk space", headers=no_store)
        raise
    return {"room_id": room_id, "rev": rev, "mime": mime}


def clear_room_image(root: str, room_id: str) -> bool:
    stem = _room_image_stem(root, room_id)
    with _room_images_locked(root):
        record = _read_room_record(stem + ".json")
        if record is None or record["room_id"] != room_id:
            return False
        _remove_room_image_files(stem)
    return True


@router.get("/room-images")
async def get_room_images(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        revisions = await asyncio.get_running_loop().run_in_executor(
            None, lambda: list_room_images(_room_images_root()))
    except Exception as exc:
        raise core._unexpected("list", exc, feature="Room pictures")
    return {"ok": True, "revisions": revisions}


@router.get("/room-images/{room_id}")
async def get_room_image(room_id: str, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    room_id = _room_image_id(room_id)
    try:
        record = await asyncio.get_running_loop().run_in_executor(
            None, lambda: read_room_image(_room_images_root(), room_id))
    except Exception as exc:
        raise core._unexpected("read", exc, feature="Room pictures")
    if record is None:
        raise HTTPException(status_code=404, detail="No picture for this room", headers={"Cache-Control": "no-store"})
    return {"ok": True, **record}


@router.put("/room-images/{room_id}")
async def put_room_image(room_id: str, request: Request, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    room_id = _room_image_id(room_id)
    # Pictures belong to the host, not a profile: one window for everyone.
    try:
        mint = _room_image_limiter.acquire("")
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    try:
        body = await core._read_json_body(request, ROOM_IMAGE_MAX_BODY_BYTES)
        stored = await asyncio.get_running_loop().run_in_executor(
            _room_image_executor, lambda: store_room_image(_room_images_root(), room_id, body))
    except HTTPException as exc:
        if exc.status_code < 500 or exc.status_code == 507:
            _room_image_limiter.release("", mint)
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    except Exception as exc:
        _room_image_limiter.release("", mint)
        raise core._unexpected("save", exc, feature="Room pictures")
    return {"ok": True, **stored}


@router.delete("/room-images/{room_id}")
async def delete_room_image(room_id: str, response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    room_id = _room_image_id(room_id)
    try:
        removed = await asyncio.get_running_loop().run_in_executor(
            None, lambda: clear_room_image(_room_images_root(), room_id))
    except Exception as exc:
        raise core._unexpected("clear", exc, feature="Room pictures")
    return {"ok": True, "removed": removed}


# --- Room planner ------------------------------------------------------------
#
# A dashboard run as its own process plans the turns of the group rooms it
# hosts, and it imports these routes before it starts them. room_planner.py
# in the plugin's folder keeps a long thread getting turns there, as the plugin's
# own load does in the gateway.

_room_planner_module: Any = None


def _guard_room_planner() -> None:
    global _room_planner_module
    try:
        path = os.path.join(core._plugin_root(), "room_planner.py")
        spec = importlib.util.spec_from_file_location("conduit_push_dashboard_room_planner", path)
        if spec is None or spec.loader is None:
            raise ImportError("This plugin install is missing room_planner.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.install()
        _room_planner_module = module
    except Exception:  # noqa: BLE001 — group rooms only, never the routes
        logger.warning("Conduit plugin: couldn't guard Hermes' group room planner", exc_info=True)


_guard_room_planner()


# room_cutoff.py in the plugin's folder fails the turn of a member whose Hermes
# no longer accepts the room once, instead of the room retrying it forever,
# as the plugin's own load does in the gateway.

_room_cutoff_module: Any = None


def _guard_room_cutoff() -> None:
    global _room_cutoff_module
    try:
        path = os.path.join(core._plugin_root(), "room_cutoff.py")
        spec = importlib.util.spec_from_file_location("conduit_push_dashboard_room_cutoff", path)
        if spec is None or spec.loader is None:
            raise ImportError("This plugin install is missing room_cutoff.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.install()
        _room_cutoff_module = module
    except Exception:  # noqa: BLE001 — group rooms only, never the routes
        logger.warning("Conduit plugin: couldn't guard Hermes' group room driver", exc_info=True)


_guard_room_cutoff()


# room_wakeups.py in the plugin's folder keeps a room's pass that ends from
# keeping every room's pass going, as the plugin's own load does in the
# gateway.

_room_wakeups_module: Any = None


def _guard_room_wakeups() -> None:
    global _room_wakeups_module
    try:
        path = os.path.join(core._plugin_root(), "room_wakeups.py")
        spec = importlib.util.spec_from_file_location("conduit_push_dashboard_room_wakeups", path)
        if spec is None or spec.loader is None:
            raise ImportError("This plugin install is missing room_wakeups.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.install()
        _room_wakeups_module = module
    except Exception:  # noqa: BLE001 — group rooms only, never the routes
        logger.warning("Conduit plugin: couldn't guard Hermes' group room wakeups", exc_info=True)


_guard_room_wakeups()


# room_cleanup.py in the plugin's folder deletes the hidden chats of a disbanded
# room's agents on this host, as the plugin's own load does in the gateway.
# It reads the room stores through room_rules.py, so it loads as part of the
# plugin's private package.

_room_cleanup_module: Any = None


def _guard_room_cleanup() -> None:
    global _room_cleanup_module
    try:
        module = core._plugin_package_module("room_cleanup")
        module.install()
        _room_cleanup_module = module
    except Exception:  # noqa: BLE001 — group rooms only, never the routes
        logger.warning("Conduit plugin: couldn't guard Hermes' group room disband", exc_info=True)


_guard_room_cleanup()
# Reads this installation's inbox while it has invites or Links, in the
# process serving the dashboard (Linked Rooms, room_links.py).
linked_rooms._start_room_links()
