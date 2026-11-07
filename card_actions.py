"""Taps on a card's buttons: what the user tapped on a conduit_show card
whose button says ``tap``, kept on the host until Hermes hears it.

Conduit sends a tap before it sends the user's message about it (like
"Send · Email to Mei"): the chat's ids (a chat is known by more than one),
the card as the user saw it, the button's id and the card's field values.
The plugin's pre_llm_call hook hands the tap to that message's turn
(``note``), once, with the card's Markdown, so Hermes knows exactly what the
user said yes to: a tap is the user's OK for what the card showed, nothing
more (Eric, 2026-11-07, issue 1946). Hermes then does it with its own tools
and shows the card again under its live name with the result.

The dashboard loads this module through the plugin's package.
"""

from __future__ import annotations

import json
import re
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import card_schema
from .checklist_store import _exclusive, _sync_directory

FILE_NAME = "conduit-card-taps.json"
MAX_TAPS = 100
MAX_SESSIONS_PER_WRITE = 4
MAX_SESSION_CHARS = 200
MAX_VALUE_CHARS = 2000
# What Hermes reads of the card the user saw.
MAX_CARD_MARKDOWN_CHARS = 3000
# A tap Hermes hasn't heard in this long went with a message that never
# reached it: it's forgotten rather than handed to a later turn.
KEEP_S = 15 * 60

_lock = threading.Lock()


class TapError(ValueError):
    pass


