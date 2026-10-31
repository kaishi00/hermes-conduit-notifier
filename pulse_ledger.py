"""The outcome ledger (designs/proactivity.md 3.6).

Digests leave tool output out, so they can't say what actually happened.
This is that record: one row per tool call, from ``post_tool_call`` in
every session, cron run and subagent of the profile. It keeps the tool, its
action, a short label, the status Hermes worked out (ok or error, with the
error type), and the identifiers the result returned (a reminder or event
id, a cron job id, a path, a URL), so a later item can link to it by
evidence instead of wording. The output itself is not kept, and no model is
involved; a failed terminal command keeps one line of it as its error, the
line that says what went wrong (or the last, when none does, marked as
showing no error), so a report can tell a real failure from a harmless
non-zero exit. That line loses URL user info and queries, secret-looking
``key=value`` pairs, bearer tokens and long key-like runs first.

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
# A line of a failed command's output that says something went wrong. A
# command whose output has none of these is kept as ``exit 2, no error in its
# output: <last line>``: it exited non-zero with nothing wrong to show, like
# an ``ls`` of a glob that matched nothing with its stderr silenced.
FAILURE_MARK = re.compile(
    r"traceback|error|err!|exception|interrupt|systemexit|stop(?:async)?iteration|generatorexit|fatal|fail|denied|not found|no such|refused|timed out|timeout|cannot|can't|"
    r"could not|couldn't|unable|panic|killed|terminated|segmentation fault|abort|invalid|forbidden|"
    r"unauthori[sz]ed|unreachable|not permitted|not allowed|broken pipe|\breset\b|\bmissing\b|\bbad\b|"
    r"\bwrong\b|\bunknown\b|\bno route\b|\bresolve\b|\busage:|no rule|\bstop\.|\*\*\*|packet loss|\brejected\b|"
    r"\bconflict|\bnot ok\b|nxdomain|\bnot a\b|no space|\blocked\b|\bunmet\b|corrupt|is a directory|already in use|read-only|not empty|unavailable|\blimit\b|exceeded|quota|out of memory|\boom\b|unexpected eof|(?-i:\bE[A-Z]{3,}\b)", re.I)
# Exit codes that are never harmless whatever was printed: a timeout (124),
# a command that couldn't run (126, 127) or one killed by a signal (128+).
LOUD_EXITS = 124
# A failure is harmless only when the command itself said its failure
# doesn't matter, and its output shows nothing wrong: the part that set the
# exit silenced its errors (``2>/dev/null``), or is a yes/no check whose exit
# 1 means "no" (grep, test, diff...). Any other exit stays, however it's
# worded, so a missing failure word never hides a real failure.
EXPECTED = "an expected exit"
HARMLESS = re.compile(rf"exit -?\d+, {EXPECTED}\b")
_SILENCED = re.compile(r"(?:2|&)>\s*/dev/null|>\s*/dev/null\s+2>&1")
_YES_NO = frozenset({"grep", "egrep", "fgrep", "rg", "test", "[", "[[", "diff", "cmp", "pgrep", "which", "type"})
# Lookups whose silenced errors are only "not there": a silenced git push,
# curl or script still counts, since what it silenced was its failure.
_LOOKUPS = frozenset({"ls", "find", "stat", "cat", "head", "tail", "file", "du", "wc", "readlink", "realpath",
                      "test", "[", "[["}) | _YES_NO
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})
# What may feed a yes/no check in a pipe without hiding a failure of its own.
_PRODUCERS = _LOOKUPS | frozenset({"ps", "echo", "printf", "printenv", "sort", "uniq", "cut", "tr"})
NO_OUTPUT = "no output"
# What a kept output line could carry that mustn't be kept: a URL's user info
# and query (each URL cleaned on its own, so the rest of the line stays),
# ``user:pass@`` without a scheme, a secret-looking ``key=value`` and a
# bearer token, and any long run of letters and digits that could be a key.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_USERINFO = re.compile(r"[^\s:/@'\"]+:[^\s:/@'\"]+@(?=[\w.-])")
# A strong key's value is always a secret; a weak key's (auth, session,
# cookie, signature, token) only when it looks like one, so "Authentication:
# failed" keeps its reason. A quoted value (JSON) always counts.
_STRONG_KEY = re.compile(r"(?i)passw(?:or)?d|pwd|(?:\b|_)pass\b|secret|api[_ -]?key|apikey|credential|private[_-]?key|"
                         r"access[_-]?key")
_SECRET_PAIR = re.compile(
    r"(?i)\b([\w.-]*(?:passw(?:or)?d|pwd|(?:\b|_)pass\b|secret|api[_ -]?key|apikey|credential|private[_-]?key|"
    r"access[_-]?key|token|"
    r"auth|authorization|cookie|session|signature)[\w.-]*)([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)")
_FLAG_SECRET = re.compile(r"(?i)(--?(?:password|passwd|pass|token|secret|api-key|apikey|auth)[ =]|\s(?:-[uU]|--(?:proxy-)?user)(?:\s+|=)(?=\S*:)|\s-p(?=\S))\S+")
# A spaced -p is often a path (mkdir -p), so only a secret-looking value goes.
_SPACED_P = re.compile(r"(\s-p\s+)(\S+)")
_KNOWN_TOKEN = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-)[A-Za-z0-9_-]{10,})")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=-]{8,})")
# Webhook URLs carry their secret in the path (Slack, Discord).
_WEBHOOK_PATH = re.compile(r"(?i)(/services/T\w+/B\w+/|/api/webhooks/\d+/)[\w-]+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LONG_RUN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")
REDACTED = "[redacted]"
# How much of a kept line is redacted: this runs inside the tool call, and the
# patterns cost more than linear on one very long run of characters.
REDACT_WINDOW = 400
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


# A value that says what's wrong with a secret rather than being one.
_JUDGMENT = re.compile(r"(?i)[\"']?(?:missing|invalid|expired|required|wrong|failed|empty|incorrect|denied|none|null|"
                       r"unset|not|undefined|rejected|revoked)[\"'.,]?")


def _looks_secret(value: str) -> bool:
    bare = value.strip("\"'")
    if value[:1] in "\"'" or (any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare)):
        return True
    return len(bare) >= 16 and not bare.replace("_", "").replace("-", "").isalpha()


def _bearer_secret(scheme: str, value: str) -> bool:
    # Bearer is always followed by its token; basic and token are also words.
    if _JUDGMENT.fullmatch(value):
        return False
    return scheme.lower() == "bearer" or len(value) >= 16 or _looks_secret(value)


def _redact(text: str) -> str:
    text = _ANSI.sub("", text)
    text = _URL_IN_TEXT.sub(lambda m: _WEBHOOK_PATH.sub(lambda w: w.group(1) + REDACTED, _safe_url(m.group())), text)
    text = _WEBHOOK_PATH.sub(lambda w: w.group(1) + REDACTED, text)
    text = _USERINFO.sub("", text)
    text = _KNOWN_TOKEN.sub(REDACTED, text)
    text = _FLAG_SECRET.sub(lambda m: m.group(1) + REDACTED, text)
    text = _SPACED_P.sub(lambda m: m.group(1) + REDACTED if _looks_secret(m.group(2)) else m.group(), text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}" if _bearer_secret(m.group(1), m.group(2)) else m.group(),
                       text)
    text = _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + REDACTED
                            if not _JUDGMENT.fullmatch(m.group(3))
                            and (_STRONG_KEY.search(m.group(1)) or _looks_secret(m.group(3))) else m.group(), text)
    return _LONG_RUN.sub(REDACTED, text)


def _around(line: str, at: int) -> str:
    """At most REDACT_WINDOW characters on each side of ``at``. A cut start
    moves on to the next space, so it never begins inside a token (a URL's
    user info among them)."""
    start, end = max(0, at - REDACT_WINDOW), at + REDACT_WINDOW
    part = line[start:end]
    if start:
        # With no space before ``at``, the window starts at ``at`` itself.
        part = part[part.index(" ") + 1:] if " " in part[:at - start] else line[at:end]
    return part


def _clip(text: str, limit: int) -> str:
    text = _printable(re.sub(r"\s+", " ", text).strip())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _command_name(part: str) -> tuple[str, list[str]]:
    words = [word for word in part.split() if "=" not in word.split("/")[0] and word not in ("sudo", "env")]
    first = words[0] if words else ""
    # A project's own ./test or scripts/which isn't the tool it's named after.
    return (first.rsplit("/", 1)[-1] if "/" not in first or first.startswith(("/bin/", "/usr/")) else ""), words


def expected_exit(command: Any, code: int) -> bool:
    """True when the part of ``command`` that set exit ``code`` said its
    failure doesn't matter. Only the last statement counts (after ``;`` or a
    new line); in an ``&&`` chain every step must qualify, an ``||`` never
    does. In a pipe the last command sets the exit, and every earlier one
    must be a lookup or a plain producer, so a failing ``curl`` piped into
    ``grep -q`` still shows. ``find`` with an action runs other commands, so
    it's no lookup."""
    if not isinstance(command, str) or not 0 < code < LOUD_EXITS:
        return False
    statement = re.split(r"[;\n]", command.strip().rstrip(";").strip())[-1]
    # A lone & backgrounds a command and runs the next one, so it hides nothing.
    if "||" in statement or "&" in re.sub(r"&&|\d*>&\d*-?|&>", "", statement) or not statement.strip():
        return False
    for step in statement.split("&&"):
        *earlier, last = [part.strip() for part in step.split("|")]
        for part in earlier:
            name, words = _command_name(part)
            if name not in _PRODUCERS or (name == "find" and _FIND_ACTIONS & set(words)):
                return False
        name, words = _command_name(last)
        # A yes/no check is excused at exit 1 only, silenced or not: grep's 2 is an error.
        lookup = name in _LOOKUPS - _YES_NO and not (name == "find" and _FIND_ACTIONS & set(words))
        if not ((_SILENCED.search(last) and lookup) or (code == 1 and name in _YES_NO)):
            return False
    return True


