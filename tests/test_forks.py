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
