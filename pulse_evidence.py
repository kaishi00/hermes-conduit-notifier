"""What happened after a remembered item came up, beyond its own chat.

A promise or commitment is often settled somewhere else: the work gets done
in another chat or by a cron run, a later release makes the question moot,
or a background process is still on it. A follow-up check and the report
read only the item's own chat and memory's open items, so they kept asking
about things already done or still running (Eric, 2026-10-24).

Two lookups, both read-only:

- ``later``: rows in other chats and cron runs after the item came up that
  share at least ``MIN_SHARED`` words with what the item says, the closest
  first, as much as a budget holds;
- ``background``: what Hermes is running in the background right now, from
  the process registry's crash-recovery checkpoint (``processes.json``),
  with the chat that started each process.
"""

from __future__ import annotations

import functools
import json
import os
import re
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import pulse_hermes as hermes

PROCESSES = "processes.json"
# Rows read per lookup, newest first, each cut to PER_ROW characters.
MAX_ROWS = 5000
PER_ROW = 600
# Shared words a row needs to count as on the same subject.
MIN_SHARED = 2
LATER_BUDGET = 3_000
MAX_PROCESSES = 12
# Largest pid os.kill takes; a bigger one is a corrupt checkpoint.
MAX_PID = 2**31 - 1

_QUOTES = str.maketrans("‘’“”", "''\"\"")
_WORD = re.compile(r"[a-z0-9][a-z0-9'.-]{2,}")
# Words too common to say two messages are about the same thing.
_COMMON = frozenset("""
the and for are but not you your yours with this that these those from have has had was were will would
could should can just any all into onto out about after before again also been being both each few more
most other some such than then them they their there here when where which while who whom why how what
its it's i'm i'll i've i'd you'll you're we're we'll our ours let's say said says tell told sure okay ok
yes yeah done still want wants need needs make made like know get got going gonna one two now later
today tomorrow yesterday week day time thing things way back over under very really much many well good
right left new old ask asked check checks checking soon hermes pulse chat user don't didn't doesn't won't
can't isn't aren't wasn't
""".split())


def words(text: Any) -> frozenset[str]:
    """The words of ``text`` that can tie two messages together."""
    return _words(text if isinstance(text, str) else "")


@functools.lru_cache(maxsize=MAX_ROWS)
def _words(text: str) -> frozenset[str]:
    # Cached: the report scores the same rows against every item.
    raw = re.sub(r"\s+", " ", text.translate(_QUOTES)).casefold()
    return frozenset({word.strip(".-'") for word in _WORD.findall(raw)} - _COMMON - {""})


def _local(epoch: float, zone: tzinfo) -> str:
    return datetime.fromtimestamp(epoch, zone).strftime("%a %d %b %H:%M")


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def later_rows(home: Path, *, after: float, exclude: Iterable[str], limit: int = MAX_ROWS) -> list[Any]:
    """Rows written after ``after`` outside the chats in ``exclude``, newest first."""
    return hermes.later_messages(home, after=after, exclude=exclude, max_chars=PER_ROW, limit=limit)


def _scored(rows: Sequence[Any], subject: str, after: float) -> list[Any]:
    """The rows after ``after`` sharing at least MIN_SHARED words with
    ``subject``, the closest first, then the newest."""
    wanted = words(subject)
    if len(wanted) < MIN_SHARED:
        return []
    scored = []
    for row in rows:
        if row["timestamp"] <= after:
            continue
        shared = len(wanted & words(row["content"]))
        if shared >= MIN_SHARED:
            scored.append((shared, row))
    scored.sort(key=lambda pair: (-pair[0], -pair[1]["timestamp"]))
    return [row for _, row in scored]


def closest(rows: Sequence[Any], subject: str, *, after: float) -> Any:
    """The row closest to ``subject`` after ``after``, or None."""
    found = _scored(rows, subject, after)
    return found[0] if found else None


