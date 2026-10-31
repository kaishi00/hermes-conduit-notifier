"""Hermes writes its own notification (#1415): the ``conduit_notify`` tool.

The push a turn sends when it ends ("Response ready") shows the start of
the reply. When the user asked to be told when something's done, or a
scheduled job reports, a heading and a sentence Hermes writes say it
better. The tool keeps them for the chat until the turn's reply push goes
out (``take``), which then carries them instead of the reply's opening:
one push per turn, under the user's own notification settings, so a phone
on Generic or Chat names still shows none of the words. It never sends a
push of its own; a reply that sends none ([SILENT], a muted job) sends none
with them either.

A ``key`` names what the notification is about ("report"): the next one
this profile writes with the same key, from any chat or scheduled job,
replaces it on the phone instead of adding another (#1579). ``quiet``
makes that update land in Notification Center without a sound or a
banner (the push's level is passive).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, NamedTuple

from . import client

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_notify"
TOOLSET = "conduit"
MAX_TITLE_CHARS = 60
MAX_BODY_CHARS = 300
MAX_KEY_CHARS = 40
# Letters, digits and - _ . : / in a key; anything else becomes "-".
_KEY_JUNK = re.compile(r"[^\w.:/-]+")
# A heading the turn's push never took (the turn died first) isn't one a
# later turn of the chat should carry.
KEEP_S = 2 * 60 * 60
MAX_KEPT = 256

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Write the notification Conduit shows on the user's phone when this turn ends, instead of the start of "
        "your reply. Use it when the user asked to be notified or told when something is done (\"let me know "
        "when the report is ready\"), and for the result of a scheduled job or a long task. The title says what "
        "happened in a few words (\"Report finished\"); the body says the result in a sentence or two, only "
        "what this turn actually found or did. It goes with your final reply, so still end the turn with the "
        "full answer, and it follows the user's notification settings: they may see only the chat's name. A Pulse "
        "check writes its own notification, so this one isn't used there. It doesn't ring; to call the user, use "
        "conduit_call_user. To update a notification you sent earlier instead of adding another (\"Report in "
        "progress\", later \"Report ready\"), give both the same key; a key is shared by this profile's chats and "
        "scheduled jobs. Set quiet for an update that shouldn't make a sound or show a banner."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": MAX_TITLE_CHARS,
                      "description": f"What happened, in a few words (up to {MAX_TITLE_CHARS} characters)."},
            "body": {"type": "string", "maxLength": MAX_BODY_CHARS,
                     "description": f"The result in a sentence or two (up to {MAX_BODY_CHARS} characters)."},
            "key": {"type": "string", "maxLength": MAX_KEY_CHARS,
                    "description": ("Optional. A short name for what this is about (\"report\", \"backup\"). A later "
                                    "notification with the same key replaces this one on the phone. Leave it out "
                                    "for a new notification.")},
            "quiet": {"type": "boolean",
                      "description": ("Optional. True: no sound or banner, the notification only appears (or "
                                      "updates) in Notification Center.")},
        },
        "required": ["title", "body"],
    },
}



class Headline(NamedTuple):
    """What a turn wrote for its push."""
    title: str
    body: str
    key: str = ""
    quiet: bool = False


_lock = threading.Lock()
# session id -> (what the turn wrote, kept at)
_kept: dict[str, tuple[Headline, float]] = {}
# session id -> (turn id, headline): what a turn's push took, for a
# second report of the same turn, which replaces the first.
_taken: dict[str, tuple[str, Headline]] = {}


def register(ctx: Any, *, is_child: Callable[[Any], bool]) -> None:
    """Registers the tool, if this Hermes supports tools. One that refuses
    it keeps everything else working."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child)

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🔔")
        except TypeError:
            # A Hermes without the display fields.
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def available() -> bool:
    """Shown to the model only while this profile is paired with Conduit."""
    try:
        return client.load_state() is not None
    except Exception:  # noqa: BLE001
        return False


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False) -> str:
    try:
        return json.dumps(_handle(args, session_id, is_child), ensure_ascii=False)
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_notify failed", exc_info=True)
        return json.dumps({"ok": False, "error": "unavailable",
                           "note": "The notification couldn't be written. Your reply still notifies as usual."})


def _handle(args: Any, session_id: Any, is_child: Callable[[Any], bool]) -> dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    title = _line(args.get("title"), MAX_TITLE_CHARS)
    body = _line(args.get("body"), MAX_BODY_CHARS)
    if not title or not body:
        return {"ok": False, "error": "missing_text", "note": "Give both a title and a body."}
    session = session_id.strip() if isinstance(session_id, str) else ""
    if is_child(session):
        # A subagent's turn sends no push of its own; its parent's reply does.
        return {"ok": False, "error": "subagent",
                "note": "Only the main conversation's reply notifies. Report back; the main agent can write it."}
    if not session:
        return {"ok": False, "error": "no_session", "note": "This conversation doesn't send notifications."}
    if client.load_state() is None:
        return {"ok": False, "error": "not_paired",
                "note": "This Hermes profile isn't paired with the Conduit app, so nothing reaches the phone."}
    key = slot_key(args.get("key"))
    quiet = args.get("quiet") is True
    keep(session, title, body, key=key, quiet=quiet)
    note = ("The notification goes out with your final reply when this turn ends, if the user's "
            "settings let a reply notify. End the turn with your full answer as usual.")
    if key:
        note += f" It replaces this profile's earlier notification with the key \"{key}\", if it's still showing."
    return {"ok": True, "note": note, **({"key": key} if key else {})}


def slot_key(value: Any) -> str:
    """A key as the tool keeps it: one line, lower case, short."""
    if not isinstance(value, str):
        return ""
    return _KEY_JUNK.sub("-", " ".join(value.split()).casefold()).strip("-")[:MAX_KEY_CHARS]


def keep(session_id: str, title: str, body: str, now: float | None = None, *, key: str = "", quiet: bool = False) -> None:
    """The chat's next reply push carries these; a newer call replaces them."""
    now = time.time() if now is None else now
    with _lock:
        _kept.pop(session_id, None)
        _kept[session_id] = (Headline(title, body, key, quiet), now)
        while len(_kept) > MAX_KEPT:
            del _kept[next(iter(_kept))]


def take(session_id: str, turn_id: Any = None, now: float | None = None) -> Headline | None:
    """What this chat's turn wrote for its push, once; the same turn
    reported again gets it again."""
    now = time.time() if now is None else now
    turn = turn_id if isinstance(turn_id, str) and turn_id else None
    with _lock:
        kept = _kept.pop(session_id, None)
        if kept is None:
            taken = _taken.get(session_id)
            return taken[1] if turn and taken and taken[0] == turn else None
        if now - kept[1] > KEEP_S:
            return None
        headline = kept[0]
        if turn:
            _taken.pop(session_id, None)
            _taken[session_id] = (turn, headline)
            while len(_taken) > MAX_KEPT:
                del _taken[next(iter(_taken))]
        return headline


def drop(session_id: str) -> None:
    """The turn ended: what it wrote never rides a later turn's reply, even
    when this one sent none (interrupted, or failed before answering)."""
    with _lock:
        _kept.pop(session_id, None)
        _taken.pop(session_id, None)


def _line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()
