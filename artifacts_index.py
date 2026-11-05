"""What Hermes made in a profile's chats, for Conduit's Artifacts screens.

An artifact is anything a chat produced or carried that the user may want to
open again:

- ``delivered``: a file Hermes sent with a ``MEDIA:<path>`` tag;
- ``preview``: an HTML page Hermes drew in place with ``::preview{file="…"}``;
- ``written``: a document, image or media file Hermes wrote with a tool
  (``write_file``, or a producer tool such as ``text_to_speech`` or
  ``image_generate`` that reports the file it saved). Source code a coding
  session writes is left out by its extension. A picture ``image_generate``
  reports by URL is listed as a link;
- ``attached``: a file the user sent, as its ``@file:`` reference;
- ``link``: a web link in one of Hermes' replies.

Hidden chats (a group room member's turns, say) and a bot's workstreams
(``workstreams``, the caller's list: Hermes shows a resolved one so
Desktop's link opens it) are left out of the profile's list, but a bot's Bot Chat, a hidden session titled "Bot Chat", is
listed, and a chat asked for by id is read whether hidden or not.

The list is read from the profile's ``state.db`` (read only), newest message
first, a bounded window of rows per request: ``before`` pages back from the
message id the previous page stopped at. Within a page one path or URL is
listed once, at its newest mention; a later page can list it again at an
older one, and the client keeps the first.

The dashboard loads this module by path, so it imports nothing from the
plugin package.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

# Rows scanned per window, and the most one request reads before it hands back
# a cursor, so a large history never holds a request for long.
WINDOW_ROWS = 4_000
MAX_SCAN_ROWS = 40_000
DEFAULT_LIMIT = 60
MAX_LIMIT = 200
MAX_QUERY_CHARS = 200
MAX_NAME_CHARS = 200
MAX_PATH_CHARS = 4_096
MAX_URL_CHARS = 2_048
MAX_TITLE_CHARS = 200
# A message longer than this is clipped before it is scanned.
MAX_SCAN_CHARS = 200_000
# The most artifacts one message adds to a page (the rest of a message
# naming thousands of links or files isn't listed), so a page and its
# file checks stay bounded.
MAX_ITEMS_PER_ROW = MAX_LIMIT

KINDS = ("image", "video", "audio", "page", "document", "file", "link")
# The chips Conduit shows, as filters on kind.
GROUPS = {
    "images": ("image", "video"),
    "pages": ("page",),
    "files": ("audio", "document", "file"),
    "links": ("link",),
}

_IMAGE = {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "svg", "tif", "tiff"}
_VIDEO = {"mp4", "mov", "m4v", "webm", "mkv", "avi"}
_AUDIO = {"mp3", "m4a", "wav", "ogg", "opus", "flac", "aac", "caf"}
_PAGE = {"html", "htm", "xhtml"}
_DOCUMENT = {"pdf", "txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "rtf",
             "doc", "docx", "xls", "xlsx", "ppt", "pptx", "key", "numbers", "pages", "odt", "ods",
             "odp", "epub", "ics", "vcf", "gpx", "kml", "kmz", "zip", "tar", "gz", "tgz", "7z"}
# What a tool writes that counts as an artifact. Source code does not.
_WRITTEN = _IMAGE | _VIDEO | _AUDIO | _PAGE | _DOCUMENT

_MEDIA_RE = re.compile(
    r"""MEDIA:[^\S\n]*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/|[A-Za-z]:[/\\])[^\s`"'<>]+)""")
_PREVIEW_RE = re.compile(r"(?:^|(?<=\s))::preview\{([^{}\n]{0,1024})\}")
_PREVIEW_FILE_RE = re.compile(r"""\bfile=(?:"([^"]*)"|'([^']*)')""")
_FILE_REF_RE = re.compile(r"""@file:(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|[^\s`"'<>]+)""")
_MD_LINK_RE = re.compile(r"(!?)\[([^\]\n]{0,300})\]\((https?://[^\s)<>]+)\)")
_BARE_URL_RE = re.compile(r"(?<![(\[<\"'=])\bhttps?://[^\s<>\"'`)\]]+")
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_PRODUCER_TOOL_RE = re.compile(
    r"(?:^|_)(?:creat(?:e|ion)|download|export|generat(?:e|ion)|render|save|speech|tts|write)(?:_|$)",
    re.IGNORECASE)
