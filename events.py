"""Pure event builders for Hermes lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

# Keep in sync with plugin.yaml. Reported on every event so the relay can
# expose per-gateway compatibility state to the app (Settings > Notifications).
PLUGIN_VERSION = "0.3.0"
PLUGIN_CAPABILITIES = [
    "approval-decisions",
    "clarify-loop",
    "batch-clarify-decisions",
    "version-reporting",
]

# Bounds for batch clarify decisions, mirroring the single-question limits
# (8 questions x 8 choices, same text caps) so a pushed batch can never
# grow past what the relay, APNs, or a card should carry.
MAX_BATCH_QUESTIONS = 8
MAX_BATCH_CHOICES = 8

# Same accepted-qid contract as the relay's sanitizeBatchQuestions: this
# plugin must never emit a batch the relay would silently shrink.
_QID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_QID_RESERVED = {"__proto__", "constructor", "prototype"}


def event_id(prefix: str, *parts: Any) -> str:
    values = [str(part).strip() for part in parts if str(part or "").strip()]
    if values:
        digest = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:32]
        return f"{prefix}:{digest}"
    return f"{prefix}:{uuid.uuid4()}"


def push_event(
    kind: str,
    *,
    identifier: str,
    session_id: str = "",
    profile: str = "default",
    title: str = "",
    body: str = "",
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": identifier,
        "type": kind,
        "plugin_version": PLUGIN_VERSION,
        "plugin_capabilities": list(PLUGIN_CAPABILITIES),
    }
    if session_id:
        event["session_id"] = session_id[:180]
    if profile:
        event["profile"] = profile[:80]
    if title:
        event["title"] = _clean(title, 120)
    if body:
        event["body"] = _clean(body, 500)
    # `decision` carries the structured card content that lets Conduit render
    # an answerable card from the push payload alone, without the one-shot
    # gateway stream event (which is missed while the app is backgrounded).
    # The decision kind must match the event type: approval.needed -> approval
    # (answered via the gateway's approval.respond), input.needed -> clarify
    # (answered via the relay's decision loop with a plugin-minted id).
    if decision and kind in ("approval.needed", "input.needed"):
        sanitized = sanitize_decision(decision)
        expected_kind = "approval" if kind == "approval.needed" else "clarify"
        if sanitized and sanitized.get("kind") == expected_kind:
            event["decision"] = sanitized
    return event


def approval_decision(*, session_key: str, description: str) -> dict[str, Any]:
    """Build the structured payload for an approval notification.

    The `pre_approval_request` hook exposes `description` and `session_key` but
    not the allow_session/allow_permanent flags, so the exact server-side choice
    set is unknown here. ``once`` and ``deny`` are always valid choices (Hermes
    builds every `ApprovalRequest` with at least those two), so the push card
    offers that always-safe subset; the live foreground card still shows the
    full set. The raw `command` is intentionally omitted — it can carry
    secrets, and the relay/APNs path is an extra egress transport.
    """
    return {
        "kind": "approval",
        "session_key": session_key,
        "description": description,
        "choices": ["once", "deny"],
    }


def clarify_decision(
    *,
    request_id: str,
    question: str,
    choices: list[str] | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the structured payload for a clarify notification.

    The gateway's own clarify request id is minted inside its blocking prompt
    and unreachable to plugins, so the middleware mints this id and answers
    return through the relay's /v1/decisions endpoints while the original
    clarify callback keeps serving desktop/CLI clients.

    `questions` carries the FULL batch (current Hermes `questions[]`
    protocol): each entry keeps the gateway qid as identity plus its
    choices and multi_select flag, so background Conduit devices see and
    answer every question instead of only the first. The collapsed
    `question`/`choices` summary stays in the payload for older Conduit
    builds, which render an answerable first-question card from it.

    Legacy scalar invocations pass no `questions`, keeping the decision —
    and therefore the relay's parked decision and answer shape — scalar
    end to end. Protocol provenance lives in the original invocation shape,
    never in the normalized question count.
    """
    decision: dict[str, Any] = {"kind": "clarify", "request_id": request_id, "question": question}
    if choices:
        decision["choices"] = choices
    if questions:
        decision["questions"] = questions
    return decision


