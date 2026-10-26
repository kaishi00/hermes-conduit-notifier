"""Predictions for Conduit's opt-in Predictive features (hermes-conduit
Predictive design, 2026-10-25).

Conduit asks three kinds of question about a chat, each answered by a fork
of the live chat like Hermes' ``/btw`` (agent/side_question.py): the same
prompt cache, every tool refused, nothing written to the transcript, state.db
or memory.

- ``replies``: what the user will most likely send next, after a reply.
- ``draft``: while the user pauses typing, continuations of their unsent
  draft and the whole messages they probably mean.
- ``turn``: while the user looks at the chat without writing, whether the
  agent wants a turn of its own, and if not when to ask again.

``prompt.btw`` sends its answer to every client on the chat, so Hermes
Desktop would show each prediction as a ``[btw …]`` note. The dashboard
route ``POST /predict`` runs the fork itself and answers only the device
that asked. This module holds what doesn't need Hermes: the request's
checks, the question's words and the answer's checks. Conduit measures every
duration and sends it; the question writes them out so the model never
calculates.

Standalone (no imports from the plugin package): the dashboard loads it from
its file.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

KINDS = ("replies", "draft", "turn")
MODELS = ("chat", "fast")
MAX_SESSION_ID_CHARS = 200
MAX_DRAFT_CHARS = 2000
MAX_LANGUAGE_CHARS = 40
MAX_LOCAL_TIME_CHARS = 60
MIN_COUNT, MAX_COUNT, DEFAULT_COUNT = 1, 5, 3
# Continuations of a draft: always up to three, a few words each.
MAX_NEXT = 3
MAX_NEXT_CHARS = 60
# The prompt asks for 140; a reply that runs over is kept up to this, and
# a longer one is left out.
MAX_REPLY_CHARS = 280
MAX_WHY_CHARS = 160
MAX_PLAN_CHARS = 400
# A guess the model gives under this is left out.
MIN_P = 0.1
# When the agent asks to be asked again, held to this window (seconds).
MIN_ASK_AGAIN_S, MAX_ASK_AGAIN_S = 30, 900
MAX_CHECKS = 10
# Longest the route waits for an answer (seconds); Conduit may ask for less.
MAX_WAIT_S, DEFAULT_WAIT_S = 30.0, 20.0
TAG = "[conduit:predict:{kind} v1]"


class Request:
    __slots__ = ("session_id", "kind", "count", "draft", "language", "model", "facts", "wait_s")

    def __init__(self, session_id: str, kind: str, count: int, draft: str, language: Optional[str],
                 model: str, facts: Dict[str, Any], wait_s: float) -> None:
        self.session_id = session_id
        self.kind = kind
        self.count = count
        self.draft = draft
        self.language = language
        self.model = model
        self.facts = facts
        self.wait_s = wait_s


def _one_line(text: str) -> str:
    """Request text that goes into the question on one line: control
    characters and line breaks become spaces, so it can't start lines of
    its own."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f\u2028\u2029]", " ", text)).strip()


def _quoted_draft(draft: str) -> str:
    """The draft between the question's markers, which it can't close."""
    return draft.replace("<<<", "\u2039\u2039\u2039").replace(">>>", "\u203a\u203a\u203a")


def _seconds(value: Any, name: str) -> int:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or value != value
            or value < 0 or value > 10 * 365 * 86400):
        raise ValueError(f"facts.{name} must be a number of seconds")
    return int(value)