_PRODUCER_KEYS = ("file_path", "output_path", "output_file", "saved_to", "image", "image_path", "audio_path",
                  "video_path", "path", "files_created", "files_written")
# Tools whose arguments name the file they write.
_WRITE_TOOLS = {"write_file"}
_TRAILING = ".,;:!?)]}*_"


def kind_for(path: str) -> str:
    ext = _extension(path)
    if ext in _IMAGE:
        return "image"
    if ext in _VIDEO:
        return "video"
    if ext in _AUDIO:
        return "audio"
    if ext in _PAGE:
        return "page"
    if ext in _DOCUMENT:
        return "document"
    return "file"


def _extension(path: str) -> str:
    name = re.split(r"[/\\]", path.split("?", 1)[0].split("#", 1)[0])[-1]
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        return value[1:-1].strip()
    return value.rstrip(_TRAILING)


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _is_local_path(value: str) -> bool:
    return value.startswith(("/", "~/")) or bool(re.match(r"^[A-Za-z]:[/\\]", value))


def _name(path: str) -> str:
    return re.split(r"[/\\]", path.rstrip("/\\"))[-1] or path


def _prose(text: str) -> str:
    """Text without fenced code: an example in a code block isn't a delivery."""
    return _FENCE_RE.sub(" ", text)


def _media_paths(text: str) -> list:
    """``MEDIA:`` paths as Hermes' gateway reads them when it's importable
    (spaced paths, quoting, its masking of code and quotes), else a plain
    reading of the tag."""
    extract = _hermes_extract_media()
    if extract is not None:
        try:
            return [path for path, _ in extract(_prose(text))[0]]
        except Exception:  # noqa: BLE001 - a gateway change must not cost the list
            pass
    return [_unquote(m.group("path")) for m in _MEDIA_RE.finditer(_prose(text))]


_extract_media: Any = False


def _hermes_extract_media() -> Optional[Callable[[str], Any]]:
    global _extract_media
    if _extract_media is False:
        try:
            from gateway.platforms.base import BasePlatformAdapter

            _extract_media = BasePlatformAdapter.extract_media
        except Exception:  # noqa: BLE001 - tests, or a Hermes without the gateway
            _extract_media = None
    return _extract_media


def assistant_items(text: str) -> Iterable[tuple]:
    """(source, kind, path or url, label) found in an assistant reply."""
    if not text:
        return
    text = text[:MAX_SCAN_CHARS]
    for path in _media_paths(text):
        if path:
            yield ("delivered", kind_for(path), path, "")
    prose = _prose(text)
    for match in _PREVIEW_RE.finditer(prose):
        found = _PREVIEW_FILE_RE.search(match.group(1))
        path = (found.group(1) or found.group(2) or "").strip() if found else ""
        if path:
            yield ("preview", kind_for(path), path, "")
    linked = set()
    for bang, label, url in _MD_LINK_RE.findall(prose):
        url = url.rstrip(_TRAILING)
        linked.add(url)
        if bang:
            yield ("link", "image", url, label.strip())
        else:
            yield ("link", "link", url, label.strip())
    for url in _BARE_URL_RE.findall(prose):
        url = url.rstrip(_TRAILING)
        if url not in linked:
            yield ("link", "link", url, "")


def user_items(text: str) -> Iterable[tuple]:
    if not text or "@file:" not in text:
        return
    # A reference shown in a code block is an example, not an attachment.
    for match in _FILE_REF_RE.finditer(_prose(text[:MAX_SCAN_CHARS])):
        path = _unquote(match.group("path"))
        if _is_local_path(path):
            yield ("attached", kind_for(path), path, "")


def tool_call_items(raw: Any) -> Iterable[tuple]:
    """Files an assistant turn's tool calls write (``write_file``'s path)."""
    calls = _json(raw)
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        if str(function.get("name") or "") not in _WRITE_TOOLS:
            continue
        args = _json(function.get("arguments"))
        path = args.get("path") if isinstance(args, dict) else None
        if isinstance(path, str) and _is_local_path(path.strip()) and _extension(path) in _WRITTEN:
            yield ("written", kind_for(path.strip()), path.strip(), "")


