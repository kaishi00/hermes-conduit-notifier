"""Pure event builders for Hermes lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

# Keep in sync with plugin.yaml. Reported on every event so the relay can
# expose per-gateway compatibility state to the app (Settings > Notifications).
PLUGIN_VERSION = "0.2.0"
PLUGIN_CAPABILITIES = [
    "approval-decisions",
    "clarify-loop",
    "version-reporting",
]


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


def clarify_decision(*, request_id: str, question: str, choices: list[str] | None = None) -> dict[str, Any]:
    """Build the structured payload for a clarify notification.

    The gateway's own clarify request id is minted inside its blocking prompt
    and unreachable to plugins, so the middleware mints this id and answers
    return through the relay's /v1/decisions endpoints while the original
    clarify callback keeps serving desktop/CLI clients.
    """
    decision: dict[str, Any] = {"kind": "clarify", "request_id": request_id, "question": question}
    if choices:
        decision["choices"] = choices
    return decision


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
        sanitized = {"kind": "clarify"}
        for key in ("request_id", "question"):
            value = _clean(decision.get(key, ""), 500)
            if value:
                sanitized[key] = value
        choices = decision.get("choices")
        if isinstance(choices, list):
            cleaned = [value for value in (_clean(choice, 80) for choice in choices) if value]
            if cleaned:
                sanitized["choices"] = cleaned[:8]
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
