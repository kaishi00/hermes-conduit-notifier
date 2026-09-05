import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]

# client.py imports the Hermes runtime for the state path; tests only exercise
# functions that never touch it, so provide a stub before loading the plugin.
if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants

# _format_result imports the real strip_recommended from the Hermes runtime;
# mirror its exact semantics so formatting assertions match production.
if "tools" not in sys.modules:
    _tools = types.ModuleType("tools")
    _tools.__path__ = []
    sys.modules["tools"] = _tools
if "tools.clarify_tool" not in sys.modules:
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool

# The repo directory name is hyphenated, so import the plugin modules under a
# synthetic package (the same trick Hermes's plugin loader performs).
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg
if "conduit_push.clarify_loop" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("conduit_push.clarify_loop", ROOT / "clarify_loop.py")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["conduit_push.clarify_loop"] = _module
    _spec.loader.exec_module(_module)

import conduit_push.clarify_loop as loop  # noqa: E402


class _FakeState:
    """Stand-in for client.load_state / poll_decision / enqueue / cancel_decision."""

    def __init__(self, poll_results):
        self.paired = True
        self.poll_results = list(poll_results)
        self.enqueued = []
        self.poll_ids = []
        self.cancelled_ids = []

    def load_state(self):
        return {"credential": "x"} if self.paired else None

    def poll_decision(self, request_id):
        self.poll_ids.append(request_id)
        if self.poll_results:
            result = self.poll_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"status": "pending"}

    def enqueue(self, event):
        self.enqueued.append(event)
        # Match the real boundary: True = accepted by the delivery queue.
        return True

    def cancel_decision(self, request_id):
        # Stubbed so tests stay hermetic: the real client would attempt an
        # HTTPS call the moment the original clarify path wins the race.
        self.cancelled_ids.append(request_id)
        return True


def _install(fake):
    loop.client.load_state = fake.load_state
    loop.client.poll_decision = fake.poll_decision
    loop.client.enqueue = fake.enqueue
    loop.client.cancel_decision = fake.cancel_decision
    loop.POLL_INTERVAL_SECONDS = 0.01


def _kwargs(next_call, args, **extra):
    return {"tool_name": "clarify", "args": args, "next_call": next_call, "session_id": "sess-1", **extra}


def _batch_args():
    """A two-question clarify invocation in the current wire shape."""
    return {
        "questions": [
            {"question": "Which environment?", "choices": ["staging", "prod"]},
            {"question": "Which tests should run?", "choices": ["unit", "ui"], "multi_select": True},
        ]
    }


def test_non_clarify_tools_pass_through_untouched():
    fake = _FakeState([])
    _install(fake)
    calls = []
    result = loop.middleware(tool_name="read_file", args={"path": "x"}, next_call=lambda a: calls.append(a) or "original")
    assert result == "original"
    assert fake.enqueued == []
    assert calls == [{"path": "x"}]


def test_unpaired_profile_keeps_original_path_only():
    fake = _FakeState([])
    fake.paired = False
    _install(fake)
    result = loop.middleware(**_kwargs(lambda a: "original", {"question": "Q?"}))
    assert result == "original"
    assert fake.enqueued == []


def test_queue_full_drop_uses_the_native_path_immediately_without_polling():
    # enqueue returning False means the event was dropped BEFORE delivery
    # (local queue full): the relay will never park the decision, so polling
    # is phantom work that can only burn the unknown grace before the
    # inevitable native fallback. The middleware must skip the race
    # entirely — no thread, no poll_decision calls, native answer now.
    fake = _FakeState([])
    dropped = []
    fake.enqueue = lambda event: dropped.append(event) or False
    _install(fake)
    threads = []

    def native(args):
        threads.append(threading.current_thread().name)
        return "native result"

    result = loop.middleware(**_kwargs(native, {
        "question": "Which environment?",
        "choices": ["staging", "prod"],
    }))
    assert result == "native result"
    # Deterministic regression proof: on the pre-fix code next_call ran on
    # the spawned "conduit-push-clarify" daemon thread; with the fix it runs
    # synchronously on the caller's thread.
    assert threads == [threading.current_thread().name]
    assert len(dropped) == 1, "the clarify event was attempted exactly once"
    assert fake.enqueued == []
    assert fake.poll_ids == [], "no phantom relay polling after a local drop"
    assert fake.cancelled_ids == []