def tool_result_items(tool_name: Any, content: Any) -> Iterable[tuple]:
    """Files a producer tool reports it saved (``{"file_path": …}``), and a
    picture ``image_generate`` reports by URL, as an image link."""
    if not isinstance(tool_name, str) or not _PRODUCER_TOOL_RE.search(tool_name):
        return
    payload = _json(content)
    if not isinstance(payload, dict):
        return
    for key in _PRODUCER_KEYS:
        values = payload.get(key)
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, str):
                path = _unquote(value)
                if _is_local_path(path) and _extension(path) in _WRITTEN:
                    yield ("written", kind_for(path), path, "")
                elif key == "image" and value.strip().startswith(("https://", "http://")):
                    # image_generate reports a hosted picture by its URL: a
                    # whole JSON value, so no sentence punctuation to trim.
                    yield ("link", "image", value.strip(), "")


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


MAX_JSON_CHARS = 1_000_000


def _json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    # A tool's multi-megabyte output isn't parsed for one path.
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_JSON_CHARS:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def row_items(role: str, content: Any, tool_calls: Any, tool_name: Any) -> list:
    text = content if isinstance(content, str) else ""
    if role == "assistant":
        return [*assistant_items(text), *tool_call_items(tool_calls)]
    if role == "user":
        return list(user_items(text))
    if role == "tool":
        return list(tool_result_items(tool_name, text))
    return []


def _describe(source: str, kind: str, target: str, label: str, row: sqlite3.Row, title: str,
              stat: Callable[[str], Optional[int]]) -> Optional[dict]:
    item: dict = {
        "kind": kind,
        "source": source,
        "session_id": str(row["session_id"]),
        "session_title": _clip(title, MAX_TITLE_CHARS),
        "message_id": int(row["id"]),
        "timestamp": _number(row["timestamp"]),
    }
    if source == "link":
        if len(target) > MAX_URL_CHARS:
            return None
        try:
            parsed = urlparse(target)
        except ValueError:  # an unclosed "[" in a reply's half-written link
            return None
        try:
            host = (parsed.hostname or "").lower()
        except ValueError:
            host = ""
        if parsed.scheme not in ("http", "https") or not host:
            return None
        # Never a URL's user:password@.
        item["url"] = parsed._replace(netloc=parsed.netloc.rpartition("@")[2]).geturl() if "@" in parsed.netloc else target
        # The host alone, never a URL's user:password@.
        item["name"] = _clip(label or host + (parsed.path if parsed.path not in ("", "/") else ""),
                             MAX_NAME_CHARS)
        item["host"] = host
        return item
    if len(target) > MAX_PATH_CHARS or "\x00" in target:
        return None
    item["path"] = target
    item["name"] = _clip(_name(target), MAX_NAME_CHARS)
    size = stat(target)
    item["exists"] = size is not None
    if size is not None:
        item["size"] = size
    return item


def _file_size(path: str) -> Optional[int]:
    try:
        info = os.stat(os.path.expanduser(path))
    except (OSError, ValueError):
        return None
    return info.st_size if not os.path.isdir(os.path.expanduser(path)) else None


def _matches(item: dict, kinds: Optional[tuple], query: str) -> bool:
    if kinds is not None and item["kind"] not in kinds:
        return False
    if query:
        haystack = " ".join((item.get("name", ""), item.get("session_title", ""), item.get("path", ""),
                             item.get("url", ""))).lower()
        return all(word in haystack for word in query.split())
    return True


def _candidate_sql(columns: set) -> str:
    """Cheap SQL prefilter: rows that could hold an artifact at all."""
    parts = [
        "(m.role = 'assistant' AND (m.content LIKE '%MEDIA:%' OR m.content LIKE '%::preview%'"
        " OR m.content LIKE '%http%'))",
        "(m.role = 'user' AND m.content LIKE '%@file:%')",
        # Every key in _PRODUCER_KEYS has one of these in it.
        "(m.role = 'tool' AND (m.content LIKE '%path%' OR m.content LIKE '%_file%'"
        " OR m.content LIKE '%saved_to%' OR m.content LIKE '%files_%' OR m.content LIKE '%\"image\"%'))",
    ]
    if "tool_calls" in columns:
        parts.append("(m.role = 'assistant' AND m.tool_calls LIKE '%write_file%')")
    return "(" + " OR ".join(parts) + ")"


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def open_db(home: Any) -> sqlite3.Connection:
    """The profile's session store, read only."""
    path = Path(home) / "state.db"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


