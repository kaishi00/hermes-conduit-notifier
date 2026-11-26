"""Spoken answers from a saved call: the words a live call heard, submitted to
the question Hermes still waits on — the iPhone has no way to send them while
the call goes on, so they only arrive once the call has ended (the Watch
answers live instead, through answer_call_waiting)."""

import importlib.util
import pathlib
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_answers", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()

# The call that ended the "Bloquer accès internet TV Hisense" evening: the
# model read the first question (naming its choices), the user answered it
# across two turns, and later words were a complaint about the answer never
# arriving — none of which the second question, never asked, should see.
FAILED_CALL = [
    {"role": "assistant", "content": "Hermes a une question sur la TV : tu vois quoi comme système, VIDAA ou Google TV ?",
     "timestamp": 1.0},
    {"role": "user", "content": "Fire TV Home Version c'est le 72 601 84.1", "timestamp": 2.0},
    {"role": "user", "content": "software version Fire OS 8.1.6.3 RS8163-3414", "timestamp": 3.0},
    {"role": "assistant", "content": "Parfait, merci. Hermes est en pause tant que tu n'as pas répondu.",
     "timestamp": 4.0},
    {"role": "user", "content": "Je viens de te donner la réponse, tu l'as pas passée la réponse", "timestamp": 5.0},
    {"role": "assistant", "content": "Tu as raison, je m'en occupe et je le relance.", "timestamp": 6.0},
]

HISENSE = [
    {"qid": "os", "question": "Quel système tourne sur la TV ?",
     "choices": ["VIDAA", "Google TV", "Autre"], "multi_select": False},
    {"qid": "dev", "question": "Comment as-tu activé le mode développeur ?", "choices": [], "multi_select": False},
]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def store(home):
    calls = api.hermes_calls._calls_store().CallStore(api.core._calls_home())
    calls.update_settings({"enabled": True, "alerts": True})
    return calls


def _waiting(store, questions=HISENSE, session_id="chat-1", request_id="conduit-push-req"):
    assert store.note_waiting(session_id, "question", request_id=request_id, questions=questions)


def _answers(store):
    """The user's spoken words as Hermes takes them, once every question has
    one; None while any question keeps its card."""
    return store.take_answers("conduit-push-req")


def test_a_calls_spoken_answers_reach_the_waiting_question(store):
    _waiting(store, questions=[HISENSE[0]])
    assert api.voice_calls._voice_spoken_answers("chat-1", FAILED_CALL) == 1
    assert _answers(store) == {"os": "Fire TV Home Version c'est le 72 601 84.1 "
                                    "software version Fire OS 8.1.6.3 RS8163-3414"}
    assert store.waiting_for("chat-1") is None, "taken once, as a card's answers are"


def test_a_question_the_call_never_asked_keeps_its_card(store):
    _waiting(store)
    assert api.voice_calls._voice_spoken_answers("chat-1", FAILED_CALL) == 1
    waiting = store.waiting_for("chat-1")
    assert waiting["questions"][0]["answered"] is True
    assert waiting["questions"][1]["answered"] is False
    assert _answers(store) is None, "the second question still wants its card"


def test_a_choice_named_aloud_answers_like_the_card_would(store):
    _waiting(store, questions=[HISENSE[0]])
    call = [{"role": "assistant", "content": "Tu vois VIDAA ou Google TV ?", "timestamp": 1.0},
            {"role": "user", "content": "C'est Google TV", "timestamp": 2.0}]
    assert api.voice_calls._voice_spoken_answers("chat-1", call) == 1
    assert _answers(store) == {"os": "Google TV"}


def test_questions_asked_one_at_a_time_each_take_their_own_words(store):
    _waiting(store)
    call = [
        {"role": "assistant", "content": "Tu vois quoi comme système, VIDAA ou Google TV ?", "timestamp": 1.0},
        {"role": "user", "content": "C'est un Fire TV", "timestamp": 2.0},
        {"role": "assistant", "content": "Et comment as-tu activé le mode développeur ?", "timestamp": 3.0},
        {"role": "user", "content": "En appuyant sept fois sur Build", "timestamp": 4.0},
    ]
    assert api.voice_calls._voice_spoken_answers("chat-1", call) == 2
    assert _answers(store) == {"os": "C'est un Fire TV", "dev": "En appuyant sept fois sur Build"}