def test_enqueue_reports_whether_the_event_was_queued():
    # The boundary the clarify loop depends on: True while the delivery
    # queue has capacity, False once it is full (event dropped) or the
    # profile lost its pairing.
    import queue as queue_module

    # Earlier tests in this file install fakes onto the shared client
    # module; reload so the GENUINE enqueue boundary is under test.
    importlib.reload(loop.client)
    original_load_state = loop.client.load_state
    original_events = loop.client._events
    original_worker_started = loop.client._worker_started
    loop.client._events = queue_module.Queue(maxsize=1)
    loop.client._worker_started = True  # keep the delivery worker unspawned
    try:
        loop.client.load_state = lambda: {"credential": "x"}  # paired, without touching the filesystem
        assert loop.client.enqueue({"type": "response.ready"}) is True
        assert loop.client.enqueue({"type": "response.ready"}) is False, "a full queue must drop and report False"
        loop.client.load_state = lambda: None  # unpaired: nothing can be delivered
        assert loop.client.enqueue({"type": "response.ready"}) is False, "an unpaired profile must report False"
    finally:
        loop.client.load_state = original_load_state
        loop.client._events = original_events
        loop.client._worker_started = original_worker_started


def test_relay_answer_wins_and_formats_like_the_builtin_tool():
    fake = _FakeState([{"status": "answered", "answer": "Red"}])
    _install(fake)
    release = threading.Event()

    def blocking_original(args):
        release.wait(5)
        return "never used"

    result = loop.middleware(**_kwargs(blocking_original, {
        "question": "Which color?",
        "choices": ["Red", "Blue"],
    }))
    release.set()
    parsed = json.loads(result)
    assert parsed == {
        "question": "Which color?",
        "choices_offered": ["Red", "Blue"],
        "user_response": "Red",
    }
    # The push carried the plugin-minted, answerable decision.
    event = fake.enqueued[0]
    assert event["type"] == "input.needed"
    assert event["body"] == "Which color?"
    decision = event["decision"]
    assert decision["kind"] == "clarify"
    assert decision["request_id"].startswith("conduit-push-")
    assert decision["question"] == "Which color?"
    assert decision["choices"] == ["Red", "Blue"]
    # A legacy scalar invocation pushes the SCALAR decision: no questions[],
    # so the relay parks it on the scalar path and answers {"answer": ...}.
    assert "questions" not in decision
    assert fake.poll_ids == [decision["request_id"]]


def test_original_answer_wins_when_gateway_responds_first():
    fake = _FakeState([])
    _install(fake)

    def original(args):
        return json.dumps({"question": "Q", "choices_offered": ["A"], "user_response": "A"})

    result = loop.middleware(**_kwargs(original, {"question": "Q"}))
    assert json.loads(result)["user_response"] == "A"


def _blocking_original(unused_value="unused"):
    release = threading.Event()
    holder = {}

    def original(args):
        release.wait(5)
        return unused_value

    return original, release


def test_poll_transport_errors_are_tolerated_until_answered():
    fake = _FakeState([
        RuntimeError("network down"),
        {"status": "pending"},
        {"status": "answered", "answer": "Blue"},
    ])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        release.set()
    assert json.loads(result)["user_response"] == "Blue"


def test_recommended_label_is_stripped_from_relay_answer():
    fake = _FakeState([{"status": "answered", "answer": "Rebase onto main (Recommended)"}])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, {
            "question": "How?",
            "choices": ["Rebase onto main", "Merge"],
        }))
    finally:
        release.set()
    assert json.loads(result)["user_response"] == "Rebase onto main"


def test_multi_select_answer_parses_json_and_csv():
    for raw in ('["Red", "Blue"]', "Red, Blue"):
        fake = _FakeState([{"status": "answered", "answer": raw}])
        _install(fake)
        original, release = _blocking_original()
        try:
            result = loop.middleware(**_kwargs(original, {
                "question": "Pick",
                "choices": ["Red", "Blue"],
                "multi_select": True,
            }))
        finally:
            release.set()
        assert json.loads(result)["user_response"] == ["Red", "Blue"]


def test_dict_shaped_choices_flatten_to_labels():
    fake = _FakeState([{"status": "answered", "answer": "Ship it"}])
    _install(fake)
    original, release = _blocking_original()
    try:
        loop.middleware(**_kwargs(original, {
            "question": "What now?",
            "choices": [{"label": "Ship it"}, {"description": "Hold"}, "Ask user"],
        }))
    finally:
        release.set()
    assert fake.enqueued[0]["decision"]["choices"] == ["Ship it", "Hold", "Ask user"]


