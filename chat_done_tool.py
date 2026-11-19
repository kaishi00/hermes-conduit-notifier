"""The agent marks its chat done in Conduit: the ``conduit_chat_done`` tool.

Conduit's Sessions list dims a done chat and lists it under the Status
filter's Done (hermes-conduit: Done chats); the user marks one from a row's
menu or swipe, and the agent marks its own chat here, when the user asks or
their own instructions say to (a wrap-up skill, say). The mark is kept with
the chat's ids (chat_marks_store): its session and the ones it continued
through compactions, so the mark finds the chat however Conduit knows it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from hermes_constants import get_hermes_home

from .chat_marks_store import ChatMarksError, ChatMarksStore
from .link_ask_tool import lineage

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_chat_done"
TOOLSET = "conduit"

DESCRIPTION = (
    "Mark this chat done in the user's Conduit Sessions list, or open it again (done: false). Conduit dims a "
    "done chat on every device until someone opens it again. Use it when the user asks, or when their own "
    "instructions or a skill say to mark finished work done (a wrap-up, say); don't mark a chat done on your "
    "own judgment otherwise."
)

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "done": {"type": "boolean", "description": "true (default) marks this chat done; false opens it again."},
        },
    },
}


def register(ctx: Any, *, is_child: Callable[[Any], bool]) -> None:
    """Registers the tool, if this Hermes supports plugin tools."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child)

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="✅")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def _answer(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False,
           store: ChatMarksStore | None = None, chain: Callable[[str], list[str]] = lineage) -> str:
    try:
        args = args if isinstance(args, dict) else {}
        session = session_id.strip() if isinstance(session_id, str) else ""
        if not session:
            return _answer(ok=False, error="no_chat", note="There's no chat to mark here.")
        # A subagent's session isn't a chat in the user's list.
        if is_child(session):
            return _answer(ok=False, error="subagent",
                           note="Only the main conversation can mark its chat done. Report back instead.")
        done = args.get("done", True)
        if not isinstance(done, bool):
            return _answer(ok=False, error="bad_done", note="done is true or false.")
        # Its session, then the ones it continued through compactions, the
        # lineage root last: Conduit finds the mark by any of them.
        ids = list(dict.fromkeys([session, *chain(session)]))
        if len(ids) > 8:
            ids = ids[:7] + ids[-1:]
        (store or ChatMarksStore(get_hermes_home())).mark(ids, done, by="agent")
        if done:
            return _answer(ok=True, done=True, note="Marked done in Conduit. Tell the user only if they asked.")
        return _answer(ok=True, done=False, note="Open again in Conduit.")
    except ChatMarksError as error:
        return _answer(ok=False, error="bad_chat", note=str(error))
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_chat_done failed", exc_info=True)
        return _answer(ok=False, error="unavailable", note="The chat couldn't be marked. Tell the user.")
