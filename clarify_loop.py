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

import contextvars
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from . import client
from .events import (
    clarify_decision,
    event_id,
    normalize_clarify_questions,
    push_event,
)

logger = logging.getLogger("hermes.plugins.conduit_push")

REQUEST_ID_PREFIX = "conduit-push-"
POLL_INTERVAL_SECONDS = 2.0
# Consecutive unproductive polls (unknown status or transport errors) tolerated
# while the event POST and the first poll race (enqueue is async, delivery can
# lag, relays blip). Past this the decision was never parked or the relay is
# unreachable -- polling can never succeed -- so fall back to the original path
# instead of polling the full budget.
UNKNOWN_POLL_GRACE = 30  # ~60s at the base interval
# Hard cap on polling even when the gateway configures an unlimited clarify
# timeout; the relay expires pending decisions at 2h, and polling stops on
# the unknown-after-pending transition well before this in practice.
MAX_POLL_SECONDS = 24 * 60 * 60.0
# Unanswered polls back off exponentially (capped) so a parked-but-idle
# decision costs ~a handful of requests per minute instead of 30+, and a
# gateway's concurrent loops stay well under the relay's shared poll budget.
MAX_POLL_BACKOFF_SECONDS = 10.0

# Set by register() so pushes carry the profile the rest of the plugin uses.
profile = "default"


def set_profile(value: str) -> None:
    global profile
    profile = value or "default"


def middleware(is_child_session: Callable[[str], bool] | None = None, **kwargs: Any) -> Any:
    """``tool_execution`` middleware entry point.

    ``is_child_session`` mirrors the hook path's subagent exclusion so a
    delegated child session's clarify keeps the original path only.
    """
    tool_name = str(kwargs.get("tool_name") or "")
    if tool_name != "clarify":
        return kwargs["next_call"](kwargs.get("args") or {})
    if not client.load_state():
        # Unpaired profile: no push is possible, keep the original path only.
        return kwargs["next_call"](kwargs.get("args") or {})
    session_id = _text(kwargs.get("session_id"))
    if is_child_session and is_child_session(session_id):
        return kwargs["next_call"](kwargs.get("args") or {})

    args = dict(kwargs.get("args") or {})
    # The FULL batch is normalized first: current Hermes calls carry
    # questions[] and legacy scalar calls become a one-question batch.
    # Reducing the payload to its first question would silently drop
    # questions 2..N from the background path. `question` stays the
    # notification body / collapsed summary for pre-batch Conduit builds.
    #
    # Protocol provenance comes from the ORIGINAL invocation — a one-entry
    # questions[] call is batch protocol and must produce the batch result
    # shape; cardinality of the normalized list must never decide this.
    batch_protocol = isinstance(args.get("questions"), list) and len(args["questions"]) > 0
    batch = normalize_clarify_questions(args)
    if not batch:
        return kwargs["next_call"](args)
    question = batch[0]["question"]
    labels = list(batch[0]["choices"])
    request_id = f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex[:12]}"

    client.enqueue(push_event(
        "input.needed",
        identifier=event_id("input", kwargs.get("turn_id"), kwargs.get("tool_call_id"), request_id),
        session_id=session_id,
        profile=profile,
        body=question,
        decision=clarify_decision(
            request_id=request_id,
            question=question,
            choices=labels or None,
            questions=[
                {"qid": entry["qid"], "question": entry["question"], "choices": entry["choices"], "multi_select": entry["multi_select"]}
                for entry in batch
            ],
        ),
    ))

    return _first_answer_wins(
        request_id=request_id,
        next_call=kwargs["next_call"],
        args=args,
        batch=batch,
        batch_protocol=batch_protocol,
    )


