import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events import REDACTED_APPROVAL_TEXT, REDACTED_QUESTION_TEXT, approval_decision, clarify_decision, clarification_text, event_id, is_silent_response, normalize_clarify_questions, push_event, redact_event, sanitize_call, sanitize_decision


def test_event_identifiers_are_stable_for_replayed_hooks():
    assert event_id("response", "turn-1", "session-1") == event_id("response", "turn-1", "session-1")
    assert event_id("response", "turn-1", "session-1") != event_id("response", "turn-2", "session-1")


def test_push_event_has_bounded_preview_and_session_context():
    event = push_event(
        "response.ready",
        identifier="response:12345678",
        session_id="session-1",
        profile="Furina",
        body="x" * 700,
    )
    assert event["session_id"] == "session-1"
    assert event["profile"] == "Furina"
    assert len(event["body"]) == 500


def test_clarification_text_accepts_single_and_multiple_question_shapes():
    assert clarification_text({"question": "Choose one"}) == "Choose one"
    assert clarification_text({"questions": [{"question": "First question"}]}) == "First question"


def test_silent_response_matches_only_the_exact_sentinel():
    assert is_silent_response("[Silent]")
    assert is_silent_response("  [silent]\n")
    assert not is_silent_response("Response: [Silent]")
    assert not is_silent_response("")
    assert not is_silent_response(None)


def test_push_event_attaches_sanitized_approval_decision():
    event = push_event(
        "approval.needed",
        identifier="approval:12345678",
        session_id="sess-1",
        profile="default",
        body="Hermes is waiting for your approval.",
        decision=approval_decision(session_key="sess-1", description="Run a dangerous shell command"),
    )
    decision = event["decision"]
    assert decision["kind"] == "approval"
    assert decision["session_key"] == "sess-1"
    assert decision["description"] == "Run a dangerous shell command"
    # once/deny are always-valid server-side; raw command is intentionally absent
    assert decision["choices"] == ["once", "deny"]
    assert "command" not in decision


def test_push_event_omits_decision_when_session_key_missing():
    event = push_event(
        "approval.needed",
        identifier="approval:12345678",
        session_id="",
        decision=approval_decision(session_key="", description="something"),
    )
    # No session_key → not answerable → degrade to the plain routing stub.
    assert "decision" not in event


def test_push_event_omits_decision_for_non_decision_events():
    event = push_event(
        "response.ready",
        identifier="response:12345678",
        session_id="sess-1",
        decision={"kind": "approval", "description": "x", "session_key": "sess-1"},
    )
    assert "decision" not in event


def test_sanitize_decision_rejects_unknown_kind_and_oversized_fields():
    assert sanitize_decision({"kind": "sudo", "description": "x"}) == {}
    decision = sanitize_decision({
        "kind": "approval",
        "description": "d" * 900,
        "session_key": "k",
        "choices": ["once", "deny", "a" * 200, "", "b"],
        "command": "secret",
    })
    assert decision["kind"] == "approval"
    assert len(decision["description"]) == 500
    assert len(decision["choices"]) == 4  # empties dropped, all retained within cap
    assert all(len(c) <= 80 for c in decision["choices"])
    assert "command" not in decision  # unknown fields are not echoed


def test_sanitize_decision_does_not_coerce_none_into_strings():
    # A None session_key must be rejected (not become the truthy "None"), and
    # a None choice entry must be dropped rather than stringified.
    assert sanitize_decision({"kind": "approval", "session_key": None, "description": "x"}) == {}
    decision = sanitize_decision({
        "kind": "approval",
        "session_key": "s",
        "description": "x",
        "choices": ["once", None, "deny"],
    })
    assert decision["choices"] == ["once", "deny"]


