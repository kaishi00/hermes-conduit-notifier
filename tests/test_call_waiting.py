"""What Hermes waits on, kept for a call from Hermes to read and answer
with no chat open (calls_store: note_waiting, answer_waiting)."""

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("conduit_push_test_call_waiting_store", ROOT / "calls_store.py")
store_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = store_module
_spec.loader.exec_module(store_module)
CallStore = store_module.CallStore
map_answer = store_module.map_answer
is_skip = store_module.is_skip

BATCH = [
    {"qid": "q0", "question": "Which environment?", "choices": ["staging (Recommended)", "prod"], "multi_select": False},
    {"qid": "q1", "question": "Which tests should run?", "choices": ["unit", "ui", "smoke"], "multi_select": True},
]

# A batch Conduit shows as a form: several questions, some with no choices.
FORM = [
    {"qid": "q0", "question": "Leaving from?", "choices": [], "multi_select": False},
    {"qid": "q1", "question": "Your kind of escape?", "choices": ["Beach", "City", "Pass"], "multi_select": False},
    {"qid": "q2", "question": "Total trip budget?", "choices": [], "multi_select": False},
]


@pytest.fixture
def store(tmp_path):
    clock = {"now": 1000.0}
    calls = CallStore(tmp_path, clock=lambda: clock["now"])
    calls.update_settings({"enabled": True, "alerts": True})
    calls.clock_state = clock
    return calls


def test_nothing_is_kept_with_alert_calls_off(tmp_path):
    calls = CallStore(tmp_path)
    assert calls.note_waiting("s1", "question", request_id="conduit-push-a", questions=BATCH) is False
    assert calls.waiting_for("s1") is None


def test_a_call_reads_the_batch_and_answers_it_one_question_at_a_time(store):
    assert store.note_waiting(["s1", "rt-1"], "question", request_id="conduit-push-a", questions=BATCH)
    waiting = store.waiting_for("rt-1")
    assert waiting["kind"] == "question"
    assert [q["number"] for q in waiting["questions"]] == [1, 2]
    assert waiting["questions"][0]["choices"] == ["staging (Recommended)", "prod"]

    first = store.answer_waiting("s1", answer="the recommended one")
    assert first["status"] == "answered" and first["answer"] == "staging"
    assert first["next"]["number"] == 2 and first["next"]["multi_select"] is True
    assert store.take_answers("conduit-push-a") is None, "not until every question has an answer"

    second = store.answer_waiting("s1", answer="one and three")
    assert second["answer"] == "unit, smoke" and "next" not in second
    assert store.answers_ready("conduit-push-a")
    assert store.take_answers("conduit-push-a") == {"q0": "staging (Recommended)", "q1": json.dumps(["unit", "smoke"])}
    assert store.waiting_for("s1") is None, "taken once"


def test_a_forms_questions_may_be_left_blank_by_saying_skip(store):
    store.note_waiting("s1", "question", request_id="conduit-push-f", questions=FORM)
    waiting = store.waiting_for("s1")
    assert [question["may_leave_blank"] for question in waiting["questions"]] == [True, True, True]

    first = store.answer_waiting("s1", answer="Toronto")
    assert first["answer"] == "Toronto" and first["next"]["may_leave_blank"] is True
    # A choice it names comes first: "pass" picks the choice called Pass.
    assert store.answer_waiting("s1", answer="pass", question=2)["answer"] == "Pass"
    skipped = store.answer_waiting("s1", answer="Leave it blank, please.")
    assert skipped == {"status": "answered", "question": 3, "answer": "left blank", "skipped": True}
    assert store.take_answers("conduit-push-f") == {"q0": "Toronto", "q1": "Pass", "q2": ""}


def test_words_that_leave_it_blank_win_over_a_choice_named_inside_them(store):
    # Only a choice named whole is picked over them: "leave it blank" beside
    # a choice called Blank, or "I'll pass" beside Pass, leaves it blank.
    form = [
        {"qid": "q0", "question": "Anything to add?", "choices": [], "multi_select": False},
        {"qid": "q1", "question": "Which cushion?", "choices": ["Blank", "Striped"], "multi_select": False},
        {"qid": "q2", "question": "Your kind of escape?", "choices": ["Beach", "Pass"], "multi_select": False},
    ]
    store.note_waiting("s1", "question", request_id="conduit-push-g", questions=form)
    assert store.answer_waiting("s1", answer="leave it blank", question=2)["skipped"] is True
    assert store.answer_waiting("s1", answer="I'll pass", question=3)["skipped"] is True
    assert store.answer_waiting("s1", answer="blank")["answer"] == "blank", "not a skip: the words go as they are"
    assert store.take_answers("conduit-push-g") == {"q0": "blank", "q1": "", "q2": ""}


