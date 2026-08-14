"""Pure event builders for Hermes lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


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
    event: dict[str, Any] = {"event_id": identifier, "type": kind}
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
    # It is optional and only meaningful for decision notifications, so it is
    # ignored for any other event type even if a caller passes it.
    if decision and kind in ("approval.needed", "input.needed"):
        sanitized = sanitize_decision(decision)
        if sanitized:
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


def sanitize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Bound and whitelist a decision payload before it leaves the gateway.

    Defense in depth: the relay re-validates, but keep this side bounded too
    so a malformed hook payload can never push an oversized or unknown-shaped
    object through to the relay/APNs.
    """
    if not isinstance(decision, dict):
        return {}
    kind = _clean(decision.get("kind", ""), 40)
    if kind not in ("approval", "clarify"):
        return {}
    sanitized: dict[str, Any] = {"kind": kind}
    for key in ("session_key", "request_id", "question", "description"):
        value = _clean(decision.get(key, ""), 500)
        if value:
            sanitized[key] = value
    choices = decision.get("choices")
    if isinstance(choices, list):
        cleaned = [_clean(str(choice), 80) for choice in choices if str(choice).strip()]
        if cleaned:
            sanitized["choices"] = cleaned[:8]
    # Require both something to display and the key needed to answer it,
    # otherwise the card is useless and the notification should degrade to the
    # plain routing stub. Approval responds by session_key; clarify by
    # request_id. (Clarify content ships in a later phase.)
    has_display = bool(sanitized.get("description") or sanitized.get("question"))
    answerable = sanitized.get("session_key") if kind == "approval" else sanitized.get("request_id")
    if not has_display or not answerable:
        return {}
    return sanitized


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
    return " ".join(str(value).split())[:maximum]
