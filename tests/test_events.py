import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events import approval_decision, clarification_text, event_id, is_silent_response, push_event, sanitize_decision


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
    clarify_decision = {"kind": "clarify", "request_id": "r1", "question": "Which?", "choices": ["a"]}
    approval_decision = {"kind": "approval", "session_key": "s", "description": "d", "choices": ["once"]}

    # A clarify decision can never ride an approval event (or vice versa).
    event = push_event(
        "approval.needed",
        identifier="approval:12345678",
        session_id="s",
        decision=clarify_decision,
    )
    assert "decision" not in event

    event = push_event(
        "input.needed",
        identifier="input:12345678",
        session_id="s",
        decision=approval_decision,
    )
    assert "decision" not in event