def test_skip_is_only_an_answer_where_nothing_may_be_left_blank(store):
    # A batch of choices only, and a question on its own, show as today's
    # card, which has no blank: "skip" goes to Hermes as their words.
    store.note_waiting("s1", "question", request_id="conduit-push-a", questions=BATCH)
    assert [question["may_leave_blank"] for question in store.waiting_for("s1")["questions"]] == [False, False]
    assert store.answer_waiting("s1", answer="skip")["answer"] == "skip"
    store.note_waiting("s2", "question", request_id="conduit-push-b", questions=FORM[:1])
    assert store.waiting_for("s2")["questions"][0]["may_leave_blank"] is False
    assert store.answer_waiting("s2", answer="skip")["answer"] == "skip"
    assert store.take_answers("conduit-push-b") == {"q0": "skip"}


# Conduit reads the same words the same way (HermesCallWaitingTests'
# testSkipIsReadTheSameWayAsThePlugin): the two lists move together.
SKIPS = [
    "skip", "Skip it.", "skip this one, please", "I'll pass", "leave it blank", "Leave that empty",
    "go ahead and skip it please", "Überspringen", "lass es leer", "ich passe", "sáltala", "déjalo en blanco",
    "je passe", "laisse-le vide", "lewati saja", "biarkan kosong", "salta questa domanda", "lascialo in bianco",
    "pomiń to pytanie", "pode pular", "deixa em branco", "Пропусти этот вопрос", "оставь пустым", "bunu atla",
    "boş bırak", "スキップ", "スキップでお願いします", "飛ばしてください", "空欄のままでお願いします", "跳过", "这个问题跳过吧",
    "留空", "스킵해 주세요", "비워 두세요", "이건 패스할게요", "skip to the next question", "Skip to the next one.",
    "überspringen, nächste Frage", "salta a la siguiente pregunta", "passons à la suivante",
    "lewati, lanjut ke berikutnya", "salta alla prossima domanda", "pomiń, następne pytanie", "pula pra próxima",
    "пропусти, следующий вопрос", "atla, sonraki soru", "スキップして次の質問へ", "跳过，下一题", "跳過，下一個",
    "건너뛰어, 다음 질문으로", "다음으로 넘어가",
]
NOT_SKIPS = [
    "", "Toronto", "not sure yet", "next week", "don't skip it", "skip the hotel", "skip?", "blanco", "leer",
    "go ahead and skip it now please", "nicht überspringen", "no lo saltes", "不要跳过", "跳过吗", "スキップしないで",
    "건너뛰지 마세요", "the next one", "next question", "nächste Woche", "la siguiente", "次の質問", "下一个", "다음",
]


@pytest.mark.parametrize("words", SKIPS)
def test_skip_words_in_the_apps_languages(words):
    assert is_skip(words)


@pytest.mark.parametrize("words", NOT_SKIPS)
def test_words_that_are_an_answer_not_a_skip(words):
    assert not is_skip(words)


def test_answering_a_question_already_answered_or_gone(store):
    store.note_waiting("s1", "question", request_id="conduit-push-a", questions=BATCH[:1])
    store.answer_waiting("s1", answer="2", question=1)
    assert store.answer_waiting("s1", answer="1", question=1)["status"] == "answered"
    assert store.take_answers("conduit-push-a") == {"q0": "prod"}
    assert store.answer_waiting("s1", answer="1")["status"] == "not_waiting"
    store.note_waiting("s1", "question", request_id="conduit-push-b", questions=BATCH)
    with pytest.raises(ValueError):
        store.answer_waiting("s1", answer="1", question=5)


def test_a_long_multi_select_answer_survives_a_reload(store):
    choices = [f"choice {number} " + "x" * 190 for number in range(12)]
    store.note_waiting("s1", "question", request_id="conduit-push-c",
                       questions=[{"qid": "q0", "question": "Which?", "choices": choices, "multi_select": True}])
    store.answer_waiting("s1", answer=", ".join(str(number) for number in range(1, 13)))
    assert json.loads(store.take_answers("conduit-push-c")["q0"]) == choices


def test_an_answer_hermes_should_hear_in_the_users_words(store):
    choices = ["staging (Recommended)", "prod"]
    assert map_answer("Before I choose: what does prod change?", choices, False) == (
        "Before I choose: what does prod change?", [])
    assert map_answer("not the recommended one", choices, False) == ("not the recommended one", [])
    assert map_answer("something else entirely", choices, False) == ("something else entirely", [])
    assert map_answer("the second one", choices, False) == ("prod", ["prod"])
    assert map_answer("Staging", choices, False) == ("staging (Recommended)", ["staging"])
    assert map_answer("let's go with prod", choices, False) == ("prod", ["prod"])
    assert map_answer("1 and 2", choices, False) == ("1 and 2", []), "one choice only"
    assert map_answer("7", choices, False) == ("7", [])