def terminal_failure(result: Any, command: Any = None) -> Optional[str]:
    """A failed terminal command's error, ``exit 1: <line>``, from its result
    (``output``/``exit_code``/``error``); None when it didn't fail or has an
    error of its own. Hermes words a bare non-zero exit in the user's language
    and leaves the output out, so this is worked out here. Whether the exit
    was expected (expected_exit, with no failure word in the output) is
    decided on the raw command and output and written into the error, so
    redacting or cutting the kept line can't change it."""
    parsed = _parsed(result)
    if not isinstance(parsed, dict) or parsed.get("error"):
        return None
    code = parsed.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int) or code == 0:
        return None
    output = parsed.get("output")
    lines = [part.strip() for part in output.splitlines() if part.strip()] if isinstance(output, str) else []
    if not lines:
        return f"exit {code}, {EXPECTED}, {NO_OUTPUT}" if expected_exit(command, code) else f"exit {code}, {NO_OUTPUT}"
    marked = [(part, mark) for part in lines if (mark := FAILURE_MARK.search(part))]
    if marked:
        # The last marked line is the exception under a traceback. It's
        # redacted whole first (a cut could start inside a URL's user info),
        # then a long one is kept from just before its mark, so the mark
        # survives the cut.
        part, first = marked[-1]
        clean = _redact(_around(part, first.start()))
        mark = FAILURE_MARK.search(clean)
        start = max(0, mark.start() - 60) if mark else 0
        head, excerpt = f"exit {code}", ("…" if start else "") + clean[start:]
    else:
        head = f"exit {code}, {EXPECTED}" if expected_exit(command, code) else f"exit {code}"
        # The line's end is what's kept: room is what MAX_ERROR leaves after the head.
        tail = lines[-1][-2 * REDACT_WINDOW:]
        if len(tail) < len(lines[-1]):
            # Never start inside a token: past the first space, or past a cut
            # URL's user info when there's none.
            tail = tail[tail.index(" ") + 1:] if " " in tail else re.sub(r"^[^\s/@]*@", "", tail)
        excerpt = _printable(re.sub(r"\s+", " ", _redact(tail)).strip())
        room = MAX_ERROR - len(head) - 2
        if len(excerpt) > room:
            excerpt = "…" + excerpt[-(room - 1):]
    return _clip(f"{head}: {excerpt}", MAX_ERROR)


def harmless_exit(row: dict[str, Any]) -> bool:
    """A terminal command whose non-zero exit it said doesn't matter, with
    nothing wrong in its output (terminal_failure). A bare exit from before
    outputs were kept isn't one: there's nothing to judge it by."""
    if row.get("tool") != "terminal" or row.get("status") != "error":
        return False
    return bool(HARMLESS.match(row.get("error") or ""))


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
    # A terminal failure's error is already redacted and cut; _short's URL
    # cleaning would cut it again at a plain "#" or "?".
    command = args.get("command") if isinstance(args, dict) else None
    kept = terminal_failure(result, command) if status == "error" and tool == "terminal" else None
    action, label, digest = describe_call(args)
    pulse_store = _store(home)
    pulse_store.add_outcome({
        "at": now, "session_id": _text(session_id), "turn_id": _text(turn_id), "task_id": _text(task_id),
        "tool_call_id": _text(tool_call_id), "tool": tool, "action": action, "label": label,
        "args_digest": digest, "status": status, "error_type": _short(error_type or "", 60),
        "error": (kept or _short(error_message or "", MAX_ERROR)) if status == "error" else "",
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
