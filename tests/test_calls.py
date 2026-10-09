import importlib.util
import json
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
        # What the relay answers once the failures are used up.
        self.answers = []
        self.sent = []
        self.enqueued = []

    def send_now(self, event):
        self.sent.append(event)
        if self.failures:
            raise self.failures.pop(0)
        if self.answers:
            return self.answers.pop(0)
        return {"accepted": True}

    def enqueue(self, event):
        self.enqueued.append(event)
        return True


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(calls, "_sleep", lambda seconds, wake=None: waited.append(seconds))
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
    fake.failures = [_Rejected(400, "invalid_event_type")]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 1
    assert fake.enqueued == [READY]


def _Rejected(status, detail="request_rejected"):
    return sys.modules["conduit_push.client"].RelayRejected(status, detail)


def test_a_relay_that_fails_to_handle_the_call_is_tried_again(home, fake, sleeps):
    fake.failures = [_Rejected(503), _Rejected(500)]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 3
    assert fake.enqueued == []


def test_a_retry_the_relay_answers_as_a_duplicate_sends_the_usual_push(home, fake, sleeps):
    # The relay took the event before failing, so a retry is only deduped.
    fake.failures = [_Rejected(500, "internal_error")]
    fake.answers = [{"accepted": True, "duplicate": True}]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 2
    assert fake.enqueued == [READY]


@pytest.mark.parametrize("detail", ["apns_unreachable", "apns_rejected"])
def test_a_call_apple_didnt_take_sends_the_usual_push_at_once(home, fake, sleeps, detail):
    fake.failures = [_Rejected(502, detail)]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 1
    assert fake.enqueued == [READY]


def test_a_duplicate_after_a_dropped_connection_counts_as_sent(home, fake, sleeps):
    fake.failures = [OSError("reset")]
    fake.answers = [{"accepted": True, "duplicate": True}]
    _watch(home, "rt-1")
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert len(fake.sent) == 2
    assert fake.enqueued == []


def test_relay_errors_carry_their_status_and_a_bounded_detail(monkeypatch):
    import io
    import urllib.error

    real = sys.modules["conduit_push.client"]

    def refuse(body):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(body))
        return urlopen

    monkeypatch.setattr(real.urllib.request, "urlopen", refuse(b'{"error": "apns_unreachable"}'))
    with pytest.raises(real.RelayRejected) as caught:
        real.request_json("https://relay.example/v1/events", method="POST", payload={})
    assert (caught.value.status, caught.value.detail) == (502, "apns_unreachable")

    monkeypatch.setattr(real.urllib.request, "urlopen", refuse(b'{"error": {"nested": "' + b"x" * 500 + b'"}}'))
    with pytest.raises(real.RelayRejected) as caught:
        real.request_json("https://relay.example/v1/events", method="POST", payload={})
    assert isinstance(caught.value.detail, str) and len(caught.value.detail) == 200

    # A relay can't split or forge log lines through it.
    monkeypatch.setattr(real.urllib.request, "urlopen", refuse(b'{"error": "bad\\nWARNING forged\\r\\u001b[31m"}'))
    with pytest.raises(real.RelayRejected) as caught:
        real.request_json("https://relay.example/v1/events", method="POST", payload={})
    assert caught.value.detail == "bad WARNING forged [31m"

    monkeypatch.setattr(real.urllib.request, "urlopen", refuse(b'{"error": null}'))
    with pytest.raises(real.RelayRejected) as caught:
        real.request_json("https://relay.example/v1/events", method="POST", payload={})
    assert caught.value.detail == "request_rejected"

    monkeypatch.setattr(real.urllib.request, "urlopen", refuse(b"<html>proxy</html>"))
    with pytest.raises(real.RelayRejected) as caught:
        real.request_json("https://relay.example/v1/events", method="POST", payload={})
    assert caught.value.detail == "request_rejected"

    # However it is raised.
    raised = real.RelayRejected(500, "a\nb")
    assert raised.detail == "a b" and "\n" not in str(raised)
    assert real.RelayRejected(500, " \t ").detail == "request_rejected"


def test_a_rate_limited_call_is_not_tried_again(home, fake, sleeps):
    fake.failures = [_Rejected(429)]
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
    def sleep(seconds, wake=None):
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

    def sleep(seconds, wake=None):
        # Conduit hung up and released the watch while the waiter slept.
        assert calls._store(home).hold(watch_id, 0) == {"status": "ended", "outcome": "done"}
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    assert fake.sent == []


