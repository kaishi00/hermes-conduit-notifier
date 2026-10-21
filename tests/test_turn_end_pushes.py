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


def test_a_pulse_turn_whose_end_never_comes_ends_no_live_activity(pushed, monkeypatch):
    later, ended = [], []
    monkeypatch.setattr(plugin, "_later", lambda delay, work: later.append((delay, work)))
    monkeypatch.setattr(plugin.pulse, "is_pulse_turn", lambda session_id, turn_id: True)
    monkeypatch.setattr(plugin.live_activity.reporter, "turn_ended", lambda *args: ended.append(args))
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="1 item.")
    [(_, work)] = later
    work()
    assert pushed == [] and ended == []


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


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """Hermes' session store, with a writer for its lineage columns."""
    import sqlite3

    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_not_child_lineage", set())
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT)")

    def add(session_id, parent=None, end_reason=None):
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (session_id, parent, end_reason))
        db.commit()

    yield add
    db.close()
    plugin._child_sessions.clear()
    plugin._compacted_children.clear()


@pytest.fixture(autouse=True)
def no_delegation_flag(monkeypatch):
    """This Hermes has no delegation flag unless a test sets one."""
    monkeypatch.setattr(plugin, "_delegated_child_check", False)


@pytest.fixture
def delegation_flag(monkeypatch):
    state = {"set": False}
    monkeypatch.setattr(plugin, "_delegated_child_check", lambda: state["set"])
    return state


def test_the_delegation_flag_marks_the_running_session_a_child(delegation_flag):
    health = sys.modules["conduit_push.health_tool"]
    delegation_flag["set"] = True
    assert health._handle({"action": "overview"}, "rotated", plugin._is_child, None)["error"] == "subagent"
    delegation_flag["set"] = False
    assert not plugin._is_child("rotated")


def test_a_background_task_ending_inside_its_childs_run_still_notifies(pushed, delegation_flag):
    plugin._pre_llm_call(session_id="p", turn_id="t")
    plugin._on_session_end(session_id="p", turn_id="t", completed=True, failed=False, interrupted=False)
    delegation_flag["set"] = True
    _subagent()
    assert _types(pushed) == ["background_task.finished"]


def test_the_delegation_flag_is_resolved_once(monkeypatch):
    module = types.ModuleType("agent.delegation_context")
    module.is_delegated_child_context = lambda: True
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.delegation_context", module)
    monkeypatch.setattr(plugin, "_delegated_child_check", None)
    assert plugin._is_child("x")
    assert plugin._delegated_child_check is module.is_delegated_child_context


@pytest.mark.parametrize("broken", [None, "raises"])
def test_a_missing_or_broken_delegation_module_leaves_ids_to_answer(monkeypatch, broken):
    if broken:
        module = types.ModuleType("agent.delegation_context")
        module.__getattr__ = lambda name: (_ for _ in ()).throw(RuntimeError("half initialised"))
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.delegation_context", module)
    else:
        monkeypatch.setitem(sys.modules, "agent.delegation_context", None)
    monkeypatch.setattr(plugin, "_delegated_child_check", None)
    assert not plugin._is_child("x")
    assert plugin._delegated_child_check is False


def test_a_delegation_check_that_raises_falls_back_to_ids(monkeypatch):
    def broken():
        raise TypeError("changed signature")

    monkeypatch.setattr(plugin, "_delegated_child_check", broken)
    plugin._subagent_start(child_session_id="known")
    try:
        assert plugin._is_child("known") and not plugin._is_child("x")
    finally:
        plugin._child_sessions.discard("known")


def _compacted_child(add):
    """Parent "p" delegated "c", which compacted twice: c → c2 → c3."""
    add("p")
    add("c", parent="p", end_reason="compression")
    add("c2", parent="c", end_reason="compression")
    add("c3", parent="c2")
    plugin._subagent_start(child_session_id="c")


