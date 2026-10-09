import importlib.util
import pathlib
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "tools" not in sys.modules:
    _tools = types.ModuleType("tools")
    _tools.__path__ = []
    sys.modules["tools"] = _tools
if "tools.clarify_tool" not in sys.modules:
    # Same stand-in as test_clarify_loop (whichever file loads first wins).
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


calls = _load("conduit_push.calls", ROOT / "calls.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
CallStore = sys.modules["conduit_push.calls_store"].CallStore


class FakeClient:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent = []
        self.enqueued = []

    def send_now(self, event):
        self.sent.append(event)
        if self.failures:
            raise self.failures.pop(0)
        return {"accepted": True}

    def enqueue(self, event):
        self.enqueued.append(event)
        return True


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(calls, "_sleep", waited.append)
    return waited


@pytest.fixture
def home(tmp_path, monkeypatch, sleeps):
    monkeypatch.setattr(calls, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(calls, "_spawn", lambda work: work())
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(calls, "client", fake)
    monkeypatch.setattr(plugin, "enqueue", fake.enqueue)
    return fake


def _watch(home, *session_ids, title="Check the server", **settings):
    store = CallStore(home)
    store.update_settings({"enabled": True, **settings})
    return store.add_watch(list(session_ids), title)["id"]


READY = {"event_id": "response:1", "type": "response.ready"}


def test_a_watched_job_that_finishes_sends_a_call_request_instead(home, fake):
    watch_id = _watch(home, "rt-1", "st-1")
    assert calls.turn_ended("st-1", "done", profile="default", fallback=READY) is True
    assert fake.enqueued == []
    [event] = fake.sent
    assert event["type"] == "call.requested"
    assert event["event_id"] == f"call:{watch_id}"
    assert event["session_id"] == "st-1"
    assert event["title"] == "Hermes wants to talk"
    assert event["body"] == "“Check the server” finished."
    assert event["call"] == {"id": watch_id, "kind": "done", "title": "Check the server", "session_ids": ["rt-1", "st-1"]}


def test_failed_and_stopped_jobs_say_so(home, fake):
    _watch(home, "a", title="")
    _watch(home, "b", min_gap_s=30)
    calls.turn_ended("a", "failed", profile="default", fallback=None)
    assert fake.sent[-1]["body"] == "Your job failed."
    CallStore(home).update_settings({"min_gap_s": 30})
    # The gap holds the second one back; the outcome copy is what matters.
    event = calls.call_event({"id": "0123456789abcdef", "title": "B", "session_ids": ["b"]}, "stopped",
                             session_id="b", profile="default")
    assert event["body"] == "“B” stopped before finishing."
    assert event["call"]["kind"] == "stopped"


def test_an_unwatched_turn_is_not_a_call(home, fake):
    _watch(home, "rt-1")
    assert calls.turn_ended("rt-2", "done", profile="default", fallback=READY) is False
    assert fake.sent == []


def test_a_held_back_call_leaves_the_usual_push_to_the_hook(home, fake):
    _watch(home, "a")
    _watch(home, "b")
    assert calls.turn_ended("a", "done", profile="default", fallback=READY) is True
    assert calls.turn_ended("b", "done", profile="default", fallback=READY) is False  # inside the gap
    assert len(fake.sent) == 1


def test_a_replayed_turn_end_calls_once(home, fake):
    _watch(home, "rt-1")
    assert calls.turn_ended("rt-1", "done", profile="default", fallback=READY) is True
    assert calls.turn_ended("rt-1", "done", profile="default", fallback=READY) is False
    assert len(fake.sent) == 1


def test_transport_failures_retry_with_the_same_event(home, fake, sleeps):
    fake.failures = [OSError("reset"), OSError("reset")]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 3
    assert len({event["event_id"] for event in fake.sent}) == 1
    assert sleeps == [2.0, 4.0]
    assert fake.enqueued == []


def test_a_call_that_never_goes_out_sends_the_usual_push(home, fake):
    fake.failures = [OSError("down")] * 3
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert fake.enqueued == [READY]


def test_a_refused_call_sends_the_usual_push_at_once(home, fake):
    # An older relay doesn't know call.requested.
    fake.failures = [RuntimeError("Conduit relay rejected the request: invalid_event_type (400).")]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 1
    assert fake.enqueued == [READY]


def test_a_broken_watch_file_never_breaks_the_turn(home, fake, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("disk")

    monkeypatch.setattr(CallStore, "fire", broken)
    assert calls.turn_ended("rt-1", "done", profile="default", fallback=READY) is False


# --- The hooks -------------------------------------------------------------


def test_post_llm_call_keeps_the_ready_push_for_unwatched_sessions(home, fake):
    plugin._post_llm_call(session_id="rt-1", turn_id="t1", assistant_response="All done.")
    [event] = fake.enqueued
    assert event["type"] == "response.ready"
    assert event["body"] == "All done."


def test_post_llm_call_on_a_watched_job_calls_instead_of_notifying(home, fake):
    _watch(home, "rt-1")
    plugin._post_llm_call(session_id="rt-1", turn_id="t1", assistant_response="All done.")
    assert fake.enqueued == []
    assert fake.sent[0]["type"] == "call.requested"
    # on_session_end for the same finished turn finds the watch gone.
    plugin._on_session_end(session_id="rt-1", turn_id="t1", completed=True, interrupted=False)
    assert len(fake.sent) == 1
    assert fake.enqueued == []


def test_on_session_end_reports_a_watched_failure_as_a_call(home, fake):
    _watch(home, "rt-1")
    plugin._on_session_end(session_id="rt-1", turn_id="t1", completed=False, interrupted=False)
    assert fake.sent[0]["call"]["kind"] == "failed"
    assert fake.enqueued == []


def test_on_session_end_reports_a_watched_stop_as_a_call(home, fake):
    _watch(home, "rt-1")
    plugin._on_session_end(session_id="rt-1", turn_id="t1", completed=False, interrupted=True)
    assert fake.sent[0]["call"]["kind"] == "stopped"


def test_on_session_end_keeps_the_failure_push_for_unwatched_sessions(home, fake):
    plugin._on_session_end(session_id="rt-1", turn_id="t1", completed=False, interrupted=False)
    assert [event["type"] for event in fake.enqueued] == ["turn.failed"]
    plugin._on_session_end(session_id="rt-1", turn_id="t2", completed=False, interrupted=True)
    plugin._on_session_end(session_id="rt-1", turn_id="t3", completed=True, interrupted=False)
    assert len(fake.enqueued) == 1


def test_a_silent_reply_on_a_watched_job_still_calls(home, fake):
    _watch(home, "rt-1")
    plugin._post_llm_call(session_id="rt-1", turn_id="t1", assistant_response="[Silent]")
    assert fake.sent[0]["type"] == "call.requested"


def test_a_subagent_turn_never_fires_a_watch(home, fake):
    _watch(home, "child-1")
    plugin._subagent_start(child_session_id="child-1")
    try:
        plugin._post_llm_call(session_id="child-1", turn_id="t1", assistant_response="x")
        plugin._on_session_end(session_id="child-1", turn_id="t1", completed=False, interrupted=False)
    finally:
        plugin._child_sessions.discard("child-1")
    assert fake.sent == []
    assert CallStore(home).watch_count() == 1


# --- Held watches: registered during the call ------------------------------


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(calls, "_clock", lambda: now[0])
    return now


def _held_watch(home, *session_ids, hold_s=180, title="Check the server"):
    store = calls._store(home)
    store.update_settings({"enabled": True})
    return store.add_watch(list(session_ids), title, hold_s=hold_s)["id"]


def test_a_job_ending_during_the_call_gets_its_usual_push(home, fake, clock, monkeypatch):
    monkeypatch.setattr(calls, "_wait_for_holds", lambda home, profile: None)
    _held_watch(home, "rt-1")
    assert calls.turn_ended("rt-1", "done", profile="default", fallback=READY) is False
    assert fake.sent == []


def test_a_hold_that_runs_out_calls_without_a_second_push(home, fake, clock, monkeypatch):
    # The phone went away mid-call: nobody renews or releases the hold.
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    watch_id = _held_watch(home, "rt-1", "st-1")
    assert calls.turn_ended("st-1", "done", profile="default", fallback=READY) is False
    [event] = fake.sent
    assert event["type"] == "call.requested"
    assert event["event_id"] == f"call:{watch_id}"
    assert event["session_id"] == "st-1"
    assert event["body"] == "“Check the server” finished."
    assert fake.enqueued == []


def test_a_hold_released_at_hang_up_leaves_the_call_to_conduit(home, fake, clock, monkeypatch):
    watch_id = _held_watch(home, "rt-1")

    def sleep(seconds):
        # Conduit hung up and released the watch while the waiter slept.
        assert calls._store(home).hold(watch_id, 0) == {"status": "ended", "outcome": "done"}
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert fake.sent == []


def test_a_watch_removed_during_the_call_never_calls(home, fake, clock, monkeypatch):
    watch_id = _held_watch(home, "rt-1")

    def sleep(seconds):
        # Conduit told the user in the call and removed the watch.
        calls._store(home).remove_watch(watch_id)
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert fake.sent == []


def test_the_next_turn_end_sweeps_a_call_a_restart_dropped(home, fake, clock, monkeypatch):
    monkeypatch.setattr(calls, "_wait_for_holds", lambda home, profile: None)
    _held_watch(home, "rt-1", hold_s=60)
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    clock[0] += 61
    # Any other session's turn end finds the overdue call.
    assert calls.turn_ended("other", "done", profile="default", fallback=READY) is False
    assert [event["type"] for event in fake.sent] == ["call.requested"]
    assert fake.sent[0]["session_id"] == "rt-1"


def test_a_sweep_still_sends_when_this_turns_check_fails(home, fake, clock, monkeypatch):
    monkeypatch.setattr(calls, "_wait_for_holds", lambda home, profile: None)
    _held_watch(home, "rt-1", hold_s=60)
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    clock[0] += 61

    def broken(self, session_id, outcome):
        raise OSError("disk full")
    monkeypatch.setattr(CallStore, "fire", broken)
    assert calls.turn_ended("other", "done", profile="default", fallback=READY) is False
    assert [event["type"] for event in fake.sent] == ["call.requested"]


def test_a_waiter_that_cannot_start_lets_the_next_turn_try(home, fake, clock, monkeypatch):
    def no_threads(work):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(calls, "_spawn", no_threads)
    _held_watch(home, "rt-1")
    # The turn goes on, and the home isn't left marked as waiting.
    assert calls.turn_ended("rt-1", "done", profile="default", fallback=READY) is False
    assert str(home) not in calls._waiting


def test_a_call_that_cannot_be_spawned_leaves_the_usual_push(home, fake, monkeypatch):
    def no_threads(work):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(calls, "_spawn", no_threads)
    _watch(home, "rt-1")
    assert calls.turn_ended("rt-1", "done", profile="default", fallback=READY) is False
