"""Terminals on the Hermes host, for Conduit for desktop's workbench (issue 450).

A terminal is a shell started in a chat's folder, on a pseudo-terminal this
process holds. It keeps running while no window shows it (Conduit closed, the
workbench folded): its last output is kept, and a window that comes back asks
for what it missed by offset. It ends when the person closes it, when its
shell exits, after a week with no window on it, or when the dashboard stops.

Output is bytes, counted from the terminal's start: ``end`` is how many bytes
it has written, and ``start`` the first one still kept. A viewer is a
callback this calls from the reader thread with each chunk; the socket route
hands it to its event loop.

POSIX hosts only: Windows has no pty module, and terminals answer 501 there.
"""

from __future__ import annotations

import errno
import os
import secrets
import signal
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import subprocess
import sys

try:  # POSIX only
    import fcntl
    import termios
except ImportError:  # pragma: no cover - Windows
    fcntl = termios = None  # type: ignore[assignment]

# Run as ``python -I -c _CONTROLLING_TTY shell args``: the pty on stdin becomes
# this new session's controlling terminal, then the shell replaces it.
_CONTROLLING_TTY = (
    "import os, sys, fcntl, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)

SCROLLBACK_BYTES = 256 * 1024
READ_CHUNK = 64 * 1024
MAX_PER_PROFILE = 8
MAX_TOTAL = 32
IDLE_UNWATCHED_S = 7 * 24 * 3600
MAX_TITLE_CHARS = 80
# How long typing waits on a shell that isn't reading before it's dropped.
WRITE_TIMEOUT_S = 2.0
MAX_INPUT_BYTES = 64 * 1024
MIN_SIZE, MAX_COLS, MAX_ROWS = 2, 500, 200
KILL_GRACE_S = 2.0


class TerminalError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def supported() -> bool:
    return termios is not None and hasattr(os, "openpty")


def _shell() -> List[str]:
    shell = os.environ.get("SHELL") or ""
    if not shell or not os.path.isfile(shell):
        try:
            import pwd
            shell = pwd.getpwuid(os.getuid()).pw_shell or ""
        except Exception:
            shell = ""
    if not shell or not os.path.isfile(shell):
        shell = "/bin/sh"
    # A login shell, so the PATH and prompt are the ones the person's own
    # terminal has there.
    return [shell, "-l"]


def _size(cols: Any, rows: Any) -> Tuple[int, int]:
    try:
        cols, rows = int(cols), int(rows)
    except (TypeError, ValueError):
        return 80, 24
    return max(MIN_SIZE, min(MAX_COLS, cols)), max(MIN_SIZE, min(MAX_ROWS, rows))


class Terminal:
    def __init__(self, owner: str, root: str, title: str, cols: int, rows: int,
                 argv: Optional[List[str]] = None, clock: Callable[[], float] = time.time) -> None:
        self.id = secrets.token_urlsafe(12)
        self.owner = owner
        self.root = root
        self.title = title
        self.created = clock()
        self._clock = clock
        self.last_watched = self.created
        self.exit_code: Optional[int] = None
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._end = 0
        self._viewers: Dict[int, Callable[[Optional[bytes], int], None]] = {}
        self._next_viewer = 0
        self.cols, self.rows = cols, rows
        command = argv or _shell()
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "Conduit"}
        master, slave = os.openpty()
        try:
            # Started by subprocess (no Python runs between fork and exec in
            # this threaded process); a small helper makes the pty the new
            # session's controlling terminal, so job control and Ctrl+C work,
            # then becomes the shell.
            self._process = subprocess.Popen([sys.executable, "-I", "-c", _CONTROLLING_TTY, *command],
                                             stdin=slave, stdout=slave, stderr=slave, cwd=root, env=env,
                                             start_new_session=True, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.pid, self.fd = self._process.pid, master
        self._resize_fd(cols, rows)
        self._reader = threading.Thread(target=self._read, name=f"conduit-terminal-{self.id[:6]}", daemon=True)
        self._reader.start()

    # Output

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.fd, READ_CHUNK)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                chunk = b""
            if not chunk:
                break
            with self._lock:
                self._buffer += chunk
                self._end += len(chunk)
                if len(self._buffer) > SCROLLBACK_BYTES:
                    del self._buffer[:len(self._buffer) - SCROLLBACK_BYTES]
                viewers = list(self._viewers.values())
                end = self._end
            for viewer in viewers:
                try:
                    viewer(chunk, end)
                except Exception:
                    pass
        code = self._wait()
        with self._lock:
            self.exit_code = code
            viewers = list(self._viewers.values())
            end = self._end
        for viewer in viewers:
            try:
                viewer(None, end)
            except Exception:
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _wait(self) -> int:
        code = self._process.wait()
        return 128 - code if code < 0 else code

    @property
    def start(self) -> int:
        with self._lock:
            return self._end - len(self._buffer)

    @property
    def end(self) -> int:
        with self._lock:
            return self._end

    def since(self, offset: Any) -> Tuple[bytes, int, int]:
        """What the terminal wrote from ``offset`` (bytes), and the kept range."""
        with self._lock:
            start = self._end - len(self._buffer)
            try:
                offset = int(offset)
            except (TypeError, ValueError):
                offset = start
            offset = max(start, min(self._end, offset))
            return bytes(self._buffer[offset - start:]), start, self._end

    def watch(self, viewer: Callable[[Optional[bytes], int], None], offset: Any = None) -> Tuple[int, bytes, int, int]:
        """Adds a viewer, atomically with what it missed: (key, missed, start, end)."""
        with self._lock:
            start = self._end - len(self._buffer)
            try:
                offset = int(offset) if offset is not None else start
            except (TypeError, ValueError):
                offset = start
            offset = max(start, min(self._end, offset))
            missed = bytes(self._buffer[offset - start:])
            key = self._next_viewer
            self._next_viewer += 1
            self._viewers[key] = viewer
            self.last_watched = self._clock()
            return key, missed, start, self._end

    def unwatch(self, key: int) -> None:
        with self._lock:
            self._viewers.pop(key, None)
            self.last_watched = self._clock()

    @property
    def watched(self) -> bool:
        with self._lock:
            return bool(self._viewers)

    # Input

    def write(self, data: bytes) -> None:
        if self.exit_code is not None:
            raise TerminalError(409, "This terminal has ended")
        if len(data) > MAX_INPUT_BYTES:
            raise TerminalError(413, "That's too much to type at once")
        view = memoryview(data)
        deadline = time.monotonic() + WRITE_TIMEOUT_S
        while view:
            try:
                written = os.write(self.fd, view)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if exc.errno == errno.EAGAIN:
                    # The shell isn't reading: what's left is dropped rather than holding a worker.
                    if time.monotonic() >= deadline:
                        raise TerminalError(409, "This terminal isn't reading what's typed")
                    time.sleep(0.01)
                    continue
                raise TerminalError(409, "This terminal has ended")
            view = view[written:]

    def _resize_fd(self, cols: int, rows: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def resize(self, cols: Any, rows: Any) -> None:
        self.cols, self.rows = _size(cols, rows)
        if self.exit_code is None:
            self._resize_fd(self.cols, self.rows)

    def close(self) -> None:
        if self.exit_code is not None:
            return
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                try:
                    os.kill(self.pid, sig)
                except ProcessLookupError:
                    return
            deadline = time.monotonic() + KILL_GRACE_S
            while time.monotonic() < deadline:
                if self.exit_code is not None:
                    return
                time.sleep(0.02)

    def row(self) -> Dict[str, Any]:
        with self._lock:
            return {"id": self.id, "root": self.root, "title": self.title, "created": self.created,
                    "exited": self.exit_code is not None, "exitCode": self.exit_code,
                    "watched": bool(self._viewers), "start": self._end - len(self._buffer), "end": self._end,
                    "cols": self.cols, "rows": self.rows}


class TerminalHost:
    """Every terminal this process holds, by owner (a profile's key)."""

    def __init__(self, clock: Callable[[], float] = time.time,
                 argv: Optional[List[str]] = None) -> None:
        self._clock = clock
        self._argv = argv
        self._lock = threading.Lock()
        self._terminals: Dict[str, Terminal] = {}

    def _reap(self) -> None:
        now = self._clock()
        with self._lock:
            stale = [t for t in self._terminals.values()
                     if not t.watched and now - t.last_watched > IDLE_UNWATCHED_S]
            for terminal in stale:
                self._terminals.pop(terminal.id, None)
        for terminal in stale:
            terminal.close()

    def create(self, owner: str, root: str, title: Any = None, cols: Any = 80, rows: Any = 24) -> Terminal:
        if not supported():
            raise TerminalError(501, "Terminals need a Linux or Mac Hermes host")
        self._reap()
        with self._lock:
            mine = [t for t in self._terminals.values() if t.owner == owner and t.exit_code is None]
            if len(mine) >= MAX_PER_PROFILE:
                raise TerminalError(429, f"At most {MAX_PER_PROFILE} terminals at a time: close one first")
            if len([t for t in self._terminals.values() if t.exit_code is None]) >= MAX_TOTAL:
                raise TerminalError(429, "This Hermes has too many terminals open: close one first")
            # Ended terminals of this owner beyond the limit are forgotten.
            ended = sorted((t for t in self._terminals.values() if t.owner == owner and t.exit_code is not None),
                           key=lambda t: t.created)
            for terminal in ended[:-MAX_PER_PROFILE]:
                self._terminals.pop(terminal.id, None)
        text = title.strip()[:MAX_TITLE_CHARS] if isinstance(title, str) and title.strip() else ""
        cols, rows = _size(cols, rows)
        try:
            terminal = Terminal(owner, root, text or os.path.basename(root.rstrip("/")) or root, cols, rows,
                                argv=self._argv, clock=self._clock)
        except OSError as exc:
            raise TerminalError(500, f"Couldn't start a shell ({exc.strerror or 'error'})")
        with self._lock:
            self._terminals[terminal.id] = terminal
        return terminal

    def get(self, owner: str, terminal_id: Any) -> Terminal:
        with self._lock:
            terminal = self._terminals.get(terminal_id) if isinstance(terminal_id, str) else None
        if terminal is None or terminal.owner != owner:
            raise TerminalError(404, "That terminal has closed")
        return terminal

    def list(self, owner: str, root: Optional[str] = None) -> List[Dict[str, Any]]:
        self._reap()
        with self._lock:
            mine = [t for t in self._terminals.values() if t.owner == owner and (root is None or t.root == root)]
        return [t.row() for t in sorted(mine, key=lambda t: t.created)]

    def rename(self, owner: str, terminal_id: Any, title: Any) -> Terminal:
        terminal = self.get(owner, terminal_id)
        if not isinstance(title, str) or not title.strip():
            raise TerminalError(400, "A name is required")
        terminal.title = title.strip()[:MAX_TITLE_CHARS]
        return terminal

    def close(self, owner: str, terminal_id: Any) -> None:
        terminal = self.get(owner, terminal_id)
        with self._lock:
            self._terminals.pop(terminal.id, None)
        terminal.close()

    def close_all(self) -> None:
        with self._lock:
            terminals = list(self._terminals.values())
            self._terminals.clear()
        for terminal in terminals:
            terminal.close()


_host: Optional[TerminalHost] = None
_host_lock = threading.Lock()


def host() -> TerminalHost:
    """This process's terminals, made once."""
    global _host
    with _host_lock:
        if _host is None:
            _host = TerminalHost()
        return _host
