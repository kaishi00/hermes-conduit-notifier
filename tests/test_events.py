import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events import clarification_text, event_id, push_event


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