def normalize_clarify_questions(args: Any) -> list[dict[str, Any]]:
    """Normalize every clarify arg shape into one batch-capable list.

    Accepts the current `questions[]` protocol AND the legacy scalar
    `question`/`prompt`/`message` shape (which becomes a one-question
    batch). Nothing is discarded: each entry keeps a stable qid
    (gateway-style `q<index>` — the same minting the gateway's own bridge
    uses), the question text, its flattened choice labels, and the
    multi_select flag. Entries are NEVER reduced to the first question.

    Upstream's own normalizer is used when importable so qid minting and
    choice flattening match the tool exactly; a local mirror keeps this
    working against older Hermes installs.
    """
    if not isinstance(args, dict):
        return []
    raw_questions = args.get("questions")
    if isinstance(raw_questions, list) and raw_questions:
        try:
            from tools.clarify_tool import _normalize_questions

            normalized, error = _normalize_questions(raw_questions)
            if error is None and normalized:
                return [
                    {
                        "qid": entry["qid"],
                        "id": entry.get("id"),
                        "question": entry["question"],
                        "choices": list(entry["choices_offered"] or []),
                        "multi_select": bool(entry["multi_select"]),
                    }
                    for entry in normalized[:MAX_BATCH_QUESTIONS]
                ]
        except Exception:
            pass
        return _normalize_questions_locally(raw_questions)
    text = scalar_question_text(args)
    if not text:
        return []
    labels = flatten_choice_labels(args.get("choices"))
    return [
        {
            "qid": "q0",
            "id": None,
            "question": text,
            "choices": labels,
            # multi_select without choices degrades exactly like the gateway.
            "multi_select": bool(args.get("multi_select")) and bool(labels),
        }
    ]


def scalar_question_text(args: Any) -> str:
    """The legacy scalar question text, without clarification_text's canned
    fallback — an unparseable shape must normalize to NO question, never to
    a synthetic prompt the user would be asked to answer."""
    if not isinstance(args, dict):
        return ""
    for key in ("question", "prompt", "message"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_questions_locally(raw_questions: list[Any]) -> list[dict[str, Any]]:
    """Local mirror of the upstream batch normalizer (older Hermes installs).

    Matches tools.clarify_tool._normalize_questions precisely: qids are
    minted from the RAW position (`q{enumerate index}`), bare-string entries
    are tolerated, a non-dict entry or an entry without question text
    rejects the WHOLE batch (upstream returns an error, it never skips), and
    multi_select is honored only when choices exist.
    """
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_questions[:MAX_BATCH_QUESTIONS]):
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            return []
        text = str(item.get("question") or "").strip()
        if not text:
            return []
        labels = flatten_choice_labels(item.get("choices"))
        normalized.append(
            {
                "qid": f"q{index}",
                "id": str(item.get("id") or "").strip() or None,
                "question": text,
                "choices": labels,
                # Upstream honors multi_select only when choices exist.
                "multi_select": bool(item.get("multi_select")) and bool(labels),
            }
        )
    return normalized


def flatten_choice_labels(choices: Any) -> list[str]:
    """Flatten clarify tool choices to display labels.

    Mirrors tools.clarify_tool._flatten_choice: bare strings pass through and
    LLM-emitted dict shapes unwrap via their canonical user-facing keys.
    """
    if not isinstance(choices, list):
        return []
    labels: list[str] = []
    for choice in choices:
        label = ""
        if isinstance(choice, str):
            label = choice.strip()
        elif isinstance(choice, dict):
            for key in ("label", "description", "text", "title"):
                value = choice.get(key)
                if isinstance(value, str) and value.strip():
                    label = value.strip()
                    break
        if label:
            labels.append(label)
    return labels[:8]


