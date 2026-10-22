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


live = _load("conduit_push.live_activity", ROOT / "live_activity.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
client = sys.modules["conduit_push.client"]


class Relay:
    def __init__(self, tracked=True):
        self.tracked = tracked
        self.posts = []
        self.failure = None

    def __call__(self, payload):
        self.posts.append(payload)
        if self.failure is not None:
            raise self.failure
        return {"tracked": self.tracked}


@pytest.fixture
def clock():
    return [100.0]


@pytest.fixture
def relay():
    return Relay()


@pytest.fixture
def reporter(clock, relay):
    return live.LiveActivityReporter(post=relay, clock=lambda: clock[0], start_thread=False)


def test_tool_names_become_coarse_categories():
    assert live.tool_category("terminal") == "terminal"
    assert live.tool_category("browser_click") == "browser"
    assert live.tool_category("mcp_github_create_issue") == "connector"
    assert live.tool_category("web_search") == "web"
    assert live.tool_category("my_secret_project_tool") == "tool"
    assert live.tool_category(None) == "tool"


def test_the_first_report_waits_a_moment_then_carries_the_turn(reporter, relay, clock):
    reporter.turn_started("s-1")
    reporter.tool_started("s-1", "terminal")
    reporter.context_used("s-1", 41_000, 100_000)
    assert reporter.flush() == pytest.approx(live.FIRST_DELAY_S)
    assert relay.posts == []
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert relay.posts == [{"session_id": "s-1", "state": {"phase": "tool", "tools": 1, "tool": "terminal", "context": 41}}]


def test_a_watched_turn_reports_at_most_every_interval(reporter, relay, clock):
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    reporter.tool_started("s-1", "read_file")
    reporter.tool_started("s-1", "web_search")
    assert reporter.flush() == pytest.approx(live.MIN_INTERVAL_S)
    assert len(relay.posts) == 1
    clock[0] += live.MIN_INTERVAL_S
    reporter.flush()
    assert relay.posts[-1]["state"] == {"phase": "tool", "tools": 2, "tool": "web"}


def test_a_turn_nobody_watches_is_asked_about_rarely(reporter, relay, clock):
    relay.tracked = False
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    reporter.thinking("s-1")
    reporter.tool_started("s-1", "terminal")
    clock[0] += live.MIN_INTERVAL_S
    reporter.flush()
    assert len(relay.posts) == 1
    clock[0] += live.UNTRACKED_RETRY_S
    reporter.flush()
    assert len(relay.posts) == 2


def test_the_end_goes_out_at_once_and_only_once(reporter, relay, clock):
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    reporter.tool_started("s-1", "terminal")
    reporter.turn_ended("s-1", "done")
    reporter.flush()
    assert relay.posts[-1] == {"session_id": "s-1", "state": {"phase": "tool", "tools": 1, "tool": "terminal"}, "end": "done"}
    # Hermes also fires on_session_end after post_llm_call.
    reporter.turn_ended("s-1", "done")
    reporter.flush()
    assert len(relay.posts) == 2
    # Updates after the end are dropped; a new turn starts fresh.
    reporter.thinking("s-1")
    reporter.flush()
    assert len(relay.posts) == 2
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert relay.posts[-1] == {"session_id": "s-1", "state": {"phase": "thinking", "tools": 0}}


def test_waiting_on_the_user_and_failures(reporter, relay, clock):
    reporter.turn_started("s-1")
    reporter.waiting("s-1", "approval")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert relay.posts[-1]["state"]["phase"] == "approval"
    reporter.tool_started("s-1", "clarify")
    clock[0] += live.MIN_INTERVAL_S
    reporter.flush()
    assert relay.posts[-1]["state"]["phase"] == "input"
    reporter.turn_ended("s-1", "failed")
    reporter.flush()
    assert relay.posts[-1]["end"] == "failed"


def test_a_relay_without_the_route_turns_reporting_off_for_a_while(reporter, relay, clock):
    relay.failure = client.RelayRejected(404, "not_found")
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    relay.failure = None
    reporter.turn_started("s-2")
    clock[0] += 60
    reporter.flush()
    assert len(relay.posts) == 1
    clock[0] += live.UNSUPPORTED_S
    reporter.thinking("s-2")
    reporter.flush()
    assert relay.posts[-1]["session_id"] == "s-2"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_caller_pauses_every_turn_instead_of_retrying(reporter, relay, clock, status):
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    relay.failure = client.RelayRejected(status, "request_rejected")
    reporter.turn_ended("s-1", "done")
    reporter.flush()
    reporter.turn_started("s-2")
    for _ in range(30):
        clock[0] += live.END_RETRY_S
        reporter.flush()
    assert len(relay.posts) == 2
    relay.failure = None
    clock[0] += live.REFUSED_S
    reporter.thinking("s-2")
    reporter.flush()
    assert relay.posts[-1]["session_id"] == "s-2"
    assert len(relay.posts) == 3


def test_a_refusal_holds_back_the_rest_of_the_batch(reporter, relay, clock):
    for session_id in ("s-1", "s-2", "s-3"):
        reporter.turn_started(session_id)
    relay.failure = client.RelayRejected(403, "request_rejected")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert len(relay.posts) == 1
    relay.failure = None
    clock[0] += live.REFUSED_S
    reporter.flush()
    assert sorted(post["session_id"] for post in relay.posts[1:]) == ["s-1", "s-2", "s-3"]


def test_a_working_credential_clears_the_refusal(monkeypatch):
    state = {"relay_url": "https://relay.example", "credential": "c", "relay_rejected_at": "2026-10-21T00:00:00Z"}
    marked = []
    monkeypatch.setattr(client, "load_state", lambda: state)
    monkeypatch.setattr(client, "_mark_relay_rejected", lambda sent_with, rejected, **_: marked.append((sent_with, rejected)))
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: {"tracked": True})
    assert live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}}) == {"tracked": True}
    assert marked == [(state, False)]


