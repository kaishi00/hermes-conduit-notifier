"""The workspace explorer's file operations (Conduit's Folder tab, #1389).

Conduit browses one root at a time: the open chat's working folder, or a
project folder of the profile. Every operation here takes that root and keeps
to it: a path that resolves outside it (through ``..`` or a symlink) is
refused, and so is any file Hermes' own file routes refuse to show (``.env``,
credential stores). The root itself is the caller's choice: any folder on a
personal install, as Hermes' /api/fs routes allow, and only inside its files
folder (``boundary``) on a hosted or locked one. Reads and previews go through Hermes' own routes
(/api/fs/read-text, read-data-url, download); this adds what those lack:
listings with sizes and dates, search by name and inside files, recent files,
and new folders and files, save with a conflict check, rename and move,
duplicate, delete and upload.

Two backends: this host's files (a Docker profile's container paths
translated to their host mount by the caller's resolver), and an SSH
profile's workspace, through Hermes' own SSH adapter, in plain POSIX shell.
Paths come back the way Hermes' /api/fs/list gives them: host paths for this
host, the SSH machine's paths for SSH.
"""

from __future__ import annotations

import base64
import math
import binascii
import errno
import os
import posixpath
import secrets
import shlex
import shutil
import stat as stat_module
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Folders a listing and a search pass over, as Hermes' /api/fs/list does.
HIDDEN_DIRS = frozenset({
    ".git", ".hg", ".svn", ".cache", ".next", ".turbo", ".venv", "__pycache__",
    "build", "dist", "node_modules", "target", "venv",
})
# Hermes' read-side denylist, used when its own isn't importable.
_SENSITIVE_FILES = frozenset({
    ".env", ".envrc", "auth.json", "credentials.json", ".netrc", ".pgpass", "id_rsa", "id_ed25519",
    "id_ecdsa", "id_dsa",
})
_SENSITIVE_DIRS = frozenset({"mcp-tokens", "pairing", ".ssh", ".gnupg"})

MAX_NAME_BYTES = 255
MAX_PATH_CHARS = 4096
MAX_ENTRIES = 2000
# Copy names an SSH duplicate tries, ``a copy`` through ``a copy 100``.
DUPLICATE_NAMES_TRIED = 100
# A single SSH step (not a walk) that takes longer is given up on.
SSH_STEP_TIMEOUT_S = 25
SEARCH_MAX_RESULTS = 200
SEARCH_MAX_MATCHES_PER_FILE = 5
SEARCH_MAX_FILES = 20000
SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
SEARCH_TIME_S = 8.0
SNIPPET_CHARS = 160
SEARCH_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
RECENT_LIMIT = 40
SAVE_MAX_BYTES = 8 * 1024 * 1024
UPLOAD_MAX_BYTES = 25 * 1024 * 1024


class WorkspaceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env.") or lowered == ".envrc":
        return True
    return lowered in _SENSITIVE_FILES


def default_sensitive(path: str) -> bool:
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if parts and is_sensitive_name(parts[-1]):
        return True
    return any(part.lower() in _SENSITIVE_DIRS for part in parts)


def _any_case(name: str) -> str:
    """A shell glob matching ``name`` in any letter case."""
    return "".join(f"[{ch.lower()}{ch.upper()}]" if ch.isalpha() else ("[" + ch + "]" if ch in "*?[" else ch)
                   for ch in name)