def test_push_event_binds_decision_kind_to_event_type():
    # Approval rides approval.needed; clarify (relay-loop, plugin-minted id)
    # rides input.needed. Cross-pairings are dropped so the two contracts can
    # never disagree.
    clarify_payload = {"kind": "clarify", "request_id": "conduit-push-r1", "question": "Which?", "choices": ["a"]}
    approval_payload = {"kind": "approval", "session_key": "s", "description": "d", "choices": ["once"]}

    event = push_event(
        "approval.needed",
        identifier="approval:12345678",
        session_id="s",
        decision=clarify_payload,
    )
    assert "decision" not in event

    event = push_event(
        "input.needed",
        identifier="input:12345678",
        session_id="s",
        decision=approval_payload,
    )
    assert "decision" not in event


def test_push_event_attaches_clarify_decision_on_input_events():
    from events import clarify_decision as build_clarify_decision

    decision = build_clarify_decision(request_id="conduit-push-abc", question="Which?", choices=["Red", "Blue"])
    event = push_event(
        "input.needed",
        identifier="input:12345678",
        session_id="s",
        decision=decision,
    )
    assert event["decision"] == {
        "kind": "clarify",
        "request_id": "conduit-push-abc",
        "question": "Which?",
        "choices": ["Red", "Blue"],
    }

    # Open-ended clarifies (no choices) are valid too.
    open_ended = build_clarify_decision(request_id="conduit-push-def", question="What next?")
    event = push_event("input.needed", identifier="input:12345678", session_id="s", decision=open_ended)
    assert event["decision"]["question"] == "What next?"
    assert "choices" not in event["decision"]

    # The sanitizer requires the answerable plugin id.
    assert sanitize_decision({"kind": "clarify", "question": "Which?"}) == {}


def test_flatten_choice_labels_unwraps_llm_dict_shapes():
    from events import flatten_choice_labels

    assert flatten_choice_labels(["Ship it", {"label": "Hold"}, {"description": "Ask"}, {"unknown": "x"}, ""]) == [
        "Ship it",
        "Hold",
        "Ask",
    ]
    assert flatten_choice_labels("not-a-list") == []


def test_events_report_plugin_version_and_capabilities():
    from events import PLUGIN_CAPABILITIES, PLUGIN_VERSION

    event = push_event("response.ready", identifier="response:12345678", session_id="s")
    assert event["plugin_version"] == PLUGIN_VERSION
    assert event["plugin_capabilities"] == PLUGIN_CAPABILITIES
    assert "approval-decisions" in event["plugin_capabilities"]
    assert "clarify-loop" in event["plugin_capabilities"]


def test_plugin_hello_is_a_non_notifying_version_announcement():
    from events import PLUGIN_VERSION, plugin_hello

    hello = plugin_hello()
    assert hello["type"] == "plugin.hello"
    assert hello["plugin_version"] == PLUGIN_VERSION
    # Stable id per version: replays dedupe, a version bump re-announces.
    assert plugin_hello() == hello