def _clean(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return card_schema.clean_text(text)[:limit].rstrip()


def _sessions(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TapError("sessions is a list of the chat's ids")
    sessions = [_clean(item, MAX_SESSION_CHARS) for item in value[:MAX_SESSIONS_PER_WRITE]]
    sessions = list(dict.fromkeys(item for item in sessions if item))
    if not sessions:
        raise TapError("sessions needs the chat's id")
    return sessions


def _tapped(card: dict[str, Any], action: str) -> dict[str, Any] | None:
    """The card's tap button with this id."""
    buttons = [*card.get("actions", []), *(element for section in card.get("sections", [])
                                         for element in section["items"] if element["kind"] == "button")]
    return next((button for button in buttons if button["action"] == "tap" and button["id"] == action), None)


def _fields(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {element["id"]: element for section in card.get("sections", []) for element in section["items"]
            if element["kind"] == "field"}


def _values(value: Any, fields: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The field values the tap carried, for the card's own fields only."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TapError("values maps each field's id to what the user left in it")
    values = {}
    for key, field in fields.items():
        raw = value.get(key)
        if isinstance(raw, bool):
            raw = "on" if raw else "off"
        elif isinstance(raw, (int, float)):
            raw = card_schema._cell(raw)
        text = _clean(raw, MAX_VALUE_CHARS)
        if text and field["input"] in ("choice", "toggle", "date", "number"):
            # Only what the field could hold: an option, on or off, a date, a number within its bounds.
            text = card_schema._field_value({**field, "value": text}) or ""
        values[key] = text
    return values


class TapStore:
    def __init__(self, home: Any, clock: Callable[[], float] = time.time):
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"taps": []}
        if not isinstance(state, dict) or not isinstance(state.get("taps"), list):
            return {"taps": []}
        now = self.clock()
        return {"taps": [tap for tap in state["taps"] if _well_formed(tap) and now - tap["at"] < KEEP_S]}

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Load, let the caller change, then save, under a lock file: the
        dashboard saves taps while the agent hears them, in another process."""
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
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".conduit-card-taps-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def save(self, body: Any) -> dict[str, Any]:
        """A tap as Conduit sends it, checked against the card it came from;
        answers the tap's id."""
        if not isinstance(body, dict):
            raise TapError("Expected a JSON object")
        sessions = _sessions(body.get("sessions"))
        cards = card_schema.normalize_cards([body.get("card")])
        if not cards:
            raise TapError("card is the card the user tapped, as the chat holds it")
        card = cards[0]
        action = _clean(body.get("action"), 40)
        button = _tapped(card, action)
        if button is None:
            raise TapError("action names none of the card's tap buttons")
        values = _values(body.get("values"), _fields(card))
        tap = {"id": uuid.uuid4().hex, "at": self.clock(), "heard": False, "action": action,
               "label": button["label"], "title": card.get("title", ""), "live": card.get("live", ""),
               "markdown": _card_markdown(card), "values": values}
        with self._locked() as state:
            for session in sessions:
                state["taps"].append({**tap, "session": session})
            state["taps"] = state["taps"][-MAX_TAPS:]
        return {"ok": True, "id": tap["id"]}

    def note(self, session_id: Any, message: Any = ...) -> str | None:
        """The taps in this chat Hermes hasn't heard yet, once, for the turn
        of the user's message about them (it starts with the button's label):
        another turn in the chat meanwhile (a cron report, a workstream's)
        leaves them for that message."""
        session = _clean(session_id, MAX_SESSION_CHARS)
        if not session:
            return None
        if message is ...:
            said = None
        elif isinstance(message, str) and message.strip():
            said = message.strip()
        else:
            # A turn with no message of the user's (a cron or workstream turn)
            # leaves the taps for the message about them.
            return None
        with self._locked() as state:
            taps = [tap for tap in state["taps"] if tap["session"] == session and not tap["heard"]
                    and (said is None or said.startswith(tap["label"]))]
            heard = {tap["id"] for tap in taps}
            # A tap kept under each of the chat's ids is heard under them all.
            for tap in state["taps"]:
                if tap["id"] in heard:
                    tap["heard"] = True
        return "\n".join(_line(tap) for tap in taps) or None


def _open_fence(text: str) -> str | None:
    """The code fence a card's Markdown leaves open at its end, if any; a
    mono element's inline code (fenced on its own line, opened and closed
    there) is no fence."""
    fence = None
    for line in text.split("\n"):
        run = re.match(r"`{3,}", line)
        if not run:
            continue
        rest = line[len(run.group()):]
        if fence is None:
            if not (rest.strip() and rest.rstrip().endswith(run.group())):
                fence = run.group()
        elif run.group() == fence and not rest.strip():
            fence = None
    return fence


def _card_markdown(card: dict[str, Any]) -> str:
    """The card's Markdown for the tap's note: cut at a line, a code block
    the cut lands in closed, and folded sections said to be folded."""
    text = card_schema.fallback_markdown([card])
    if len(text) > MAX_CARD_MARKDOWN_CHARS:
        # Room for the longest fence the card could leave open, as clip_markdown does.
        longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=3)
        cut = text[:MAX_CARD_MARKDOWN_CHARS - 1 - longest]
        text = cut[:cut.rfind("\n")] if "\n" in cut else cut
        fence = _open_fence(text)
        if fence is not None:
            text += "\n" + fence
    if any(section.get("more") for section in card.get("sections", [])):
        text += ("\n(Part of this card was folded under Show more, so they may not have opened it; its fields "
                 "went with their starting values unless they changed them.)")
    return text


def _line(tap: dict[str, Any]) -> str:
    name = f'"{tap["title"]}"' if tap["title"] else "a card"
    if tap["live"]:
        name += f" (live name {tap['live']})"
    line = (f'[Conduit: the user tapped "{tap["label"]}" (tap {tap["action"]}) on {name} in this chat; their '
            "message says so. The tap is their OK for exactly what the card showed them, nothing more")
    if tap["values"]:
        filled = "; ".join(f"{key}: {value}" if value else f"{key}: (empty)" for key, value in tap["values"].items())
        line += f", with the fields as they left them ({filled})"
    line += (". Do it now with your tools, then show the card again with the same live name, saying how it went "
             "(done, or what went wrong). The card they saw:\n" + tap["markdown"] + "]")
    return line


def _well_formed(tap: Any) -> bool:
    return (isinstance(tap, dict) and all(isinstance(tap.get(key), str) for key in
                                          ("id", "session", "action", "label", "title", "live", "markdown"))
            and isinstance(tap.get("at"), (int, float)) and not isinstance(tap.get("at"), bool)
            and isinstance(tap.get("heard"), bool) and isinstance(tap.get("values"), dict))
