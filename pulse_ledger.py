"""The outcome ledger (designs/proactivity.md 3.6).

Digests leave tool output out, so they can't say what actually happened.
This is that record: one row per tool call, from ``post_tool_call`` in
every session, cron run and subagent of the profile. It keeps the tool, its
action, a short label, the status Hermes worked out (ok or error, with the
error type), and the identifiers the result returned (a reminder or event
id, a cron job id, a path, a URL), so a later item can link to it by
evidence instead of wording. The output itself is not kept, and no model is
involved.

Recorded only while Pulse is on.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from . import pulse_store as store

# How long `enabled` is trusted before the store is read again: this runs on
# every tool call in every process.
ENABLED_TTL_S = 30.0
RETENTION_S = 90 * 86400.0
MAX_ROWS = 50_000
PRUNE_EVERY_S = 3600.0
MAX_IDS = 8
MAX_ID_LEN = 160
MAX_LABEL = 80
MAX_ERROR = 200
# The ledger write runs inside the tool call: under contention it gives up
# quickly instead of holding the call up.
WRITE_BUSY_TIMEOUT_S = 0.5

# Arguments that name what a call was about. Never the command, prompt or
# content: those can carry secrets, and the transcript already has them.
LABEL_KEYS = ("title", "name", "summary", "label", "query", "path", "url", "file", "job_name")
ACTION_KEYS = ("action", "operation", "op", "mode")
# Identifier keys the result may carry, at the top level or one level in.
ID_KEYS = frozenset({"id", "path", "url", "file", "file_path", "uri", "href"})
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
NESTED_KEYS = ("result", "data", "item", "reminder", "event", "job", "watch", "file", "created", "updated")

_enabled: dict[str, tuple[float, bool]] = {}
_stores: dict[str, store.PulseStore] = {}
_last_prune: dict[str, float] = {}
_lock = threading.Lock()


def recording(home: Path, now: Optional[float] = None) -> bool:
    """Whether Pulse is on for this home, cached for ``ENABLED_TTL_S``. A
    home where Pulse was never turned on gets no store file."""
    now = time.monotonic() if now is None else now
    key = str(home)
    with _lock:
        cached = _enabled.get(key)
        if cached and now < cached[0]:
            return cached[1]
    on = (Path(home) / store.DB_NAME).exists() and bool(_store(home).get("enabled"))
    with _lock:
        _enabled[key] = (now + ENABLED_TTL_S, on)
    return on


def _store(home: Path) -> store.PulseStore:
    """One store per home for the hot path, so its schema runs once."""
    key = str(home)
    with _lock:
        cached = _stores.get(key)
    if cached is None:
        cached = store.PulseStore(Path(home) / store.DB_NAME, busy_timeout_s=WRITE_BUSY_TIMEOUT_S)
        with _lock:
            cached = _stores.setdefault(key, cached)
    return cached


def forget_cache() -> None:
    with _lock:
        _enabled.clear()
        _stores.clear()
        _last_prune.clear()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _printable(text: str) -> str:
    return "".join(c for c in text if c.isprintable())


def _safe_url(text: str) -> str:
    """A URL without its user info, query or fragment, where tokens ride.
    A value with no scheme loses everything from its first ``?`` or ``#``
    when it looks like a path or carries a ``key=value`` after it; plain
    words with a question mark are kept."""
    if not _URL.match(text):
        cut = re.search(r"[?#]", text)
        if cut and ("/" in text or "=" in text[cut.start():]):
            return text[:cut.start()]
        return text
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        return text.split("://", 1)[0] + "://…"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _short(value: Any, limit: int) -> str:
    text = _safe_url(re.sub(r"\s+", " ", value if isinstance(value, str) else str(value)).strip())
    text = _printable(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _parsed(result: Any) -> Any:
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, (bytes, bytearray)):
        result = result.decode("utf-8", "replace")
    if isinstance(result, str) and result[:1] in "{[":
        try:
            return json.loads(result)
        except ValueError:
            return None
    return None


def _is_id_key(key: str) -> bool:
    # ``id``, ``*_id``, a few path-like keys, and camelCase ``*Id`` that
    # starts lowercase (``watchId``); ``ID`` or ``PaidId`` style keys don't count.
    lowered = key.lower()
    return lowered in ID_KEYS or lowered.endswith("_id") or (key.endswith("Id") and key[:1].islower())


def extract_ids(result: Any) -> dict[str, str]:
    """The identifiers a tool result names, as ``{key: value}`` (``job.id``
    for one found a level in). Only scalars, a few of them, kept short."""
    parsed = _parsed(result)
    found: dict[str, str] = {}

    def take(prefix: str, obj: dict) -> None:
        for key, value in obj.items():
            if len(found) >= MAX_IDS:
                return
            if not isinstance(key, str) or isinstance(value, bool) or not _is_id_key(key):
                continue
            if isinstance(value, (str, int)) and str(value).strip():
                found.setdefault(prefix + key, _short(value, MAX_ID_LEN))

    if isinstance(parsed, dict):
        take("", parsed)
        for nested in NESTED_KEYS:
            inner = parsed.get(nested)
            if isinstance(inner, dict):
                take(f"{nested}.", inner)
            elif isinstance(inner, list) and inner and isinstance(inner[0], dict):
                take(f"{nested}[0].", inner[0])
    return found


def describe_call(args: Any) -> tuple[str, str, str]:
    """``(action, label, digest)`` of a call's arguments. The digest matches
    a repeated call without keeping its arguments."""
    if not isinstance(args, dict):
        args = {}
    action = next((_short(args[k], 40) for k in ACTION_KEYS if isinstance(args.get(k), str) and args[k]), "")
    label = next((_short(args[k], MAX_LABEL) for k in LABEL_KEYS if isinstance(args.get(k), str) and args[k].strip()),
                 "")
    try:
        canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(args)
    return action, label, hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def record(home: Path, *, tool_name: Any, args: Any = None, result: Any = None, session_id: Any = None,
           turn_id: Any = None, task_id: Any = None, tool_call_id: Any = None, status: Any = None,
           error_type: Any = None, error_message: Any = None, duration_ms: Any = 0,
           now: Optional[float] = None) -> bool:
    """Writes one tool call's outcome; False when Pulse is off or there's no
    tool name. Hermes works out ``status`` (ok or error) for the hook; one
    that sends none counts the call as ok unless the result says error."""
    tool = _text(tool_name)
    if not tool or not recording(home):
        return False
    now = time.time() if now is None else now
    if status not in ("ok", "error"):
        parsed = _parsed(result)
        failed = isinstance(parsed, dict) and bool(parsed.get("error"))
        status = "error" if failed else "ok"
        if failed and not error_message:
            error_message = parsed.get("error")
    action, label, digest = describe_call(args)
    pulse_store = _store(home)
    pulse_store.add_outcome({
        "at": now, "session_id": _text(session_id), "turn_id": _text(turn_id), "task_id": _text(task_id),
        "tool_call_id": _text(tool_call_id), "tool": tool, "action": action, "label": label,
        "args_digest": digest, "status": status, "error_type": _short(error_type or "", 60),
        "error": _short(error_message or "", MAX_ERROR) if status == "error" else "",
        "ids_json": json.dumps(extract_ids(result), ensure_ascii=False),
        "duration_ms": int(duration_ms) if isinstance(duration_ms, (int, float)) and duration_ms >= 0 else 0,
    })
    key = str(home)
    with _lock:
        due = now - _last_prune.get(key, 0.0) >= PRUNE_EVERY_S
        if due:
            _last_prune[key] = now
    if due:
        pulse_store.prune_outcomes(before=now - RETENTION_S, keep=MAX_ROWS)
    return True


def line(row: dict[str, Any]) -> str:
    """One outcome as a digest line: ``phone action 'Call dentist' → ok, id R123``."""
    parts = [row["tool"]]
    if row.get("action"):
        parts.append(row["action"])
    if row.get("label"):
        parts.append(f"'{row['label']}'")
    result = row["status"]
    if row["status"] == "error":
        why = row.get("error") or row.get("error_type")
        if why:
            result += f": {why}"
    try:
        ids = json.loads(row.get("ids_json") or "{}")
    except ValueError:
        ids = {}
    if isinstance(ids, dict) and ids:
        result += ", " + ", ".join(f"{key} {value}" for key, value in ids.items())
    return _printable(f"{' '.join(parts)} → {result}")
