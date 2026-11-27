"""One slow summary no longer takes a chat's helper models away (helper_holds)."""
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEADLINE_ERR = TimeoutError("Auxiliary streamed call timed out at the host compression deadline after 0s "
                            "(the caller already stopped waiting)")
HERMES_SAYS = ("⚠ No auxiliary LLM provider configured: Hermes has no helper model for summarising long chats, "
               "so older messages will be cut without a summary. Run `hermes setup` to add one.")


@pytest.fixture
def holds(monkeypatch):
    """helper_holds, loaded fresh, beside stand-ins for Hermes' auxiliary
    client (its hold table and quarantine) and compression module
    (upstream/test_helper_holds.py holds the real ones to it)."""
    client = types.ModuleType("agent.auxiliary_client")
    client._aux_unhealthy_until = {}
    client.deadline = None
    client._current_aux_stream_deadline = lambda: client.deadline
    held = []

    def quarantine(task, fb_label, fb_provider, fb_err, *, base_url="", tag="", reason=None, model=None):
        held.append((fb_provider, reason))
        client._aux_unhealthy_until[("home", fb_provider)] = time.time() + 600

    client._quarantine_fallback_candidate = quarantine
    compression = types.ModuleType("agent.conversation_compression")
    warned = []
    compression._emit_feasibility_notice = lambda agent, msg: warned.append(msg)
    agent = types.ModuleType("agent")
    agent.auxiliary_client, agent.conversation_compression = client, compression
    constants = types.ModuleType("hermes_constants")
    constants.hermes_home_key = lambda: "home"
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", client)
    monkeypatch.setitem(sys.modules, "agent.conversation_compression", compression)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    spec = importlib.util.spec_from_file_location("conduit_push_helper_holds_test", ROOT / "helper_holds.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.client, module.compression, module.held, module.warned = client, compression, held, warned
    yield module
    module.uninstall()


def _quarantine(holds, err, reason="request timed out"):
    holds.client._quarantine_fallback_candidate("compression", "stepfun", "stepfun", err, reason=reason)


def test_a_fallback_the_deadline_stopped_isnt_held(holds):
    assert holds.install()
    _quarantine(holds, DEADLINE_ERR)
    assert holds.held == [] and holds.client._aux_unhealthy_until == {}


def test_the_deadline_having_passed_counts_too(holds):
    assert holds.install()
    holds.client.deadline = time.monotonic() - 1
    _quarantine(holds, TimeoutError("Request timed out."))
    assert holds.held == []


def test_anything_else_is_held_as_hermes_holds_it(holds):
    assert holds.install()
    _quarantine(holds, TimeoutError("Request timed out."))
    # A spent deadline with another reason is the provider's own answer (a 402 that came late).
    _quarantine(holds, DEADLINE_ERR, reason="payment error")
    holds.client.deadline = time.monotonic() + 60
    _quarantine(holds, TimeoutError("Request timed out."))
    assert holds.held == [("stepfun", "request timed out"), ("stepfun", "payment error"),
                          ("stepfun", "request timed out")]


def test_the_warning_names_helpers_on_hold(holds):
    assert holds.install()
    until = holds.client._aux_unhealthy_until
    until[("home", "zai")] = time.time() + 290
    until[("home", "custom-endpoint", "https://api.stepfun.ai/step_plan/v1")] = time.time() + 500
    until[("other-profile", "nous")] = time.time() + 500
    until[("home", "openrouter")] = time.time() - 5
    holds.compression._emit_feasibility_notice(object(), HERMES_SAYS)
    said = holds.warned[-1]
    assert "(zai, api.stepfun.ai)" in said and "the first of them is back in about 5 minutes" in said
    assert "nous" not in said and "openrouter" not in said and "hermes setup" not in said


def test_hermes_words_stand_when_nothing_is_on_hold_or_for_other_warnings(holds):
    assert holds.install()
    holds.compression._emit_feasibility_notice(object(), HERMES_SAYS)
    holds.client._aux_unhealthy_until[("home", "zai")] = time.time() + 60
    other = "⚠ Configured auxiliary compression provider 'zai' is unavailable, so ..."
    holds.compression._emit_feasibility_notice(object(), other)
    assert holds.warned == [HERMES_SAYS, other]


def test_one_minute_is_singular():
    spec = importlib.util.spec_from_file_location("conduit_push_helper_holds_words", ROOT / "helper_holds.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "(about 1 more minute)" in module.held_warning(["zai"], 20)
    assert "(a, b, c and 2 more)" in module.held_warning(list("abcde"), 300)


def test_a_hold_table_that_cant_be_read_leaves_hermes_words(holds):
    assert holds.install()

    class Shifting(dict):
        def items(self):
            raise RuntimeError("dictionary changed size during iteration")

    holds.client._aux_unhealthy_until = Shifting()
    holds.compression._emit_feasibility_notice(object(), HERMES_SAYS)
    assert holds.warned == [HERMES_SAYS]


def test_install_is_once_and_uninstall_puts_hermes_own_back(holds):
    own_q = holds.client._quarantine_fallback_candidate
    own_n = holds.compression._emit_feasibility_notice
    assert holds.install() and holds.install()
    guard = holds.client._quarantine_fallback_candidate
    assert guard is not own_q and guard.__wrapped__ is own_q
    holds.uninstall()
    assert holds.client._quarantine_fallback_candidate is own_q
    assert holds.compression._emit_feasibility_notice is own_n


def test_a_guard_an_earlier_load_left_is_unwrapped(holds):
    own_q = holds.client._quarantine_fallback_candidate
    assert holds.install()
    holds._installed.clear()  # a reload of the plugin: its guard stays on Hermes
    assert holds.install()
    assert holds.client._quarantine_fallback_candidate.__wrapped__ is own_q