def test_a_failed_refusal_mark_still_reports_the_relay_answer(monkeypatch):
    state = {"relay_url": "https://relay.example", "credential": "c", "relay_rejected_at": "x"}
    monkeypatch.setattr(client, "load_state", lambda: state)

    def broken(sent_with, rejected, **_):
        raise RuntimeError("state unavailable")

    monkeypatch.setattr(client, "_mark_relay_rejected", broken)
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: {"tracked": True})
    assert live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}}) == {"tracked": True}

    def refuse(url, **kwargs):
        raise client.RelayRejected(401, "unauthorized")

    monkeypatch.setattr(client, "request_json", refuse)
    with pytest.raises(client.RelayRejected):
        live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}})


def test_a_reload_starts_unpaused(reporter, relay, clock):
    relay.failure = client.RelayRejected(401, "unauthorized")
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    reporter.stop()
    reporter.start()
    relay.failure = None
    reporter.turn_started("s-2")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert relay.posts[-1]["session_id"] == "s-2"


def test_a_refusal_in_flight_across_a_reload_does_not_pause_the_new_reporter(reporter, relay, clock):
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    stale = []

    def post(payload):
        # The plugin reloads while this send is on its way, then it's refused.
        reporter.stop()
        reporter.start()
        stale.append(payload)
        raise client.RelayRejected(401, "unauthorized")

    reporter._post = post
    reporter.flush()
    reporter._post = relay
    reporter.turn_started("s-2")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert len(stale) == 1
    assert relay.posts[-1]["session_id"] == "s-2"


def test_a_refused_credential_tells_status_to_pair_again(monkeypatch):
    state = {"relay_url": "https://relay.example", "credential": "c"}
    marked = []
    monkeypatch.setattr(client, "load_state", lambda: state)
    monkeypatch.setattr(client, "_mark_relay_rejected", lambda sent_with, rejected, **_: marked.append((sent_with, rejected)))

    def refuse(url, **kwargs):
        raise client.RelayRejected(401, "unauthorized")

    monkeypatch.setattr(client, "request_json", refuse)
    with pytest.raises(client.RelayRejected):
        live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}})
    assert marked == [(state, True)]


def test_an_end_the_relay_did_not_take_is_sent_again(reporter, relay, clock):
    reporter.turn_started("s-1")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    relay.failure = client.RelayRejected(503, "unavailable")
    reporter.turn_ended("s-1", "done")
    reporter.flush()
    assert relay.posts[-1]["end"] == "done"
    # on_session_end's second end doesn't count as delivered either.
    reporter.turn_ended("s-1", "done")
    relay.failure = None
    assert reporter.flush() == pytest.approx(live.END_RETRY_S)
    clock[0] += live.END_RETRY_S
    reporter.flush()
    assert [post.get("end") for post in relay.posts] == [None, "done", "done"]
    clock[0] += live.BACKOFF_S
    reporter.flush()
    assert len(relay.posts) == 3, "delivered once, then forgotten"