def test_plugin_version_constant_matches_manifest():
    # PLUGIN_VERSION misreporting compatibility is the failure this feature
    # exists to prevent; keep the constant and the manifest in lockstep.
    import pathlib
    import re

    from events import PLUGIN_VERSION

    manifest = (pathlib.Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    match = re.search(r"^version:\s*(\S+)", manifest, re.MULTILINE)
    assert match, "plugin.yaml must declare a version"
    assert match.group(1) == PLUGIN_VERSION


def test_sanitize_decision_requires_choices_and_mirrors_relay_contract():
    # The relay enforces session_key + description + non-empty whitelisted
    # choices; mirror that here so plugin-side tests describe the real contract.
    assert sanitize_decision({"kind": "approval", "session_key": "s", "description": "d"}) == {}
    assert sanitize_decision({"kind": "approval", "session_key": "s", "description": "d", "choices": ["  "]}) == {}


def test_normalize_clarify_questions_preserves_the_full_batch():
    batch = normalize_clarify_questions({
        "questions": [
            {"question": "Which environment?", "choices": ["staging", "prod"]},
            {"question": "Which tests?", "choices": [{"label": "unit"}, "ui"], "multi_select": True},
            "Just one word?",
        ]
    })
    assert [(entry["qid"], entry["question"]) for entry in batch] == [
        ("q0", "Which environment?"),
        ("q1", "Which tests?"),
        ("q2", "Just one word?"),
    ]
    assert batch[0]["choices"] == ["staging", "prod"]
    assert batch[1]["multi_select"] is True
    assert batch[1]["choices"] == ["unit", "ui"], "dict-shaped choices flatten to labels"
    assert batch[2]["multi_select"] is False


def test_normalize_clarify_questions_scalar_becomes_one_question_batch():
    batch = normalize_clarify_questions({
        "question": "Deploy now?",
        "choices": ["Ship it"],
        "multi_select": True,
    })
    assert len(batch) == 1
    assert batch[0] == {
        "qid": "q0",
        "id": None,
        "question": "Deploy now?",
        "choices": ["Ship it"],
        "multi_select": True,
    }
    # multi_select without choices degrades like the gateway does.
    assert normalize_clarify_questions({"question": "Notes?", "multi_select": True})[0]["multi_select"] is False


def test_normalize_clarify_questions_unparseable_yields_no_question():
    assert normalize_clarify_questions({}) == []
    assert normalize_clarify_questions({"questions": [{"no_question": True}]}) == []
    assert normalize_clarify_questions(None) == []


def test_clarify_decision_carries_batch_and_collapsed_summary():
    decision = clarify_decision(
        request_id="conduit-push-x",
        question="Which environment?",
        choices=["staging", "prod"],
        questions=[
            {"qid": "q0", "question": "Which environment?", "choices": ["staging", "prod"], "multi_select": False},
            {"qid": "q1", "question": "Notes?", "choices": [], "multi_select": False},
        ],
    )
    assert decision["kind"] == "clarify"
    assert decision["question"] == "Which environment?", "collapsed copy for pre-batch devices"
    assert [q["qid"] for q in decision["questions"]] == ["q0", "q1"]


def test_sanitize_decision_bounds_batch_questions():
    sanitized = sanitize_decision({
        "kind": "clarify",
        "request_id": "conduit-push-x",
        "question": "summary",
        "questions": [
            {"qid": "q0", "question": "x" * 900, "choices": [f"choice-{i}-" + "y" * 70 for i in range(20)], "multi_select": True},
            {"question": "no qid"},
            {"qid": "q1", "question": "kept", "multi_select": "truthy-coerced"},
        ],
    })
    assert sanitized["request_id"] == "conduit-push-x"
    assert len(sanitized["questions"]) == 2
    first = sanitized["questions"][0]
    assert first["question"] == "x" * 500
    assert len(first["choices"]) == 8, "choice count is bounded like the relay"
    assert len(set(first["choices"])) == 8
    assert first["multi_select"] is True
    assert sanitized["questions"][1]["qid"] == "q1"
    assert "multi_select" not in sanitized["questions"][1]


def test_sanitize_decision_qid_contract_mirrors_the_relay():
    # Same accepted charset, same reserved-name rejection, same
    # first-qid-wins dedup as the relay's sanitizeBatchQuestions: a batch
    # this plugin emits must never be silently shrunk by the relay.
    sanitized = sanitize_decision({
        "kind": "clarify",
        "request_id": "conduit-push-sym",
        "question": "summary",
        "questions": [
            {"qid": "q0", "question": "Kept", "choices": ["a", "a", "b"]},
            {"qid": "q0", "question": "Duplicate qid dropped"},
            {"qid": "__proto__", "question": "Reserved dropped"},
            {"qid": "constructor", "question": "Reserved dropped too"},
            {"qid": "bad charset!", "question": "Charset dropped"},
            {"qid": "q1", "question": "Also kept", "choices": ["x", "x"]},
        ],
    })
    assert [(question["qid"], question["question"]) for question in sanitized["questions"]] == [
        ("q0", "Kept"),
        ("q1", "Also kept"),
    ]
    assert sanitized["questions"][0]["choices"] == ["a", "b"], "duplicate choice values collapse"
    assert sanitized["questions"][1]["choices"] == ["x"]


def test_normalize_questions_locally_matches_upstream_raw_position_minting():
    # The local mirror follows the upstream normalizer exactly: qids come
    # from the RAW enumerate position and a malformed entry rejects the
    # whole batch (upstream returns an error, it never skips).
    batch = normalize_clarify_questions({
        "questions": [
            "Bare string question",
            {"question": "Second", "choices": ["a"]},
        ]
    })
    assert [entry["qid"] for entry in batch] == ["q0", "q1"]
    assert batch[0]["question"] == "Bare string question"


def test_normalize_questions_locally_rejects_whole_batch_on_malformed_entry():
    # A leading malformed entry mirrors upstream's whole-batch error — the
    # valid second question must NOT be renumbered onto the malformed slot.
    batch = normalize_clarify_questions({
        "questions": [
            {"no_question": True},
            {"question": "Valid second"},
        ]
    })
    assert batch == [], "upstream rejects the batch; the mirror must not skip-and-renumber"


def test_redact_event_strips_text_but_keeps_approval_routing():
    event = push_event(
        "approval.needed",
        identifier="approval:12345678",
        session_id="sess-1",
        title="Run rm -rf build",
        body="Run rm -rf build",
        decision=approval_decision(session_key="sess-1", description="Run rm -rf build"),
    )
    redacted = redact_event(event)
    assert "title" not in redacted and "body" not in redacted
    assert redacted["decision"] == {
        "kind": "approval",
        "session_key": "sess-1",
        "description": REDACTED_APPROVAL_TEXT,
        "choices": ["once", "deny"],
    }
    assert redacted["session_id"] == "sess-1"
    # The original event is untouched.
    assert event["decision"]["description"] == "Run rm -rf build"
    assert event["body"] == "Run rm -rf build"
    # The redacted decision still passes the sanitizer the relay mirrors.
    assert sanitize_decision(redacted["decision"]) == redacted["decision"]


def test_redact_event_keeps_clarify_qids_and_choices_but_not_question_text():
    decision = clarify_decision(
        request_id="conduit-push-abc123",
        question="Deploy to prod?",
        choices=["Yes", "No"],
        questions=[
            {"qid": "q1", "question": "Deploy to prod?", "choices": ["Yes", "No"]},
            {"qid": "q2", "question": "Which region?", "choices": ["eu", "us"], "multi_select": True},
        ],
    )
    event = push_event("input.needed", identifier="input:12345678", body="Deploy to prod?", decision=decision)
    redacted = redact_event(event)["decision"]
    assert redacted["request_id"] == "conduit-push-abc123"
    assert redacted["question"] == REDACTED_QUESTION_TEXT
    assert redacted["choices"] == ["Yes", "No"]
    assert [q["qid"] for q in redacted["questions"]] == ["q1", "q2"]
    assert [q["question"] for q in redacted["questions"]] == ["Question 1", "Question 2"]
    assert redacted["questions"][1]["multi_select"] is True
    assert "Deploy to prod?" not in json.dumps(redact_event(event))
    assert sanitize_decision(redacted) == redacted


def test_redact_event_fails_closed_on_unexpected_shapes():
    assert "decision" not in redact_event({"type": "approval.needed", "decision": "Run rm -rf build"})
    assert "decision" not in redact_event({"type": "input.needed", "decision": {"kind": "other", "question": "secret"}})
    redacted = redact_event({
        "type": "input.needed",
        "decision": {"kind": "clarify", "request_id": "r", "question": "secret", "questions": ["secret", {"qid": "q1", "question": "secret"}]},
    })
    assert "secret" not in json.dumps(redacted)
    assert redacted["decision"]["questions"] == [{"qid": "q1", "question": "Question 1"}]
    # A clarify without its routing request id is not answerable: dropped whole.
    assert "decision" not in redact_event({"decision": {"kind": "clarify", "question": "secret", "questions": "secret"}})
    assert "questions" not in redact_event({"decision": {"kind": "clarify", "request_id": "r", "question": "secret", "questions": "secret"}})["decision"]


def test_redact_event_drops_unknown_keys_at_every_level():
    redacted = redact_event({
        "event_id": "approval:1",
        "type": "approval.needed",
        "extra": "secret",
        "decision": {"kind": "approval", "session_key": "s", "description": "x", "command": "rm -rf / secret", "choices": ["once", "deny"]},
    })
    assert "secret" not in json.dumps(redacted)
    assert redacted["decision"] == {"kind": "approval", "description": REDACTED_APPROVAL_TEXT, "session_key": "s", "choices": ["once", "deny"]}
    clarify = redact_event({"decision": {"kind": "clarify", "request_id": "r", "question": "q", "questions": [{"qid": "q1", "question": "q", "note": "secret"}]}})
    assert "secret" not in json.dumps(clarify)


def test_redact_event_applies_sanitizer_and_approval_vocabulary():
    approval = redact_event({"decision": {"kind": "approval", "session_key": "s", "description": "x", "choices": ["Run rm -rf / secret", "deny"]}})
    assert approval["decision"]["choices"] == ["deny"]
    fallback = redact_event({"decision": {"kind": "approval", "session_key": "s", "description": "x", "choices": ["secret"]}})
    assert fallback["decision"]["choices"] == ["once", "deny"]
    assert "secret" not in json.dumps(approval)
    clarify = redact_event({"decision": {"kind": "clarify", "request_id": "r", "question": "q", "questions": [
        {"qid": "__proto__", "question": "q", "choices": ["a"]},
        {"qid": "q1", "question": "q", "choices": ["a"]},
    ]}})
    assert [q["qid"] for q in clarify["decision"]["questions"]] == ["q1"]


CALL = {"id": "0123456789abcdef01234567", "kind": "done", "title": "Check the server", "session_ids": ["rt-1", "st-1"]}


def test_call_request_carries_a_sanitized_call():
    event = push_event(
        "call.requested",
        identifier="call:0123456789abcdef01234567",
        session_id="rt-1",
        profile="default",
        title="Hermes wants to talk",
        body="Check the server finished.",
        call={**CALL, "title": "Check\nthe server " + "x" * 300, "extra": "dropped"},
    )
    assert event["call"]["id"] == CALL["id"]
    assert event["call"]["kind"] == "done"
    assert event["call"]["session_ids"] == ["rt-1", "st-1"]
    assert len(event["call"]["title"]) == 120
    assert "extra" not in event["call"]


def test_call_is_only_attached_to_call_requests():
    event = push_event("response.ready", identifier="response:12345678", call=CALL)
    assert "call" not in event


@pytest.mark.parametrize("call", [
    {**CALL, "id": "short"},
    {**CALL, "id": "has space in it here"},
    {**CALL, "kind": "maybe"},
    {**CALL, "session_ids": ["bad id"]},
    {**CALL, "session_ids": []},
    "not a dict",
])
def test_a_malformed_call_is_dropped(call):
    assert sanitize_call(call) is None


def test_call_sessions_are_bounded():
    assert sanitize_call({**CALL, "session_ids": ["a", "b", "c", "d", "e"]})["session_ids"] == ["a", "b", "c", "d"]


def test_redact_event_keeps_only_the_call_routing():
    event = push_event(
        "call.requested",
        identifier="call:0123456789abcdef01234567",
        session_id="rt-1",
        title="Hermes wants to talk",
        body="Check the server finished.",
        call=CALL,
    )
    redacted = redact_event(event)
    assert redacted["call"] == {"id": CALL["id"], "kind": "done", "session_ids": ["rt-1", "st-1"]}
    assert "title" not in redacted
    assert "body" not in redacted


def test_a_call_carries_why_hermes_calls_and_redaction_drops_it():
    event = push_event(
        "call.requested",
        identifier="call:0123456789abcdef01234567",
        session_id="sk-1",
        title="Hermes wants to talk",
        body="Hermes needs your OK: Run the migration",
        call={**CALL, "kind": "approval", "reason": "Run\nthe migration " + "x" * 300},
    )
    assert event["call"]["kind"] == "approval"
    assert event["call"]["reason"].startswith("Run the migration ") and len(event["call"]["reason"]) == 200
    assert sanitize_call({**CALL, "kind": "question"})["kind"] == "question"
    assert sanitize_call({**CALL, "reason": 5}).get("reason") is None
    assert "reason" not in redact_event(event)["call"]
