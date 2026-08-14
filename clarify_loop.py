"""Answerable clarify cards for backgrounded Conduit devices.

Hermes mints its clarify request id inside the gateway's blocking prompt,
where no plugin hook can observe it, so a push alone can never produce an
answerable clarify card through ``clarify.respond``. This module instead
wraps clarify execution with ``tool_execution`` middleware:

* mint a plugin-owned id and push the question/choices to the device,
* run the ORIGINAL tool call on a daemon thread (desktop/CLI answering via
  the gateway's own ``clarify.request`` is completely unchanged),
* poll the relay for the device's answer while both wait,
* first answer wins; the loser resolves into a discarded result.

The tool result is formatted exactly like the built-in ``clarify_tool`` so
the agent cannot tell the paths apart.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any

from . import client
from .events import clarify_decision, event_id, flatten_choice_labels, push_event

logger = logging.getLogger("hermes.plugins.conduit_push")

REQUEST_ID_PREFIX = "conduit-push-"
POLL_INTERVAL_SECONDS = 2.0
# Hard cap on polling even when the gateway configures an unlimited clarify
# timeout; the relay expires pending decisions at 2h anyway.
MAX_POLL_SECONDS = 24 * 60 * 60.0

# Set by register() so pushes carry the profile the rest of the plugin uses.
profile = "default"


def set_profile(value: str) -> None:
    global profile
    profile = value or "default"


def middleware(**kwargs: Any) -> Any:
    """``tool_execution`` middleware entry point."""
    tool_name = str(kwargs.get("tool_name") or "")
    if tool_name != "clarify":
        return kwargs["next_call"](kwargs.get("args") or {})
    if not client.load_state():
        # Unpaired profile: no push is possible, keep the original path only.
        return kwargs["next_call"](kwargs.get("args") or {})

    args = dict(kwargs.get("args") or {})
    question = str(args.get("question") or "").strip()
    if not question:
        return kwargs["next_call"](args)

    labels = flatten_choice_labels(args.get("choices"))
    multi_select = bool(args.get("multi_select"))
    request_id = f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex[:12]}"

    client.enqueue(push_event(
        "input.needed",
        identifier=event_id("input", kwargs.get("turn_id"), kwargs.get("tool_call_id"), request_id),
        session_id=_text(kwargs.get("session_id")),
        profile=profile,
        body=question,
        decision=clarify_decision(request_id=request_id, question=question, choices=labels or None),
    ))

    return _first_answer_wins(
        request_id=request_id,
        next_call=kwargs["next_call"],
        args=args,
        question=question,
        offered=labels,
        multi_select=multi_select,
    )


def _first_answer_wins(
    *,
    request_id: str,
    next_call: Any,
    args: dict[str, Any],
    question: str,
    offered: list[str],
    multi_select: bool,
) -> Any:
    """Race the original clarify call against the relay answer poll."""
    outcome: dict[str, Any] = {}

    def _run_original() -> None:
        try:
            outcome["original"] = next_call(args)
        except Exception as exc:  # pragma: no cover - mirrors tool error path
            outcome["original_error"] = exc

    # Daemon thread: if the relay answer wins, this thread keeps blocking on
    # the gateway's own prompt until it is answered or times out; its result
    # is then discarded. It must never hold up interpreter shutdown.
    threading.Thread(target=_run_original, name="conduit-push-clarify", daemon=True).start()

    deadline = time.monotonic() + _clarify_poll_budget()
    while time.monotonic() < deadline:
        if "original" in outcome or "original_error" in outcome:
            if "original_error" in outcome:
                raise outcome["original_error"]
            return outcome["original"]
        try:
            status = client.poll_decision(request_id)
        except Exception:
            status = {"status": "pending"}  # transport hiccup: keep waiting
        if status.get("status") == "answered":
            return _format_result(
                question=question,
                offered=offered,
                answer=str(status.get("answer") or ""),
                multi_select=multi_select,
            )
        time.sleep(POLL_INTERVAL_SECONDS)

    # Poll budget exhausted (unlimited clarify timeout): fall back to whatever
    # the original path produces, waiting for it if necessary.
    if "original_error" in outcome:
        raise outcome["original_error"]
    while "original" not in outcome and "original_error" not in outcome:
        time.sleep(POLL_INTERVAL_SECONDS)
    if "original_error" in outcome:
        raise outcome["original_error"]
    return outcome["original"]


def _clarify_poll_budget() -> float:
    """Match the gateway's configured clarify timeout, capped for safety."""
    try:
        from tools.clarify_gateway import get_clarify_timeout

        timeout = get_clarify_timeout()
    except Exception:
        timeout = 3600
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        return MAX_POLL_SECONDS
    return min(float(timeout) + 30.0, MAX_POLL_SECONDS)


def _format_result(*, question: str, offered: list[str], answer: str, multi_select: bool) -> str:
    """Build the same JSON the built-in clarify_tool returns for an answer."""
    from tools.clarify_tool import strip_recommended

    if multi_select and offered:
        user_response: Any = [strip_recommended(part) for part in _parse_multi_select(answer)]
    else:
        user_response = strip_recommended(answer)
    return json.dumps(
        {
            "question": question,
            "choices_offered": offered,
            "user_response": user_response,
        },
        ensure_ascii=False,
    )


def _parse_multi_select(raw: str) -> list[str]:
    """Parse a multi-select answer: JSON array or comma-separated labels.

    Local mirror of the built-in tool's parser (a private function there).
    """
    text = str(raw).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(part).strip() for part in parsed if str(part).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
