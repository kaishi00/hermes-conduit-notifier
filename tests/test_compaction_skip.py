"""A hidden turn still runs when Hermes compacts its chat (compaction_skip)."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIPPED = "Context was compacted. The previous response is complete — awaiting your next message."


@pytest.fixture
def skip(monkeypatch):
    """compaction_skip, loaded fresh, beside a stand-in for Hermes' loop whose
    check skips whenever the last row isn't a plain user ask (v0.23.0's
    verdict for a handoff followed by a hidden ask, or by nothing;
    upstream/test_compaction_skip.py holds the real one to it)."""
    loop = types.ModuleType("agent.conversation_loop")
    calls = []

    def check(messages, user_message):
        calls.append((messages, user_message))
        last = messages[-1] if messages else {}
        return not (last.get("role") == "user" and not last.get("display_kind") and last is not HANDOFF)

    loop._should_skip_model_call_for_reference_handoff = check
    loop._HANDOFF_SKIP_FINAL_RESPONSE = SKIPPED
    loop._COMPRESSION_TIMEOUT_FINAL_RESPONSE = "Compression timed out, worded anew."
    agent = types.ModuleType("agent")
    agent.conversation_loop = loop
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", loop)
    spec = importlib.util.spec_from_file_location("conduit_push_compaction_skip_test", ROOT / "compaction_skip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.loop, module.own, module.calls = loop, check, calls
    yield module
    module.uninstall()


HANDOFF = {"role": "user", "content": "[CONTEXT COMPACTION] earlier work"}


def test_a_hidden_turns_own_ask_goes_on_to_the_model(skip):
    assert skip.install()
    check = skip.loop._should_skip_model_call_for_reference_handoff
    ask = "Pulse report pAB-1."
    assert check([HANDOFF, {"role": "user", "content": ask, "display_kind": "hidden"}], ask) is False
    # Hermes' verdict stands for anything else: an ask it already counts, a
    # handoff alone, another hidden row than this turn's, another display kind.
    assert check([HANDOFF, {"role": "user", "content": ask}], ask) is False
    assert check([HANDOFF], ask) is True
    assert check([HANDOFF, {"role": "user", "content": "older", "display_kind": "hidden"}], ask) is True
    assert check([HANDOFF, {"role": "user", "content": ask, "display_kind": "notification"}], ask) is True
    assert check([HANDOFF, {"role": "assistant", "content": "done"}], ask) is True
    assert len(skip.calls) == 6  # Hermes' own check always runs first


def test_install_is_once_and_uninstall_puts_hermes_check_back(skip):
    assert skip.install() and skip.install()
    guard = skip.loop._should_skip_model_call_for_reference_handoff
    assert guard is not skip.own and guard.__wrapped__ is skip.own
    skip.uninstall()
    assert skip.loop._should_skip_model_call_for_reference_handoff is skip.own
    # A guard an earlier load left behind is unwrapped, never stacked.
    skip.loop._should_skip_model_call_for_reference_handoff = guard
    assert skip.install()
    assert skip.loop._should_skip_model_call_for_reference_handoff.__wrapped__ is skip.own


def test_a_hermes_without_the_check_is_left_alone(skip):
    del skip.loop._should_skip_model_call_for_reference_handoff
    assert skip.install() is False


def test_hermes_status_lines_are_told_apart_from_replies(skip):
    assert skip.notice(SKIPPED) == skip.SKIPPED_DETAIL
    assert skip.notice("  Context was compacted.  The previous response is complete — awaiting\nyour next message. ") \
        == skip.SKIPPED_DETAIL
    # As this Hermes words it, and as v0.23.0 did.
    assert skip.notice("Compression timed out, worded anew.") == skip.TIMED_OUT_DETAIL
    assert skip.notice(skip.TIMED_OUT) == skip.TIMED_OUT_DETAIL
    assert skip.notice("Context was compacted. Here's your report: 1. [date] Mum's birthday") == ""
    assert skip.notice(None) == "" and skip.notice("") == ""