def related(rows: Sequence[Any], subject: str, *, after: float, budget: int = LATER_BUDGET) -> list[Any]:
    """The rows after ``after`` that share at least MIN_SHARED words with
    ``subject``: the closest first, then the newest, as many as ``budget``
    characters hold; oldest first."""
    kept, used = [], 0
    for row in _scored(rows, subject, after):
        size = min(len(row["content"] or ""), PER_ROW) + 60
        if used + size > budget:
            continue
        kept.append(row)
        used += size
    return sorted(kept, key=lambda row: (row["timestamp"], row["id"]))


def lines(rows: Sequence[Any], zone: tzinfo) -> list[str]:
    out = []
    for row in rows:
        where = "cron run" if row["source"] == "cron" else "chat"
        who = "User" if row["role"] == "user" else "Hermes"
        title = _clip(row["title"] or "Untitled", 60)
        out.append(f"[{_local(row['timestamp'], zone)}] {where} \"{title}\", {who}: {_clip(row['content'], PER_ROW)}")
    return out


def later_section(home: Path, subject: str, *, after: float, exclude: Iterable[str], zone: tzinfo) -> str:
    """The "later in other chats" section for one item; "" when nothing
    matches. Raises HermesUnreadable like the other lookups."""
    found = related(later_rows(home, after=after, exclude=exclude), subject, after=after)
    if not found:
        return ""
    return ("## Later in other chats and cron runs, on the same subject\n"
            "(Matched by shared words, so some may be about something else.)\n" + "\n".join(lines(found, zone)))


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


def _alive(entry: dict) -> bool:
    """False only for a host process that is surely gone; a sandboxed one,
    or one this process can't signal, counts as running."""
    pid = entry.get("pid")
    if entry.get("pid_scope") not in (None, "", "host") or not isinstance(pid, int) or isinstance(pid, bool):
        return True
    if not 0 < pid <= MAX_PID:
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


def _started(entry: dict) -> float:
    """When the process started; 0 when the checkpoint has no sane time."""
    value = entry.get("started_at")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value < 1e11:
        return 0.0  # NaN fails the range test too
    return float(value)


def processes(home: Path) -> list[dict]:
    """Background processes Hermes is running now, oldest first. A missing
    or unreadable checkpoint is none."""
    try:
        data = json.loads((Path(home) / PROCESSES).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    entries = [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
    running = [entry for entry in entries if _alive(entry)]

    return sorted(running, key=_started)


def _redact(text: str) -> str:
    """``text`` with secrets taken out by Hermes's own redactor, again on read
    in case an older Hermes wrote the checkpoint without it; "" when the
    redactor can't be loaded, so nothing unredacted leaves the host."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception:  # noqa: BLE001 - no redactor, no text
        return ""


def background(home: Path, zone: tzinfo) -> str:
    """The "running in the background" section: each process's command, when
    it started and the chat that started it; "" when nothing runs. The text
    goes to the model and into the report turn, so each command and pattern
    passes Hermes's redactor first; without it, a process shows as "a
    background process"."""
    running = processes(home)
    if not running:
        return ""
    def parent(entry: dict) -> str:
        value = entry.get("parent_session_id")
        return value if isinstance(value, str) else ""
    try:
        titles = hermes.chat_titles(home, [parent(entry) for entry in running])
    except hermes.HermesUnreadable:
        titles = {}
    out = []
    for entry in running[:MAX_PROCESSES]:
        started = _started(entry)
        when = f", since {_local(started, zone)}" if started else ""
        title = titles.get(parent(entry), "")
        source = f", started in \"{_clip(title, 60)}\"" if title else ""
        watching = entry.get("watch_patterns")
        patterns = [_redact(_clip(p, 40)) for p in watching[:3]] if isinstance(watching, list) else []
        watch = f", watching for {', '.join(p for p in patterns if p)}" if any(patterns) else ""
        command = _redact(_clip(entry.get("command"), 160)) or "a background process"
        out.append(f"- {command}{when}{source}{watch}")
    more = f"\n(and {len(running) - MAX_PROCESSES} more)" if len(running) > MAX_PROCESSES else ""
    return "## Running in the background now\n" + "\n".join(out) + more
