"""A summary that runs into compaction's deadline doesn't put the helper
models on hold (helper_holds).

Hermes summarises a long chat on the compaction helper within a deadline of
its own. When a summary call runs into it, Hermes still walks its fallback
providers, each fails at once (its caller had stopped waiting), and Hermes
holds each for ten minutes as if it had timed out (agent/auxiliary_client.py,
``_quarantine_fallback_candidate``). With the chat's own provider among them,
every compaction for those ten minutes says "No auxiliary LLM provider
configured" (Furina's Bot Chat on Eric's server, 2026-11-27).

``test_hermes_leaves_a_fallback_the_deadline_stopped_off_hold`` asks Hermes'
own quarantine, without the plugin: a known failure until Hermes tells the
two apart, when the guard gets another look. The other test puts the guards
on and checks both: no hold for a spent deadline, Hermes' hold for a real
timeout, and the warning naming the helper on hold.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

import pytest

from conftest import ROOT

DEADLINE_ERR = ("Auxiliary streamed call timed out at the host compression deadline after 0s (the caller "
                "already stopped waiting; streaming on would only pin its session lease)")
PROVIDER = "stepfun"


@pytest.fixture
def holds() -> Any:
    from agent import auxiliary_client

    saved = dict(auxiliary_client._aux_unhealthy_until)
    auxiliary_client._aux_unhealthy_until.clear()
    yield auxiliary_client
    auxiliary_client._aux_unhealthy_until.clear()
    auxiliary_client._aux_unhealthy_until.update(saved)


def _quarantine(client: Any, err: str) -> None:
    client._quarantine_fallback_candidate("compression", PROVIDER, PROVIDER, TimeoutError(err),
                                          reason="request timed out")


def test_hermes_leaves_a_fallback_the_deadline_stopped_off_hold(holds: Any, known_failure: Any) -> None:
    _quarantine(holds, DEADLINE_ERR)
    held = holds._is_provider_unhealthy(PROVIDER)
    known_failure(held is False, "spent deadline held", f"{PROVIDER} on hold after {DEADLINE_ERR!r}")


def test_the_guards_are_on_hermes(holds: Any) -> None:
    from agent import conversation_compression

    spec = importlib.util.spec_from_file_location("conduit_helper_holds", ROOT / "helper_holds.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    own_quarantine = holds._quarantine_fallback_candidate
    own_notice = conversation_compression._emit_feasibility_notice
    # The warning the guard rewords is this Hermes' own.
    import inspect
    assert module.NO_PROVIDER in inspect.getsource(conversation_compression)
    try:
        assert module.install()
        _quarantine(holds, DEADLINE_ERR)
        assert not holds._is_provider_unhealthy(PROVIDER)
        # A fallback that timed out on its own is still held, as Hermes holds it.
        _quarantine(holds, "Request timed out.")
        assert holds._is_provider_unhealthy(PROVIDER)

        warned: list = []

        class Agent:
            def _emit_warning(self, msg: str) -> None:
                warned.append(msg)

        hermes_says = ("⚠ No auxiliary LLM provider configured: Hermes has no helper model for summarising "
                       "long chats, so older messages will be cut without a summary. Run `hermes setup` to add one.")
        conversation_compression._emit_feasibility_notice(Agent(), hermes_says)
        assert warned and PROVIDER in warned[-1] and "paused" in warned[-1] and "hermes setup" not in warned[-1]
        # Nothing on hold: Hermes' own words stand.
        holds._aux_unhealthy_until.clear()
        conversation_compression._emit_feasibility_notice(Agent(), hermes_says)
        assert warned[-1] == hermes_says
    finally:
        module.uninstall()
    assert holds._quarantine_fallback_candidate is own_quarantine
    assert conversation_compression._emit_feasibility_notice is own_notice
