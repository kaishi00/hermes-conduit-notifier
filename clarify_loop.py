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

The tool result is built by the built-in ``clarify_tool``'s own result helper
when it is importable (a local mirror otherwise), so the agent cannot tell the
paths apart.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from . import calls, client
from .session_titles import title as chat_title, titled
from .events import (
    card_choice_labels,
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
# instead of polling the full budget. The grace is ITERATION-based, so its
# wall-clock span follows the backoff schedule: the break fires on the 30th
# unproductive poll before its sleep, i.e. 29 sleeps of 2+4+8 + 26*10 =
# ~274s (~4.6 min), NOT the ~60s a flat interval would give. That is
# deliberate: it stays far under a default 3600s clarify timeout while
# tolerating a long relay blip. (A locally DROPPED push never reaches this
# loop at all -- enqueue reports the drop and the middleware falls back
# immediately.)
UNKNOWN_POLL_GRACE = 30
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

    # The pushed decision's wire shape mirrors the ORIGINAL invocation, not
    # the normalized list: a legacy scalar call pushes the scalar decision
    # (no questions[]) so the relay parks it on the scalar path and its
    # answer returns as {"status": "answered", "answer": ...} — the shape
    # this module's scalar branch reads. A questions[] call — even a
    # one-entry one — pushes the batch and completes through per-qid
    # answers. batch_protocol is the only authoritative discriminator; the
    # cardinality of `batch` must never decide the wire shape.
    questions_payload = (
        [
            {"qid": entry["qid"], "question": entry["question"], "choices": entry["choices"], "multi_select": entry["multi_select"]}
            for entry in batch
        ]
        if batch_protocol
        else None
    )

    push = push_event(
        "input.needed",
        identifier=event_id("input", kwargs.get("turn_id"), kwargs.get("tool_call_id"), request_id),
        session_id=session_id,
        profile=profile,
        title=titled("Input needed", session_id),
        title_label="input.needed",
        session_title=chat_title(session_id),
        body=question,
        decision=clarify_decision(
            request_id=request_id,
            question=question,
            choices=labels or None,
            questions=questions_payload,
        ),
    )
    try:
        queued = client.enqueue(push)
    except Exception as error:
        # The push sidecar failed LOCALLY (worker startup, queue/runtime
        # error): the relay will never park this decision, exactly like a
        # queue-full drop. An optional-notification failure must never abort
        # the tool call — use the native path immediately. Only the enqueue
        # boundary is guarded; next_call's own exceptions propagate.
        logger.warning(
            "Conduit clarify notification enqueue failed; using native clarify path: %s",
            error,
        )
        return kwargs["next_call"](args)
    if not queued:
        # The event was dropped BEFORE delivery (local queue full): the relay
        # will never park this decision, so every poll is a phantom — the
        # unknown grace can only burn minutes before the inevitable native
        # fallback. Use the native path immediately.
        return kwargs["next_call"](args)

    # With alert calls on, a question left unanswered for a minute calls
    # the user (#449); any answer, from anywhere, stops it.
    # A batch says how many wait, so a reason clipped to its length keeps it.
    reason = question if len(batch) == 1 else f"{len(batch)} questions, first: {question}"
    # The whole batch, for a call answered with no chat open (the Watch) to
    # read out and answer; the answers come back through
    # ``calls.taken_answers``.
    calls.note_waiting(session_id, "question", request_id=request_id, questions=batch)
    calls.alert(session_id, "question", reason, profile=profile)
    try:
        return _first_answer_wins(
            request_id=request_id,
            next_call=kwargs["next_call"],
            args=args,
            batch=batch,
            batch_protocol=batch_protocol,
        )
    finally:
        calls.cancel_alerts(session_id, "question")


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
    previous_remaining: int | None = None
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
        if client.notifications_paused():
            # The relay refused this pairing after the push was queued: the
            # push was dropped, and polling would only hammer the refusal.
            break
        try:
            status = client.poll_decision(request_id)
        except Exception:
            # Transport failure (relay down, DNS, throttled). Counted against
            # the same grace as unknowns: a persistently unreachable relay
            # must not disable the never-parked stop and poll the full budget.
            status = {"status": "__error__"}
        if not isinstance(status, dict):
            # A 200 whose body isn't an object (a misbehaving relay or
            # proxy): unproductive like a transport failure, not a crash
            # that leaves the decision parked.
            status = {"status": "__error__"}
        if batch_protocol and not isinstance(status.get("answers") or {}, dict):
            # An answers map that isn't an object: unproductive too, not a
            # crash past relay_won that leaves the decision parked.
            status = {"status": "__error__"}
        state = str(status.get("status") or "")
        call_answers = calls.taken_answers(request_id)
        if call_answers is not None:
            # Answered on a call from Hermes, with no chat open (the Watch):
            # the parked decision is released like after a native answer.
            _release_decision(request_id, relay_won)
            return _call_answer_result(batch, batch_protocol, call_answers)
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
            # answer arrived. Format it as a batch result — the other rows
            # come back unanswered with a null response, the upstream absence
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
            remaining = status.get("remaining")
            if isinstance(remaining, list):
                # Batch progress (a shrinking remaining set means another qid
                # just locked) drops back to the short interval: the next
                # answer is imminent, and the capped backoff would otherwise
                # add seconds of dead latency between the remaining answers.
                if previous_remaining is not None and len(remaining) < previous_remaining:
                    unanswered_polls = 0
                previous_remaining = len(remaining)
        elif state == "rejected":
            # An answer for an end-to-end encrypted pairing that the paired
            # phone did not seal (client.opened_answers). It never answers
            # the tool call: release the decision and let Hermes' own clarify
            # path take the question.
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
        _sleep_until_call_answers(request_id, _poll_delay(unanswered_polls))

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


# How often a parked question looks for a call's answers between relay polls.
CALL_ANSWER_POLL_SECONDS = 1.0


def _sleep_until_call_answers(request_id: str, seconds: float) -> None:
    """Sleeps ``seconds``, waking early once a call has answered the batch,
    so the answer doesn't wait out the relay's poll backoff."""
    remaining = seconds
    while remaining > 0:
        step = min(CALL_ANSWER_POLL_SECONDS, remaining)
        time.sleep(step)
        remaining -= step
        if remaining > 0 and calls.has_call_answers(request_id):
            return


def _call_answer_result(batch: list[dict[str, Any]], batch_protocol: bool, answers: dict[str, str]) -> str:
    """The tool result for answers given on a call: the same shapes as a
    device's answers through the relay."""
    if batch_protocol:
        return _format_batch_result(batch, answers)
    entry = batch[0]
    return _format_result(question=entry["question"], offered=list(entry["choices"]),
                          answer=str(answers.get(entry["qid"]) or ""), multi_select=entry["multi_select"])


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

    # Under the profile that parked the decision (see the native-race worker).
    threading.Thread(target=contextvars.copy_context().run, args=(_cancel,), name="conduit-push-release",
                     daemon=True).start()


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
        user_response: Any = [strip_recommended(_full_choice(part, offered)) for part in _parse_multi_select(answer)]
    else:
        user_response = strip_recommended(_full_choice(answer, offered))
    return json.dumps(
        {
            "question": question,
            "choices_offered": offered,
            "user_response": user_response,
        },
        ensure_ascii=False,
    )


def _format_batch_result(batch: list[dict[str, Any]], answers: dict[str, Any]) -> str:
    """Build the JSON the built-in clarify_tool returns for a batch.

    Uses Hermes' own ``tools.clarify_tool._result`` when it is importable, so
    the relay path can't drift from the native one: one row per question in
    the original order with ``status`` (answered, skipped, unanswered) and a
    ``user_response`` that is null unless answered, plus ``outcome``. A qid
    the relay never locked (only through the legacy collapsed-answer path) is
    left out of the answers, so it comes back unanswered. ``_local_result``
    mirrors it for a Hermes without the helper.

    A card shows each choice cut to its label length; an answer made from a
    cut label is mapped back to the full choice first (``_full_choice``), so
    ``user_response`` names a choice Hermes offered.
    """
    normalized = [
        {
            "qid": entry["qid"],
            "question": entry["question"],
            "choices_offered": list(entry["choices"]) or None,
            "multi_select": entry["multi_select"],
        }
        for entry in batch
    ]
    reply = {
        "answers": {
            entry["qid"]: _full_choice_answer(answers[entry["qid"]], entry["choices"], entry["multi_select"])
            for entry in batch
            if entry["qid"] in answers
        },
        "outcome": "submitted",
    }
    try:
        from tools.clarify_tool import _result
    except ImportError:
        return _local_result(normalized, reply)
    return _result(normalized, reply)


def _local_result(normalized: list[dict[str, Any]], reply: dict[str, Any]) -> str:
    """Local mirror of ``tools.clarify_tool._result`` (a private function
    there) for a Hermes that doesn't have it."""
    answers = reply.get("answers") or {}
    responses = []
    for entry in normalized:
        raw = answers.get(entry["qid"])
        value = _clean_answer(raw, entry["multi_select"]) if raw not in (None, "") else None
        if value:
            status = "answered"
        else:
            status, value = ("skipped" if entry["qid"] in answers else "unanswered"), None
        responses.append({"question": entry["question"], "choices_offered": entry["choices_offered"],
                          "status": status, "user_response": value})
    result: dict[str, Any] = {"responses": responses, "outcome": reply["outcome"]}
    if reply.get("notice"):
        result["notice"] = str(reply["notice"])
    return json.dumps(result, ensure_ascii=False)


def _clean_answer(raw: Any, multi: bool) -> Any:
    """Local mirror of ``tools.clarify_tool._clean_answer``: a multi-select
    answer is a list, a JSON array string, or one typed ("Other") answer."""
    from tools.clarify_tool import strip_recommended

    if not multi:
        return strip_recommended(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        raw = parsed if isinstance(parsed, list) else [raw]
    return [strip_recommended(item) for item in raw if str(item).strip()]


def _full_choice_answer(raw: Any, offered: list[str], multi: bool) -> Any:
    """``raw`` with each answered card label mapped back to its full choice
    (a multi-select JSON array item by item)."""
    if not offered or not isinstance(raw, str):
        return raw
    if multi:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return json.dumps([_full_choice(item, offered) if isinstance(item, str) else item for item in parsed],
                              ensure_ascii=False)
    return _full_choice(raw, offered)


# The shortest clipped choice a call's answer is mapped back from.
CALL_CHOICE_PREFIX_MIN = 80


def _full_choice(answer: str, offered: list[str]) -> str:
    """The offered choice a card label stands for. Cards show each choice cut
    to 80 characters (``events.card_choice_labels``), so a tapped long choice
    comes back cut. Only a label that names exactly one offered choice is
    mapped; anything else is kept as typed."""
    text = answer.strip()
    if not text or text in offered:
        return answer
    matches = {choice for choice in offered if text in card_choice_labels(choice)}
    if not matches and len(text) >= CALL_CHOICE_PREFIX_MIN:
        # A call's pick of a choice too long for the calls store, which
        # keeps its start (calls_store.MAX_WAITING_CHOICE_CHARS).
        matches = {choice for choice in offered if choice.strip().startswith(text)}
    return matches.pop() if len(matches) == 1 else answer


def _parse_multi_select(raw: str) -> list[str]:
    """Parse a legacy scalar multi-select answer: JSON array or
    comma-separated labels, as the pre-batch built-in tool did.
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