MAX_LINEAGE = 256
MAX_TITLE_HOPS = 16


def lineage(conn: sqlite3.Connection, session_ids: Iterable[str], *, ancestors: bool = True) -> list:
    """The chats' sessions with the ones they continue from and the ones
    that continue them (a compression's continuation, a subagent's run),
    since Hermes keeps one chat's history across them. Without
    ``ancestors``, only the ones that continue them."""
    found = [sid for sid in dict.fromkeys(str(s) for s in session_ids if s)][:MAX_LINEAGE]
    if not found or "parent_session_id" not in _columns(conn, "sessions"):
        return found
    known = set(found)
    roots = []
    for sid in found:
        current, chain = sid, {sid}
        # One budget for every chain, so the ids stay a bounded IN (...).
        while ancestors and len(known) < MAX_LINEAGE:
            row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            parent = str(row[0]) if row and row[0] else ""
            if not parent or parent in chain:
                break
            current = parent
            chain.add(current)
            known.add(current)
        roots.append(current)
    frontier = list(dict.fromkeys(roots))
    expanded: set = set()
    while frontier and len(known) < MAX_LINEAGE:
        expanded.update(frontier)
        marks = ",".join("?" * len(frontier))
        children = [str(r[0]) for r in conn.execute(
            f"SELECT id FROM sessions WHERE parent_session_id IN ({marks})", frontier)]
        frontier = [c for c in dict.fromkeys(children) if c not in expanded]
        room = MAX_LINEAGE - len(known)
        frontier = [c for c in frontier if c in known] + [c for c in frontier if c not in known][:room]
        known.update(frontier)
    return list(known)


# The title Hermes gives a bot's one Bot Chat (Desktop's hermes-bots plugin
# and Conduit both find the chat by it, trimmed; a compression's
# continuation is titled "Bot Chat #2" and is found through its parent).
BOT_CHAT_TITLE = "Bot Chat"


def bot_chat_lineage(conn: sqlite3.Connection, session_columns: Optional[set] = None) -> list:
    """The profile's hidden Bot Chat sessions, with the sessions that
    continue them (never the ones they came from: a hidden room stays
    hidden). A profile has one Bot Chat; the same id budget as ``lineage``
    bounds a profile that somehow has more."""
    columns = _columns(conn, "sessions") if session_columns is None else session_columns
    if not {"hidden", "title"} <= columns:
        return []
    ids = [str(row[0]) for row in conn.execute(
        "SELECT id FROM sessions WHERE hidden = 1 AND TRIM(title, char(32, 9, 10, 13)) = ? LIMIT ?",
        (BOT_CHAT_TITLE, MAX_LINEAGE))]
    return lineage(conn, ids, ancestors=False) if ids else []


def _titles(conn: sqlite3.Connection, has_parent: bool) -> Callable[[str, Any], str]:
    """A session's title, else the nearest titled session it continues."""
    cache: dict = {}

    def title(session_id: str, own: Any) -> str:
        if isinstance(own, str) and own.strip():
            return own
        if not has_parent:
            return ""
        if session_id in cache:
            return cache[session_id]
        found, current = "", session_id
        for _ in range(MAX_TITLE_HOPS):
            row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
            if not row or not row[0]:
                break
            current = str(row[0])
            parent = conn.execute("SELECT title FROM sessions WHERE id = ?", (current,)).fetchone()
            if parent and isinstance(parent[0], str) and parent[0].strip():
                found = parent[0]
                break
        cache[session_id] = found
        return found

    return title


