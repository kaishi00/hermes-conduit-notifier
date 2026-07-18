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
    return event


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
