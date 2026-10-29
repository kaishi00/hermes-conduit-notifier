"""A fork of a chat (the self-improvement review, /btw, Conduit's
predictions) runs under the chat's session id; on a Hermes that runs plugin
hooks for it, nothing it does reaches the phone as the chat's turn."""
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import test_turn_end_pushes as turn_ends  # noqa: E402 — the plugin as that file loads it, with its fixtures

plugin = turn_ends.plugin
pushed = turn_ends.pushed
lock_screen = turn_ends.lock_screen
no_delegation_flag = turn_ends.no_delegation_flag
PREDICTION = '{"replies":[{"text":"Re-share the tarball and I\'ll try grabbing it from my phone","p":0.6}]}'


def _load(name="conduit_forks_under_test"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "forks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fork:
    """A fork as Hermes builds it: its run calls the turn's hooks."""

    def __init__(self, hooks=None):
        self.hooks = hooks or (lambda: None)
        self.seen = []

    def run_conversation(self, user_message, conversation_history=None):
        self.seen.append(user_message)
        self.hooks()
        return {"final_response": PREDICTION}


@pytest.fixture
def hermes(monkeypatch):
    """Hermes' fork builder, as far as the plugin touches it."""
    background_review = types.ModuleType("agent.background_review")
    built = []

    def build_cache_parity_fork(agent, task_cfg=None, *, max_iterations, write_origin="background_review"):
        fork = Fork(getattr(agent, "hooks", None))
        built.append(fork)
        return fork, {}, False

    background_review.build_cache_parity_fork = build_cache_parity_fork
    agent_package = types.ModuleType("agent")
    agent_package.background_review = background_review
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.background_review", background_review)
    return types.SimpleNamespace(module=background_review, own=build_cache_parity_fork, built=built)


@pytest.fixture
def marks(hermes):
    """The plugin's forks module, installed on Hermes' builder."""
    assert plugin.forks.install()
    yield plugin.forks
    plugin.forks.uninstall()


def _fork_run(hermes, hooks):
    # Hermes and the predict route take the builder off the module when they fork.
    fork, _runtime, _routed = hermes.module.build_cache_parity_fork(types.SimpleNamespace(hooks=hooks),
                                                                   max_iterations=3, write_origin="conduit_predict")
    return fork.run_conversation(user_message="Side question: what will the user send next?")


def test_a_forks_run_is_marked_and_nothing_else_is(hermes, marks):
    inside = []
    result = _fork_run(hermes, lambda: inside.append(marks.inside()))
    assert inside == [True] and result == {"final_response": PREDICTION}
    assert not marks.inside()
    assert hermes.built[0].seen == ["Side question: what will the user send next?"]


def test_a_failed_fork_run_leaves_no_mark(hermes, marks):
    def fails():
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        _fork_run(hermes, fails)
    assert not marks.inside()


def test_unloading_puts_hermes_own_builder_back(hermes):
    forks = _load()
    assert forks.install()
    assert hermes.module.build_cache_parity_fork is not hermes.own
    forks.uninstall()
    assert hermes.module.build_cache_parity_fork is hermes.own


def test_a_wrapper_an_earlier_load_left_is_not_wrapped_again(hermes):
    first, second = _load("conduit_forks_first"), _load("conduit_forks_second")
    assert first.install()
    first._wrapped_module = None  # an earlier load that never unloaded
    assert second.install()
    assert second._wrapped[0] is hermes.own
    second.uninstall()
    assert hermes.module.build_cache_parity_fork is hermes.own


def test_a_hermes_without_the_builder_is_left_alone(monkeypatch):
    background_review = types.ModuleType("agent.background_review")
    monkeypatch.setitem(sys.modules, "agent.background_review", background_review)
    monkeypatch.setitem(sys.modules, "agent", types.SimpleNamespace(background_review=background_review))
    assert not _load().install()


def _prediction_hooks():
    # What an older Hermes calls for the fork's turn, under the chat's id.
    plugin._pre_llm_call(session_id="s", turn_id="f", user_message="Side question: …")
    plugin._pre_api_request(session_id="s")
    plugin._post_api_request(session_id="s", usage={"prompt_tokens": 9000}, context_length=200000)
    plugin._post_llm_call(session_id="s", turn_id="f", assistant_response=PREDICTION)
    plugin._on_session_end(session_id="s", turn_id="f", completed=True, failed=False, interrupted=False)


def test_a_prediction_after_the_turn_sends_no_push(pushed, hermes, marks, lock_screen):
    reporter, posts = lock_screen
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Everything exists farm-side.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    reporter.flush()
    sent = len(posts)
    _fork_run(hermes, _prediction_hooks)
    reporter.flush()
    assert turn_ends._types(pushed) == ["response.ready"]
    assert pushed[0]["body"] == "Everything exists farm-side."
    assert len(posts) == sent, "nor anything on the Lock Screen"


def test_a_fork_inside_the_chats_turn_leaves_its_reply_to_the_turn(pushed, hermes, marks):
    # The self-improvement review or a /btw can run while the chat's turn waits for its end.
    plugin._pre_llm_call(session_id="s", turn_id="t", user_message="Why does the download fail?")
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Everything exists farm-side.")
    _fork_run(hermes, _prediction_hooks)
    assert pushed == []
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    assert [event["body"] for event in pushed] == ["Everything exists farm-side."]


def test_without_the_mark_an_older_hermes_pushed_the_fork(pushed, hermes):
    # Why the mark is there: the same hooks, unmarked, are the chat's turn.
    _fork_run(hermes, _prediction_hooks)
    assert [event["body"] for event in pushed] == [PREDICTION]


def test_a_forks_teardown_is_marked_too(hermes, marks):
    fork, _runtime, _routed = hermes.module.build_cache_parity_fork(types.SimpleNamespace(), max_iterations=3)
    seen = []
    fork.close = lambda: seen.append(marks.inside())
    marks._mark(fork)
    fork.close()
    assert seen == [True] and not marks.inside()


def test_a_forks_tool_call_still_meets_pulse(hermes, marks, monkeypatch):
    # Pulse holds a call of its home chat's turns while it runs there; a fork of that chat is held the same.
    held = {"action": "block", "message": "Pulse is working in this chat."}
    asked = []
    monkeypatch.setattr(plugin.pulse, "pre_tool_call", lambda *args: asked.append(args) or held)
    monkeypatch.setattr(plugin, "_room_tool_block", lambda kwargs: None)
    blocks = []
    _fork_run(hermes, lambda: blocks.append(plugin._pre_tool_call(session_id="home", turn_id="f",
                                                                 tool_name="memory", args={})))
    assert blocks == [held] and len(asked) == 1


def test_a_forks_allowed_tool_call_starts_nothing(hermes, marks, monkeypatch, lock_screen):
    reporter, posts = lock_screen
    monkeypatch.setattr(plugin.pulse, "pre_tool_call", lambda *args: None)
    monkeypatch.setattr(plugin, "_room_tool_block", lambda kwargs: None)
    sent = []
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    monkeypatch.setattr(plugin, "_clarify_loop_active", False)
    reporter.flush()
    before = len(posts)
    results = []
    _fork_run(hermes, lambda: results.append(plugin._pre_tool_call(session_id="s", turn_id="f",
                                                                  tool_name="clarify", args={"question": "?"})))
    reporter.flush()
    assert results == [None] and sent == [] and len(posts) == before


def test_a_subagent_still_skips_pulse(monkeypatch):
    asked = []
    monkeypatch.setattr(plugin.pulse, "pre_tool_call", lambda *args: asked.append(args))
    monkeypatch.setattr(plugin, "_room_tool_block", lambda kwargs: None)
    monkeypatch.setattr(plugin, "_is_child", lambda session_id: True)
    assert plugin._pre_tool_call(session_id="c", turn_id="t", tool_name="memory", args={}) is None
    assert asked == []


def test_a_forks_approval_sends_nothing(hermes, marks, monkeypatch):
    sent = []
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    _fork_run(hermes, lambda: plugin._pre_approval_request(session_id="s", session_key="s", turn_id="f",
                                                           description="rm -rf build", command="rm -rf build"))
    assert sent == []


def test_a_fork_started_before_a_pulse_attempt_is_not_held(hermes, marks, monkeypatch):
    # The rule for the chat's own turns (pulse._held_home_turn): only a turn that started after the attempt is held.
    monkeypatch.setattr(plugin.pulse, "_in_flight_here", lambda session_id: [10.0**10])
    monkeypatch.setattr(plugin, "_room_framing", lambda *args: None)
    held = []

    def hooks():
        plugin._pre_llm_call(session_id="home", turn_id="f-early", user_message="Side question: …")
        held.append(plugin.pulse._held_home_turn("home", "f-early"))
        held.append(plugin.pulse._held_home_turn("home", "f-unnoted"))
        plugin._on_session_end(session_id="home", turn_id="f-early", completed=True, failed=False, interrupted=False)

    _fork_run(hermes, hooks)
    assert held == [False, True]
    assert ("home", "f-early") not in plugin.pulse._started, "its end forgets when it started"


def test_a_fork_cant_use_conduits_tools(hermes, marks):
    results = []
    _fork_run(hermes, lambda: results.append(plugin.notify_tool._handle(
        {"title": "Done", "body": "Your build passed."}, "s", plugin._is_child)))
    assert results[0]["error"] == "subagent"
    assert plugin.notify_tool._handle({"title": "Done", "body": "Your build passed."}, "", plugin._is_child)[
        "error"] == "no_session", "outside the fork, the chat's own turn isn't refused as a subagent"


def test_a_declined_call_note_waits_for_the_chats_next_turn(hermes, marks, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.calls, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_room_framing", lambda *args: None)
    plugin.calls._store(tmp_path).record_outcome("a1b2c3d4e5f6a1b2c3d4e5f6", ["s"], "declined", age_s=0)
    contexts = []
    _fork_run(hermes, lambda: contexts.append(plugin._pre_llm_call(session_id="s", turn_id="f",
                                                                  user_message="Side question: …")))
    assert "declined" not in str(contexts[0])
    turn = plugin._pre_llm_call(session_id="s", turn_id="t", user_message="hi")
    assert turn["context"].startswith("[Conduit: the user declined the phone call")


def test_a_forks_approval_answer_leaves_the_chats_alert_and_card(hermes, marks, monkeypatch, lock_screen):
    reporter, posts = lock_screen
    cancelled = []
    monkeypatch.setattr(plugin.calls, "cancel_alerts", lambda *args: cancelled.append(args))
    reporter.flush()
    before = len(posts)
    _fork_run(hermes, lambda: plugin._post_approval_response(session_id="s", session_key="s", choice="deny"))
    reporter.flush()
    assert cancelled == [] and len(posts) == before
    plugin._post_approval_response(session_id="s", session_key="s", choice="once")
    assert cancelled == [("s", "approval")], "the chat's own answer still stops its alert"


def test_a_fork_inside_the_chats_turn_leaves_the_turns_state(pushed, hermes, marks, monkeypatch):
    monkeypatch.setattr(plugin, "_unnamed_ends", {})
    monkeypatch.setattr(plugin, "_room_framing", lambda *args: None)
    plugin._pre_llm_call(session_id="s", turn_id="t", user_message="Why does the download fail?")
    parent, unnamed = dict(plugin._parent_turns), dict(plugin._unnamed_ends)
    _fork_run(hermes, _prediction_hooks)
    assert plugin._parent_turns == parent and plugin._unnamed_ends == unnamed