def sanitize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Bound and whitelist a decision payload before it leaves the gateway.

    Defense in depth: the relay re-validates, but keep this side bounded too
    so a malformed hook payload can never push an oversized or unknown-shaped
    object through to the relay/APNs. Mirrors the relay's contract:
    approval requires the session key that routes the answer, the description
    that renders the card, and non-empty whitelisted choices; clarify
    requires the plugin-minted request id and the question.
    """
    if not isinstance(decision, dict):
        return {}
    kind = _clean(decision.get("kind", ""), 40)
    if kind == "approval":
        sanitized: dict[str, Any] = {"kind": "approval"}
        for key in ("session_key", "description"):
            value = _clean(decision.get(key, ""), 500)
            if value:
                sanitized[key] = value
        choices = decision.get("choices")
        if isinstance(choices, list):
            # Clean first, then drop empties: filtering on the raw value would
            # coerce None to the truthy "None" and forward it as a choice.
            cleaned = [value for value in (_clean(choice, 80) for choice in choices) if value]
            if cleaned:
                sanitized["choices"] = cleaned[:8]
        if not sanitized.get("description") or not sanitized.get("session_key"):
            return {}
        if not sanitized.get("choices"):
            return {}
        return sanitized
    if kind == "clarify":
        sanitized: dict[str, Any] = {"kind": "clarify"}
        for key in ("request_id", "question"):
            value = _clean(decision.get(key, ""), 500)
            if value:
                sanitized[key] = value
        choices = decision.get("choices")
        if isinstance(choices, list):
            cleaned = [value for value in (_clean(choice, 80) for choice in choices) if value]
            if cleaned:
                sanitized["choices"] = cleaned[:MAX_BATCH_CHOICES]
        # Batch form: the full question set rides alongside the collapsed
        # summary. Each entry keeps its qid (per-question answer identity),
        # text, labels, and multi_select flag; one malformed entry is dropped
        # rather than failing the whole decision.
        raw_questions = decision.get("questions")
        if isinstance(raw_questions, list):
            questions: list[dict[str, Any]] = []
            seen_qids: set[str] = set()
            for entry in raw_questions[:MAX_BATCH_QUESTIONS]:
                if not isinstance(entry, dict):
                    continue
                qid = _clean(entry.get("qid", ""), 40)
                # Mirror of the relay's sanitizeBatchQuestions: same accepted
                # charset, same reserved-name rejection, same first-qid-wins
                # dedup and choice dedup — the relay must never silently
                # shrink a batch this plugin emits.
                if not _QID_PATTERN.fullmatch(qid) or qid in _QID_RESERVED or qid in seen_qids:
                    continue
                text = _clean(entry.get("question", ""), 500)
                if not text:
                    continue
                question: dict[str, Any] = {"qid": qid, "question": text}
                labels = entry.get("choices")
                if isinstance(labels, list):
                    cleaned_labels: list[str] = []
                    for label in labels[:MAX_BATCH_CHOICES]:
                        cleaned = _clean(label, 80)
                        if cleaned and cleaned not in cleaned_labels:
                            cleaned_labels.append(cleaned)
                    if cleaned_labels:
                        question["choices"] = cleaned_labels
                if entry.get("multi_select") is True:
                    # Strict boolean: a truthy string must not coerce into a
                    # selected-state flag (mirrors the relay's === true check).
                    question["multi_select"] = True
                questions.append(question)
                seen_qids.add(qid)
            if questions:
                sanitized["questions"] = questions
        if not sanitized.get("request_id") or not sanitized.get("question"):
            return {}
        return sanitized
    return {}


def plugin_hello() -> dict[str, Any]:
    """A control event, sent right after pairing, that reports this plugin's
    version and capabilities without producing a notification. The relay
    records it so the app can show plugin compatibility immediately instead of
    waiting for the first real event."""
    return push_event(
        "plugin.hello",
        identifier=event_id("hello", PLUGIN_VERSION),
        profile="default",
        body="",
    )


def clarification_text(args: Any) -> str:
    if not isinstance(args, dict):
        return "Hermes needs your response before it can continue."
    for key in ("question", "prompt", "message"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    questions = args.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        if isinstance(first, str):
            return first.strip()
        if isinstance(first, dict):
            return str(first.get("question") or first.get("prompt") or "").strip()
    return "Hermes needs your response before it can continue."


def is_silent_response(value: Any) -> bool:
    """Return true only for Hermes' exact no-notification response sentinel."""
    return isinstance(value, str) and " ".join(value.split()).casefold() == "[silent]"


def _clean(value: str, maximum: int) -> str:
    # Non-strings (None, numbers, nested objects) must not be coerced —
    # str(None) would produce a truthy "None" that slips past emptiness
    # checks and gets forwarded to the relay. Drop them instead, matching
    # the relay's cleanText semantics.
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]