def test_an_approval_is_answered_once_and_a_cut_command_only_denied(store):
    assert store.note_waiting("s1", "approval", description="Delete the build cache", command="rm -rf build")
    waiting = store.waiting_for("s1")
    assert waiting == {"kind": "approval", "description": "Delete the build cache", "command": "rm -rf build",
                       "command_cut": False, "answered": False}
    with pytest.raises(ValueError):
        store.answer_waiting("s1", choice="always")
    assert store.answer_waiting("s1", choice="once")["status"] == "answered"
    taken = store.take_approval("s1")
    assert taken["choice"] == "once" and len(taken["command_sha"]) == 64
    assert store.take_approval("s1") is None

    store.note_waiting("s2", "approval", description="Run a long script", command="x" * 2000)
    assert store.waiting_for("s2")["command_cut"] is True
    assert store.answer_waiting("s2", choice="once")["status"] == "refused"
    assert store.answer_waiting("s2", choice="deny", reason="Before I choose: why?")["status"] == "answered"
    assert store.take_approval("s2")["reason"] == "Before I choose: why?"


def test_a_wait_is_forgotten_once_answered_elsewhere_or_old(store):
    store.note_waiting("s1", "approval", description="Deploy", command="make deploy")
    store.note_waiting("s1", "question", request_id="conduit-push-a", questions=BATCH)
    assert store.forget_waiting("s1", "approval") == 1
    assert store.waiting_for("s1")["kind"] == "question"
    store.clock_state["now"] += store_module.WAITING_TTL_S + 1
    assert store.waiting_for("s1") is None


def test_an_approval_with_no_command_can_only_be_denied(store):
    store.note_waiting("s1", "approval", description="Clean up", command="")
    assert store.answer_waiting("s1", choice="once")["status"] == "refused"
    assert store.answer_waiting("s1", choice="deny")["status"] == "answered"


def test_a_calls_approval_answer_is_said_to_go_only_once_hermes_took_it(store):
    store.note_waiting("s1", "approval", description="Delete the build cache", command="rm -rf build")
    answer_id = store.answer_waiting("s1", choice="once")["answer_id"]
    claimed = store.take_approval("s1", keep=True)
    assert claimed["answer_id"] == answer_id
    # Being passed on: not taken back however long it takes.
    assert store.approval_delivery(answer_id, wait_s=0) == "claimed"
    assert store.waiting_for("s1")["answered"] is True
    store.settle_approval(answer_id, True)
    assert store.approval_delivery(answer_id, wait_s=0) == "resolved"
    assert store.waiting_for("s1") is None

    store.note_waiting("s1", "approval", description="Deploy", command="make deploy")
    answer_id = store.answer_waiting("s1", choice="deny")["answer_id"]
    store.take_approval("s1", keep=True)
    store.settle_approval(answer_id, False)
    assert store.approval_delivery(answer_id, wait_s=0) == "missed"


def test_a_calls_approval_answer_nothing_took_is_taken_back(store):
    store.note_waiting("s1", "approval", description="Deploy", command="make deploy")
    answer_id = store.answer_waiting("s1", choice="once")["answer_id"]
    assert store.approval_delivery(answer_id, wait_s=0) == "withdrawn"
    # Still waiting, unanswered: the agent's process finds nothing to pass on.
    assert store.waiting_for("s1")["answered"] is False
    assert store.take_approval("s1", keep=True) is None
    assert store.approval_delivery(answer_id, wait_s=0) == "withdrawn"

    # Answered on its card before anything took it: missed, at once.
    answer_id = store.answer_waiting("s1", choice="once")["answer_id"]
    store.forget_waiting("s1", "approval")
    assert store.approval_delivery(answer_id, wait_s=60) == "missed"


def test_an_approval_answer_from_a_plugin_before_delivery_reads_as_unanswered(store):
    store.note_waiting("s1", "approval", description="Deploy", command="make deploy")
    state = json.loads(store.path.read_text())
    state["waiting"][0]["choice"] = "once"
    store.path.write_text(json.dumps(state))
    assert store.waiting_for("s1")["answered"] is False
    assert store.take_approval("s1") is None


def test_an_answer_whose_delivery_record_is_gone_is_still_taken_back(store):
    store.note_waiting("s1", "approval", description="Deploy", command="make deploy")
    answer_id = store.answer_waiting("s1", choice="once")["answer_id"]
    state = json.loads(store.path.read_text())
    state["delivered"] = []
    store.path.write_text(json.dumps(state))
    assert store.approval_delivery(answer_id, wait_s=0) == "withdrawn"
    assert store.waiting_for("s1")["answered"] is False
