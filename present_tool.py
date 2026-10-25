"""Hermes asks the user to OK an action on their phone: the
``conduit_present`` tool.

Before something the user should approve (sending a message for them,
buying, deleting), Hermes keeps a confirm prompt (present_store) and wakes
the phone through the relay. Conduit shows a panel over everything, the call
included, and the user confirms, edits or declines by tap or, during a call,
by voice. The tool returns the answer, waiting for it up to ``wait_seconds``.
Nothing here acts on the answer: the agent does the action itself, and only
after a confirm.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import client, session_titles
from .events import push_event
from .present_store import (DEFAULT_TTL_MINUTES, MAX_ACTION_LABEL_CHARS, MAX_PREVIEW_CHARS, MAX_SPOKEN_YES,
                            MAX_SPOKEN_YES_CHARS, MAX_TITLE_CHARS, MAX_TTL_MINUTES, PresentError, PresentStore,
                            first_line)
from .present_store import _text as clean_text

logger = logging.getLogger("hermes.plugins.conduit_push")

TOOL_NAME = "conduit_present"
TOOLSET = "conduit"
MAX_WAIT_SECONDS = 120
# Polling the file the dashboard route writes: quick at first, since the
# user often answers within seconds, then once a second.
POLL_START_S = 0.25
POLL_MAX_S = 1.0

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Ask the user to OK an action on their phone before you do it. Use it before anything the user should "
        "approve: sending a message or email for them, buying or booking, deleting, posting, sharing their data, "
        "anything sensitive or hard to undo. action \"confirm\" shows a panel in Conduit on the user's phone (over "
        "everything, also during a voice call) with your title, a Markdown preview of exactly what will happen "
        "and a button with action_label, plus Edit and Not now. The user answers by tap, or by voice during a "
        "live call (\"send it\"): give spoken_yes phrases that fit. Set wait_seconds to wait for the answer "
        "(up to 120); otherwise check later with status. Only \"confirmed\" means go ahead, and only with what "
        "the preview showed. \"declined\" means don't. \"waiting\" or \"expired\" is never a yes: don't act, and "
        "ask again or ask in chat if it still matters. On \"edit\" the user will send the corrected request "
        "themselves as a new message: don't act on the original and don't ask again, wait for it. cancel takes "
        "back a prompt you no longer need."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["confirm", "status", "cancel"],
                       "description": "confirm: show a prompt. status: how prompts were answered (one by id, or "
                                      "every waiting prompt and answer you haven't seen). cancel: take one back."},
            "title": {"type": "string", "maxLength": MAX_TITLE_CHARS,
                      "description": f"For confirm: a short question, like \"Send to Mei?\" (up to {MAX_TITLE_CHARS} "
                                     "characters)."},
            "preview": {"type": "string", "maxLength": MAX_PREVIEW_CHARS,
                        "description": "For confirm: exactly what will happen, in Markdown (the message text, the "
                                       f"order, what gets deleted), up to {MAX_PREVIEW_CHARS} characters. Its first "
                                       "line is the notification's text."},
            "action_label": {"type": "string", "maxLength": MAX_ACTION_LABEL_CHARS,
                             "description": "For confirm: the button's label, a verb like \"Send\", \"Buy\" or "
                                            f"\"Delete\" (up to {MAX_ACTION_LABEL_CHARS} characters; default "
                                            "\"Confirm\")."},
            "spoken_yes": {"type": "array", "maxItems": MAX_SPOKEN_YES,
                           "items": {"type": "string", "maxLength": MAX_SPOKEN_YES_CHARS},
                           "description": "For confirm: short phrases that confirm by voice during a call, like "
                                          "\"send it\" (up to 5)."},
            "ttl_minutes": {"type": "integer", "minimum": 1, "maximum": MAX_TTL_MINUTES,
                            "description": f"For confirm: how long the prompt stays up (default {DEFAULT_TTL_MINUTES}, "
                                           f"at most {MAX_TTL_MINUTES})."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS,
                             "description": "For confirm: wait this long for the answer (default 0: return right "
                                            f"away; at most {MAX_WAIT_SECONDS})."},
            "id": {"type": "string", "description": "For status (optional) and cancel: the prompt's id."},
        },
        "required": ["action"],
    },
}

_NOTES = {
    "waiting": ("Shown on the user's phone, not answered yet. Don't do the action: check with status (id) later, "
                "or tell the user it's waiting on their phone."),
    "confirmed": "The user confirmed. Go ahead with exactly what the preview showed, nothing more.",
    "declined": "The user said not now. Don't do it, and don't ask again unless they bring it up.",
    "edit": ("The user wants to change it and will send the corrected request themselves as a new message. Don't "
             "act on the original and don't ask again: wait for their message."),
    "expired": "Nobody answered before the prompt expired. That is not a yes: don't do it.",
    "cancelled": "Taken back; the phone no longer shows it.",
}


def register(ctx: Any, *, is_child: Callable[[Any], bool] = lambda _: False,
             profile: Callable[[], str] = lambda: "default") -> None:
    """Registers the tool, if this Hermes supports plugin tools."""
    if not hasattr(ctx, "register_tool"):
        return

    def handler(args: Any, session_id: Any = None, **_: Any) -> str:
        return handle(args, session_id=session_id, is_child=is_child, profile=profile())

    tool = {"name": TOOL_NAME, "toolset": TOOLSET, "schema": SCHEMA, "handler": handler, "check_fn": available}
    try:
        try:
            ctx.register_tool(**tool, description=SCHEMA["description"], emoji="✅")
        except TypeError:
            ctx.register_tool(**tool)
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not register %s", TOOL_NAME, exc_info=True)


def _store() -> PresentStore:
    return PresentStore(get_hermes_home())


def available() -> bool:
    """Shown to the model only once a Conduit that shows confirm prompts
    (present-v1) has exchanged with this profile."""
    try:
        return client.load_state() is not None and _store().has_device()
    except Exception:  # noqa: BLE001
        return False


def handle(args: Any, *, session_id: Any = None, is_child: Callable[[Any], bool] = lambda _: False,
           profile: str = "default", sleep: Callable[[float], None] = time.sleep,
           monotonic: Callable[[], float] = time.monotonic) -> str:
    try:
        return _answer(**_handle(args if isinstance(args, dict) else {}, session_id, is_child, profile, sleep,
                                 monotonic))
    except Exception:  # noqa: BLE001 — a tool must never raise
        logger.warning("conduit_present failed", exc_info=True)
        return _answer(ok=False, error="unavailable",
                       note="The prompt couldn't be shown or read. Don't do the action; ask the user in chat.")


def _handle(args: dict[str, Any], session_id: Any, is_child: Callable[[Any], bool], profile: str,
            sleep: Callable[[float], None], monotonic: Callable[[], float]) -> dict[str, Any]:
    session = session_id.strip() if isinstance(session_id, str) else ""
    # A subagent can be steered by what its tools read, so prompts come only
    # from the main conversation, as calls do.
    if is_child(session):
        return {"ok": False, "error": "subagent",
                "note": "Only the main conversation can ask the user to confirm. Report back; the main agent can ask."}
    action = args.get("action")
    store = _store()
    if action == "confirm":
        return _confirm(store, args, session, profile, sleep, monotonic)
    if action == "status":
        prompt_id = args.get("id")
        if prompt_id not in (None, "") and not isinstance(prompt_id, str):
            return {"ok": False, "error": "invalid", "note": "id is a prompt id from confirm."}
        prompts = store.status(prompt_id or None)
        if prompt_id and not prompts:
            return {"ok": False, "error": "unknown_id", "note": "No such prompt (answers are kept a week)."}
        return {"ok": True, "prompts": [_with_note(prompt) for prompt in prompts]}
    if action == "cancel":
        prompt_id = args.get("id")
        if not isinstance(prompt_id, str) or not prompt_id:
            return {"ok": False, "error": "no_id", "note": "Give the id of the prompt to take back."}
        prompt = store.cancel(prompt_id)
        if prompt is None:
            return {"ok": False, "error": "unknown_id", "note": "No such prompt."}
        if prompt["status"] != "cancelled":
            return {"ok": True, **_with_note(prompt),
                    "note": "Already " + prompt["status"] + ", so it can't be taken back. " + _NOTES[prompt["status"]]}
        return {"ok": True, **_with_note(prompt)}
    return {"ok": False, "error": "invalid", "note": "action is confirm, status or cancel."}


def _confirm(store: PresentStore, args: dict[str, Any], session: str, profile: str,
             sleep: Callable[[float], None], monotonic: Callable[[], float]) -> dict[str, Any]:
    wait = args.get("wait_seconds", 0)
    if wait is None:
        wait = 0
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= MAX_WAIT_SECONDS:
        return {"ok": False, "error": "invalid", "note": f"wait_seconds is 0 to {MAX_WAIT_SECONDS}."}
    try:
        prompt = store.confirm(args.get("title"), args.get("preview"), args.get("action_label"),
                               spoken_yes=args.get("spoken_yes"), session_id=session,
                               ttl_minutes=args.get("ttl_minutes"))
    except PresentError as error:
        return {"ok": False, "error": error.code, "note": str(error)}
    # Answers the agent hasn't seen yet ride along, taken only once this
    # prompt is kept so a refused one leaves them for later.
    earlier = [item for item in store.unreported_outcomes() if item["id"] != prompt["id"]]
    # Cleaned as the store keeps it, so the notification shows what the panel will.
    _nudge(prompt, clean_text(args.get("preview"), MAX_PREVIEW_CHARS), profile)
    if wait > 0:
        prompt = _wait(store, prompt, wait, sleep, monotonic)
    answer: dict[str, Any] = {"ok": True, **_with_note(prompt)}
    if earlier:
        answer["earlier_answers"] = [_with_note(item) for item in earlier]
    return answer


def _wait(store: PresentStore, prompt: dict[str, Any], seconds: float, sleep: Callable[[float], None],
          monotonic: Callable[[], float]) -> dict[str, Any]:
    """Polls the store (the phone's answer arrives through the dashboard,
    another process) until the prompt leaves waiting or the time is up."""
    deadline = monotonic() + seconds
    delay = POLL_START_S
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return prompt
        sleep(min(delay, remaining))
        delay = min(delay * 1.5, POLL_MAX_S)
        current = store.get(prompt["id"])
        if current is None:
            return prompt
        if current["status"] != "waiting":
            # Returned to the agent now, so it counts as reported.
            return next(iter(store.status(prompt["id"])), current)
        prompt = current


def _nudge(prompt: dict[str, Any], preview: Any, profile: str) -> None:
    """Wakes the phone through the relay. Never fails the tool: a phone that
    misses it picks the prompt up on its next exchange."""
    try:
        body = first_line(preview if isinstance(preview, str) else "")
        client.enqueue(push_event("phone.changes", identifier=f"present:{prompt['id']}", profile=profile,
                                  profile_name=session_titles.bot_name(),
                                  title=prompt["title"], body=body, mode="alert"))
    except Exception:  # noqa: BLE001
        logger.warning("conduit_push: could not wake the phone for a confirm prompt", exc_info=True)


def _with_note(prompt: dict[str, Any]) -> dict[str, Any]:
    shown = {key: value for key, value in prompt.items() if key not in ("device", "finished_at")}
    shown["note"] = _NOTES[prompt["status"]]
    if prompt["status"] == "edit" and prompt.get("edit_text"):
        shown["note"] += " What they typed is in edit_text."
    return shown


def _answer(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)
