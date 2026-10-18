"""One notice per turn: a failed turn's error text isn't a reply, and a
subagent that ends inside its parent's turn is part of that reply."""
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
if "tools.clarify_tool" not in sys.modules:
    _tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    _tools.__path__ = getattr(_tools, "__path__", [])
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        # Same semantics as test_clarify_loop's stub, whichever module loads first.
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


plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
events = sys.modules["conduit_push.events"]


@pytest.fixture
def pushed(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin.calls, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin.calls, "failed_turn", lambda *args, **kwargs: False)
    monkeypatch.setattr(plugin, "_desktop_watched", lambda session_id, started: False)
    monkeypatch.setattr(plugin, "_session_title", lambda session_id: "")
    monkeypatch.setattr(plugin, "_titled", lambda title, session_id: title)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    monkeypatch.setattr(plugin, "_parent_turns", {})
    sent = []
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    return sent


def _types(sent):
    return [event["type"] for event in sent]


def test_a_finished_turn_sends_its_reply_when_it_ends(pushed):
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="**Done**, see [log](https://x/1).")
    assert pushed == [], "the reply waits for the turn's end"
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    [event] = pushed
    assert event["type"] == "response.ready"
    assert event["body"] == "Done, see log."


def test_a_failed_turn_sends_only_its_failure(pushed):
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="The provider returned an error.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=False, failed=True, interrupted=False)
    assert _types(pushed) == ["turn.failed"]


def test_an_older_hermes_without_failed_counts_an_unfinished_turn_failed(pushed):
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Error.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=False, interrupted=False)
    assert _types(pushed) == ["turn.failed"]


def test_a_turn_stopped_at_the_step_limit_still_answered(pushed):
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="I ran out of steps; here's where I got.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=False, failed=False, interrupted=False)
    assert _types(pushed) == ["response.ready"]


def test_a_reply_whose_turn_end_never_comes_goes_out_after_the_grace(pushed, monkeypatch):
    later = []
    monkeypatch.setattr(plugin, "_later", lambda delay, work: later.append((delay, work)))
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    [(delay, work)] = later
    assert delay == plugin.READY_GRACE_S
    work()
    assert _types(pushed) == ["response.ready"]
    # The late end finds nothing held, so nothing goes out twice.
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    work()
    assert _types(pushed) == ["response.ready"]


def test_held_replies_are_per_chat(pushed):
    plugin._post_llm_call(session_id="a", turn_id="t", assistant_response="For A.")
    plugin._post_llm_call(session_id="b", turn_id="t", assistant_response="For B.")
    plugin._on_session_end(session_id="b", turn_id="t", completed=False, failed=True, interrupted=False)
    plugin._on_session_end(session_id="a", turn_id="t", completed=True, failed=False, interrupted=False)
    assert [(event["type"], event.get("body")) for event in pushed] == [
        ("turn.failed", None), ("response.ready", "For A.")]


def test_the_grace_timer_stops_once_the_turn_ends(pushed, monkeypatch):
    class Timer:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    timer = Timer()
    monkeypatch.setattr(plugin, "_later", lambda delay, work: timer)
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    assert timer.cancelled


def test_too_many_held_replies_send_the_oldest(pushed, monkeypatch):
    monkeypatch.setattr(plugin, "MAX_TURN_STARTS", 2)
    for turn in ("t1", "t2", "t3"):
        plugin._post_llm_call(session_id="s", turn_id=turn, assistant_response=turn)
    assert [event["body"] for event in pushed] == ["t1"]


def test_an_end_without_its_turn_id_settles_the_chats_held_reply(pushed):
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Error text.")
    plugin._on_session_end(session_id="s", completed=False, failed=True, interrupted=False)
    assert _types(pushed) == ["turn.failed"]
    assert plugin._pending_ready == {}


def test_a_turn_reported_twice_keeps_one_timer(pushed, monkeypatch):
    class Timer:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    timers = []
    monkeypatch.setattr(plugin, "_later", lambda delay, work: timers.append(Timer()) or timers[-1])
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="First.")
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Second.")
    assert timers[0].cancelled and not timers[1].cancelled
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    assert [event["body"] for event in pushed] == ["Second."]


def test_a_turn_without_an_id_pushes_at_once(pushed):
    plugin._post_llm_call(session_id="s", assistant_response="Done.")
    assert _types(pushed) == ["response.ready"]


