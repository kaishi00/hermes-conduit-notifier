import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events import clarification_text, event_id, is_silent_response, push_event


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
