"""Where in Conduit the message a turn answers was sent from (hermes-conduit#888).

Hermes can tell a turn came from Conduit (reply_sources.py), not from which
of its surfaces: an iPhone, an iPad, the Watch, CarPlay, Siri. So the agent
answered a request made while driving the way it answers a typed chat, with
long Markdown, tables and links.

Conduit says so just before it sends the message: ``POST /turn-surface``
with the chat's runtime session id, the SHA-256 of the exact text it sends
in ``prompt.submit``, the surface and how the message was entered (typed or
spoken). ``prompt.submit`` refuses keys Hermes doesn't know, so the value
can't ride there. The dashboard keeps it on Hermes' own record of that chat
(its gateway runs in the dashboard process), and the turn's
``pre_llm_call`` takes the one whose hash matches the turn's message and
adds one line, for example
"[Conduit: this message came from the user's Apple Watch, spoken.]". The
plugin's own Watch transports name their surface themselves.

Matching the text keeps the line on the message it describes: a message
that steered or redirected a turn already running never becomes a turn of
its own, so its report labels nothing (not a goal's next step, not a
background result), and two queued messages each keep their own. A turn
whose text Hermes changed, or that merges several messages, gets no line.
It counts only when the turn came from Conduit's socket, and a report older
than ``FRESH_S`` counts as none. Under plugin isolation or a turn on a
compute host the record isn't in this process, and the turn gets no line,
as before.

Standalone (no imports from the plugin package): the dashboard loads it from
its file, the plugin imports it.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from typing import Any, Callable, Optional, Tuple

# On Hermes' in-memory session record. Hermes never reads it.
KEY = "conduit_turn_surface"
# A message queued behind a long turn may wait this long before it runs.
FRESH_S = 900.0
# Reports kept per chat; the oldest goes first.
MAX_KEPT = 8
MAX_SESSION_ID_CHARS = 200
_SHA256 = re.compile(r"[0-9a-f]{64}")

# What each surface reads as in the line.
SURFACES = {
    "iphone": "the user's iPhone",
    "ipad": "the user's iPad",
    "mac": "the user's Mac",
    "vision": "the user's Apple Vision Pro",
    "watch": "the user's Apple Watch",
    "carplay": "CarPlay in the user's car",
    "siri": "Siri or a Shortcut",
}
INPUTS = ("typed", "spoken")
# Said after the line where the surface changes what a good answer looks like.
HINTS = {
    "carplay": "They are likely driving: answer briefly, in plain sentences meant to be heard, "
               "with no tables, links or lists.",
    "watch": "Its screen is small: keep the answer short, with no tables.",
}
# Said for a message typed on a screen, where a clarify batch with open
# questions shows as a form (hermes-conduit's ClarifyActivity.isForm). Only
# when Conduit says it was typed: spoken, a form is read out one question at
# a time, and Conduit reports how every message it sends was entered.
SCREENS = ("iphone", "ipad", "mac", "vision")
FORM_HINT = ("When you need several details only the user can give (like dates or a budget), ask them in one "
             "clarify call with open questions: Conduit shows it as a form, and they may leave some blank.")


class Report:
    __slots__ = ("session_id", "text_sha256", "surface", "entry")

    def __init__(self, session_id: str, text_sha256: str, surface: str, entry: Optional[str]) -> None:
        self.session_id = session_id
        self.text_sha256 = text_sha256
        self.surface = surface
        self.entry = entry


def parse(body: Any) -> Report:
    """A route's body as a report. Raises ValueError, with what's wrong, for
    anything else."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > MAX_SESSION_ID_CHARS:
        raise ValueError("session_id must be a chat's runtime session id")
    digest = body.get("text_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("text_sha256 must be the lowercase hex SHA-256 of the message's text")
    surface = body.get("surface")
    if not isinstance(surface, str) or surface not in SURFACES:
        raise ValueError(f"surface must be one of {', '.join(SURFACES)}")
    entry = body.get("input")
    if entry is not None and (not isinstance(entry, str) or entry not in INPUTS):
        raise ValueError(f"input must be one of {', '.join(INPUTS)}, or left out")
    return Report(session_id.strip(), digest, surface, entry)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(session: dict, report: Report, clock: Callable[[], float] = time.monotonic) -> None:
    """Keeps ``report`` on ``session`` (Hermes' record) for the turn of its text."""
    now = clock()
    kept = session.get(KEY)
    kept = {digest: value for digest, value in kept.items() if _fresh(value, now)} if isinstance(kept, dict) else {}
    # A repeated text moves to the end, so eviction takes the oldest report.
    kept.pop(report.text_sha256, None)
    kept[report.text_sha256] = (report.surface, report.entry, now)
    while len(kept) > MAX_KEPT:
        kept.pop(next(iter(kept)))
    # One assignment: the turn thread reads the whole map or the old one.
    session[KEY] = kept


def take(session: Any, message: Any, clock: Callable[[], float] = time.monotonic
         ) -> Optional[Tuple[str, Optional[str]]]:
    """Removes the report for ``message`` kept on ``session`` and returns its
    surface and entry while fresh."""
    if not isinstance(session, dict):
        return None
    kept = session.get(KEY)
    text = _message_text(message)
    if not isinstance(kept, dict) or text is None:
        return None
    value = kept.pop(text_sha256(text), None)
    if value is None or not _fresh(value, clock()):
        return None
    surface, entry, _at = value
    return surface, entry if entry in INPUTS else None


def _fresh(value: Any, now: float) -> bool:
    if not (isinstance(value, tuple) and len(value) == 3 and value[0] in SURFACES):
        return False
    at = value[2]
    return isinstance(at, float) and 0 <= now - at <= FRESH_S


def _message_text(message: Any) -> Optional[str]:
    """The turn's text: a string, or a parts list's text parts (a message
    sent with images)."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = [part.get("text") for part in message if isinstance(part, dict) and part.get("type") == "text"]
        texts = [part for part in parts if isinstance(part, str)]
        return "\n".join(texts) if texts else None
    return None


def line(surface: str, entry: Optional[str] = None) -> str:
    where = SURFACES[surface] + (f", {entry}" if entry else "")
    hint = FORM_HINT if surface in SCREENS and entry == "typed" else HINTS.get(surface)
    return f"[Conduit: this message came from {where}.{' ' + hint if hint else ''}]"


def current_session() -> Any:
    """Hermes' record of the chat whose turn runs on this thread, if Hermes'
    app gateway runs in this process. Never imports it: a copy this plugin
    imported would be a private one no client uses."""
    record_var = getattr(sys.modules.get("tui_gateway.server"), "_current_runtime_session_record", None)
    try:
        return record_var.get() if callable(getattr(record_var, "get", None)) else None
    except Exception:  # noqa: BLE001 — unknown: no line
        return None


def note(from_conduit: bool, message: Any, transport: Any = None,
         clock: Callable[[], float] = time.monotonic) -> Optional[str]:
    """The line for this turn, or None. Never raises: a hook calls this."""
    try:
        if not from_conduit:
            return None
        kept = take(current_session(), message, clock)
        if kept is None:
            # The plugin's Watch jobs and call chats name their own surface.
            surface = getattr(transport, "conduit_surface", None)
            kept = (surface, getattr(transport, "conduit_input", None)) if surface in SURFACES else None
        if kept is None:
            return None
        surface, entry = kept
        return line(surface, entry if entry in INPUTS else None)
    except Exception:  # noqa: BLE001
        return None