def test_child_sessions_keep_the_original_path():
    fake = _FakeState([])
    _install(fake)
    kwargs = _kwargs(lambda a: "original", {"question": "Q?"}, session_id="child-1")
    result = loop.middleware(is_child_session=lambda session_id: session_id == "child-1", **kwargs)
    assert result == "original"
    assert fake.enqueued == []
    # The parent session still engages the loop.
    parent_kwargs = _kwargs(lambda a: "original", {"question": "Q?"}, session_id="parent-1")
    loop.middleware(is_child_session=lambda session_id: session_id == "child-1", **parent_kwargs)
    assert len(fake.enqueued) == 1


def test_polling_stops_when_relay_expires_the_decision():
    # pending once, then unknown: the relay's 2h TTL expired the decision.
    # The loop must stop polling (no third request) and wait for the
    # original path instead of hammering a request that can never succeed.
    fake = _FakeState([{"status": "pending"}, {"status": "unknown"}, {"status": "pending"}])
    _install(fake)
    original, release = _blocking_original("late original result")
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        release.set()
    assert result == "late original result"
    assert len(fake.poll_ids) == 2, "polling must stop after unknown-following-pending"


def test_unknown_before_first_pending_is_tolerated():
    # The push event and the first poll race (enqueue is async), so an early
    # unknown must NOT be treated as expiry; polling continues to the answer.
    fake = _FakeState([
        {"status": "unknown"},
        {"status": "pending"},
        {"status": "answered", "answer": "Blue"},
    ])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        release.set()
    assert json.loads(result)["user_response"] == "Blue"
    assert len(fake.poll_ids) == 3


def test_poll_budget_coerces_string_timeout():
    sys.modules["tools.clarify_gateway"] = types.SimpleNamespace(get_clarify_timeout=lambda: "3600")
    try:
        assert loop._clarify_poll_budget() == 3630.0
        sys.modules["tools.clarify_gateway"] = types.SimpleNamespace(get_clarify_timeout=lambda: 0)
        assert loop._clarify_poll_budget() == loop.MAX_POLL_SECONDS
    finally:
        sys.modules.pop("tools.clarify_gateway", None)


def test_polling_stops_when_no_card_was_delivered():
    # deliverable:false means device preferences suppressed the card; nobody
    # can answer by id, so the first poll result ends the polling.
    fake = _FakeState([{"status": "pending", "deliverable": False}, {"status": "pending"}])
    _install(fake)
    original, release = _blocking_original("original result")
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        release.set()
    assert result == "original result"
    assert len(fake.poll_ids) == 1


def test_consecutive_unknown_polls_stop_after_grace_window():
    # The push was never parked (dropped at enqueue or delivery): tolerate a
    # short grace for the delivery race, then stop instead of polling the
    # entire budget. The later "answered" must never be reached.
    fake = _FakeState([{"status": "unknown"}] * 5 + [{"status": "answered", "answer": "late"}])
    _install(fake)
    original, release = _blocking_original("original result")
    old_grace = loop.UNKNOWN_POLL_GRACE
    loop.UNKNOWN_POLL_GRACE = 3
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        loop.UNKNOWN_POLL_GRACE = old_grace
        release.set()
    assert result == "original result"
    assert len(fake.poll_ids) == 3, "polling must stop at the grace boundary"


def test_persistent_transport_errors_stop_after_grace_window():
    # A persistently unreachable relay must not poll the full budget: errors
    # count against the same grace as unknowns.
    fake = _FakeState([RuntimeError("relay down")] * 5)
    _install(fake)
    original, release = _blocking_original("original result")
    old_grace = loop.UNKNOWN_POLL_GRACE
    loop.UNKNOWN_POLL_GRACE = 3
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        loop.UNKNOWN_POLL_GRACE = old_grace
        release.set()
    assert result == "original result"
    assert len(fake.poll_ids) == 3


def test_transport_error_after_pending_does_not_stop_immediately():
    # A transient blip after the decision was parked keeps polling to the
    # answer (the grace counter resets on a successful pending/answered view).
    fake = _FakeState([
        {"status": "pending"},
        RuntimeError("blip"),
        {"status": "answered", "answer": "Blue"},
    ])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Q?"}))
    finally:
        release.set()
    assert json.loads(result)["user_response"] == "Blue"


