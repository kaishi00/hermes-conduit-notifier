"""Pure event builders for Hermes lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

# Keep in sync with plugin.yaml. Reported on every event so the relay can
# expose per-gateway compatibility state to the app (Settings > Notifications).
PLUGIN_VERSION = "0.77.0"
PLUGIN_CAPABILITIES = [
    "approval-decisions",
    "clarify-loop",
    "batch-clarify-decisions",
    "version-reporting",
    "e2e-v1",
    "hermes-calls",
    "location-locate",
    "maps-v1",
    "live-activities",
    "present-v1",
]

# PROTOCOL/STORE bounds for batch clarify decisions (8 questions x 8
# choices, same text caps), mirrored by the relay. This is the maximum a
# decision may CONTAIN — it is NOT a promise that every valid batch fits an
# answerable card: APNs caps the whole notification at 4 KB, so a wide or
# long-text batch can still exceed the relay's payload guard. That case
# degrades safely (plain banner delivered, decision parked deliverable=false,
# native clarify path continues) instead of losing the notification.
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


# Hermes session keys (gateway ``build_session_key``, custom API ids up to 256
# characters) carry the platform's own ids: Matrix ``!room:server`` and
# ``@user:server``, WhatsApp ``…@g.us``, a ``main`` profile's ``agent:main~``.
# They are opaque routing strings, kept whole: one with a control character or
# over this many UTF-8 bytes is dropped, never cut (a cut key names another
# chat, or none). The relay applies the same rule (server.mjs
# SESSION_KEY_MAX_BYTES), and so do the stores the dashboard loads by path
# (calls_store, present_store, settings_store), which keep their own copy.
SESSION_KEY_MAX_BYTES = 512
_TITLE_LABEL = re.compile(r"^[a-z_]+(\.[a-z_]+){1,2}\Z")
_SESSION_KEY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def session_key(value: Any) -> str:
    """``value`` unchanged when it is a session key a push can carry, else ""."""
    if not isinstance(value, str) or not value or _SESSION_KEY_CONTROL.search(value):
        return ""
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate
        return ""
    return value if size <= SESSION_KEY_MAX_BYTES else ""


def push_event(
    kind: str,
    *,
    identifier: str,
    session_id: str = "",
    profile: str = "default",
    title: str = "",
    body: str = "",
    title_label: str = "",
    session_title: str = "",
    decision: dict[str, Any] | None = None,
    call: dict[str, Any] | None = None,
    mode: str | None = None,
    locate: dict[str, Any] | None = None,
    maps: dict[str, Any] | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": identifier,
        "type": kind,
        "plugin_version": PLUGIN_VERSION,
        "plugin_capabilities": list(PLUGIN_CAPABILITIES),
    }
    if session_key(session_id):
        event["session_id"] = session_id
    if profile:
        event["profile"] = profile[:80]
    # No push shows a host path: a MEDIA tag in a question, an approval, a
    # call, an update or a phone change reads as its attachment too. A reply
    # comes through plain_text, which already did this and kept fenced code.
    if title:
        event["title"] = _shown_clean(title, 120)
    if body:
        event["body"] = _shown_clean(body, 500)
    # Conduit writes the title in its own language (#1144): `title_label`
    # names which one (an event type, or `phone.<kind>`), and the chat's
    # title rides on its own instead of glued into `title`. `title` stays for
    # older apps and the relay.
    if isinstance(title_label, str) and _TITLE_LABEL.match(title_label):
        event["title_label"] = title_label
    # How many things a Pulse push brings, for its title ("Pulse noticed 3 things").
    if isinstance(count, int) and not isinstance(count, bool) and 0 < count <= 100 and kind == "response.ready":
        event["count"] = count
    chat_title = _clean(session_title, 120) if isinstance(session_title, str) else ""
    if chat_title:
        event["session_title"] = chat_title
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
    # `call` (#449) names the job a call request is about, so Conduit can open
    # its chat and tell the user how it ended.
    if call is not None and kind == "call.requested":
        sanitized_call = sanitize_call(call)
        if sanitized_call:
            event["call"] = sanitized_call
    # `locate` asks the phone for a fresh location (conduit_location): the
    # request id the phone answers with, and nothing else.
    if locate is not None and kind == "location.requested":
        locate_id = locate.get("id") if isinstance(locate, dict) else None
        if isinstance(locate_id, str) and _LOCATE_ID.match(locate_id):
            event["locate"] = {"id": locate_id}
    # `maps` asks the phone to answer a maps lookup (conduit_maps) through the
    # same location push: the id it fetches the sealed lookup with, nothing
    # else.
    if maps is not None and kind == "location.requested":
        maps_id = maps.get("id") if isinstance(maps, dict) else None
        if isinstance(maps_id, str) and _MAPS_ID.match(maps_id):
            event["maps"] = {"id": maps_id}
    # `mode` (phone.changes only) tells the relay how to wake the phone for a
    # change Hermes queued: "alert" shows a notification the user taps to
    # review, "background" wakes Conduit silently to apply a change the user
    # lets Hermes make without asking. Routing, not content: it stays visible
    # to the relay when the rest is sealed.
    if kind == "phone.changes":
        event["mode"] = mode if mode in PHONE_CHANGE_MODES else "alert"
    return event


PHONE_CHANGE_MODES = ("alert", "background")
_LOCATE_ID = re.compile(r"^l_[A-Za-z0-9_-]{16,64}\Z")
_MAPS_ID = re.compile(r"^m_[A-Za-z0-9_-]{16,64}\Z")


# How a watched job ended, or (an alert call) what Hermes waits on.
CALL_KINDS = ("done", "failed", "stopped", "approval", "question")
_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")


def sanitize_call(value: Any) -> dict[str, Any] | None:
    """A call request's job, bounded: the watch id, how the job ended (or
    what Hermes waits on), its title, why Hermes calls and its session ids
    (runtime and stored). None when malformed."""
    if not isinstance(value, dict):
        return None
    call_id = value.get("id")
    kind = value.get("kind")
    raw_ids = value.get("session_ids")
    if not isinstance(call_id, str) or not _CALL_ID.match(call_id) or kind not in CALL_KINDS:
        return None
    if not isinstance(raw_ids, list):
        return None
    session_ids: list[str] = []
    for item in raw_ids:
        if not session_key(item):
            return None
        if item not in session_ids:
            session_ids.append(item)
    if not session_ids:
        return None
    call: dict[str, Any] = {"id": call_id, "kind": kind, "session_ids": session_ids[:4]}
    if value.get("origin") == "alert":
        # Hermes called on its own about a failed turn (alert calls).
        call["origin"] = "alert"
    title = _shown_clean(value.get("title") if isinstance(value.get("title"), str) else "", 120)
    if title:
        call["title"] = title
    reason = _shown_clean(value.get("reason") if isinstance(value.get("reason"), str) else "", 200)
    if reason:
        call["reason"] = reason
    return call


REDACTED_APPROVAL_TEXT = "Hermes needs your approval. Open Conduit for details."
REDACTED_QUESTION_TEXT = "Hermes needs your answer. Open Conduit for details."


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Strip chat content from an outgoing event, keeping answer routing.

    For profiles that opt into `hermes conduit-push redact on` (#192): no
    title/body text leaves the gateway, and a decision keeps only the
    structural fields the device needs to answer it (kind, session_key or
    request_id, qids, choices, multi_select), with its display text replaced
    by generic copy. Choice labels are kept because the answer is one of
    them; an approval's are filtered to the gateway's once/session/always/deny
    vocabulary. Returns a new
    dict; the input is not mutated.
    """
    # Fail closed: both the event and its decision are rebuilt from explicit
    # allowlists, so an unknown or text-bearing key (a raw `command`, say)
    # can never ride along with a redacted event.
    # The decision is first run through the same sanitizer push_event uses
    # (qid charset/reserved names, bounds), so a raw send_now caller gets the
    # same guarantees as the in-repo builders.
    redacted = {key: event[key] for key in _REDACTED_EVENT_KEYS if key in event}
    if event.get("type") == "phone.changes" and event.get("mode") in PHONE_CHANGE_MODES:
        # How the relay wakes the phone: routing, not content.
        redacted["mode"] = event["mode"]
    label = event.get("title_label")
    if isinstance(label, str) and _TITLE_LABEL.match(label):
        # Which title the phone writes: not content, so it stays.
        redacted["title_label"] = label
    count = event.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and 0 < count <= 100:
        # How many things a Pulse report noticed, for that title: a number, not content.
        redacted["count"] = count
    decision = sanitize_decision(event.get("decision")) if event.get("decision") is not None else {}
    if decision.get("kind") == "approval":
        # Unknown labels never transit; fall back to the always-valid subset
        # (see approval_decision) so the card stays answerable.
        choices = [choice for choice in decision.get("choices", []) if choice in _APPROVAL_CHOICES]
        redacted["decision"] = {
            "kind": "approval",
            "description": REDACTED_APPROVAL_TEXT,
            "session_key": decision["session_key"],
            "choices": choices or ["once", "deny"],
        }
    elif decision.get("kind") == "clarify":
        clarify: dict[str, Any] = {
            "kind": "clarify",
            "question": REDACTED_QUESTION_TEXT,
            **{key: decision[key] for key in ("request_id", "choices") if key in decision},
        }
        questions = decision.get("questions")
        if isinstance(questions, list):
            entries = [entry for entry in questions if isinstance(entry, dict)]
            clarify["questions"] = [
                {
                    "question": f"Question {index + 1}",
                    **{key: entry[key] for key in ("qid", "choices", "multi_select") if key in entry},
                }
                for index, entry in enumerate(entries)
            ]
        redacted["decision"] = clarify
    call = sanitize_call(event.get("call")) if event.get("type") == "call.requested" else None
    if call:
        # The job's title and the reason are chat content; its id, outcome
        # and sessions route.
        redacted["call"] = {key: call[key] for key in ("id", "kind", "session_ids", "origin") if key in call}
    locate = event.get("locate") if event.get("type") == "location.requested" else None
    locate_id = locate.get("id") if isinstance(locate, dict) else None
    if isinstance(locate_id, str) and _LOCATE_ID.match(locate_id):
        # Only the request id the phone answers with: routing, not content.
        redacted["locate"] = {"id": locate_id}
    maps = event.get("maps") if event.get("type") == "location.requested" else None
    maps_id = maps.get("id") if isinstance(maps, dict) else None
    if isinstance(maps_id, str) and _MAPS_ID.match(maps_id):
        redacted["maps"] = {"id": maps_id}
    return redacted


