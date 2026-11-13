"""Prediction forks share their chat's session ID, not its resource ownership."""
import sys
import types

import pytest

from test_predict import api


@pytest.fixture
def fork_runtime(monkeypatch):
    parent = types.SimpleNamespace(resources_alive=True, memory_shutdowns=0)
    state = types.SimpleNamespace(released=0, usage=[], fail_run=False, fail_usage=False)

    class Fork:
        def run_conversation(self, **kwargs):
            if state.fail_run:
                raise RuntimeError("provider failed")
            return {"final_response": " prediction "}

        def release_clients(self):
            state.released += 1

        def shutdown_memory_provider(self):
            parent.memory_shutdowns += 1

        def close(self):
            # This fork's session ID is the parent's, as on Hermes.
            parent.resources_alive = False

    fork = Fork()
    review = types.ModuleType("agent.background_review")
    review.build_cache_parity_fork = lambda *a, **k: (fork, {}, False)
    review._digest_history = lambda history: history
    review._snapshot_review_usage = lambda child: {"input_tokens": 2}

    def record_usage(agent, usage, **kwargs):
        if state.fail_usage:
            raise RuntimeError("usage store unavailable")
        state.usage.append((agent, usage, kwargs))

    def release(agent, child, *, task):
        # Like Hermes, this is sequential: a snapshot failure can precede release.
        record_usage(agent, review._snapshot_review_usage(child), task=task)
        child.release_clients()

    review._record_review_usage_to_parent = record_usage
    review.release_cache_parity_fork = release
    side = types.ModuleType("agent.side_question")
    side._FORK_PROMPT = "Answer without tools."
    side.trim_snapshot_for_fork = lambda history: history
    package = types.ModuleType("agent")
    package.background_review = review
    package.side_question = side
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.background_review", review)
    monkeypatch.setitem(sys.modules, "agent.side_question", side)
    plugins = types.ModuleType("hermes_cli.plugins")
    state.whitelist = None
    plugins.set_thread_tool_whitelist = lambda tools, **kwargs: setattr(state, "whitelist", tools)
    plugins.clear_thread_tool_whitelist = lambda: setattr(state, "whitelist", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    return parent, fork, review, state


@pytest.mark.parametrize("has_helper", [True, False])
@pytest.mark.parametrize("fail_run", [True, False])
@pytest.mark.parametrize("fail_usage", [True, False])
def test_prediction_releases_clients_without_closing_its_chat(fork_runtime, has_helper, fail_run, fail_usage):
    parent, fork, review, state = fork_runtime
    state.fail_run = fail_run
    state.fail_usage = fail_usage
    if not has_helper:
        del review.release_cache_parity_fork
    if fail_run:
        with pytest.raises(RuntimeError, match="^provider failed$"):
            api.chats._predict_fork_answer(parent, [], "What next?", {})
    else:
        assert api.chats._predict_fork_answer(parent, [], "What next?", {}) == "prediction"
    assert parent.resources_alive
    assert parent.memory_shutdowns == 0
    assert state.released == 1
    assert state.whitelist is None
    expected_usage = [(parent, {"input_tokens": 2}, {"task": "conduit_predict"} if has_helper else {})]
    assert state.usage == ([] if fail_usage else expected_usage)


def test_legacy_fork_without_a_client_releaser_never_closes_the_chat(fork_runtime):
    parent, fork, review, state = fork_runtime
    del review.release_cache_parity_fork
    fork.release_clients = None
    assert api.chats._predict_fork_answer(parent, [], "What next?", {}) == "prediction"
    assert parent.resources_alive
    assert parent.memory_shutdowns == 0
    assert state.whitelist is None


@pytest.mark.parametrize("has_helper", [True, False])
@pytest.mark.parametrize("fail_run", [True, False])
def test_bad_usage_snapshot_does_not_skip_client_release(fork_runtime, has_helper, fail_run):
    parent, fork, review, state = fork_runtime
    state.fail_run = fail_run
    if not has_helper:
        del review.release_cache_parity_fork

    def bad_snapshot(child):
        raise ValueError("invalid usage counter")

    review._snapshot_review_usage = bad_snapshot
    if fail_run:
        with pytest.raises(RuntimeError, match="^provider failed$"):
            api.chats._predict_fork_answer(parent, [], "What next?", {})
    else:
        assert api.chats._predict_fork_answer(parent, [], "What next?", {}) == "prediction"
    assert parent.resources_alive and parent.memory_shutdowns == 0
    assert state.released == 1
    assert state.usage == []
    assert state.whitelist is None