def test_non_question_arg_shapes_still_produce_a_card():
    # LLMs deviate from the clarify schema; the hook path handled prompt /
    # message / questions[0] and the loop must too.
    fake = _FakeState([{"status": "answered", "answer": "Ship it"}])
    _install(fake)
    original, release = _blocking_original()
    try:
        loop.middleware(**_kwargs(original, {"prompt": "Deploy now?", "choices": ["Ship it"]}))
    finally:
        release.set()
    event = fake.enqueued[0]
    assert event["body"] == "Deploy now?"
    assert event["decision"]["question"] == "Deploy now?"


def test_poll_delay_backs_off_and_caps():
    old_base, old_cap = loop.POLL_INTERVAL_SECONDS, loop.MAX_POLL_BACKOFF_SECONDS
    loop.POLL_INTERVAL_SECONDS = 2.0
    loop.MAX_POLL_BACKOFF_SECONDS = 10.0
    try:
        assert loop._poll_delay(1) == 2.0
        assert loop._poll_delay(2) == 4.0
        assert loop._poll_delay(3) == 8.0
        assert loop._poll_delay(4) == 10.0
        assert loop._poll_delay(500) == 10.0
    finally:
        loop.POLL_INTERVAL_SECONDS, loop.MAX_POLL_BACKOFF_SECONDS = old_base, old_cap


def test_one_question_questions_array_is_batch_protocol_and_formats_batch_result():
    # A ONE-entry questions[] call is still batch protocol: the relay's
    # answers map must format into the upstream batch result shape, never
    # the legacy scalar shape.
    fake = _FakeState([
        {"status": "answered", "answers": {"q0": "staging"}, "remaining": []},
    ])
    _install(fake)
    original, release = _blocking_original("never used")
    try:
        result = loop.middleware(**_kwargs(original, {
            "questions": [{"question": "Which environment?", "choices": ["staging", "prod"]}],
        }))
    finally:
        release.set()
    parsed = json.loads(result)
    assert parsed == {
        "responses": [
            {"question": "Which environment?", "choices_offered": ["staging", "prod"], "user_response": "staging"},
        ]
    }
    # Guard against "fixing" scalar by collapsing every single-question case:
    # a one-entry questions[] invocation pushes the BATCH wire shape.
    decision = fake.enqueued[0]["decision"]
    assert [q["qid"] for q in decision["questions"]] == ["q0"]
    assert decision["questions"][0]["question"] == "Which environment?"


def test_legacy_scalar_single_question_still_formats_scalar_result():
    # Provenance, not cardinality: this invocation used `question`, so the
    # push must be the scalar decision (no questions[]) — even though the
    # normalized internal batch has one entry. The relay then answers in the
    # scalar shape ({status, answer}); a batch-parked decision would return
    # {status, answers: {q0: ...}} and this branch would format an empty
    # answer, which is exactly the regression the wire shape prevents.
    fake = _FakeState([{"status": "answered", "answer": "staging"}])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, {
            "question": "Which environment?",
            "choices": ["staging", "prod"],
        }))
    finally:
        release.set()
    decision = fake.enqueued[0]["decision"]
    assert decision["question"] == "Which environment?"
    assert decision["choices"] == ["staging", "prod"]
    assert "questions" not in decision, "a scalar invocation must push a scalar decision"
    parsed = json.loads(result)
    assert parsed["user_response"] == "staging"
    assert "responses" not in parsed, "the legacy scalar shape must not gain batch wrapping"