def parse(body: Any) -> Request:
    """A route's body as a request. Raises ValueError, with what's wrong, for
    anything else."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > MAX_SESSION_ID_CHARS:
        raise ValueError("session_id must be a chat's runtime session id")
    kind = body.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    count = body.get("count", DEFAULT_COUNT)
    if isinstance(count, bool) or not isinstance(count, int) or not MIN_COUNT <= count <= MAX_COUNT:
        raise ValueError(f"count must be a whole number from {MIN_COUNT} to {MAX_COUNT}")
    draft = body.get("draft", "")
    if not isinstance(draft, str) or len(draft) > MAX_DRAFT_CHARS:
        raise ValueError(f"draft must be text of at most {MAX_DRAFT_CHARS} characters")
    if kind == "draft" and not draft.strip():
        raise ValueError("a draft question needs the draft")
    language = body.get("language")
    if language is not None and (not isinstance(language, str) or len(language) > MAX_LANGUAGE_CHARS):
        raise ValueError("language must be a language's name, or left out")
    model = body.get("model", "chat")
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}")
    wait_s = body.get("wait_s", DEFAULT_WAIT_S)
    if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or not 0 < wait_s <= MAX_WAIT_S:
        raise ValueError(f"wait_s must be a number of seconds up to {MAX_WAIT_S:g}")
    facts: Dict[str, Any] = {}
    if kind == "turn":
        raw = body.get("facts")
        if not isinstance(raw, dict):
            raise ValueError("a turn question needs facts")
        facts["last_reply_s"] = _seconds(raw.get("last_reply_s"), "last_reply_s")
        facts["on_screen_s"] = _seconds(raw.get("on_screen_s"), "on_screen_s")
        check, checks = raw.get("check"), raw.get("max_checks")
        if (isinstance(check, bool) or isinstance(checks, bool) or not isinstance(check, int)
                or not isinstance(checks, int) or not 1 <= check <= checks <= MAX_CHECKS):
            raise ValueError(f"facts.check and facts.max_checks must be whole numbers, 1 ≤ check ≤ max_checks ≤ {MAX_CHECKS}")
        facts["check"], facts["max_checks"] = check, checks
        local_time = raw.get("local_time")
        if local_time is not None and (not isinstance(local_time, str) or len(local_time) > MAX_LOCAL_TIME_CHARS):
            raise ValueError("facts.local_time must be short text, or left out")
        facts["local_time"] = (_one_line(local_time) or None) if isinstance(local_time, str) else None
    return Request(session_id.strip(), kind, count, draft, _one_line(language or "") or None, model, facts,
                   float(wait_s))


def duration(seconds: int) -> str:
    """``192`` as "3 min 12 s": written out, so the model never converts."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


def _language_line(request: Request) -> str:
    if request.language:
        return f"Write in the user's language ({request.language} unless the chat shows otherwise)."
    return "Write in the language the user writes in."


def question(request: Request) -> str:
    """The side question the fork answers."""
    tag = TAG.format(kind=request.kind)
    if request.kind == "replies":
        return "\n".join((
            f"{tag} Predict what the user will most likely send next in this chat.",
            f'Return only JSON: {{"replies":[{{"text":string,"p":number}}]}}, at most {request.count}, best first.',
            "Each text is the user's own next message, in their voice, at most 140 characters, ready to send as is.",
            "p is your probability, 0 to 1, that it is what they send. Make each one different.",
            _language_line(request),
            'Do not answer the user and do not describe the chat. If nothing is likely: {"replies":[]}.',
        ))
    if request.kind == "draft":
        return "\n".join((
            f"{tag} The user paused while typing their next message in this chat.",
            "Their unsent draft, exactly, between the markers (it may end mid-word):",
            f"<<<{_quoted_draft(request.draft)}>>>",
            'Return only JSON: {"next":[{"text":string,"p":number}],"replies":[{"text":string,"p":number}]}.',
            f"next: up to {MAX_NEXT} continuations of 1 to 6 words that start exactly where the draft stops"
            " (begin with a space when a new word starts), best first.",
            f"replies: up to {request.count} complete messages the user probably means to send, best first,"
            " at most 140 characters each.",
            "p is your probability, 0 to 1.",
            _language_line(request),
            "Do not answer the draft.",
        ))
    facts = request.facts
    lines = [
        f"{tag} You may get one extra turn in this chat without the user writing.",
        "Measured for you, do not compute anything: your last reply ended "
        f"{duration(facts['last_reply_s'])} ago. The user has had this chat on screen without typing for "
        f"{duration(facts['on_screen_s'])}. This is check {facts['check']} of {facts['max_checks']} for that reply.",
    ]
    if facts.get("local_time"):
        lines.append(f"The user's local time: {facts['local_time']}.")
    lines += [
        "Say yes only for something concrete and useful: a check you promised, a result worth looking at,"
        " a clear next step, or a question that unblocks them. Otherwise wait; silence is fine.",
        'Return only JSON: {"take_turn":boolean,"why":string,"plan":string,"ask_again_in_seconds":integer|null}.',
        "why: at most 20 words, shown to the user, in their language. plan: what you would do with the turn.",
        "ask_again_in_seconds: when to ask you again if you wait now; null means not again until the user writes.",
    ]
    return "\n".join(lines)