def _first_answer_wins(
    *,
    request_id: str,
    next_call: Any,
    args: dict[str, Any],
    batch: list[dict[str, Any]],
    batch_protocol: bool,
) -> Any:
    """Race the original clarify call against the relay answer poll.

    Single-question decisions resolve on one relay answer, exactly as before.
    Batch-protocol decisions (a non-empty original ``questions`` array —
    including exactly one question) complete only when EVERY qid is locked
    through the relay and format the built-in batch result shape; a native
    (desktop/CLI) completion of the whole original call wins instead,
    releases the parked decision, and the losing path is discarded.
    """
    outcome: dict[str, Any] = {}
    relay_won = False

    def _run_original() -> None:
        try:
            outcome["original"] = next_call(args)
        except Exception as exc:  # pragma: no cover - mirrors tool error path
            outcome["original_error"] = exc

    # Hermes invokes middleware synchronously inline (verified against
    # hermes_cli.middleware._run_execution_chain: plain calls, no event loop),
    # so exactly one of the two racing paths must move off-thread — this one.
    # The gateway's own prompt blocks on a threading.Event, which is
    # thread-safe, but contextvars do NOT propagate to new threads; run the
    # body through a copy of the current context so anything the tool path
    # reads (hook scoping, session context) behaves as if inline.
    # Daemon: if the relay answer wins, this thread keeps blocking on the
    # gateway's own prompt until it is answered or times out; its result is
    # then discarded. It must never hold up interpreter shutdown.
    threading.Thread(
        target=contextvars.copy_context().run,
        args=(_run_original,),
        name="conduit-push-clarify",
        daemon=True,
    ).start()

    deadline = time.monotonic() + _clarify_poll_budget()
    saw_pending = False
    consecutive_unproductive = 0
    unanswered_polls = 0
    while time.monotonic() < deadline:
        if "original" in outcome or "original_error" in outcome:
            # The native path resolved the whole call first (desktop/CLI
            # answered through the gateway). Release the parked decision so
            # a late device answer is rejected (409) instead of being
            # reported as accepted while its result is discarded.
            _release_decision(request_id, relay_won)
            if "original_error" in outcome:
                raise outcome["original_error"]
            return outcome["original"]
        try:
            status = client.poll_decision(request_id)
        except Exception:
            # Transport failure (relay down, DNS, throttled). Counted against
            # the same grace as unknowns: a persistently unreachable relay
            # must not disable the never-parked stop and poll the full budget.
            status = {"status": "__error__"}
        state = str(status.get("status") or "")
        if state == "answered":
            if batch_protocol and "answers" in status:
                # Complete batch: the relay only reports answered once every
                # qid is locked, so first-answer-wins held per question.
                relay_won = True
                return _format_batch_result(batch, status.get("answers") or {})
            if not batch_protocol:
                relay_won = True
                offered = list(batch[0]["choices"])
                return _format_result(
                    question=batch[0]["question"],
                    offered=offered,
                    answer=str(status.get("answer") or ""),
                    multi_select=batch[0]["multi_select"],
                )
            # Batch protocol, but the answer arrived in the legacy collapsed
            # shape (old relay or pre-batch device): only the first question's
            # answer arrived. Format it as a batch result — the unanswered
            # rows surface as empty user_response, the upstream absence
            # semantics — instead of pretending the whole batch was answered.
            relay_won = True
            return _format_batch_result(
                batch,
                {batch[0]["qid"]: str(status.get("answer") or "")},
            )
        if state == "pending":
            consecutive_unproductive = 0
            saw_pending = True
            if status.get("deliverable") is False:
                # Parked but no card was shown (device preferences disabled
                # this notification): nobody can answer by id. Stop polling
                # and let the original path's timeout conclude the race.
                break
        elif state == "unknown" and saw_pending:
            # unknown-after-pending: the relay expired the decision (2h TTL,
            # far under an unlimited clarify timeout).
            break
        else:
            # Unknown before pending, or a transport failure: tolerated for
            # the grace window, then treated as never-parked/unreachable.
            consecutive_unproductive += 1
            if consecutive_unproductive >= UNKNOWN_POLL_GRACE:
                break
        unanswered_polls += 1
        time.sleep(_poll_delay(unanswered_polls))

    # Poll budget exhausted (unlimited clarify timeout): fall back to whatever
    # the original path produces, waiting for it if necessary. The parked
    # decision is released first so late device answers are rejected.
    _release_decision(request_id, relay_won)
    if "original_error" in outcome:
        raise outcome["original_error"]
    while "original" not in outcome and "original_error" not in outcome:
        time.sleep(POLL_INTERVAL_SECONDS)
    if "original_error" in outcome:
        raise outcome["original_error"]
    return outcome["original"]


def _release_decision(request_id: str, relay_won: bool) -> None:
    """Cancel the parked decision when the relay path did not win.

    Strictly off the native answer's critical path: the DELETE can wait out
    the relay's HTTP timeout on a slow or unreachable relay, and the user's
    native answer must never queue behind it. The cancel is fired on a
    daemon thread — ordering with tool completion does not matter, because
    the relay rejects answers to a released decision regardless of whether
    the release has landed yet.
    """
    if relay_won:
        return

    def _cancel() -> None:
        try:
            client.cancel_decision(request_id)
        except Exception as error:  # pragma: no cover - defensive
            logger.warning("Conduit decision release failed for %s: %s", request_id, error)

    threading.Thread(target=_cancel, name="conduit-push-release", daemon=True).start()


def _poll_delay(unanswered_polls: int) -> float:
    """Exponential backoff for unanswered polls, capped.

    A parked-but-idle decision should cost a handful of requests per minute,
    not 30+, and a gateway's concurrent loops must stay well under the relay's
    shared per-gateway poll budget. Answers land within one capped interval.
    """
    base = POLL_INTERVAL_SECONDS
    if unanswered_polls <= 1:
        return base
    return min(base * (2 ** min(unanswered_polls - 1, 8)), MAX_POLL_BACKOFF_SECONDS)


def _clarify_poll_budget() -> float:
    """Match the gateway's configured clarify timeout, capped for safety."""
    try:
        from tools.clarify_gateway import get_clarify_timeout

        timeout = float(get_clarify_timeout())
    except Exception:
        timeout = 3600.0
    if timeout <= 0:
        return MAX_POLL_SECONDS
    return min(timeout + 30.0, MAX_POLL_SECONDS)


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


def _format_batch_result(batch: list[dict[str, Any]], answers: dict[str, Any]) -> str:
    """Build the same JSON the built-in clarify_tool returns for a batch.

    Mirrors tools.clarify_tool._batch_result: one row per question in the
    original order, each carrying the model-supplied id when present, the
    offered choices, and the cleaned user response (multi-select answers
    parse back into a list). A qid the relay never locked — only possible
    through the legacy collapsed-answer path — yields an empty
    user_response, which upstream defines as an absence rather than a skip.
    """
    from tools.clarify_tool import strip_recommended

    responses: list[dict[str, Any]] = []
    for entry in batch:
        row: dict[str, Any] = {}
        if entry.get("id"):
            row["id"] = entry["id"]
        row["question"] = entry["question"]
        row["choices_offered"] = list(entry["choices"])
        raw = str(answers.get(entry["qid"]) or "")
        if raw:
            if entry["multi_select"] and entry["choices"]:
                row["user_response"] = [strip_recommended(part) for part in _parse_multi_select(raw)]
            else:
                row["user_response"] = strip_recommended(raw)
        else:
            row["user_response"] = ""
        responses.append(row)
    return json.dumps({"responses": responses}, ensure_ascii=False)


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
