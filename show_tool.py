"""Hermes puts something on the screen of the user's voice call: the
``conduit_show`` tool.

During a spoken conversation in Conduit (Local Live, Classic voice, or a
live call that hands work to Hermes), a table, a chart, steps or links are
better seen than heard. The tool keeps a card (present_store) that the phone
picks up on its next exchange, which it runs as soon as it sees the tool in
the turn and again when the turn ends. Conduit opens the card over the call;
Hermes says in a sentence that it's there. A card nobody picks up within ten
minutes is dropped: it belongs to the conversation it was shown in.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import client
from .present_store import MAX_CARD_MARKDOWN_CHARS, MAX_CARD_TITLE_CHARS, PresentError, PresentStore

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_show"
TOOLSET = "conduit"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Show something on the user's phone screen while they talk to you in a Conduit voice call. Use it for "
        "anything better seen than heard: tables, charts, forecasts, recipes and other step-by-step instructions, "
        "lists, comparisons, code, images and links, and whenever the user asks to see or show something. Write "
        "Markdown: tables for data, numbered lists for steps, ```mermaid blocks for charts, ![description](url) "
        "for images (direct image URLs you actually found) and [title](url) for links. Then say in a sentence "
        "that it's on their screen and give the gist; don't read it out, and don't repeat it in your reply. Only "
        "for spoken turns: in a typed chat, write it in your reply instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": MAX_CARD_TITLE_CHARS,
                      "description": f"A short heading, a few words (up to {MAX_CARD_TITLE_CHARS} characters)."},
            "markdown": {"type": "string", "maxLength": MAX_CARD_MARKDOWN_CHARS,
                         "description": f"What to show, as Markdown (up to {MAX_CARD_MARKDOWN_CHARS} characters)."},
        },
        "required": ["title", "markdown"],
    },
}

SHOWN_NOTE = ("On the user's screen. Say in a sentence that it's there and give the gist; don't read it out or "
              "repeat it in your reply.")


def register(ctx: Any, *, is_child: Callable[[Any], bool] = lambda _: False) -> None:
    """Registers the tool, if this Hermes supports plugin tools."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child)

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="🖼️")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def _store() -> PresentStore:
    return PresentStore(get_hermes_home())


def available() -> bool:
    """Shown to the model only while a Conduit that shows cards has
    exchanged with this profile recently."""
    try:
        return client.load_state() is not None and _store().can_show()
    except Exception:  # noqa: BLE001
        return False


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False) -> str:
    try:
        return json.dumps(_handle(args if isinstance(args, dict) else {}, session_id, is_child), ensure_ascii=False)
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_show failed", exc_info=True)
        return json.dumps({"ok": False, "error": "unavailable",
                           "note": "It couldn't be shown. Tell the user the gist instead."})


def _handle(args: dict[str, Any], session_id: Any, is_child: Callable[[Any], bool]) -> dict[str, Any]:
    session = session_id.strip() if isinstance(session_id, str) else ""
    # The user's screen belongs to the conversation they're in, not to a
    # subagent's work.
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the main conversation can show things on the user's screen. Report back instead."}
    try:
        card = _store().show(args.get("title"), args.get("markdown"), session_id=session)
    except PresentError as error:
        note = str(error)
        if error.code in ("no_device", "rate_limited"):
            note += " Tell the user the gist instead."
        return {"ok": False, "error": error.code, "note": note}
    return {"ok": True, "id": card["id"], "note": SHOWN_NOTE}
