"""Hermes calls you (#449): the ``conduit_call_user`` tool.

Hermes asks Conduit to phone the user. The call rings when the asking turn
ends, so it opens on the finished result: the turn's final reply is in the
chat the call opens, and the reason Hermes gives is the first thing said.
The user's Conduit settings decide whether it may: "call when I ask" for a
call the user asked for, "Hermes decides" for one Hermes chose to make, and
the usual limits (calls.py). A call Hermes chose to make is first checked
against the user's own rules for such calls, when they wrote any: the tool
hands them back, and the call goes only when Hermes asks again saying it fits
(``fits_rules``). The bundled skill carries the full guidance
(``skill_view("conduit_push:calling-the-user")``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from . import calls, client

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_call_user"
TOOLSET = "conduit"
SKILL_NAME = "calling-the-user"
# Opens Calls from Hermes in Conduit's Settings, from a text reply.
SETTINGS_LINK = "[Calls from Hermes](conduit://settings/calls)"

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Phone the user through the Conduit app: their iPhone rings like a call, and when they answer, "
        "a live voice conversation opens in this chat. The call goes out when this turn ends, so finish "
        "the work first and end the turn with the result as your final reply; the call opens with your "
        "reason and that reply. Use it when the user asked to be called, phoned or rung (\"call me when "
        "it's done\"), with asked_by_user true. Without such a request, only for news the user would want "
        "right away and can't wait for them to look (asked_by_user false; refused unless the user lets "
        "Hermes decide). Never for routine updates. It doesn't ring while the user is in a voice call "
        f"with you. Before calling on your own judgment, load skill_view(\"conduit_push:{SKILL_NAME}\")."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "One short spoken sentence: why you're calling and the news, said first when they "
                    "answer (for example \"The deploy finished, and two tests failed.\"). Up to 200 characters."
                ),
            },
            "asked_by_user": {
                "type": "boolean",
                "description": "True only if the user asked you to call, phone or ring them.",
            },
            "fits_rules": {
                "type": "boolean",
                "description": (
                    "Only after a check_rules answer: true when this call clearly fits the user's rules for "
                    "calls you decide to make."
                ),
            },
        },
        "required": ["reason", "asked_by_user"],
    },
}


def register(ctx: Any, *, is_child: Callable[[Any], bool], skill_path: Any) -> None:
    """Registers the tool and its skill, if this Hermes supports them. A
    Hermes that refuses either keeps everything else working."""
    if hasattr(ctx, "register_tool"):
        def handler(args: Any, session_id: Any = None, **_: Any) -> str:
            return handle(args, session_id=session_id, is_child=is_child)

        tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
        try:
            try:
                ctx.register_tool(**tool, description=SCHEMA["description"], emoji="📞")
            except TypeError:
                # A Hermes without the display fields.
                ctx.register_tool(**tool)
        except Exception:  # noqa: BLE001
            logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)
    if hasattr(ctx, "register_skill"):
        try:
            ctx.register_skill(SKILL_NAME, skill_path)
        except Exception:  # noqa: BLE001
            logger.warning("conduit_push: could not register the %s skill", SKILL_NAME, exc_info=True)


def available() -> bool:
    """Shown to the model only while this profile is paired and some call
    Hermes can ask for is on."""
    try:
        if client.load_state() is None:
            return False
        settings = calls.settings()
        return bool(settings["enabled"] and (settings["when_asked"] or settings["decides"]))
    except Exception:  # noqa: BLE001
        return False


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False) -> str:
    try:
        return _answer(**_handle(args, session_id, is_child))
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_call_user failed", exc_info=True)
        return _answer(ok=False, error="unavailable", note="The call couldn't be set up. Tell the user in your reply instead.")


def _handle(args: Any, session_id: Any, is_child: Callable[[Any], bool]) -> dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return {"ok": False, "error": "no_reason", "note": "Give the reason: one short sentence the user hears first."}
    asked = args.get("asked_by_user") is True
    session = session_id.strip() if isinstance(session_id, str) else ""
    # A subagent's session (subagent_start told the hooks): its turn ends
    # never fire watches, so it couldn't ring.
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the main conversation can call the user. Report back; the main agent can call."}
    if not session:
        return {"ok": False, "error": "no_session", "note": "This conversation can't place a call."}
    if client.load_state() is None:
        return {"ok": False, "error": "not_paired",
                "note": "This Hermes profile isn't paired with the Conduit app, so it can't call. Say so in your reply."}
    if not asked:
        settings = calls.settings()
        rules = settings["rules"] if settings["enabled"] and settings["decides"] else ""
        # Fits only rules this conversation was just shown, as they are now.
        if rules and not (args.get("fits_rules") is True and _rules_seen(session, rules)):
            _show_rules(session, rules)
            return {"ok": False, "error": "check_rules", "rules": rules,
                    "note": ("The user wrote these rules for calls you decide to make. If this news clearly fits "
                             "them, call again with fits_rules true. If it doesn't, or you aren't sure, don't call: "
                             "put the news in your reply.")}
    try:
        calls.tool_watch(session, reason, asked=asked)
    except ValueError as error:
        if str(error) == "calls_off":
            note = ("The user hasn't turned on calls they ask for in Conduit. Tell them in your reply, and that "
                    f"they turn them on in Conduit's Settings, under {SETTINGS_LINK} (a link that opens it; "
                    "leave the link out of anything spoken).") if asked else (
                    "The user hasn't let Hermes decide when to call. Don't call; put the news in your reply.")
            return {"ok": False, "error": "calls_off", "note": note}
        if str(error) == "too_many":
            return {"ok": False, "error": "too_many", "note": "Too many calls are already waiting. Put the news in your reply."}
        return {"ok": False, "error": "invalid", "note": "The call couldn't be set up. Put the news in your reply."}
    return {"ok": True, "status": "call_at_turn_end",
            "note": ("The user's phone rings when this turn ends, unless their call limits hold it back (they get "
                     "the usual notification then) or they're already in a voice call with you. Finish the work "
                     "and end the turn with the result: the call opens with your reason and that reply. Don't "
                     "say the phone is ringing yet.")}


# Rules each conversation was shown, and when: a fits_rules call counts only
# after them.
RULES_SEEN_S = 600
_rules_shown: dict[str, tuple[str, float]] = {}
_rules_lock = threading.Lock()


def _show_rules(session: str, rules: str) -> None:
    now = time.monotonic()
    with _rules_lock:
        for key, (_, at) in list(_rules_shown.items()):
            if now - at > RULES_SEEN_S:
                del _rules_shown[key]
        _rules_shown[session] = (rules, now)


def _rules_seen(session: str, rules: str) -> bool:
    with _rules_lock:
        shown = _rules_shown.get(session)
    return shown is not None and shown[0] == rules and time.monotonic() - shown[1] <= RULES_SEEN_S


def _answer(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)