# The gateway's approval vocabulary (the relay whitelists the same set).
_APPROVAL_CHOICES = ("once", "session", "always", "deny")
_REDACTED_EVENT_KEYS = ("event_id", "type", "plugin_version", "plugin_capabilities", "session_id", "profile", "gateway")


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
        # The session key routes the answer back (approval.respond): kept
        # whole, never whitespace-collapsed or cut.
        key = session_key(decision.get("session_key"))
        if key:
            sanitized["session_key"] = key
        description = _shown_clean(decision.get("description", ""), 500)
        if description:
            sanitized["description"] = description
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
        request_id = _clean(decision.get("request_id", ""), 500)
        if request_id:
            sanitized["request_id"] = request_id
        question_text = _shown_clean(decision.get("question", ""), 500)
        if question_text:
            sanitized["question"] = question_text
        choices = decision.get("choices")
        if isinstance(choices, list):
            # Shown as card_choice_labels reads them, so a tapped label maps back.
            cleaned = [value for value in (_shown_clean(choice, 80) for choice in choices) if value]
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
                text = _shown_clean(entry.get("question", ""), 500)
                if not text:
                    continue
                question: dict[str, Any] = {"qid": qid, "question": text}
                labels = entry.get("choices")
                if isinstance(labels, list):
                    cleaned_labels: list[str] = []
                    for label in labels[:MAX_BATCH_CHOICES]:
                        cleaned = _shown_clean(label, 80)
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


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE = re.compile(r"^\s{0,3}>\s?")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?:\[[ xX]\]\s+)?")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*\Z")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*\Z")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])")
_UNDERSCORE_EMPHASIS = re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