def list_artifacts(conn: sqlite3.Connection, *, before: Optional[int] = None,
                   session_id: Optional[str] = None, session_ids: Iterable[str] = (),
                   group: Optional[str] = None,
                   query: str = "", limit: int = DEFAULT_LIMIT,
                   include_missing: bool = True,
                   workstreams: Iterable[str] = (),
                   stat: Callable[[str], Optional[int]] = _file_size,
                   window_rows: int = WINDOW_ROWS, max_scan_rows: int = MAX_SCAN_ROWS) -> dict:
    """One page of artifacts, newest first, and where the next page starts
    (``next_before``, None once the history is read to its start). One chat
    (``session_id``/``session_ids``) reads only its own rows, up to
    ``max_scan_rows`` of them, and ignores ``window_rows``. Without
    ``include_missing``, a file no longer on disk is left out, so a history
    of deleted files doesn't leave Conduit's pages mostly empty; links have
    no file and always stay. The profile's list leaves out ``workstreams``
    and the chats that continue them."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    max_scan_rows = max(1, int(max_scan_rows))
    kinds = GROUPS.get(group) if group else None
    query = " ".join(str(query or "").lower().split())[:MAX_QUERY_CHARS]
    message_columns = _columns(conn, "messages")
    session_columns = _columns(conn, "sessions")
    if not {"id", "session_id", "role", "content", "timestamp"} <= message_columns:
        raise RuntimeError("This Hermes keeps messages in a shape Conduit can't read")
    tool_calls = "m.tool_calls" if "tool_calls" in message_columns else "NULL"
    tool_name = "m.tool_name" if "tool_name" in message_columns else "NULL"
    title = "s.title" if "title" in session_columns else "NULL"
    select = (f"SELECT m.id, m.session_id, m.role, m.content, {tool_calls} AS tool_calls, "
              f"{tool_name} AS tool_name, m.timestamp, {title} AS title "
              "FROM messages m LEFT JOIN sessions s ON s.id = m.session_id ")
    candidate = f" AND {_candidate_sql(message_columns)}"
    title_for = _titles(conn, "parent_session_id" in session_columns and "title" in session_columns)

    bounds = conn.execute("SELECT MIN(id), MAX(id) FROM messages").fetchone()
    lowest, highest = bounds[0], bounds[1]
    items: list = []
    seen: set = set()
    if highest is None:
        return {"items": items, "next_before": None}
    top = min(int(before), highest + 1) if before is not None else highest + 1

    def take(row: sqlite3.Row) -> bool:
        """Lists the row's artifacts; True once the page is full."""
        found = row_items(row["role"], row["content"], row["tool_calls"], row["tool_name"])
        for source, kind, target, label in found[:MAX_ITEMS_PER_ROW]:
            key = target.rstrip("/") if source != "link" else target
            if key in seen:
                continue
            item = _describe(source, kind, target, label, row,
                             title_for(str(row["session_id"]), row["title"]), stat)
            if item is not None and not include_missing and item.get("exists") is False:
                # Gone at its newest mention, gone at every older one, so
                # each gone path is checked once.
                seen.add(key)
                continue
            # Seen once listed: a newer mention the filter leaves out
            # doesn't hide an older one it keeps.
            if item is not None and _matches(item, kinds, query):
                seen.add(key)
                items.append(item)
        return len(items) >= limit

    wanted = [*([session_id] if session_id else []), *session_ids]
    # Hidden chats stay out (a group room member's turns, and the room a
    # chat was branched from), but a bot's Bot Chat, hidden too, is the
    # bot's own chat and is listed with what continues it, and a chat asked
    # for by id is read whether hidden or not.
    shown: list = []
    hidden = ""
    if "hidden" in session_columns:
        shown = list(dict.fromkeys([*(str(sid) for sid in wanted if sid), *bot_chat_lineage(conn, session_columns)]))
        listed = f" OR m.session_id IN ({','.join('?' * len(shown))})" if shown else ""
        hidden = f" AND (COALESCE(s.hidden, 0) = 0{listed})"
    work = [str(sid) for sid in workstreams if sid]
    if work and not wanted:
        # Hidden while open, shown once resolved; the bot's, either way.
        # With every chat that continues it, however many times.
        hidden += (" AND m.session_id NOT IN (WITH RECURSIVE work(id) AS (SELECT value FROM json_each(?)"
                   " UNION SELECT c.id FROM sessions c JOIN work ON c.parent_session_id = work.id) SELECT id FROM work)"
                   if "parent_session_id" in session_columns else
                   " AND m.session_id NOT IN (SELECT value FROM json_each(?))")
        shown = [*shown, json.dumps(work)]
    if wanted:
        # One chat reads its own rows through Hermes' session index, not
        # the profile's history window by window: on a profile full of cron
        # runs, an older chat sat behind thousands of other rows and its
        # sheet paged through all of them before showing anything.
        chat = lineage(conn, wanted)
        if not chat:
            return {"items": items, "next_before": None}
        sql = (select + f"WHERE m.session_id IN ({','.join('?' * len(chat))}) AND m.id < ?{hidden}{candidate} "
               "ORDER BY m.id DESC LIMIT ?")
        scanned = 0
        for row in conn.execute(sql, [*chat, top, *shown, max_scan_rows]):
            scanned += 1
            if take(row):
                return {"items": items, "next_before": int(row["id"]) if row["id"] > lowest else None}
            if scanned >= max_scan_rows:
                return {"items": items, "next_before": int(row["id"]) if row["id"] > lowest else None}
        return {"items": items, "next_before": None}

    sql = select + f"WHERE m.id >= ? AND m.id < ?{hidden}{candidate} ORDER BY m.id DESC"
    scanned = 0
    next_before: Optional[int] = None
    while top > lowest:
        bottom = max(top - window_rows, lowest)
        stopped_at: Optional[int] = None
        for row in conn.execute(sql, [bottom, top, *shown]):
            if take(row):
                stopped_at = int(row["id"])
                break
        scanned += top - bottom
        if stopped_at is not None:
            next_before = stopped_at if stopped_at > lowest else None
            break
        top = bottom
        if top > lowest and scanned >= max_scan_rows:
            next_before = top
            break
    return {"items": items, "next_before": next_before}