def check_name(name: Any) -> str:
    """A new entry's name: one path component, never a way out of its folder."""
    text = str(name or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or "\x00" in text:
        raise WorkspaceError(400, "Choose a name without slashes")
    if len(text.encode("utf-8")) > MAX_NAME_BYTES:
        raise WorkspaceError(400, "That name is too long")
    return text


def _path_text(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        raise WorkspaceError(400, "A path is required")
    if len(text) > MAX_PATH_CHARS:
        raise WorkspaceError(400, "That path is too long")
    return text


def _snippet(line: str, start: int, length: int) -> Dict[str, Any]:
    """The matching line, trimmed around the match, and where the match sits in it."""
    line = line.rstrip("\r\n").replace("\t", " ")
    if len(line) <= SNIPPET_CHARS:
        return {"text": line, "matchStart": start, "matchLength": length}
    lead = max(0, min(start - SNIPPET_CHARS // 3, len(line) - SNIPPET_CHARS))
    text = line[lead:lead + SNIPPET_CHARS]
    prefix = "…" if lead > 0 else ""
    suffix = "…" if lead + SNIPPET_CHARS < len(line) else ""
    return {"text": prefix + text + suffix, "matchStart": start - lead + len(prefix), "matchLength": length}


def copy_name(name: str, taken: Callable[[str], bool]) -> str:
    """``report copy.md``, then ``report copy 2.md``: the first name not taken."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    for number in range(1, 1000):
        candidate = f"{stem} copy{ext}" if number == 1 else f"{stem} copy {number}{ext}"
        if not taken(candidate):
            return candidate
    raise WorkspaceError(409, "Too many copies of this file already")


# --- This host ----------------------------------------------------------------


class LocalWorkspace:
    """Files on this host. ``resolve`` turns a path the agent sees into this
    host's path (a Docker profile's container paths to their mount)."""

    def __init__(self, resolve: Callable[[str], str] = lambda raw: raw,
                 sensitive: Callable[[str], bool] = default_sensitive,
                 clock: Callable[[], float] = time.monotonic,
                 boundary: Optional[str] = None) -> None:
        self._resolve = resolve
        self._sensitive = sensitive
        self._clock = clock
        # A hosted or locked install's one folder: no root may leave it.
        self._boundary = os.path.realpath(boundary) if boundary else None

    # Paths

    def _host(self, raw: Any) -> str:
        text = _path_text(raw)
        if text.startswith("~"):
            text = os.path.expanduser(text)
        host = str(self._resolve(text))
        if not os.path.isabs(host):
            raise WorkspaceError(400, "Paths must be absolute")
        return os.path.normpath(host)

    def root(self, raw: Any) -> str:
        host = self._host(raw)
        real = os.path.realpath(host)
        if self._boundary is not None and not self._inside(self._boundary, real):
            raise WorkspaceError(403, "That's outside the folder this Hermes keeps files in")
        if not os.path.isdir(real):
            raise WorkspaceError(404, "This folder is no longer there")
        return real

    def _inside(self, root: str, real: str) -> bool:
        return real == root or real.startswith(root.rstrip(os.sep) + os.sep)

    def _existing(self, root: str, raw: Any) -> str:
        """An existing path under ``root``, its symlinks followed."""
        host = self._host(raw)
        real = os.path.realpath(host)
        if not self._inside(root, real):
            raise WorkspaceError(403, "That's outside this workspace")
        # Refused before it's looked for: a 404 would say a secret file isn't there.
        if self._sensitive(real) or self._sensitive(host):
            raise WorkspaceError(403, "Conduit doesn't open files that hold keys or passwords")
        if not os.path.lexists(real):
            raise WorkspaceError(404, "That file is no longer there")
        return real

    def _new(self, root: str, folder: Any, name: Any) -> str:
        """Where a new entry ``name`` goes in ``folder``, which must exist under ``root``."""
        parent = self._existing(root, folder)
        if not os.path.isdir(parent):
            raise WorkspaceError(400, "That's not a folder")
        target = os.path.join(parent, check_name(name))
        if self._sensitive(target):
            raise WorkspaceError(403, "Conduit doesn't create files that hold keys or passwords")
        return target

    def _entry(self, path: str, name: Optional[str] = None, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """``path``'s row. Under ``root``, a link that leads out of it shows
        as the link itself: nothing about where it leads."""
        try:
            if root is not None and os.path.islink(path) and not self._inside(root, os.path.realpath(path)):
                raise OSError("leads outside")
            st = os.stat(path)
        except OSError:
            try:
                st = os.lstat(path)
            except OSError:
                return None
        is_dir = stat_module.S_ISDIR(st.st_mode)
        return {
            "name": name or os.path.basename(path),
            "path": path,
            "isDirectory": is_dir,
            "size": None if is_dir else st.st_size,
            "mtime": st.st_mtime,
        }

    # Reading

    def list_dir(self, root_raw: Any, path_raw: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        folder = self._existing(root, path_raw)
        if not os.path.isdir(folder):
            raise WorkspaceError(400, "That's not a folder")
        entries: List[Dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(folder) as scan:
                for item in scan:
                    if item.name in HIDDEN_DIRS or self._sensitive(item.path):
                        continue
                    # A link under an innocent name to a secret stays out too.
                    if item.is_symlink() and self._sensitive(os.path.realpath(item.path)):
                        continue
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    entry = self._entry(item.path, item.name, root)
                    if entry is not None:
                        entries.append(entry)
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't read this folder")
        except OSError as exc:
            raise WorkspaceError(500, f"Couldn't read this folder ({exc.strerror or 'error'})")
        entries.sort(key=lambda item: (not item["isDirectory"], item["name"].lower(), item["name"]))
        return {"path": folder, "root": root, "entries": entries, "truncated": truncated}

    def _walk(self, root: str, deadline: float):
        """Every file under ``root`` (hidden folders and sensitive files left
        out), until the file budget or the deadline. Yields (path, name, stat)."""
        seen = 0
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as scan:
                    items = list(scan)
            except OSError:
                continue
            for item in items:
                if self._clock() > deadline or seen >= SEARCH_MAX_FILES:
                    raise _Budget()
                if item.name in HIDDEN_DIRS or self._sensitive(item.path):
                    continue
                try:
                    if item.is_dir(follow_symlinks=False):
                        stack.append(item.path)
                        yield item.path, item.name, None
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                seen += 1
                yield item.path, item.name, st

    def search(self, root_raw: Any, query: Any, contents: bool) -> Dict[str, Any]:
        root = self.root(root_raw)
        needle = str(query or "").strip()
        if not needle:
            return {"root": root, "names": [], "matches": [], "truncated": False}
        if len(needle) > 200:
            raise WorkspaceError(400, "That search is too long")
        lowered = needle.lower()
        deadline = self._clock() + SEARCH_TIME_S
        names: List[Dict[str, Any]] = []
        matches: List[Dict[str, Any]] = []
        truncated = False
        try:
            for path, name, st in self._walk(root, deadline):
                if lowered in name.lower():
                    if len(names) >= SEARCH_MAX_RESULTS:
                        truncated = True
                    else:
                        entry = self._entry(path, name)
                        if entry is not None:
                            names.append(entry)
                if not contents or st is None or st.st_size > SEARCH_MAX_FILE_BYTES:
                    continue
                if len(matches) >= SEARCH_MAX_RESULTS:
                    truncated = True
                    if len(names) >= SEARCH_MAX_RESULTS:
                        break
                    continue
                found = _search_file(path, lowered)
                if found:
                    matches.append({"path": path, "name": name, "mtime": st.st_mtime, "lines": found})
        except _Budget:
            truncated = True
        return {"root": root, "names": names, "matches": matches, "truncated": truncated}

    def recent(self, root_raw: Any, limit: int = RECENT_LIMIT) -> Dict[str, Any]:
        root = self.root(root_raw)
        deadline = self._clock() + SEARCH_TIME_S
        files: List[Dict[str, Any]] = []
        truncated = False
        try:
            for path, name, st in self._walk(root, deadline):
                if st is not None:
                    files.append({"name": name, "path": path, "isDirectory": False,
                                  "size": st.st_size, "mtime": st.st_mtime})
        except _Budget:
            truncated = True
        files.sort(key=lambda item: item["mtime"], reverse=True)
        return {"root": root, "entries": files[:max(1, min(limit, RECENT_LIMIT))], "truncated": truncated}

    # Changing

    def mkdir(self, root_raw: Any, folder: Any, name: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._new(root, folder, name)
        try:
            os.mkdir(target)
        except FileExistsError:
            raise WorkspaceError(409, "Something with that name is already there")
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't write in this folder")
        return {"entry": self._entry(target)}

    def new_file(self, root_raw: Any, folder: Any, name: Any, content: str = "") -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._new(root, folder, name)
        data = (content or "").encode("utf-8")
        if len(data) > SAVE_MAX_BYTES:
            raise WorkspaceError(413, "That's too much text to save")
        try:
            with open(target, "xb") as handle:
                handle.write(data)
        except FileExistsError:
            raise WorkspaceError(409, "Something with that name is already there")
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't write in this folder")
        return {"entry": self._entry(target)}

    def save(self, root_raw: Any, path: Any, content: str, expected_mtime: Optional[float]) -> Dict[str, Any]:
        """Replace a text file, unless it changed since the editor read it."""
        root = self.root(root_raw)
        target = self._existing(root, path)
        if not os.path.isfile(target):
            raise WorkspaceError(400, "Only files can be edited")
        data = (content or "").encode("utf-8")
        if len(data) > SAVE_MAX_BYTES:
            raise WorkspaceError(413, "That's too much text to save")
        current = os.stat(target).st_mtime
        if expected_mtime is not None and (not math.isfinite(float(expected_mtime))
                                           or abs(current - float(expected_mtime)) > 0.001):
            raise WorkspaceError(409, "This file changed since you opened it")
        temp = None
        try:
            temp = _write_temp(os.path.dirname(target), data)
            shutil.copymode(target, temp)
            os.replace(temp, target)
            temp = None
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't write this file")
        finally:
            if temp is not None and os.path.lexists(temp):
                os.unlink(temp)
        return {"entry": self._entry(target)}

    def move(self, root_raw: Any, path: Any, folder: Any, name: Any, overwrite: bool = False) -> Dict[str, Any]:
        """Rename (same folder) or move (another folder under the root)."""
        root = self.root(root_raw)
        source = self._existing(root, path)
        if source == root:
            raise WorkspaceError(400, "The workspace folder itself can't be moved")
        # A link (checked above to lead inside) moves as the link, as
        # duplicate and delete treat it, never what it points to.
        host = self._host(path)
        link = os.path.join(os.path.realpath(os.path.dirname(host)), os.path.basename(host))
        if os.path.islink(link) and self._inside(root, link):
            source = link
        target = self._new(root, folder, name)
        if target == source:
            return {"entry": self._entry(target)}
        if os.path.isdir(source) and self._inside(source, os.path.realpath(os.path.dirname(target))):
            raise WorkspaceError(400, "A folder can't go inside itself")
        if os.path.lexists(target):
            if not overwrite:
                raise WorkspaceError(409, "Something with that name is already there")
            if os.path.isdir(target) and not os.path.islink(target):
                raise WorkspaceError(409, "A folder with that name is already there")
        try:
            if overwrite:
                os.replace(source, target)
            else:
                _rename_no_replace(source, target)
        except (FileExistsError, IsADirectoryError, NotADirectoryError):
            # A folder onto a file, or a file onto a folder.
            raise WorkspaceError(409, "Something with that name is already there")
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't move this")
        except OSError as exc:
            if exc.errno == errno.EXDEV:  # another disk under the same root
                self._move_across(source, target, overwrite)
            elif exc.errno == errno.ENOENT:
                raise WorkspaceError(404, "That file or folder isn't there any more")
            else:
                raise WorkspaceError(500, f"Couldn't move this ({exc.strerror or 'error'})")
        return {"entry": self._entry(target)}

    def _move_across(self, source: str, target: str, overwrite: bool) -> None:
        """A move to another disk: copied beside the target into a private
        folder (shutil.move keeps a link a link), then landed there as on one
        disk, so nothing that appeared meanwhile is replaced or moved into.
        When it can't land, it goes back where it was."""
        if not overwrite and os.path.lexists(target):
            raise WorkspaceError(409, "Something with that name is already there")
        try:
            box = tempfile.mkdtemp(prefix=".conduit-", dir=os.path.dirname(target))
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't write in this folder")
        except FileNotFoundError:
            raise WorkspaceError(404, "That folder isn't there any more")
        except OSError as exc:
            raise WorkspaceError(500, f"Couldn't move this ({exc.strerror or exc})")
        temp = os.path.join(box, os.path.basename(target))
        try:
            try:
                shutil.move(source, temp)
            except OSError as exc:
                try:  # nothing copied yet, or a file whose source is still whole
                    if os.path.isdir(temp) and not os.path.islink(temp):
                        if not os.listdir(temp):
                            os.rmdir(temp)
                    elif os.path.lexists(temp) and os.path.lexists(source):
                        os.unlink(temp)
                except OSError:
                    pass
                if os.path.lexists(temp):  # a part copied: kept, as the source may be part gone
                    raise WorkspaceError(500, "Couldn't move all of this: what was copied is in "
                                              + os.path.basename(box) + " beside the target")
                if isinstance(exc, PermissionError):
                    raise WorkspaceError(403, "Hermes can't move this")
                if isinstance(exc, FileNotFoundError):
                    raise WorkspaceError(404, "That file or folder isn't there any more")
                raise WorkspaceError(500, f"Couldn't move this ({exc.strerror or exc})")
            try:
                if overwrite and not (os.path.isdir(temp) and not os.path.islink(temp)):
                    os.replace(temp, target)
                else:
                    _rename_no_replace(temp, target)
                return
            except (FileExistsError, IsADirectoryError, NotADirectoryError):
                failed = WorkspaceError(409, "Something with that name is already there")
            except PermissionError:
                failed = WorkspaceError(403, "Hermes can't move this")
            except OSError as exc:
                failed = WorkspaceError(500, f"Couldn't move this ({exc.strerror or exc})")
            try:
                if os.path.lexists(source):
                    raise FileExistsError(source)
                shutil.move(temp, source)  # back where it was
            except OSError:
                raise WorkspaceError(500, "Couldn't move this, and couldn't put it back: it's in "
                                          + os.path.basename(box) + " beside the target")
            raise failed
        finally:
            if not os.path.lexists(temp):
                shutil.rmtree(box, ignore_errors=True)

    def duplicate(self, root_raw: Any, path: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        source = self._existing(root, path)
        if source == root:
            raise WorkspaceError(400, "The workspace folder itself can't be duplicated")
        # A link (checked above to lead inside) is copied as the link, beside it.
        host = self._host(path)
        link = os.path.join(os.path.realpath(os.path.dirname(host)), os.path.basename(host))
        if os.path.islink(link) and self._inside(root, link):
            source = link
        folder = os.path.dirname(source)
        name = copy_name(os.path.basename(source), lambda candidate: os.path.lexists(os.path.join(folder, candidate)))
        target = os.path.join(folder, name)
        try:
            if os.path.isdir(source) and not os.path.islink(source):
                shutil.copytree(source, target, symlinks=True,
                                ignore=lambda folder, names: [n for n in names if self._sensitive(os.path.join(folder, n))])
            else:
                # A link stays a link, as the SSH copy keeps it.
                shutil.copy2(source, target, follow_symlinks=False)
        except FileExistsError:
            # Taken between choosing the name and copying.
            raise WorkspaceError(409, "Something with that name is already there")
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't write in this folder")
        return {"entry": self._entry(target)}

    def delete(self, root_raw: Any, path: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        # The link itself, not what it points to: a symlink is removed, never its target.
        host = self._host(path)
        parent = os.path.realpath(os.path.dirname(host))
        target = os.path.join(parent, os.path.basename(host))
        if target == root or os.path.realpath(target) == root:
            raise WorkspaceError(400, "The workspace folder itself can't be deleted")
        if not self._inside(root, parent) or not self._inside(root, target):
            raise WorkspaceError(403, "That's outside this workspace")
        if self._sensitive(target):
            raise WorkspaceError(403, "Conduit doesn't touch files that hold keys or passwords")
        if not os.path.lexists(target):
            raise WorkspaceError(404, "That file is no longer there")
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't delete this")
        return {"ok": True, "path": target}

    def upload(self, root_raw: Any, folder: Any, name: Any, data: bytes, overwrite: bool = False) -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._new(root, folder, name)
        if len(data) > UPLOAD_MAX_BYTES:
            raise WorkspaceError(413, "That file is too large to upload")
        if os.path.isdir(target):
            raise WorkspaceError(409, "A folder with that name is already there")
        if os.path.lexists(target) and not overwrite:
            raise WorkspaceError(409, "Something with that name is already there")
        temp = None
        try:
            temp = _write_temp(os.path.dirname(target), data)
            if overwrite:
                if os.path.lexists(target):
                    shutil.copymode(target, temp)  # as save keeps it
                os.replace(temp, target)
            else:
                _rename_no_replace(temp, target)
            temp = None
        except FileExistsError:
            raise WorkspaceError(409, "Something with that name is already there")
        except PermissionError:
            raise WorkspaceError(403, "Hermes can't write in this folder")
        finally:
            if temp is not None and os.path.lexists(temp):
                os.unlink(temp)
        return {"entry": self._entry(target)}


def _write_temp(folder: str, data: bytes) -> str:
    """``data`` in a new file of its own in ``folder``: a random name, created
    exclusively and never through a link, so nothing already there is written."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for _ in range(8):
        temp = os.path.join(folder, f".conduit-{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temp, flags, 0o666)  # the umask applies, as for any new file
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(temp)
            raise
        return temp
    raise WorkspaceError(500, "Couldn't write a temporary file here")


class _Budget(Exception):
    """The walk ran out of its file or time budget."""


def _rename_no_replace(source: str, target: str) -> None:
    """Rename, failing when ``target`` appears meanwhile (a hard link then an
    unlink, where the filesystem has them)."""
    if os.path.isdir(source) and not os.path.islink(source):
        if os.path.lexists(target):
            raise FileExistsError(target)
        os.rename(source, target)
        return
    try:
        os.link(source, target, follow_symlinks=False)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        if os.path.lexists(target):
            raise FileExistsError(target)
        os.rename(source, target)
        return
    os.unlink(source)


def _search_file(path: str, lowered: str) -> List[Dict[str, Any]]:
    """Lines of a text file holding ``lowered``, case-insensitively."""
    try:
        # Never through a link swapped in since the walk passed it.
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as handle:
            data = handle.read(SEARCH_MAX_FILE_BYTES + 1)
    except OSError:
        return []
    if b"\x00" in data[:4096]:
        return []
    text = data.decode("utf-8", errors="replace")
    if lowered not in text.lower():
        return []
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        at = line.lower().find(lowered)
        if at >= 0:
            found.append({"line": number, **_snippet(line, at, len(lowered))})
            if len(found) >= SEARCH_MAX_MATCHES_PER_FILE:
                break
    return found


# --- An SSH profile's workspace -------------------------------------------------

# Each script prints one record per line: base64 fields split by tabs, so any
# name survives. Sizes and dates come from GNU stat, else BSD stat (macOS).
_STAT = r'''
meta() {
  if _ms=$(stat -c '%Y %s' -- "$1" 2>/dev/null); then printf '%s' "$_ms";
  elif _ms=$(stat -f '%m %z' -- "$1" 2>/dev/null); then printf '%s' "$_ms";
  else printf '0 0'; fi
}
rec() {
  # $2 = l: a link shown as itself, not where it leads. $3: where a link
  # leads, sent along so a link to a secret stays out.
  _rt=${3:-}
  if [ "$2" != l ] && [ -d "$1" ]; then kind=d; else kind=f; fi
  set -- "$1" "$kind" $(meta "$1")
  if [ -n "$_rt" ]; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$(printf '%s' "$1" | base64 | tr -d '\r\n')" "$2" "$3" "$4" \
      "$(printf '%s' "$_rt" | base64 | tr -d '\r\n')"
  else
    printf '%s\t%s\t%s\t%s\n' "$(printf '%s' "$1" | base64 | tr -d '\r\n')" "$2" "$3" "$4"
  fi
}
'''
_ERROR = "__CONDUIT_FS_ERROR__:"


class SshWorkspace:
    """An SSH profile's workspace, through Hermes' SSH adapter (``fs``): its
    path rules and its connection, with the operations it lacks in shell."""

    def __init__(self, fs: Any, sensitive: Callable[[str], bool] = default_sensitive) -> None:
        self._fs = fs
        self._sensitive = sensitive

    def _norm(self, raw: Any) -> str:
        text = _path_text(raw)
        try:
            return self._fs._normalize(text)
        except Exception as exc:  # the adapter's own refusal
            raise WorkspaceError(400, str(exc) or "Invalid path")

    def _run(self, script: str, timeout: Optional[int] = None, stdin: Optional[str] = None) -> List[str]:
        # Always bounded: a wedged host never keeps a worker thread.
        result = self._fs._execute(script, stdin_data=stdin, timeout=timeout or SSH_STEP_TIMEOUT_S)
        output = result.get("output", "") or ""
        for line in output.splitlines():
            if line.startswith(_ERROR):
                code = line[len(_ERROR):].strip()
                raise WorkspaceError(*{
                    "ENOENT": (404, "That file is no longer there"),
                    "EEXIST": (409, "Something with that name is already there"),
                    "ENOTDIR": (400, "That's not a folder"),
                    "EISDIR": (400, "Only files can be edited"),
                    "EACCES": (403, "Hermes can't do that here"),
                    "EOUTSIDE": (403, "That's outside this workspace"),
                    "ESTALE": (409, "This file changed since you opened it"),
                }.get(code, (500, "The SSH workspace couldn't do that")))
        if result.get("returncode") not in (0, None):
            raise WorkspaceError(502, "The SSH workspace request failed")
        return output.splitlines()

    def _inside(self, root: str, path: str) -> bool:
        return path == root or path.startswith(root.rstrip("/") + "/")

    def root(self, raw: Any) -> str:
        path = self._norm(raw)
        lines = self._run(f'p={shlex.quote(path)}\n'
                          f'[ -d "$p" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                          'cd "$p" && pwd -P')
        real = next((line for line in lines if line.startswith("/")), path)
        return posixpath.normpath(real)

    def _check(self, root: str, raw: Any, resolve: bool = True) -> str:
        path = self._norm(raw)
        if not self._inside(root, path):
            # Its real path may still be inside: a root reached through a link.
            parent = self._real_parent(path)
            path = posixpath.join(parent, posixpath.basename(path))
            if not self._inside(root, path):
                raise WorkspaceError(403, "That's outside this workspace")
        if self._sensitive(path) or (resolve and self._sensitive(self._resolved(root, path))):
            raise WorkspaceError(403, "Conduit doesn't open files that hold keys or passwords")
        return path

    def _resolved(self, root: str, path: str) -> str:
        """Where ``path`` really is: a link's target, or the real folder it
        sits in, so a link under an innocent name to a secret is refused."""
        lines = self._run(self._guard(root) + f'p={shlex.quote(path)}; r=\n'
                          'if [ -L "$p" ] || [ -d "$p" ]; then follows "$p" >/dev/null 2>&1;'
                          ' else d=$(cd "$(dirname -- "$p")" 2>/dev/null && pwd -P) && r="$d/$(basename -- "$p")"; fi\n'
                          'printf \'%s\' "$r" | base64 | tr -d \'\\r\\n\'; echo')
        try:
            return base64.b64decode("".join(lines).strip(), validate=True).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            return ""

    def _real_parent(self, path: str) -> str:
        lines = self._run(f'cd {shlex.quote(posixpath.dirname(path))} 2>/dev/null && pwd -P || '
                          f'echo {_ERROR}ENOENT')
        return posixpath.normpath(next((line for line in lines if line.startswith("/")), "/"))

    def _guard(self, root: str) -> str:
        """Shell that refuses a path ($1) whose real folder leaves the root."""
        return (f'root={shlex.quote(root)}\n'
                'inside() { d=$(cd "$(dirname -- "$1")" 2>/dev/null && pwd -P) || return 1;'
                ' case "$d/" in "$root"/*|"$root/") return 0;; esac; return 1; }\n'
                # Where $1 itself leads: a folder's real path, or a link's
                # target, which must stay inside too.
                'follows() { if [ -d "$1" ]; then r=$(cd "$1" 2>/dev/null && pwd -P) || return 1;'
                ' elif [ -L "$1" ]; then r=$(realpath -- "$1" 2>/dev/null || readlink -f -- "$1" 2>/dev/null);'
                # Neither on an older BSD: follow the chain with plain readlink.
                ' if [ -z "$r" ]; then _fl="$1"; _fn=0;'
                ' while [ -L "$_fl" ] && [ $_fn -lt 32 ]; do _ft=$(readlink -- "$_fl") || return 1;'
                ' case "$_ft" in /*) _fl="$_ft";; *) _fl="$(dirname -- "$_fl")/$_ft";; esac; _fn=$((_fn+1)); done;'
                ' [ -L "$_fl" ] && return 1;'
                ' _fd=$(cd "$(dirname -- "$_fl")" 2>/dev/null && pwd -P) || return 1; r="$_fd/$(basename -- "$_fl")"; fi;'
                ' [ -n "$r" ] || return 1;'
                ' else inside "$1"; return; fi;'
                ' case "$r/" in "$root"/*|"$root/") return 0;; esac; return 1; }\n')

    def _records(self, lines: List[str]) -> List[Dict[str, Any]]:
        entries = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) not in (4, 5):
                continue
            try:
                path = base64.b64decode(parts[0], validate=True).decode("utf-8", errors="replace")
                mtime, size = float(parts[2]), int(parts[3])
                leads = base64.b64decode(parts[4], validate=True).decode("utf-8", errors="replace") \
                    if len(parts) == 5 else None
            except (ValueError, binascii.Error):
                continue
            if leads is not None and self._sensitive(leads):
                continue
            is_dir = parts[1] == "d"
            name = posixpath.basename(path)
            if name in HIDDEN_DIRS or self._sensitive(path):
                continue
            entries.append({"name": name, "path": path, "isDirectory": is_dir,
                            "size": None if is_dir else size, "mtime": mtime})
        return entries

    def _entry(self, path: str) -> Optional[Dict[str, Any]]:
        lines = self._run(_STAT + f'rec {shlex.quote(path)}')
        found = self._records(lines)
        return found[0] if found else None

    def list_dir(self, root_raw: Any, path_raw: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        folder = self._check(root, path_raw)
        lines = self._run(_STAT + self._guard(root) + f'p={shlex.quote(folder)}\n'
                          f'[ -e "$p" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                          f'[ -d "$p" ] || {{ echo {_ERROR}ENOTDIR; exit 1; }}\n'
                          f'follows "$p" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                          # One past the cap, so the answer can say it was cut.
                          f'n=0; for c in "$p"/* "$p"/.[!.]* "$p"/..?*; do [ -e "$c" ] || [ -L "$c" ] || continue;'
                          f' n=$((n+1)); [ $n -gt {MAX_ENTRIES + 1} ] && break;'
                          ' if [ ! -L "$c" ]; then rec "$c"; elif follows "$c"; then rec "$c" "" "$r"; else rec "$c" l; fi; done')
        entries = self._records(lines)
        truncated = len(entries) > MAX_ENTRIES or len(lines) > MAX_ENTRIES
        entries = entries[:MAX_ENTRIES]
        entries.sort(key=lambda item: (not item["isDirectory"], item["name"].lower(), item["name"]))
        return {"path": folder, "root": root, "entries": entries, "truncated": truncated}

    def _prune(self) -> str:
        names = " -o ".join(f"-name {shlex.quote(name)}" for name in sorted(HIDDEN_DIRS))
        return f'\\( -type d \\( {names} \\) \\) -prune -o'

    def search(self, root_raw: Any, query: Any, contents: bool) -> Dict[str, Any]:
        root = self.root(root_raw)
        needle = str(query or "").strip()
        if not needle:
            return {"root": root, "names": [], "matches": [], "truncated": False}
        if len(needle) > 200:
            raise WorkspaceError(400, "That search is too long")
        # find's -iname treats *, ? and [ as patterns: escape them.
        pattern = "*" + "".join("\\" + ch if ch in "*?[]\\" else ch for ch in needle) + "*"
        q_root = shlex.quote(root)
        name_lines = self._run(
            _STAT + self._guard(root) +
            f'find {q_root} {self._prune()} -iname {shlex.quote(pattern)} -print 2>/dev/null'
            f' | head -n {SEARCH_MAX_RESULTS + 2} | while IFS= read -r f; do'
            # A link goes with where it leads, so one to a secret stays out.
            ' if [ ! -L "$f" ]; then rec "$f"; elif follows "$f"; then rec "$f" "" "$r"; else rec "$f" l; fi; done',
            timeout=int(SEARCH_TIME_S) + 7)
        names = [entry for entry in self._records(name_lines) if entry["path"] != root]
        # Read past the cap (the root itself is one line): cut only when
        # more than the cap would show, or the walk itself stopped short.
        truncated = len(names) > SEARCH_MAX_RESULTS or len(name_lines) > SEARCH_MAX_RESULTS + 1
        names = names[:SEARCH_MAX_RESULTS]
        matches: List[Dict[str, Any]] = []
        if contents:
            # Files that hold keys or passwords are never read, in any case.
            excludes = " ".join(
                [f"--exclude-dir={shlex.quote(name)}" for name in sorted(HIDDEN_DIRS)]
                + [f"--exclude-dir={shlex.quote(_any_case(name))}" for name in sorted(_SENSITIVE_DIRS)]
                + [f"--exclude={shlex.quote(_any_case(name))}"
                   for name in sorted(_SENSITIVE_FILES)]
                + [f"--exclude={shlex.quote(_any_case('.env.') + '*')}"])
            # --null ends each path with a NUL, so a path may hold anything;
            # the whole output comes back base64 since a shell can't carry NULs.
            lines = self._run(
                f'grep -rIinF --null -m {SEARCH_MAX_MATCHES_PER_FILE} {excludes} -- {shlex.quote(needle)} {q_root}'
                f' 2>/dev/null | head -c {SEARCH_MAX_OUTPUT_BYTES} | base64 | tr -d \'\\r\\n\'; echo',
                timeout=int(SEARCH_TIME_S) + 7)
            try:
                raw_output = base64.b64decode("".join(lines).strip(), validate=True).decode("utf-8", errors="replace")
            except (ValueError, binascii.Error):
                raw_output = ""
            by_path: Dict[str, Dict[str, Any]] = {}
            lowered = needle.lower()
            for raw in raw_output.split("\n"):
                path, sep, rest = raw.partition("\x00")
                number_text, colon, text = rest.partition(":")
                if not sep or not colon or not number_text.isdigit():
                    continue
                number = int(number_text)
                if self._sensitive(path):
                    continue
                at = text.lower().find(lowered)
                if at < 0:
                    continue
                item = by_path.get(path)
                if item is None:
                    if len(by_path) >= SEARCH_MAX_RESULTS:
                        truncated = True
                        break
                    item = {"path": path, "name": posixpath.basename(path), "mtime": None, "lines": []}
                    by_path[path] = item
                    matches.append(item)
                item["lines"].append({"line": number, **_snippet(text, at, len(needle))})
        return {"root": root, "names": names, "matches": matches, "truncated": truncated}

    def recent(self, root_raw: Any, limit: int = RECENT_LIMIT) -> Dict[str, Any]:
        root = self.root(root_raw)
        lines = self._run(
            _STAT + f'find {shlex.quote(root)} {self._prune()} -type f -print 2>/dev/null'
            f' | head -n {SEARCH_MAX_FILES} | while IFS= read -r f; do rec "$f"; done',
            timeout=int(SEARCH_TIME_S) + 7)
        files = [entry for entry in self._records(lines) if not entry["isDirectory"]]
        files.sort(key=lambda item: item["mtime"], reverse=True)
        return {"root": root, "entries": files[:max(1, min(limit, RECENT_LIMIT))],
                "truncated": len(lines) >= SEARCH_MAX_FILES}

    def _target(self, root: str, folder: Any, name: Any) -> str:
        parent = self._check(root, folder)
        target = posixpath.join(parent, check_name(name))
        if self._sensitive(target):
            raise WorkspaceError(403, "Conduit doesn't create files that hold keys or passwords")
        return target

    def mkdir(self, root_raw: Any, folder: Any, name: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._target(root, folder, name)
        self._run(self._guard(root) + f't={shlex.quote(target)}\n'
                  f'[ -d "$(dirname -- "$t")" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                  f'inside "$t" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  f'[ -e "$t" ] || [ -L "$t" ] && {{ echo {_ERROR}EEXIST; exit 1; }}\n'
                  f'mkdir -- "$t" || {{ echo {_ERROR}EACCES; exit 1; }}')
        return {"entry": self._entry(target)}

    def new_file(self, root_raw: Any, folder: Any, name: Any, content: str = "") -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._target(root, folder, name)
        data = (content or "").encode("utf-8")
        if len(data) > SAVE_MAX_BYTES:
            raise WorkspaceError(413, "That's too much text to save")
        self._write(root, target, data, create=True)
        return {"entry": self._entry(target)}

    def _write(self, root: str, target: str, data: bytes, create: bool, expected_mtime: Optional[float] = None,
               overwrite: bool = True, existing: bool = False) -> None:
        """Write ``data`` (sent base64 on stdin) beside ``target``, then move it
        into place. ``existing``: a save, of a file that must be there and lead
        nowhere outside the root."""
        check = f'[ -e "$t" ] || [ -L "$t" ] && {{ echo {_ERROR}EEXIST; exit 1; }}\n' if create or not overwrite else ""
        if existing:
            check += (f'[ -e "$t" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                      f'follows "$t" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                      # Through a link to its file, as this host's save writes: the link stays.
                      'if [ -L "$t" ]; then t="$r"; fi\n')
        stale = ""
        if expected_mtime is not None:
            stale = (_STAT + f'set -- $(meta "$t"); [ "$1" = "{int(expected_mtime)}" ] || '
                     f'{{ echo {_ERROR}ESTALE; exit 1; }}\n')
        self._run(self._guard(root) + f't={shlex.quote(target)}\n'
                  f'[ -d "$(dirname -- "$t")" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                  f'inside "$t" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  f'[ -d "$t" ] && {{ echo {_ERROR}{"EISDIR" if existing else "EEXIST"}; exit 1; }}\n' + check + stale +
                  # A fresh name of mktemp's own: nothing already there is written through.
                  f'tmp=$(mktemp "$(dirname -- "$t")/.conduit-XXXXXXXX") || {{ echo {_ERROR}EACCES; exit 1; }}\n'
                  # -D on an older macOS, which has no -d.
                  f'{{ base64 -d 2>/dev/null || base64 -D; }} > "$tmp" || {{ rm -f "$tmp"; echo {_ERROR}EACCES; exit 1; }}\n'
                  # mktemp's file is private: give it the file's own mode, or a new file's.
                  'm=$(stat -c %a -- "$t" 2>/dev/null || stat -f %Lp -- "$t" 2>/dev/null)\n'
                  'chmod "${m:-$(printf %o $((0666 & ~0$(umask))))}" "$tmp" 2>/dev/null\n' +
                  (f'mv -f -- "$tmp" "$t" || {{ rm -f "$tmp"; echo {_ERROR}EACCES; exit 1; }}' if overwrite and not create else
                   # Never replaces what appeared since the check: mv -n leaves
                   # the temp file behind instead.
                   f'mv -n -- "$tmp" "$t" 2>/dev/null; [ -e "$tmp" ] && {{ rm -f "$tmp";'
                   f' if [ -e "$t" ] || [ -L "$t" ]; then echo {_ERROR}EEXIST; else echo {_ERROR}EACCES; fi; exit 1; }}; exit 0'),
                  stdin=base64.b64encode(data).decode("ascii") + "\n")

    def save(self, root_raw: Any, path: Any, content: str, expected_mtime: Optional[float]) -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._check(root, path)
        data = (content or "").encode("utf-8")
        if len(data) > SAVE_MAX_BYTES:
            raise WorkspaceError(413, "That's too much text to save")
        self._write(root, target, data, create=False, expected_mtime=expected_mtime, existing=True)
        return {"entry": self._entry(target)}

    def move(self, root_raw: Any, path: Any, folder: Any, name: Any, overwrite: bool = False) -> Dict[str, Any]:
        root = self.root(root_raw)
        source = self._check(root, path)
        if source == root:
            raise WorkspaceError(400, "The workspace folder itself can't be moved")
        target = self._target(root, folder, name)
        if target == source:
            return {"entry": self._entry(target)}
        if self._inside(source, posixpath.dirname(target)):
            raise WorkspaceError(400, "A folder can't go inside itself")
        exists = "" if overwrite else f'[ -e "$t" ] || [ -L "$t" ] && {{ echo {_ERROR}EEXIST; exit 1; }}\n'
        self._run(self._guard(root) + f's={shlex.quote(source)}; t={shlex.quote(target)}\n'
                  f'inside "$s" && inside "$t" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  f'[ -e "$s" ] || [ -L "$s" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                  # A link that leads out isn't moved, as on this host.
                  f'[ -L "$s" ] && ! follows "$s" && {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  f'[ -d "$t" ] && [ ! -L "$t" ] && {{ echo {_ERROR}EEXIST; exit 1; }}\n'
                  # A folder never replaces a file either.
                  f'[ -d "$s" ] && [ ! -L "$s" ] && {{ [ -e "$t" ] || [ -L "$t" ]; }} && {{ echo {_ERROR}EEXIST; exit 1; }}\n'
                  + exists +
                  f'mv {"-f" if overwrite else "-n"} -- "$s" "$t" || {{ echo {_ERROR}EACCES; exit 1; }}\n'
                  f'[ -e "$s" ] || [ -L "$s" ] && {{ echo {_ERROR}EEXIST; exit 1; }}; exit 0')
        return {"entry": self._entry(target)}

    def duplicate(self, root_raw: Any, path: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        source = self._check(root, path)
        if source == root:
            raise WorkspaceError(400, "The workspace folder itself can't be duplicated")
        folder = posixpath.dirname(source)
        # The first free copy name, picked on the host itself (whatever the
        # folder's size) right before the copy.
        candidates: List[str] = []

        def offered(candidate: str) -> bool:
            candidates.append(candidate)
            return len(candidates) < DUPLICATE_NAMES_TRIED

        copy_name(posixpath.basename(source), offered)
        quoted = " ".join(shlex.quote(candidate) for candidate in candidates)
        # The copy keeps links as links and leaves out what holds keys or
        # passwords, as this host's duplicate does.
        secrets_in_copy = " -o ".join(
            [f"-iname {shlex.quote(name)}" for name in sorted(_SENSITIVE_FILES | _SENSITIVE_DIRS)]
            + ["-iname '.env.*'"])
        lines = self._run(self._guard(root) + f's={shlex.quote(source)}; f={shlex.quote(folder)}; t=\n'
                  f'follows "$s" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  f'for n in {quoted}; do [ -e "$f/$n" ] || [ -L "$f/$n" ] || {{ t="$f/$n"; break; }}; done\n'
                  f'[ -n "$t" ] || {{ echo {_ERROR}EEXIST; exit 1; }}\n'
                  f'inside "$t" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  'printf \'%s\' "$t" | base64 | tr -d \'\\r\\n\'; echo\n'
                  # A copy that fails partway, or can't lose its secrets, goes.
                  # Never over something that appeared since the name was
                  # picked: a folder claims its name with mkdir, anything else
                  # lands with mv -n from a private folder beside it.
                  'if [ -d "$s" ] && [ ! -L "$s" ]; then\n'
                  f'  mkdir -- "$t" 2>/dev/null || {{ if [ -e "$t" ] || [ -L "$t" ]; then echo {_ERROR}EEXIST;'
                  f' else echo {_ERROR}EACCES; fi; exit 1; }}\n'
                  f'  cp -R -P -p -- "$s"/. "$t"/ || {{ rm -rf -- "$t"; echo {_ERROR}EACCES; exit 1; }}\n'
                  'else\n'
                  f'  d=$(mktemp -d "$f/.conduit-XXXXXXXX") || {{ echo {_ERROR}EACCES; exit 1; }}\n'
                  f'  cp -P -p -- "$s" "$d/c" || {{ rm -rf -- "$d"; echo {_ERROR}EACCES; exit 1; }}\n'
                  '  mv -n -- "$d/c" "$t" 2>/dev/null\n'
                  f'  if [ -e "$d/c" ] || [ -L "$d/c" ]; then rm -rf -- "$d"; echo {_ERROR}EEXIST; exit 1; fi\n'
                  '  rm -rf -- "$d"\n'
                  'fi\n'
                  f'if [ -d "$t" ] && [ ! -L "$t" ]; then find "$t" -mindepth 1 \\( {secrets_in_copy} \\) -prune -exec rm -rf -- {{}} +;'
                  f' [ -z "$(find "$t" -mindepth 1 \\( {secrets_in_copy} \\) -print 2>/dev/null | head -n 1)" ] ||'
                  f' {{ rm -rf -- "$t"; echo {_ERROR}EACCES; exit 1; }}; fi; exit 0')
        try:
            target = base64.b64decode(next(line for line in lines if line.strip()).strip(), validate=True).decode("utf-8")
        except (StopIteration, ValueError, binascii.Error):
            raise WorkspaceError(500, "The SSH workspace couldn't say where the copy went")
        return {"entry": self._entry(target)}

    def delete(self, root_raw: Any, path: Any) -> Dict[str, Any]:
        root = self.root(root_raw)
        # Only the link goes, never what it points to: one to a secret can
        # be removed, as on this host.
        target = self._check(root, path, resolve=False)
        if target == root:
            raise WorkspaceError(400, "The workspace folder itself can't be deleted")
        self._run(self._guard(root) + f't={shlex.quote(target)}\n'
                  f'inside "$t" || {{ echo {_ERROR}EOUTSIDE; exit 1; }}\n'
                  f'[ -e "$t" ] || [ -L "$t" ] || {{ echo {_ERROR}ENOENT; exit 1; }}\n'
                  f'[ "$(cd "$t" 2>/dev/null && pwd -P)" = "$root" ] && {{ echo {_ERROR}EACCES; exit 1; }}\n'
                  f'rm -rf -- "$t" || {{ echo {_ERROR}EACCES; exit 1; }}')
        return {"ok": True, "path": target}

    def upload(self, root_raw: Any, folder: Any, name: Any, data: bytes, overwrite: bool = False) -> Dict[str, Any]:
        root = self.root(root_raw)
        target = self._target(root, folder, name)
        if len(data) > UPLOAD_MAX_BYTES:
            raise WorkspaceError(413, "That file is too large to upload")
        self._write(root, target, data, create=False, overwrite=overwrite)
        return {"entry": self._entry(target)}