# Hermes' MEDIA tag as its gateway reads it (extract_media): a quoted path,
# a path with spaces up to a file extension artifacts_index knows, or a path
# without spaces. Only uppercase MEDIA: followed by a path counts, so "social
# media: report" stays prose.
# artifacts_index's file extensions and trailing punctuation, repeated so
# events stays import-free (tests/test_turn_end_pushes.py keeps them equal).
_WRITTEN = frozenset(
    "png jpg jpeg gif webp heic heif bmp svg tif tiff mp4 mov m4v webm mkv avi mp3 m4a wav ogg opus flac aac "
    "caf html htm xhtml pdf txt md markdown csv tsv json xml yaml yml rtf doc docx xls xlsx ppt pptx key "
    "numbers pages odt ods odp epub ics vcf gpx kml kmz zip tar gz tgz 7z".split())
_TRAILING = ".,;:!?)]}*_"
_PATH_START = r"(?:~/|/|[A-Za-z]:[/\\])"
_PATH_WORD = r"[^\s`\"'<>]"
_PATH_EXTENSION = (r"\.(?i:" + "|".join(sorted(_WRITTEN, key=len, reverse=True))
                   + r")(?=[\s`\"',;:!?)\]}*_~]|$)")
_MEDIA_TAG = re.compile(
    r"""[`"']?MEDIA:[^\S\n]*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'"""
    # A path without spaces up to its extension, then one with spaces, then
    # one without an extension: its words, and any next words that are
    # still folders (they hold a slash).
    + "|" + _PATH_START + _PATH_WORD + "*?" + _PATH_EXTENSION
    + "|" + _PATH_START + r"\S+(?:[^\S\n]+\S+)*?" + _PATH_EXTENSION
    + "|" + _PATH_START + _PATH_WORD + r"+(?:[^\S\n]+" + _PATH_WORD + "*/" + _PATH_WORD + "*)*"
    + r""")[`"']?""")