def nudge(request: Request, why: str, plan: str) -> str:
    """The hidden user turn Conduit sends when the agent asked for a turn."""
    facts = request.facts
    said = f' When asked, you wanted a turn: "{why}".' if why else " When asked, you wanted a turn."
    planned = f' Your plan: "{plan}".' if plan else ""
    return (f"[Conduit] It has been {duration(facts['last_reply_s'])} since your last reply, and the user has"
            f" had this chat open for {duration(facts['on_screen_s'])} without writing.{said}{planned} Go ahead"
            " if it still helps them. If not, answer exactly [SILENT].")


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _json_object(text: Any) -> Optional[Dict[str, Any]]:
    """The first JSON object in a model's answer (code fences and words
    around it allowed), or None."""
    if not isinstance(text, str):
        return None
    stripped = _FENCE.sub("", text.strip())
    start = stripped.find("{")
    while start != -1:
        try:
            value, _end = json.JSONDecoder().raw_decode(stripped, start)
        except ValueError:
            start = stripped.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = stripped.find("{", start + 1)
    return None


def _guesses(raw: Any, limit: int, max_chars: int, keep_leading_space: bool = False) -> List[Dict[str, Any]]:
    """Ranked, checked guesses: text clipped, p in 0…1 (by rank when
    missing), under ``MIN_P`` left out, repeats left out, best first."""
    if not isinstance(raw, list):
        return []
    found: List[Dict[str, Any]] = []
    seen = set()
    for rank, item in enumerate(raw[:20]):
        text = item.get("text") if isinstance(item, dict) else item if isinstance(item, str) else None
        if not isinstance(text, str):
            continue
        text = " ".join(text.split()) if not keep_leading_space else (
            (" " if text[:1].isspace() else "") + " ".join(text.split()))
        if not text.strip() or len(text) > max_chars:
            continue
        key = text.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        p = item.get("p") if isinstance(item, dict) else None
        if isinstance(p, bool) or not isinstance(p, (int, float)) or p != p:
            p = max(MIN_P, 0.6 - 0.1 * rank)
        p = min(1.0, max(0.0, float(p)))
        if p < MIN_P:
            continue
        found.append({"text": text, "p": round(p, 3)})
    found.sort(key=lambda guess: -guess["p"])
    return found[:limit]


def _short(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def answer(request: Request, text: Any) -> Dict[str, Any]:
    """The fork's answer as the route's result. An answer that isn't the
    JSON asked for reads as nothing to suggest (``parsed`` false)."""
    value = _json_object(text)
    parsed = value is not None
    value = value or {}
    if request.kind == "replies":
        return {"parsed": parsed, "replies": _guesses(value.get("replies"), request.count, MAX_REPLY_CHARS)}
    if request.kind == "draft":
        return {"parsed": parsed,
                "next": _guesses(value.get("next"), MAX_NEXT, MAX_NEXT_CHARS, keep_leading_space=True),
                "replies": _guesses(value.get("replies"), request.count, MAX_REPLY_CHARS)}
    take = value.get("take_turn") is True
    why = _short(value.get("why"), MAX_WHY_CHARS)
    plan = _short(value.get("plan"), MAX_PLAN_CHARS)
    again = value.get("ask_again_in_seconds")
    if isinstance(again, bool) or not isinstance(again, (int, float)) or again != again:
        again = None
    else:
        again = int(min(MAX_ASK_AGAIN_S, max(MIN_ASK_AGAIN_S, again)))
    result: Dict[str, Any] = {"parsed": parsed, "take_turn": take, "why": why,
                              "ask_again_in_seconds": None if take else again}
    if take:
        result["nudge"] = nudge(request, why, plan)
    return result