def test_a_child_whose_compaction_replaced_its_id_is_still_a_child(pushed, sessions):
    _compacted_child(sessions)
    health = sys.modules["conduit_push.health_tool"]
    assert plugin._is_child("c3") and plugin._is_child("c2")
    assert health._handle({"action": "overview"}, "c3", plugin._is_child, None)["error"] == "subagent"
    assert "c3" in plugin._compacted_children, "learned, so later calls skip the store"
    # Hermes reports the id the child ends under.
    plugin._subagent_stop(child_session_id="c3", parent_session_id="p", parent_turn_id="t")
    assert not plugin._child_sessions and not plugin._compacted_children
    assert not plugin._is_child("c3"), "the child ended: no more reads"


def test_a_child_ending_under_a_compacted_id_no_one_asked_about_is_cleared(pushed, sessions):
    _compacted_child(sessions)
    plugin._subagent_stop(child_session_id="c3", parent_session_id="p", parent_turn_id="t")
    assert not plugin._child_sessions


def test_the_parent_and_its_own_compactions_are_not_children(sessions):
    _compacted_child(sessions)
    sessions("p2", parent="p")  # the parent's continuation, while "p" is still open
    sessions("q", parent=None)
    assert not plugin._is_child("p")
    assert not plugin._is_child("q")
    assert not plugin._is_child("unknown")
    assert {"p", "q"} <= plugin._not_child_lineage
    assert "p2" not in plugin._not_child_lineage, "its parent is still open, so it's asked again"
    assert "unknown" not in plugin._not_child_lineage, "a session not stored yet is asked about again"


def test_a_parent_that_compacted_while_its_child_runs_is_not_a_child(sessions):
    sessions("p", end_reason="compression")
    sessions("p2", parent="p")
    sessions("c", parent="p2")
    plugin._subagent_start(child_session_id="c")
    assert not plugin._is_child("p2") and not plugin._is_child("p")


def test_a_session_branched_from_a_child_is_not_its_continuation(sessions):
    sessions("c", end_reason="branched")
    sessions("b", parent="c")
    plugin._subagent_start(child_session_id="c")
    assert not plugin._is_child("b")


def test_a_compaction_read_before_the_old_session_ends_still_counts(sessions):
    sessions("p")
    sessions("c", parent="p")
    sessions("x")
    sessions("c2", parent="c")  # the old row isn't ended yet
    sessions("x2", parent="x")
    plugin._subagent_start(child_session_id="c")
    assert plugin._is_child("c2"), "the parent is a child: no need to wait for its end"
    assert not plugin._is_child("x2") and "x2" not in plugin._not_child_lineage


def test_a_subagent_a_child_delegated_is_a_child(sessions):
    _compacted_child(sessions)
    sessions("g", parent="c3")
    assert plugin._is_child("g")


def test_a_lineage_longer_than_followed_is_not_kept(sessions, monkeypatch):
    _compacted_child(sessions)
    monkeypatch.setattr(plugin, "_LINEAGE_HOPS", 1)
    assert not plugin._is_child("c3")
    assert "c3" not in plugin._not_child_lineage


def test_a_hermes_home_that_raises_answers_no(sessions, monkeypatch):
    plugin._subagent_start(child_session_id="c")
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: (_ for _ in ()).throw(RuntimeError("no home")))
    assert not plugin._is_child("c3")
    assert plugin._is_child("c")


def test_with_no_child_running_the_store_is_not_read(sessions, monkeypatch):
    monkeypatch.setattr(plugin, "_continues_a_child", lambda *args: pytest.fail("read the store"))
    assert not plugin._is_child("anyone")


def test_a_missing_or_unreadable_store_answers_no(sessions, tmp_path):
    plugin._subagent_start(child_session_id="c")
    (tmp_path / "state.db").write_bytes(b"not a database")
    assert not plugin._is_child("c3")
    assert plugin._is_child("c")
    assert "c3" not in plugin._not_child_lineage, "an unread answer isn't kept"


def test_a_nested_subagent_under_a_compacted_child_sends_nothing(pushed, sessions):
    _compacted_child(sessions)
    plugin._subagent_start(child_session_id="grandchild")
    plugin._subagent_stop(parent_session_id="c3", parent_turn_id="t", child_session_id="grandchild",
                          child_summary="Done.", child_status="completed")
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