def test_native_result_returns_without_waiting_for_a_slow_release():
    # The release DELETE rides a daemon thread: a relay that hangs the
    # cancellation must never delay the user's native answer.
    fake = _FakeState([])
    _install(fake)
    cancel_started = threading.Event()
    release_cancel = threading.Event()

    def slow_cancel(request_id):
        cancel_started.set()
        release_cancel.wait(5)
        fake.cancelled_ids.append(request_id)
        return True

    loop.client.cancel_decision = slow_cancel

    def original(args):
        return "native result"

    started = threading.Event()
    result_holder = {}

    def run_middleware():
        started.set()
        result_holder["result"] = loop.middleware(**_kwargs(original, {"question": "Q?"}))

    worker = threading.Thread(target=run_middleware, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "middleware returned while cancellation was still hanging"
    assert result_holder["result"] == "native result"
    assert cancel_started.wait(5), "the release must still be fired, just off-thread"
    release_cancel.set()


def test_batch_push_preserves_every_question_and_relay_completion_wins():
    fake = _FakeState([
        {"status": "pending", "remaining": ["q0", "q1"], "answers": {}},
        {"status": "answered", "answers": {"q0": "staging", "q1": '["unit"]'}, "remaining": []},
    ])
    _install(fake)
    original, release = _blocking_original("never used")
    try:
        result = loop.middleware(**_kwargs(original, _batch_args()))
    finally:
        release.set()

    # The pushed decision carried the FULL batch with gateway-style qids.
    event = fake.enqueued[0]
    decision = event["decision"]
    assert decision["request_id"].startswith("conduit-push-")
    assert [(q["qid"], q["question"]) for q in decision["questions"]] == [
        ("q0", "Which environment?"),
        ("q1", "Which tests should run?"),
    ]
    assert decision["questions"][1]["multi_select"] is True
    assert event["body"] == "Which environment?", "the body stays the collapsed summary"
    assert decision["question"] == "Which environment?"

    # The relay completion formatted exactly like the built-in batch result.
    parsed = json.loads(result)
    assert parsed == {
        "responses": [
            {"question": "Which environment?", "choices_offered": ["staging", "prod"], "user_response": "staging"},
            {"question": "Which tests should run?", "choices_offered": ["unit", "ui"], "user_response": ["unit"]},
        ]
    }
    # The completed decision must not be cancelled after the relay won.
    assert fake.cancelled_ids == []


def test_native_answer_wins_and_releases_the_batch_decision():
    fake = _FakeState([])
    _install(fake)

    def original(args):
        return json.dumps({"responses": [{"question": "native", "user_response": "x"}]})

    result = loop.middleware(**_kwargs(original, _batch_args()))
    assert json.loads(result)["responses"][0]["question"] == "native"
    assert len(fake.cancelled_ids) == 1, "the parked decision is released so late device answers are rejected"
    assert fake.cancelled_ids[0].startswith("conduit-push-")


def test_batch_legacy_collapsed_answer_formats_absences():
    # An old relay/pre-batch device answers only the collapsed copy: the
    # result keeps every row, with unanswered questions as empty responses
    # (upstream absence semantics), never a fake full-batch success.
    fake = _FakeState([{"status": "answered", "answer": "staging"}])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, _batch_args()))
    finally:
        release.set()
    parsed = json.loads(result)
    assert [row["user_response"] for row in parsed["responses"]] == ["staging", ""]
    assert parsed["responses"][1]["question"] == "Which tests should run?"


def test_batch_progress_resets_the_poll_backoff():
    # A shrinking remaining set means answers are landing: the loop must
    # return to the short interval instead of compounding the capped backoff
    # while the user is mid-batch (each subsequent answer would otherwise
    # wait out the growing, capped delay).
    fake = _FakeState([
        {"status": "pending", "remaining": ["q0", "q1"]},
        {"status": "pending", "remaining": ["q1"], "answers": {"q0": "staging"}},
        {"status": "answered", "answers": {"q1": "ui"}, "remaining": []},
    ])
    _install(fake)
    sleeps = []
    real_sleep = loop.time.sleep
    loop.time.sleep = lambda seconds: sleeps.append(seconds)
    original, release = _blocking_original("never used")
    try:
        result = loop.middleware(**_kwargs(original, _batch_args()))
    finally:
        loop.time.sleep = real_sleep
        release.set()
    assert json.loads(result)["responses"][1]["user_response"] == ["ui"]
    # Poll 1 sleeps the base interval; poll 2 saw remaining shrink (2 -> 1),
    # so its sleep RESETS to base instead of backing off to 2x base.
    assert sleeps == [loop.POLL_INTERVAL_SECONDS, loop.POLL_INTERVAL_SECONDS]


def test_unparseable_clarify_args_keep_the_original_path_without_a_card():
    fake = _FakeState([])
    _install(fake)
    result = loop.middleware(**_kwargs(lambda a: "original", {"questions": [{"no_question": True}]}))
    assert result == "original"
    assert fake.enqueued == []


def test_single_question_decision_never_cancels_when_relay_wins():
    fake = _FakeState([{"status": "answered", "answer": "Red"}])
    _install(fake)
    original, release = _blocking_original()
    try:
        result = loop.middleware(**_kwargs(original, {"question": "Which color?", "choices": ["Red", "Blue"]}))
    finally:
        release.set()
    assert json.loads(result)["user_response"] == "Red"
    assert fake.cancelled_ids == []

def test_user_agent_derives_from_the_plugin_version():
    # One source of truth: bumping PLUGIN_VERSION updates the relay UA
    # automatically instead of leaving a stale hand-written constant.
    assert loop.client.USER_AGENT == f"Hermes-Conduit-Notifier/{loop.client.PLUGIN_VERSION}"
    assert loop.client.PLUGIN_VERSION == "0.3.0"