_MEDIA_DIRECTIVE = re.compile(r"\[\[(?:audio_as_voice|as_document)\]\]")
# Hermes' caches name a file img_<12 hex>.png (images, audio) or
# doc_<12 hex>_<original name> (documents).
_CACHE_NAME = re.compile(r"^(?:img|audio|video|doc)_[0-9a-f]{12}(?:_(?P<name>.+))?$", re.IGNORECASE)
_HASH_NAME = re.compile(r"^[0-9a-f-]{16,}$", re.IGNORECASE)


def _attachment(match: re.Match) -> str:
    """A MEDIA tag as a push shows it: a paperclip and the file's name, or
    its type when the name is only a hash, never the host's path."""
    quoted = match.group("path").strip()
    path = quoted.strip("`\"'").rstrip(_TRAILING + "~")
    # Punctuation or emphasis after a bare path is the sentence's, not the file's.
    tail = "" if quoted[0] in "`\"'" else quoted[len(path):]
    name = re.split(r"[/\\]", path.rstrip("/\\"))[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    cached = _CACHE_NAME.match(name) or _CACHE_NAME.match(stem)
    if cached:
        name = cached.group("name") or ""
    if not name or (not cached and _HASH_NAME.match(stem)):
        name = ext.upper()
    return f"📎 {name}".rstrip() + tail


def media_free(value: str) -> str:
    """One line with each MEDIA tag shown as its attachment, and Hermes'
    delivery directives gone."""
    return _MEDIA_TAG.sub(_attachment, _MEDIA_DIRECTIVE.sub("", value))


class _Plain(str):
    """Text plain_text made: its MEDIA tags are read, fenced code kept."""


def _shown(value: Any) -> Any:
    """``value`` with its MEDIA tags read, unless plain_text already did."""
    if not isinstance(value, str) or isinstance(value, _Plain):
        return value
    lines, fenced = [], False
    for line in value.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        lines.append(line if fenced or _FENCE.match(line) else media_free(line))
    return "\n".join(lines)


def plain_text(value: Any) -> str:
    """A Markdown reply as a notification shows it: the words without the
    markup (fences, emphasis, headings, link targets, table rules), and a
    file Hermes attached as its name, not its path. Code inside a fence
    stays, it is the content."""
    if not isinstance(value, str):
        return ""
    lines: list[str] = []
    fenced = False
    for line in value.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            lines.append(line)
            continue
        line = media_free(_QUOTE.sub("", line))
        if _RULE.match(line) or _TABLE_RULE.match(line):
            continue
        line = _HEADING.sub("", line)
        line = _BULLET.sub(r"\1• ", line)
        if line.lstrip().startswith("|"):
            line = " · ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())
        line = _LINK.sub(r"\1", _IMAGE.sub(r"\1", line))
        line = _STRONG.sub(r"\2", line)
        line = _STRIKE.sub(r"\1", _UNDERSCORE_EMPHASIS.sub(r"\1", _EMPHASIS.sub(r"\1", line)))
        lines.append(line.replace("`", ""))
    return _Plain("\n".join(lines).strip())


def card_choice_labels(choice: Any) -> set[str]:
    """The labels a clarify card can show for ``choice``: this plugin's cut
    (``sanitize_decision``: whitespace collapsed, 80 code points) and the
    relay's on a plaintext pairing (``cleanText``: 80 UTF-16 units), so an
    answer made from a cut label can be mapped back to the full choice."""
    label = _shown_clean(choice, 80)
    if not label:
        return set()
    units = label.encode("utf-16-le", "surrogatepass")[:160]
    return {label, units.decode("utf-16-le", "surrogatepass").strip()}


def _shown_clean(value: Any, maximum: int) -> str:
    """``_clean`` for text a card or the call screen shows: MEDIA tags read
    as their attachments first."""
    return _clean(_shown(value), maximum)


def _clean(value: str, maximum: int) -> str:
    # Non-strings (None, numbers, nested objects) must not be coerced —
    # str(None) would produce a truthy "None" that slips past emptiness
    # checks and gets forwarded to the relay. Drop them instead, matching
    # the relay's cleanText semantics.
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]