def test_a_failed_update_is_retried_and_holds_up_only_its_own_turn(reporter, relay, clock):
    reporter.turn_started("s-1")
    reporter.tool_started("s-1", "terminal")
    relay.failure = OSError("connection reset")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert len(relay.posts) == 1
    relay.failure = None
    reporter.turn_started("s-2")
    reporter.waiting("s-2", "approval")
    reporter.turn_ended("s-2", "done")
    reporter.flush()
    assert relay.posts[-1] == {"session_id": "s-2", "state": {"phase": "approval", "tools": 0}, "end": "done"}, \
        "the other turn's backoff never holds up this one"
    posts = len(relay.posts)
    clock[0] += live.BACKOFF_S
    reporter.flush()
    assert len(relay.posts) == posts + 1
    assert relay.posts[-1]["session_id"] == "s-1"
    assert relay.posts[-1]["state"]["tool"] == "terminal", "the lost update's state is sent again"


def test_an_end_that_never_gets_through_is_given_up(reporter, relay, clock):
    reporter.turn_started("s-1")
    relay.failure = OSError("down")
    reporter.turn_ended("s-1", "done")
    reporter.flush()
    while clock[0] < 100 + live.END_GIVE_UP_S + live.END_RETRY_S:
        clock[0] += live.END_RETRY_S
        reporter.flush()
    relay.posts.clear()
    clock[0] += live.BACKOFF_S
    assert reporter.flush() is None
    assert relay.posts == []


def test_context_ignores_numbers_that_are_not_numbers(reporter, relay, clock):
    reporter.turn_started("s-1")
    reporter.context_used("s-1", float("nan"), 100_000)
    reporter.context_used("s-1", 10, float("inf"))
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert "context" not in relay.posts[-1]["state"]


def test_redaction_leaves_out_the_tool_category(monkeypatch):
    sent = []
    monkeypatch.setattr(client, "load_state", lambda: {"relay_url": "https://relay.example", "credential": "c", "redact_content": True})
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: sent.append((url, kwargs["payload"])) or {"tracked": True})
    assert live._post_to_relay({"session_id": "s", "state": {"phase": "tool", "tool": "terminal", "tools": 1}}) == {"tracked": True}
    assert sent == [("https://relay.example/v1/live-activity", {"session_id": "s", "state": {"phase": "tool", "tools": 1}})]


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))


def test_the_hooks_feed_the_reporter(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(live, "reporter", recorder)
    monkeypatch.setattr(plugin, "enqueue", lambda event: True)
    monkeypatch.setattr(plugin.calls, "outcome_note", lambda session_id: None)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: True)
    plugin._pre_llm_call(session_id="s-1", turn_id="t-1", conversation_history=[])
    plugin._pre_api_request(session_id="s-1")
    plugin._pre_tool_call(session_id="s-1", tool_name="terminal")
    plugin._post_api_request(session_id="s-1", usage={"prompt_tokens": 500}, context_length=1000)
    plugin._on_session_end(session_id="s-1", interrupted=True)
    assert recorder.calls == [
        ("turn_started", "s-1"),
        ("thinking", "s-1"),
        ("tool_started", "s-1", "terminal"),
        ("context_used", "s-1", 500, 1000),
        ("turn_ended", "s-1", "stopped"),
    ]