def test_a_watch_removed_during_the_call_never_calls(home, fake, clock, monkeypatch):
    watch_id = _held_watch(home, "rt-1")

    def sleep(seconds, wake=None):
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


def test_a_held_call_waiting_at_start_up_gets_its_waiter_back(home, fake, clock, monkeypatch):
    def sleep(seconds, wake=None):
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    _held_watch(home, "rt-1", hold_s=60)
    # The job ended during the call; then the agent restarted.
    assert calls._store(home).fire("rt-1", "done")["status"] == "held"
    calls.resume("default")
    assert [event["type"] for event in fake.sent] == ["call.requested"]


def test_start_up_with_nothing_held_starts_nothing(home, fake, monkeypatch):
    started = []
    monkeypatch.setattr(calls, "_spawn", started.append)
    calls.resume("default")
    assert started == []


def test_one_held_call_that_cannot_go_out_never_stops_the_rest(home, fake):
    good = {"status": "call", "outcome": "done", "session_id": "b", "watch": {"id": "1" * 24, "title": "B", "session_ids": ["b"]}}
    broken = {**good, "session_id": "a", "watch": None}  # building its event raises
    calls._send_due([broken, good], "default")
    assert [event["session_id"] for event in fake.sent] == ["b"]


# --- Calls Hermes asks for, and alert calls ----------------------------------

call_tool = sys.modules["conduit_push.call_tool"]


@pytest.fixture
def paired(monkeypatch):
    monkeypatch.setattr(call_tool.client, "load_state", lambda: {"credential": "x"})


def _settings(home, **settings):
    calls._store(home).update_settings({"enabled": True, **settings})


def _tool(args, session_id="st-1", **kwargs):
    return json.loads(call_tool.handle(args, session_id=session_id, **kwargs))


def test_the_tool_calls_when_the_asking_turn_ends_with_its_reason(home, fake, paired):
    _settings(home)
    answer = _tool({"reason": "The deploy finished; two tests failed.", "asked_by_user": True})
    assert answer["ok"] is True and answer["status"] == "call_at_turn_end"
    assert fake.sent == [], "nothing rings before the turn ends"
    plugin._post_llm_call(session_id="st-1", turn_id="t1", assistant_response="Here's what happened…")
    [event] = fake.sent
    assert event["type"] == "call.requested"
    assert event["body"] == "The deploy finished; two tests failed."
    assert event["call"]["kind"] == "done"
    assert event["call"]["reason"] == "The deploy finished; two tests failed."
    assert event["call"]["session_ids"] == ["st-1"]
    assert "title" not in event["call"]
    assert fake.enqueued == [], "the call takes the ready push's place"


def test_the_tool_follows_the_users_settings(home, fake, paired):
    assert _tool({"reason": "Done.", "asked_by_user": True})["error"] == "calls_off"
    _settings(home)
    assert _tool({"reason": "Heads up.", "asked_by_user": False})["error"] == "calls_off", "Hermes decides is off"
    _settings(home, decides=True)
    assert _tool({"reason": "Heads up.", "asked_by_user": False})["ok"] is True


def test_the_tool_refuses_what_cannot_ring(home, fake, monkeypatch):
    _settings(home)
    monkeypatch.setattr(call_tool.client, "load_state", lambda: None)
    assert _tool({"reason": "Done.", "asked_by_user": True})["error"] == "not_paired"
    monkeypatch.setattr(call_tool.client, "load_state", lambda: {"credential": "x"})
    assert _tool({"reason": " ", "asked_by_user": True})["error"] == "no_reason"
    assert _tool({"reason": "Done.", "asked_by_user": True}, session_id=None)["error"] == "no_session"
    assert _tool({"reason": "Done.", "asked_by_user": True}, is_child=lambda s: s == "st-1")["error"] == "subagent"
    assert calls._store(home).watch_count() == 0


