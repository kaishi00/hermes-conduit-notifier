"""Sending a Pulse turn into a Conduit chat.

Conduit (like Hermes Desktop) creates its chats through the dashboard's
session host (``session.create`` with source ``desktop``), so they have no
messaging-gateway session key and ``inject_message`` can't reach them while
nobody has them open. Hermes's own onboarding writes into such a chat from
the server side: it reopens the chat by its stored id and submits a hidden
turn (tui_gateway/methods_onboarding.py). Pulse does the same, from the
process that hosts those sessions.

Everything here goes through the host's method table. A Hermes without it
(or without either method) reports itself unavailable instead of failing.

The chat is Pulse's own, so when another surface holds it (Hermes Desktop
keeps a chat it has open, and Hermes refuses anyone else's turn with
SESSION_NOT_OWNED) Pulse takes it over the way Conduit's "Take over" does
(chat_takeover.py) and sends once more. A turn running there is never cut
off: that answers busy and the send stays refused.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

RESUME = "session.resume"
SUBMIT = "prompt.submit"


@dataclass(frozen=True)
class Sent:
    ok: bool
    status: str  # "streaming" (running now), "queued" (behind a turn in progress), or "" when refused
    detail: str = ""


def _methods() -> Optional[dict[str, Callable[..., Any]]]:
    # Only a process that already runs the session host has it loaded;
    # importing it anywhere else would start one.
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return None
    methods = getattr(server, "_methods", None)
    if not isinstance(methods, dict) or RESUME not in methods or SUBMIT not in methods:
        return None
    return methods


def available() -> bool:
    return _methods() is not None


def take_over(session_ids: list[str]) -> str:
    """Drops other processes' claims on the chat: chat_takeover's status
    ("free", "taken_over", "busy", "same_host"), or why it couldn't."""
    from . import chat_takeover as takeover
    try:
        registry, turn_marker = takeover.modules()
        return str(takeover.take_over_session(session_ids, registry=registry, turn_marker=turn_marker,
                                              home=takeover.home(), by="Pulse").get("status") or "")
    except takeover.TakeoverError as error:
        return str(error)


def send(chat_id: str, text: str, *, profile: str = "",
         taking_over: Callable[[list[str]], str] = take_over) -> Sent:
    """Reopens the chat (the live session when someone has it open) and
    submits ``text`` as a hidden turn. A chat busy with a turn queues it
    behind that turn instead of interrupting it."""
    methods = _methods()
    if methods is None:
        return Sent(False, "", "this Hermes has no session host to send through")
    rid = f"pulse-{uuid.uuid4().hex[:12]}"
    params: dict[str, Any] = {"session_id": chat_id, "source": "desktop"}
    if profile and profile != "default":
        params["profile"] = profile
    resumed = methods[RESUME](rid, params)
    if not isinstance(resumed, dict) or "error" in resumed:
        return Sent(False, "", f"couldn't open the chat: {_message(resumed)}")
    sid = ((resumed.get("result") or {}).get("session_id") or "") if isinstance(resumed.get("result"), dict) else ""
    if not sid:
        return Sent(False, "", "couldn't open the chat: no session id came back")
    def submit() -> Any:
        return methods[SUBMIT](rid, {"session_id": sid, "text": text, "display_kind": "hidden", "queued": True})

    submitted = submit()
    if _not_owned(submitted):
        taken = taking_over(list(dict.fromkeys([chat_id, sid])))
        if taken not in ("free", "taken_over", "same_host"):
            return Sent(False, "", f"the chat refused the turn: {_message(submitted)} "
                                   f"(Pulse couldn't take it over: {taken or 'unknown'})")
        submitted = submit()
    if not isinstance(submitted, dict) or "error" in submitted:
        return Sent(False, "", f"the chat refused the turn: {_message(submitted)}")
    result = submitted.get("result") if isinstance(submitted.get("result"), dict) else {}
    status = str(result.get("status") or "")
    return Sent(True, "streaming" if status == "streaming" else "queued", status)


def _not_owned(response: Any) -> bool:
    """Hermes refused the turn because another live surface holds the chat."""
    error = response.get("error") if isinstance(response, dict) else None
    data = error.get("data") if isinstance(error, dict) else None
    return isinstance(data, dict) and data.get("reason") == "SESSION_NOT_OWNED"


def _message(response: Any) -> str:
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unknown error")[:200]
    return "unknown error"