@pytest.mark.parametrize("approval_session_id", ["s-1", None])
def test_approval_progress_uses_session_id_without_changing_routing(monkeypatch, approval_session_id):
    recorder = Recorder()
    sent = []
    monkeypatch.setattr(live, "reporter", recorder)
    monkeypatch.setattr(plugin, "enqueue", lambda event: sent.append(event) or True)
    monkeypatch.setattr(plugin.calls, "alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin.calls, "cancel_alerts", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "_titled", lambda prefix, session_id: prefix)
    payload = {"session_key": "gateway-key", "description": "Run command?"}
    if approval_session_id is not None:
        payload["session_id"] = approval_session_id
    plugin._pre_approval_request(**payload)
    plugin._post_approval_response(**payload)
    expected = approval_session_id or "gateway-key"
    assert recorder.calls == [("waiting", expected, "approval"), ("thinking", expected)]
    assert sent[0]["decision"]["session_key"] == "gateway-key"


def test_a_broken_reporter_never_breaks_a_hook(monkeypatch):
    class Broken:
        def __getattr__(self, name):
            def fail(*args):
                raise RuntimeError("boom")
            return fail
    monkeypatch.setattr(live, "reporter", Broken())
    plugin._pre_api_request(session_id="s-1")
    plugin._post_api_request(session_id="s-1", usage={"prompt_tokens": 1}, context_length=2)


def test_registration_survives_a_hermes_without_the_request_hooks():
    hooks = []

    def register_hook(name, fn):
        if name in ("pre_api_request", "post_api_request"):
            raise ValueError("unknown hook")
        hooks.append(name)
    ctx = types.SimpleNamespace(profile_name="default", register_hook=register_hook,
                                register_cli_command=lambda **kwargs: None,
                                register_tool=lambda **kwargs: None, register_skill=lambda name, path: None)
    plugin.register(ctx)
    assert "pre_tool_call" in hooks and "post_llm_call" in hooks


def test_an_earlier_backoff_does_not_hold_up_the_end(reporter, relay, clock):
    reporter.turn_started("s-1")
    relay.failure = client.RelayRejected(429, "rate_limited")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    relay.failure = None
    reporter.turn_ended("s-1", "done")
    reporter.flush()
    assert relay.posts[-1]["end"] == "done"


def test_a_report_the_relay_calls_malformed_is_not_retried(reporter, relay, clock):
    reporter.turn_started("s-1")
    relay.failure = client.RelayRejected(400, "invalid_live_activity_report")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    relay.failure = None
    clock[0] += live.BACKOFF_S
    assert reporter.flush() is None
    assert len(relay.posts) == 1


def test_the_tool_count_stays_within_what_the_relay_takes(reporter, relay, clock):
    reporter.turn_started("s-1")
    for _ in range(10_005):
        reporter.tool_started("s-1", "terminal")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert relay.posts[-1]["state"]["tools"] == 10_000


def test_a_full_table_never_evicts_the_turn_just_started(reporter, relay, clock):
    relay.failure = OSError("down")
    for index in range(live.MAX_SESSIONS):
        reporter.turn_started(f"old-{index}")
        reporter.turn_ended(f"old-{index}", "done")
    reporter.turn_started("new")
    relay.failure = None
    reporter.thinking("new")
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert any(post["session_id"] == "new" for post in relay.posts)


def test_ends_of_unknown_turns_stay_within_the_session_cap(reporter, relay, clock):
    relay.failure = OSError("down")
    for index in range(live.MAX_SESSIONS + 10):
        reporter.turn_ended(f"s-{index}", "done")
    assert len(reporter._turns) == live.MAX_SESSIONS


def test_huge_context_numbers_never_raise(reporter, relay, clock):
    reporter.turn_started("s-1")
    reporter.context_used("s-1", 1e308, 1e-300)
    clock[0] += live.FIRST_DELAY_S
    reporter.flush()
    assert relay.posts[-1]["state"]["context"] == 100


def test_a_bare_403_marks_the_host_blocked_not_the_credential(monkeypatch):
    state = {"relay_url": "https://relay.example", "credential": "c"}
    marked = []
    monkeypatch.setattr(client, "load_state", lambda: state)
    monkeypatch.setattr(client, "_mark_relay_rejected",
                        lambda sent_with, rejected, field="relay_rejected_at", **_: marked.append((field, rejected)))

    def refuse(url, **kwargs):
        raise client.RelayRejected(403, "request_rejected", from_relay=False)

    monkeypatch.setattr(client, "request_json", refuse)
    with pytest.raises(client.RelayRejected):
        live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}})
    assert marked == [("relay_blocked_at", True)]

    state["relay_blocked_at"] = "2026-10-21T00:00:00Z"
    marked.clear()
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: {"tracked": True})
    live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}})
    assert marked == [("relay_blocked_at", False)]


def test_an_accepted_report_ends_the_notification_pause(monkeypatch):
    state = {"relay_url": "https://relay.example", "credential": "c"}
    monkeypatch.setattr(client, "load_state", lambda: state)
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: {"tracked": True})
    client._refusals["c"] = {"paused_for": 60.0, "until": 1e12, "edge_blocked": True}
    try:
        live._post_to_relay({"session_id": "s", "state": {"phase": "thinking", "tools": 0}})
        assert "c" not in client._refusals
    finally:
        client._refusals.pop("c", None)
