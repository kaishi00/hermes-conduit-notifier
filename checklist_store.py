"""Checklist ticks: what the user ticked on a conduit_show checklist card,
kept on the host so every one of their Conduits shows the same ticks, and
Hermes hears what changed.

Conduit sends a checklist's whole state whenever a tick changes: the chat's
ids (a chat is known by more than one), the checklist's key (its live name,
else its title, else "checklist"), its title, its items and the ones ticked.
Ticks are kept by item text, compared without regard to case, so a checklist
shown again with the same live name keeps the ticks of the items it still
has. Hermes hears a changed checklist once, at the chat's next turn
(``note``, from the plugin's pre_llm_call hook).

The dashboard loads this module by path, so it imports nothing from the
plugin package.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-checklists.json"
MAX_LISTS = 200
MAX_SESSIONS_PER_WRITE = 4
MAX_ITEMS = 40
MAX_ITEM_CHARS = 120
MAX_KEY_CHARS = 80
MAX_SESSION_CHARS = 200
# A checklist nobody touched in this long is forgotten.
KEEP_S = 90 * 24 * 3600
DEFAULT_KEY = "checklist"

_lock = threading.Lock()


class ChecklistError(ValueError):
    pass


def list_key(card: dict[str, Any]) -> str:
    """The key a checklist card's ticks are kept under: its live name, else
    its title, else "checklist" (Conduit works it out the same way)."""
    for field in ("live", "title"):
        value = card.get(field)
        if isinstance(value, str) and value.strip():
            return _clean(value, MAX_KEY_CHARS).lower()
    return DEFAULT_KEY


def _clean(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text).strip()
    return text[:limit].rstrip()


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ChecklistError("items and ticked are lists of text")
    texts: list[str] = []
    seen: set[str] = set()
    for raw in value[:MAX_ITEMS]:
        text = _clean(raw, MAX_ITEM_CHARS)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            texts.append(text)
    return texts


def _sessions(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ChecklistError("sessions is a list of the chat's ids")
    sessions = [_clean(item, MAX_SESSION_CHARS) for item in value[:MAX_SESSIONS_PER_WRITE]]
    sessions = list(dict.fromkeys(item for item in sessions if item))
    if not sessions:
        raise ChecklistError("sessions needs the chat's id")
    return sessions


class ChecklistStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time):
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # --- reading and writing the file ---------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"lists": []}
        if not isinstance(state, dict) or not isinstance(state.get("lists"), list):
            return {"lists": []}
        now = self.clock()
        lists = [entry for entry in state["lists"] if _well_formed(entry) and now - entry["updated_at"] < KEEP_S]
        return {"lists": lists}

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save, under a lock file: the
        dashboard saves ticks while the agent marks them heard, in another
        process."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with _lock, open(lock_path, "a+b") as handle, _exclusive(handle):
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            state = self._load()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._save(state)

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".conduit-checklists-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    # --- what Conduit calls --------------------------------------------------

    def ticks(self, sessions: Any) -> dict[str, list[str]]:
        """Each checklist of the chat (by key) and the items ticked on it,
        the newest write first when two of the chat's ids hold one."""
        wanted = set(_sessions(sessions))
        found: dict[str, tuple[float, list[str]]] = {}
        for entry in self._load()["lists"]:
            if entry["session"] in wanted:
                held = found.get(entry["key"])
                if held is None or held[0] < entry["updated_at"]:
                    found[entry["key"]] = (entry["updated_at"], entry["ticked"])
        return {key: ticked for key, (_, ticked) in found.items()}

    def save(self, body: Any) -> dict[str, list[str]]:
        """A checklist's state as the user left it; answers the chat's ticks."""
        if not isinstance(body, dict):
            raise ChecklistError("Expected a JSON object")
        sessions = _sessions(body.get("sessions"))
        key = _clean(body.get("key"), MAX_KEY_CHARS).lower()
        if not key:
            raise ChecklistError("key names the checklist")
        items = _texts(body.get("items"))
        known = {item.lower(): item for item in items}
        ticked = [known[text.lower()] for text in _texts(body.get("ticked")) if text.lower() in known]
        title = _clean(body.get("title"), MAX_KEY_CHARS)
        with self._locked() as state:
            now = self.clock()
            kept = [entry for entry in state["lists"] if not (entry["session"] in sessions and entry["key"] == key)]
            for session in sessions:
                kept.append({"session": session, "key": key, "title": title, "items": items, "ticked": ticked,
                             "updated_at": now, "heard": False})
            kept.sort(key=lambda entry: entry["updated_at"])
            state["lists"] = kept[-MAX_LISTS:]
        return self.ticks(sessions)

    # --- what Hermes hears ---------------------------------------------------

    def note(self, session_id: Any) -> str | None:
        """The chat's checklists the user changed since Hermes last heard,
        once, for this turn's context."""
        session = _clean(session_id, MAX_SESSION_CHARS)
        if not session or not self.path.exists():
            return None
        with self._locked() as state:
            changed = [entry for entry in state["lists"] if entry["session"] == session and not entry["heard"]]
            # One write is kept under each of the chat's ids: heard under
            # one, it's heard under them all.
            written = {(entry["key"], entry["updated_at"]) for entry in changed}
            for entry in state["lists"]:
                if (entry["key"], entry["updated_at"]) in written:
                    entry["heard"] = True
        if not changed:
            return None
        lines = []
        for entry in changed:
            name = entry["title"] or entry["key"]
            done = set(text.lower() for text in entry["ticked"])
            open_items = [item for item in entry["items"] if item.lower() not in done]
            line = f'[Conduit: the user changed the checklist "{name}" in this chat. '
            line += f"Done: {', '.join(entry['ticked'])}" if entry["ticked"] else "Nothing is ticked"
            line += f"; still to do: {', '.join(open_items)}.]" if open_items else "; everything is done.]"
            lines.append(line)
        return "\n".join(lines)


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as present_store: flock where there is one, a
    # first-byte lock on Windows.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):
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


def _well_formed(entry: Any) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("session"), str) and isinstance(entry.get("key"), str)
            and isinstance(entry.get("title"), str) and isinstance(entry.get("items"), list)
            and isinstance(entry.get("ticked"), list)
            and all(isinstance(text, str) for text in entry["items"] + entry["ticked"])
            and isinstance(entry.get("updated_at"), (int, float)) and not isinstance(entry.get("updated_at"), bool)
            and isinstance(entry.get("heard"), bool))