def test_several_questions_read_in_one_turn_leave_the_rest_to_the_card(store):
    _waiting(store)
    call = [
        {"role": "assistant", "content": "Quel système, VIDAA ou Google TV ? Et comment as-tu activé le mode développeur ?",
         "timestamp": 1.0},
        {"role": "user", "content": "C'est un Fire TV", "timestamp": 2.0},
        {"role": "user", "content": "via le menu Build", "timestamp": 3.0},
    ]
    assert api.voice_calls._voice_spoken_answers("chat-1", call) == 1
    waiting = store.waiting_for("chat-1")
    assert waiting["questions"][0]["answered"] is True
    assert waiting["questions"][1]["answered"] is False


def test_words_that_only_greet_or_ask_stay_out(store):
    _waiting(store, questions=[HISENSE[0]])
    call = [
        {"role": "assistant", "content": "Tu vois quoi comme système, VIDAA ou Google TV ?", "timestamp": 1.0},
        {"role": "user", "content": "Allo ?", "timestamp": 2.0},
        {"role": "user", "content": "c'est quoi le numéro exact ?", "timestamp": 3.0},
        {"role": "user", "content": "C'est un Fire TV, version 72 601 84.1", "timestamp": 4.0},
    ]
    assert api.voice_calls._voice_spoken_answers("chat-1", call) == 1
    assert _answers(store) == {"os": "C'est un Fire TV, version 72 601 84.1"}


def test_a_call_that_never_asked_answers_nothing(store):
    _waiting(store)
    call = [{"role": "assistant", "content": "Salut ! Comment ça va aujourd'hui ?", "timestamp": 1.0},
            {"role": "user", "content": "Ça va bien, VIDAA", "timestamp": 2.0}]
    assert api.voice_calls._voice_spoken_answers("chat-1", call) == 0
    assert _answers(store) is None


def test_an_approval_keeps_its_card(store):
    store.note_waiting("chat-1", "approval", request_id="conduit-push-apr", description="rm -rf", command="rm -rf")
    assert api.voice_calls._voice_spoken_answers("chat-1", FAILED_CALL) == 0
    assert store.waiting_for("chat-1")["kind"] == "approval"


def test_a_card_that_answered_first_leaves_nothing_to_take(store):
    _waiting(store, questions=[HISENSE[0]])
    # The clarify loop's finally, once the card answered: the wait is dropped
    # by session, as the relay's answer having won would leave it.
    assert store.forget_waiting("chat-1") == 1
    assert api.voice_calls._voice_spoken_answers("chat-1", FAILED_CALL) == 0


def test_nothing_waiting_is_a_quiet_read(store):
    assert api.voice_calls._voice_spoken_answers("chat-1", FAILED_CALL) == 0


def test_a_call_about_another_chat_finds_nothing_to_answer(store):
    _waiting(store, questions=[HISENSE[0]], session_id="chat-2")
    assert api.voice_calls._voice_spoken_answers("chat-1", FAILED_CALL) == 0
    assert store.waiting_for("chat-2")["questions"][0]["answered"] is False


def test_the_call_end_submits_the_spoken_answers(store, monkeypatch):
    monkeypatch.setattr(api.voice_calls, "_commit_voice_call_memory", lambda *args, **kwargs: None)
    _waiting(store, questions=[HISENSE[0]])
    api.voice_calls._end_voice_call("chat-1", "gpt-live", FAILED_CALL, chat_call=True)
    assert _answers(store) == {"os": "Fire TV Home Version c'est le 72 601 84.1 "
                                    "software version Fire OS 8.1.6.3 RS8163-3414"}


# --- The pairing rules, directly -------------------------------------------------


def test_a_turn_asks_when_it_asks():
    asks = api.voice_calls._voice_answer_asks
    assert asks("c'est quoi le numéro ?") and asks("Comment as-tu fait ?")
    assert asks("what do you mean ?") and asks("What's this about") and asks("comment t'as fait ça")
    assert not asks("C'est un Fire TV, version 72 601 84.1")
    assert not asks("oui c'est fait")


def test_a_model_turn_reads_the_question_by_its_choices_or_its_lead_words():
    reads = api.voice_calls._voice_reads_question
    os_question = {"question": "Quel système tourne sur la TV ?", "choices": ["VIDAA", "Google TV", "Autre"]}
    dev_question = {"question": "Comment as-tu activé le mode développeur ?", "choices": []}
    assert reads("Tu vois VIDAA ou Google TV ?", os_question)
    assert reads("Et comment as-tu activé le mode développeur ?", dev_question)
    assert not reads("Parfait, merci. Hermes est en pause.", dev_question)
    assert not reads("Salut ! Comment ça va ?", os_question)