def test_the_tool_never_raises(home, fake, paired, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk")
    monkeypatch.setattr(calls, "tool_watch", broken)
    assert _tool({"reason": "Done.", "asked_by_user": True})["error"] == "unavailable"


def test_the_tool_shows_only_while_calls_it_can_ask_for_are_on(home, paired, monkeypatch):
    assert call_tool.available() is False
    _settings(home, when_asked=False)
    assert call_tool.available() is False
    _settings(home, decides=True)
    assert call_tool.available() is True
    monkeypatch.setattr(call_tool.client, "load_state", lambda: None)
    assert call_tool.available() is False


def test_register_adds_the_tool_and_its_skill():
    tools, skills = [], []
    ctx = types.SimpleNamespace(profile_name="default", register_hook=lambda name, fn: None,
                                register_cli_command=lambda **kwargs: None,
                                register_tool=lambda **kwargs: tools.append(kwargs),
                                register_skill=lambda name, path: skills.append((name, path)))
    plugin.register(ctx)
    [tool] = tools
    assert (tool["name"], tool["toolset"], tool["schema"]["name"]) == ("conduit_call_user", "conduit", "conduit_call_user")
    assert tool["schema"]["parameters"]["required"] == ["reason", "asked_by_user"]
    [(name, path)] = skills
    assert name == "calling-the-user" and path.is_file()
    assert "conduit_push:calling-the-user" in tool["schema"]["description"]
    assert path.read_text().startswith("---\nname: calling-the-user\n")


def test_a_hermes_without_the_display_fields_still_gets_the_tool(home, paired):
    tools = []

    def register_tool(name, toolset, schema, handler, check_fn=None):
        tools.append(handler)
    ctx = types.SimpleNamespace(profile_name="default", register_hook=lambda name, fn: None,
                                register_cli_command=lambda **kwargs: None, register_tool=register_tool)
    plugin.register(ctx)
    [handler] = tools
    _settings(home)
    # Hermes passes the agent itself as parent_agent; only a child session is refused.
    assert json.loads(handler({"reason": "Done.", "asked_by_user": True}, session_id="st-1", parent_agent=object(),
                              task_id="t"))["ok"] is True


def test_a_hermes_that_refuses_the_tool_keeps_the_hooks():
    hooks = []

    def refuse(**kwargs):
        raise TypeError("unexpected keyword")
    ctx = types.SimpleNamespace(profile_name="default", register_hook=lambda name, fn: hooks.append(name),
                                register_cli_command=lambda **kwargs: None, register_tool=refuse,
                                register_skill=lambda name, path: (_ for _ in ()).throw(ValueError("no")))
    plugin.register(ctx)
    assert "post_llm_call" in hooks and "post_approval_response" in hooks


def test_an_unanswered_approval_calls_beside_its_notification(home, fake, clock, monkeypatch):
    def sleep(seconds, wake=None):
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    _settings(home, alerts=True)
    plugin._pre_approval_request(session_key="sk-1", description="Run the migration", turn_id="t1")
    assert [event["type"] for event in fake.enqueued] == ["approval.needed"]
    [event] = fake.sent
    assert event["type"] == "call.requested"
    assert event["body"] == "Hermes needs your OK: Run the migration"
    assert (event["call"]["kind"], event["call"]["reason"], event["call"]["session_ids"]) == ("approval", "Run the migration", ["sk-1"])


def test_an_alert_due_before_a_held_call_is_not_kept_waiting(home, fake, clock, monkeypatch):
    naps = []

    def sleep(seconds, wake=None):
        naps.append(seconds)
        if len(naps) == 1:
            # The waiter sleeps on a long hold; an approval comes in meanwhile.
            plugin._pre_approval_request(session_key="sk-1", description="Run the migration", turn_id="t1")
            assert wake is not None and wake.is_set()
            return
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    _held_watch(home, "rt-1", hold_s=300)
    _settings(home, alerts=True)
    calls.turn_ended("rt-1", "done", profile="default", fallback=READY)
    # Woken, the waiter takes the approval at its minute, not the hold's end:
    # busy then (the user is still in the call), so only its notification.
    assert naps[:2] == [301.0, 61.0]
    assert [event["call"]["kind"] for event in fake.sent] == ["done"]


def test_an_answered_approval_does_not_call(home, fake, clock, monkeypatch):
    def sleep(seconds, wake=None):
        # The user answered from the notification while the waiter slept.
        plugin._post_approval_response(session_key="sk-1", choice="once")
        clock[0] += seconds
    monkeypatch.setattr(calls, "_sleep", sleep)
    _settings(home, alerts=True)
    plugin._pre_approval_request(session_key="sk-1", description="Run the migration", turn_id="t1")
    assert fake.sent == []


def test_approvals_do_not_call_with_alerts_off(home, fake, monkeypatch):
    started = []
    monkeypatch.setattr(calls, "_wait_for_holds", lambda home, profile: started.append(home))
    _settings(home)
    plugin._pre_approval_request(session_key="sk-1", description="Run the migration", turn_id="t1")
    assert started == [] and calls._store(home).watch_count() == 0


def test_a_replayed_failure_hook_is_the_same_call_to_the_relay(home, fake, clock):
    _settings(home, alerts=True)
    plugin._on_session_end(session_id="st-9", turn_id="t1", completed=False, interrupted=False)
    clock[0] += 3_600
    plugin._on_session_end(session_id="st-9", turn_id="t1", completed=False, interrupted=False)
    clock[0] += 3_600
    plugin._on_session_end(session_id="st-9", turn_id="t2", completed=False, interrupted=False)
    first, replay, other = fake.sent
    # The relay rings an event id once: the replay never rings again.
    assert replay["event_id"] == first["event_id"]
    assert other["event_id"] != first["event_id"]


def test_a_failure_hook_replayed_at_once_rings_once_and_sends_no_push(home, fake, clock):
    _settings(home, alerts=True)
    plugin._on_session_end(session_id="st-9", turn_id="t1", completed=False, interrupted=False)
    clock[0] += 5
    # Inside the gap the first call opened: the same decision, not "limited".
    plugin._on_session_end(session_id="st-9", turn_id="t1", completed=False, interrupted=False)
    first, replay = fake.sent
    assert replay["event_id"] == first["event_id"]
    assert fake.enqueued == [], "no failure notification beside the ring"
    assert len(calls._store(home)._load()["history"]) == 1, "counted once"
    # Another turn in that gap is held back as usual.
    plugin._on_session_end(session_id="st-9", turn_id="t2", completed=False, interrupted=False)
    assert [event["type"] for event in fake.enqueued] == ["turn.failed"]


def test_a_failed_turn_calls_in_place_of_its_push_with_alerts_on(home, fake):
    _settings(home, alerts=True)
    plugin._on_session_end(session_id="st-9", turn_id="t1", completed=False, interrupted=False)
    [event] = fake.sent
    assert (event["call"]["kind"], event["body"]) == ("failed", "Hermes ran into a problem with your request.")
    assert fake.enqueued == []
    # Within the gap: the usual failure push.
    plugin._on_session_end(session_id="st-8", turn_id="t2", completed=False, interrupted=False)
    assert [event["type"] for event in fake.enqueued] == ["turn.failed"]


def test_nothing_rings_during_a_live_voice_call(home, fake, paired):
    _settings(home)
    _tool({"reason": "Done.", "asked_by_user": True})
    calls._store(home).set_presence(90)
    plugin._post_llm_call(session_id="st-1", turn_id="t1", assistant_response="Done.")
    assert fake.sent == []
    assert [event["type"] for event in fake.enqueued] == ["response.ready"]


def test_a_question_left_unanswered_calls_and_an_answer_stops_it(home, fake, clock, monkeypatch, paired):
    loop = sys.modules["conduit_push.clarify_loop"]
    _settings(home, alerts=True)
    scheduled = []
    monkeypatch.setattr(calls, "_wait_for_holds", lambda home, profile: scheduled.append(home))
    monkeypatch.setattr(loop.client, "enqueue", lambda event: True)
    monkeypatch.setattr(loop, "_first_answer_wins", lambda **kwargs: (
        scheduled.append(calls._store(home).next_due()) or "answered"))
    result = loop.middleware(session_id="st-1", tool_name="clarify", args={"question": "Which branch?", "choices": ["main", "dev"]},
                             next_call=lambda args: "native")
    assert result == "answered"
    assert scheduled == [home, clock[0] + 60], "the alert waited a minute while the question was open"
    assert calls._store(home).watch_count() == 0, "the answer cancelled it"


def test_a_batch_of_questions_says_how_many_wait(home, fake, monkeypatch, paired):
    loop = sys.modules["conduit_push.clarify_loop"]
    _settings(home, alerts=True)
    reasons = []
    monkeypatch.setattr(calls, "_wait_for_holds", lambda home, profile: None)
    monkeypatch.setattr(loop.client, "enqueue", lambda event: True)
    monkeypatch.setattr(loop, "_first_answer_wins", lambda **kwargs: (
        reasons.extend(watch["reason"] for watch in calls._store(home)._load()["watches"]) or "answered"))
    questions = [{"question": "Which branch?", "choices": ["main", "dev"]}, {"question": "Ship it?", "choices": ["yes", "no"]}]
    loop.middleware(session_id="st-1", tool_name="clarify", args={"questions": questions}, next_call=lambda args: "native")
    assert reasons == ["2 questions, first: Which branch?"]
