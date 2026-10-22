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

import logging
import re
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

RESUME = "session.resume"
SUBMIT = "prompt.submit"
CONFIG_SET = "config.set"
logger = logging.getLogger(__name__)
# Like the dashboard's takeover route: a lock holder that never lets go
# can't stall the Pulse loop longer than this.
TAKEOVER_TIMEOUT_S = 20.0
# The one takeover worker that may still be waiting on Hermes' lock: while it
# is, a new takeover waits for it instead of starting another thread.
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


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
    ("free", "taken_over", "busy", "same_host"), or why it couldn't.

    One worker at a time: a worker that timed out waiting on Hermes' lock is
    left waiting, and until it finishes every refusal reports that instead of
    starting another thread (a lock Hermes never releases needs a restart)."""
    global _worker
    try:
        from . import chat_takeover as takeover
    except Exception as error:  # noqa: BLE001 — a partial install: a reason, not a crash
        return f"takeover failed: {type(error).__name__}"
    abandoned = threading.Event()
    outcome: list[str] = []

    def run() -> None:
        try:
            registry, turn_marker = takeover.modules()
            outcome.append(str(takeover.take_over_session(
                session_ids, registry=registry, turn_marker=turn_marker, home=takeover.home(), by="Pulse",
                abandoned=abandoned).get("status") or ""))
        except takeover.TakeoverError as error:
            outcome.append(str(error))
        except Exception as error:  # noqa: BLE001 — a reason for `pulse status`, never a crash in the loop
            outcome.append(f"takeover failed: {type(error).__name__}")

    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return "a takeover is still waiting on Hermes"
        worker = threading.Thread(target=run, name="conduit-pulse-takeover", daemon=True)
        _worker = worker
    try:
        worker.start()
    except RuntimeError as error:
        return f"takeover failed: {type(error).__name__}"
    worker.join(TAKEOVER_TIMEOUT_S)
    if worker.is_alive():
        abandoned.set()  # nothing is written after this
        return "takeover timed out"
    return outcome[0] if outcome else "takeover failed"


def send(chat_id: str, text: str, *, profile: str = "", model: Optional[dict] = None,
         taking_over: Optional[Callable[[list[str]], str]] = None) -> Sent:
    """Reopens the chat (the live session when someone has it open) and
    submits ``text`` as a hidden turn. A chat busy with a turn queues it
    behind that turn instead of interrupting it. With ``model``
    ({"provider", "model"}), the chat is put on that model first, as its
    own model picker would (a busy chat takes it at its next turn). It is
    the chat's model after the turn too: Pulse's chat runs on Pulse's model."""
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
    if model:
        _use_model(methods, sid, resumed.get("result"), model)
    def submit() -> Any:
        # A request id per call: the resend is a new request, not the refused one.
        return methods[SUBMIT](f"pulse-{uuid.uuid4().hex[:12]}",
                               {"session_id": sid, "text": text, "display_kind": "hidden", "queued": True})

    submitted, taken = submit(), ""
    if _not_owned(submitted):
        taken = (taking_over or take_over)(list(dict.fromkeys([chat_id, sid])))
        if taken not in ("free", "taken_over", "same_host"):
            return Sent(False, "", f"the chat refused the turn: {_message(submitted)} "
                                   f"(Pulse couldn't take it over: {taken or 'unknown'})")
        if model:  # the surface that held the chat may have kept it from switching
            _use_model(methods, sid, resumed.get("result"), model)
        submitted = submit()
        if _not_owned(submitted):
            return Sent(False, "", f"the chat refused the turn again after Pulse took it over ({taken}): "
                                   f"{_message(submitted)}")
    if not isinstance(submitted, dict) or "error" in submitted:
        after = f" after Pulse took it over ({taken})" if taken else ""
        return Sent(False, "", f"the chat refused the turn{after}: {_message(submitted)}")
    result = submitted.get("result") if isinstance(submitted.get("result"), dict) else {}
    status = str(result.get("status") or "")
    return Sent(True, "streaming" if status == "streaming" else "queued", status)


_FLAG_VALUE = re.compile(r"[A-Za-z0-9._:/@+][A-Za-z0-9._:/@+-]*")


def plain_flag_value(value: str) -> bool:
    """A model or provider name that reads as one plain word, never a flag or
    a quote, in config.set's value string."""
    return bool(_FLAG_VALUE.fullmatch(value))


def _use_model(methods: dict[str, Callable[..., Any]], sid: str, resumed: Any, model: dict) -> None:
    """Puts the chat on Pulse's model unless it is on it already. The user
    picked it in Settings > Pulse, so a price guard doesn't hold it back. A
    refusal leaves the chat's own model: the turn still goes."""
    name, provider = str(model.get("model") or ""), str(model.get("provider") or "")
    info = resumed.get("info") if isinstance(resumed, dict) else None
    if isinstance(info, dict) and info.get("model") == name and (not provider or info.get("provider") == provider):
        return
    if not plain_flag_value(name) or (provider and not plain_flag_value(provider)) or CONFIG_SET not in methods:
        return
    value = f"{name} --provider {provider} --session" if provider else f"{name} --session"
    try:
        result = methods[CONFIG_SET](f"pulse-{uuid.uuid4().hex[:12]}", {
            "key": "model", "session_id": sid, "value": value, "confirm_expensive_model": True})
    except Exception:  # noqa: BLE001 — the turn goes on the chat's own model
        logger.warning("conduit_push: Pulse couldn't put its chat on %s", name, exc_info=True)
        return
    if not isinstance(result, dict) or "error" in result:
        logger.warning("conduit_push: Pulse couldn't put its chat on %s: %s", name, _message(result))


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