def _subagent(parent="p", parent_turn="t", child="c"):
    plugin._subagent_start(child_session_id=child)
    plugin._subagent_stop(parent_session_id=parent, parent_turn_id=parent_turn, child_session_id=child,
                          child_summary="Found **three** results.", child_status="completed")


def test_a_subagent_inside_its_parents_turn_sends_nothing(pushed):
    plugin._pre_llm_call(session_id="p", turn_id="t")
    _subagent()
    _subagent(child="c2")
    assert pushed == []
    plugin._post_llm_call(session_id="p", turn_id="t", assistant_response="All three agree.")
    plugin._on_session_end(session_id="p", turn_id="t", completed=True, failed=False, interrupted=False)
    assert _types(pushed) == ["response.ready"]


def test_a_background_task_that_outlives_its_turn_notifies(pushed):
    plugin._pre_llm_call(session_id="p", turn_id="t")
    plugin._on_session_end(session_id="p", turn_id="t", completed=True, failed=False, interrupted=False)
    _subagent()
    [event] = pushed
    assert event["type"] == "background_task.finished"
    assert event["body"] == "Found three results."
    # It ends during a later turn of the same chat: still its own news.
    pushed.clear()
    plugin._pre_llm_call(session_id="p", turn_id="t2")
    _subagent(child="c3")
    assert _types(pushed) == ["background_task.finished"]


def test_a_subagent_without_a_parent_turn_id_notifies(pushed):
    plugin._pre_llm_call(session_id="p", turn_id="t")
    _subagent(parent_turn="")
    assert _types(pushed) == ["background_task.finished"]


def test_a_nested_subagent_sends_nothing(pushed):
    plugin._subagent_start(child_session_id="p-child")
    try:
        _subagent(parent="p-child")
    finally:
        plugin._child_sessions.discard("p-child")
    assert pushed == []


@pytest.mark.parametrize("markdown, plain", [
    ("## Done\n\n- [x] one\n- two", "Done\n\n• one\n• two"),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "a · b\n1 · 2"),
    ("```python\nprint(1)\n```", "print(1)"),
    ("> quoted ~~old~~ `code`\n---\n> ---", "quoted old code"),
    ("_em_ and snake_case_name", "em and snake_case_name"),
    ("2 * 3 * 4 = 24 in file_name_here", "2 * 3 * 4 = 24 in file_name_here"),
    ("![chart](a.png) *nice*", "chart nice"),
    (None, ""),
])
def test_plain_text(markdown, plain):
    assert events.plain_text(markdown) == plain


@pytest.fixture
def lock_screen(monkeypatch):
    """A real Live Activity reporter whose posts are kept; returns the ends sent."""
    live = sys.modules["conduit_push.live_activity"]
    posts = []
    reporter = live.LiveActivityReporter(post=lambda payload: posts.append(payload) or {"tracked": True},
                                         clock=lambda: 100.0, start_thread=False)
    monkeypatch.setattr(live, "reporter", reporter)
    reporter.turn_started("s")
    return reporter, posts


def _ends(posts):
    return [payload["end"] for payload in posts if payload.get("end")]


@pytest.mark.parametrize("sent_before_the_end", [False, True])
def test_a_failed_turn_ends_its_live_activity_failed(pushed, lock_screen, sent_before_the_end):
    reporter, posts = lock_screen
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="The provider returned an error.")
    if sent_before_the_end:
        reporter.flush()  # the reporter's thread runs before Hermes reports the turn's end
    plugin._on_session_end(session_id="s", turn_id="t", completed=False, interrupted=False)
    reporter.flush()
    assert _types(pushed) == ["turn.failed"]
    assert _ends(posts) == ["failed"]


def test_a_finished_turn_ends_its_live_activity_done_once(pushed, lock_screen):
    reporter, posts = lock_screen
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    reporter.flush()
    reporter.flush()
    assert _ends(posts) == ["done"]


def test_a_live_activity_whose_turn_end_never_comes_ends_done_after_the_grace(pushed, lock_screen, monkeypatch):
    reporter, posts = lock_screen
    later = []
    monkeypatch.setattr(plugin, "_later", lambda delay, work: later.append(work))
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    reporter.flush()
    assert _ends(posts) == [], "the end waits for on_session_end"
    [work] = later
    work()
    reporter.flush()
    assert _ends(posts) == ["done"]