# --- A file a reply names ------------------------------------------------------
#
# A reply often names a file by its name or a path inside a project
# (`AppState.swift`, `Conduit/Views/X.swift`), not where it lives: the chat's
# working folder is often unset, and the agent worked somewhere else. The
# chat's own history says where: the tool calls and results that read or
# wrote the file carry its full path (hermes-conduit#1809). ``resolve_name``
# looks there, newest first, after the chat's folder.

# Rows of one chat read for a name, newest first.
RESOLVE_MAX_ROWS = 20_000
MAX_RESOLVE_NAME_CHARS = 512
# Paths a row offers, and how many a request checks on disk at most.
MAX_RESOLVE_CANDIDATES = 400
# A path starts at a / or ~/ of its own, never inside a relative one
# (backup/old.md isn't /old.md).
_ABS_PATH_RE = re.compile(r"""(?:(?<![A-Za-z0-9_.~-])~/|(?<![A-Za-z0-9_.~-])/)[^\s'"`<>()\[\]{},;|\\]+"""
                          r"""|(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/][^\s'"`<>()\[\]{},;|]+""")


def _clean_name(name: str) -> str:
    text = str(name or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


# A path between quotes or backticks, where it may hold spaces or brackets
# (`/Users/me/My Project/Report (final).pdf`, a tool call's JSON string).
# Only its own quote ends it, so `/Users/me/John's Projects/a.md` is whole.
_QUOTED_PATH_RE = re.compile(r"""(["'`])((?:~/|/|(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/])(?:(?!\1)[^\n]){1,%d})\1""" % MAX_PATH_CHARS)
# A sentence's own end after a path, which a name ending in `_` or `*` keeps.
_SENTENCE_END = ".,;:!?"
# What the bare-path pattern stops at, so a quoted path holding one is read whole.
_PATH_BREAK_RE = re.compile(r"""[\s<>()\[\]{},;|]""")


def _path_candidate(path: str, suffix: str, *, strip: str) -> Optional[str]:
    """``path`` as a file path ending in ``suffix``, or None."""
    path = path.rstrip(strip)
    if not path.startswith(("/", "~")):
        # The escape before a JSON string's closing quote isn't the path's.
        path = path.rstrip(strip + "\\")
        # A Windows path, compared and opened with forward slashes.
        path = re.sub(r"/+", "/", path.replace("\\", "/"))
    # grep's `path:12:` and a compiler's `path:12:5:`.
    path = re.sub(r":\d+(?::\d+)?:?$", "", path).rstrip(":")
    # A URL's //host/... (and a UNC share) is never a file here.
    if path.startswith("//"):
        return None
    if path.endswith("/" + suffix) and len(path) <= MAX_PATH_CHARS:
        return path
    return None


# Inside a quoted run, a second path after a space or a , ; |: the run holds several
# paths ("/a/old.md moved to /b/new.md"), not one with spaces in it.
_ANOTHER_PATH_RE = re.compile(r"""[\s,;|](?:~/|/|[A-Za-z]:[\\/])""")


def _name_end(raw: str, suffix: str, strip: str) -> Optional[str]:
    """``raw`` up to where ``suffix`` ends it, when only ``strip``
    characters follow (``/notes/why?.`` for ``why?``)."""
    end = raw.replace("\\", "/").rfind("/" + suffix)
    if end < 0:
        return None
    end += 1 + len(suffix)
    tail = raw[end:]
    return raw[:end] if all(char in strip for char in tail) else None


def _bare_candidate(raw: str, suffix: str) -> Optional[str]:
    """A bare-pattern match as a path ending in ``suffix``, or None.
    Trailing punctuation is the sentence's, unless the name ends in it."""
    return (_path_candidate(raw, suffix, strip=_TRAILING + "'\"")
            or _path_candidate(_name_end(raw, suffix, _TRAILING + "'\"\\") or "", suffix, strip=""))


def _path_tokens(text: str, suffix: str, *, quoted_whole: bool = True) -> Iterable[str]:
    """Absolute paths in ``text`` that end in ``suffix`` (a whole name or
    whole trailing folders: ``/a/b/AppState.swift`` for
    ``AppState.swift``, never ``/a/MyAppState.swift``), in the order they
    appear. A quoted run that is one path with spaces, brackets or an
    apostrophe in it is never read in pieces; with ``quoted_whole`` it is
    read whole. Only a host that checks each candidate can tell a quoted
    path from quoted prose that ends in one, so an SSH profile, which
    can't, reads neither."""
    if suffix.rsplit("/", 1)[-1] not in text:
        return
    found: list = []
    whole_runs: list = []
    for match in _QUOTED_PATH_RE.finditer(text):
        run = match.group(2)
        if not _PATH_BREAK_RE.search(run.replace("'", " ")) or _ANOTHER_PATH_RE.search(run):
            continue  # the bare-path pattern reads it, piece by piece
        head = _ABS_PATH_RE.match(run)
        if head and _bare_candidate(head.group(0), suffix):
            # It opens with the whole path ("/srv/old.md renamed to
            # backup/old.md"): what follows is prose, read as such.
            continue
        # One path: a piece of it (/AppState.swift after `Reports (2026)`)
        # isn't a file the chat named.
        whole_runs.append(match.span(2))
        if not quoted_whole:
            continue
        # A nested JSON string's quote comes escaped: \" ends the path.
        strip = " \t\\" + _SENTENCE_END
        path = (_path_candidate(run, suffix, strip=" \t\\")
                or _path_candidate(_name_end(run, suffix, strip) or "", suffix, strip=""))
        if path:
            found.append((match.start(2), path))
    for match in _ABS_PATH_RE.finditer(text):
        if any(lo <= match.start() < hi for lo, hi in whole_runs):
            continue
        path = _bare_candidate(match.group(0), suffix)
        if path:
            found.append((match.start(), path))
    for _, path in sorted(found, key=lambda item: item[0]):
        yield path


def resolve_name(conn: sqlite3.Connection, session_ids: Iterable[str], name: str, *,
                 exists: Optional[Callable[[str], bool]] = None,
                 max_rows: int = RESOLVE_MAX_ROWS) -> dict:
    """Where the file a chat's reply names as ``name`` (a bare name or a
    relative path) is: joined to the chat's working folder, else the newest
    absolute path ending in it that the chat's own messages (tool calls,
    tool results, replies) carry. Hidden sessions in the lineage (a room
    the chat was branched from) are left out, as the Artifacts list leaves
    them out, except the ids asked for and the Bot Chat's. ``exists`` checks
    a candidate on the host (and refuses one it won't show); without it (a
    profile whose files are on another machine) the folder's join, else the
    newest candidate, is taken unchecked. ``{"path": None}`` when nothing
    names it."""
    suffix = _clean_name(name)
    if not suffix or len(suffix) > MAX_RESOLVE_NAME_CHARS or "\n" in suffix:
        return {"path": None}
    if _is_local_path(suffix):
        if suffix.startswith("//"):
            return {"path": None}
        if exists is None or exists(os.path.expanduser(suffix)):
            return {"path": suffix, "via": "absolute"}
        return {"path": None}
    if any(part == ".." for part in suffix.split("/")):
        return {"path": None}
    wanted = [sid for sid in session_ids if sid]
    chat = lineage(conn, wanted)
    session_columns = _columns(conn, "sessions")
    if chat and "hidden" in session_columns:
        shown = {*wanted, *bot_chat_lineage(conn, session_columns)}
        hidden = {str(row[0]) for row in conn.execute(
            f"SELECT id FROM sessions WHERE COALESCE(hidden, 0) != 0 AND id IN ({','.join('?' * len(chat))})", chat)}
        chat = [sid for sid in chat if sid in shown or sid not in hidden]
    if not chat:
        return {"path": None}
    marks = ",".join("?" * len(chat))
    checked: set = set()

    def found(path: str) -> bool:
        if path in checked or len(checked) >= MAX_RESOLVE_CANDIDATES:
            return False
        checked.add(path)
        return exists is None or exists(os.path.expanduser(path))

    if "cwd" in session_columns:
        # The chat's own folder first, then those of the sessions it
        # continues or that continue it, the most recently active first.
        rows = conn.execute(
            f"SELECT s.id, s.cwd FROM sessions s WHERE s.id IN ({marks}) ORDER BY "
            f"(SELECT MAX(m.id) FROM messages m WHERE m.session_id = s.id) DESC, s.id", chat).fetchall()
        folders = {str(row[0]): str(row[1] or "") for row in rows}
        order = [sid for sid in wanted if sid in folders] + [str(row[0]) for row in rows if str(row[0]) not in wanted]
        for cwd in dict.fromkeys(folders.get(sid, "") for sid in order):
            cwd = cwd.strip()
            if re.match(r"^[A-Za-z]:[\\/]", cwd):
                cwd = re.sub(r"/+", "/", cwd.replace("\\", "/"))
            cwd = cwd.rstrip("/")
            # No folder recorded (or the disk's top) isn't a folder to join.
            if cwd and cwd != "~" and not re.fullmatch(r"[A-Za-z]:", cwd) and _is_local_path(cwd + "/"):
                candidate = f"{cwd}/{suffix}"
                if found(candidate):
                    return {"path": candidate, "via": "folder"}

    # Only a host that checks each candidate can tell a quoted path from
    # quoted prose that ends in one.
    host_checks = exists is not None and getattr(exists, "checks_host", True)
    message_columns = _columns(conn, "messages")
    tool_calls = "tool_calls" if "tool_calls" in message_columns else "NULL"
    # The name's last part narrows the rows sqlite hands back (after a / or,
    # on Windows, a \\).
    leaf = suffix.rsplit("/", 1)[-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    sql = (f"SELECT content, {tool_calls} AS tool_calls FROM messages WHERE session_id IN ({marks}) "
           f"AND (content LIKE ? ESCAPE '\\' OR {tool_calls} LIKE ? ESCAPE '\\') ORDER BY id DESC LIMIT ?")
    pattern = f"%{leaf}%"
    for row in conn.execute(sql, [*chat, pattern, pattern, max(1, int(max_rows))]):
        for column in ("content", "tool_calls"):
            text = row[column]
            if not isinstance(text, str):
                continue
            # A JSON-encoded tool call escapes nothing in a POSIX path, but
            # a result can hold one as an escaped string.
            text = text[:MAX_SCAN_CHARS].replace("\\/", "/").replace("\\\\", "\\")
            # Newest mention in the row first, as rows are.
            for path in reversed(list(_path_tokens(text, suffix, quoted_whole=host_checks))):
                if found(path):
                    return {"path": path, "via": "chat"}
        if len(checked) >= MAX_RESOLVE_CANDIDATES:
            break
    return {"path": None}
