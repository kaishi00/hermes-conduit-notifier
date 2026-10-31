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


# A member's hidden chat for a room someone else hosts (room_rules.ROOM_SESSION_ID).
REMOTE_ROOM = "room_" + "0123456789abcdef" * 2


def test_a_turn_in_a_room_someone_else_hosts_sends_no_reply(pushed):
    # The room shows it, on its home's side: the hidden chat it's named by isn't one the phone opens.
    plugin._post_llm_call(session_id=REMOTE_ROOM, turn_id="t", assistant_response="Lions at 3.")
    plugin._on_session_end(session_id=REMOTE_ROOM, turn_id="t", completed=True, failed=False, interrupted=False)
    assert pushed == []


def test_a_failed_turn_in_a_room_someone_else_hosts_sends_no_failure(pushed, monkeypatch):
    alerts = []
    monkeypatch.setattr(plugin.calls, "failed_turn", lambda *args, **kwargs: alerts.append(args) or False)
    plugin._post_llm_call(session_id=REMOTE_ROOM, turn_id="t", assistant_response="The provider returned an error.")
    plugin._on_session_end(session_id=REMOTE_ROOM, turn_id="t", completed=False, failed=True, interrupted=False)
    assert pushed == [] and alerts == [], "nor an alert call"


def test_a_turn_in_a_room_this_hermes_hosts_still_notifies(pushed, monkeypatch):
    hosted = plugin.room_rules.RoomTurn(room_id="conduit-1", side="home", with_others=True, session_id="s")
    monkeypatch.setattr(plugin.room_rules, "room_turn", lambda session_id, home: hosted)
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Done.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    assert _types(pushed) == ["response.ready"]


def test_a_turn_whose_room_cant_be_told_still_notifies(pushed, monkeypatch):
    def unread(session_id, home):
        raise plugin.room_rules.SessionStoreError("couldn't read session s")

    monkeypatch.setattr(plugin.room_rules, "room_turn", unread)
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="The provider returned an error.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=False, failed=True, interrupted=False)
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


@pytest.mark.parametrize("status, label, body", [
    ("completed", "background_task.finished", None),
    (None, "background_task.finished", None),
    ("failed", None, "A delegated task failed."),
    ("timeout", None, "A delegated task timeout."),
])
def test_a_summaryless_background_task_lets_the_phone_word_only_a_finish(pushed, status, label, body):
    # Conduit writes a finish's title and line in its own language (#1144);
    # any other ending keeps the host's words, which say how it ended.
    plugin._pre_llm_call(session_id="p", turn_id="t")
    plugin._on_session_end(session_id="p", turn_id="t", completed=True, failed=False, interrupted=False)
    plugin._subagent_start(child_session_id="c")
    plugin._subagent_stop(parent_session_id="p", parent_turn_id="t", child_session_id="c",
                          **({"child_status": status} if status else {}))
    [event] = pushed
    assert event.get("title_label") == label
    assert event.get("body") == body


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
    ("Deck ready: MEDIA:/opt/data/cache/documents/dbdb29c1e72a6b48fa49002c4eab1ae1.pptx",
     "Deck ready: 📎 PPTX"),
    ("Here: MEDIA:/opt/data/cache/documents/doc_0123456789ab_Voice AI deck.pptx and more",
     "Here: 📎 Voice AI deck.pptx and more"),
    ("[[audio_as_voice]]\nMEDIA:/tmp/tts/reply.ogg", "📎 reply.ogg"),
    ("See `MEDIA:/tmp/out/report final.pdf`.", "See 📎 report final.pdf."),
    ('Saved MEDIA:"/a b/notes.key", done', "Saved 📎 notes.key, done"),
    ("MEDIA: ~/out/chart.png then MEDIA:/tmp/plain", "📎 chart.png then 📎 plain"),
    ("MEDIA:/Users/John Doe/Desktop/Final Report.tiff done", "📎 Final Report.tiff done"),
    ("MEDIA:/Users/John Doe/Desktop/notes now", "📎 notes now"),
    ("MEDIA:/opt/data/cache/images/img_0123456789ab.png", "📎 PNG"),
    ("MEDIA:/tmp/dbdb29c1e72a6b48fa49002c4eab1ae1", "📎"),
    ("Look **MEDIA:/a/b.png** and MEDIA:/a/c.png! ok?", "Look 📎 b.png and 📎 c.png! ok?"),
    # Prose and code aren't attachments.
    ("social media: report, see the MEDIA: section", "social media: report, see the MEDIA: section"),
    ("```\nMEDIA:/tmp/a.png\n[[audio_as_voice]]\n```", "MEDIA:/tmp/a.png\n[[audio_as_voice]]"),
    (None, ""),
])
def test_plain_text(markdown, plain):
    assert events.plain_text(markdown) == plain


def test_a_push_reads_media_paths_like_the_artifacts_list():
    artifacts = _load("conduit_push.artifacts_index", ROOT / "artifacts_index.py")
    assert events._WRITTEN == artifacts._WRITTEN
    assert events._TRAILING == artifacts._TRAILING


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


def test_a_live_activity_of_a_turn_without_its_id_ends_done_after_the_grace(pushed, lock_screen, monkeypatch):
    reporter, posts = lock_screen
    monkeypatch.setattr(plugin, "_unnamed_ends", {})
    later = []
    monkeypatch.setattr(plugin, "_later", lambda delay, work: later.append((delay, work)))
    plugin._post_llm_call(session_id="s", assistant_response="Done.")
    assert _types(pushed) == ["response.ready"], "without an id the reply can't wait"
    reporter.flush()
    assert _ends(posts) == []
    [(delay, work)] = later
    assert delay == plugin.READY_GRACE_S
    work()
    reporter.flush()
    assert _ends(posts) == ["done"]


def test_a_turn_without_its_id_keeps_the_end_on_session_end_reports(pushed, lock_screen, monkeypatch):
    reporter, posts = lock_screen
    monkeypatch.setattr(plugin, "_unnamed_ends", {})
    later = []
    monkeypatch.setattr(plugin, "_later", lambda delay, work: later.append(work))
    plugin._post_llm_call(session_id="s", assistant_response="The provider returned an error.")
    plugin._on_session_end(session_id="s", completed=False, failed=True, interrupted=False)
    [work] = later
    work()  # a timer already past cancel() ends nothing
    reporter.flush()
    assert _ends(posts) == ["failed"]
    assert plugin._unnamed_ends == {}


def test_a_turn_without_its_id_dropped_before_its_timer_starts_ends_nothing(pushed, lock_screen, monkeypatch):
    reporter, posts = lock_screen
    monkeypatch.setattr(plugin, "_unnamed_ends", {})
    later = []

    def start(delay, work):
        plugin._drop_unnamed_end("s")  # on_session_end lands between the two
        later.append(work)

    monkeypatch.setattr(plugin, "_later", start)
    plugin._post_llm_call(session_id="s", assistant_response="Done.")
    assert plugin._unnamed_ends == {}
    [work] = later
    work()
    reporter.flush()
    assert _ends(posts) == []


def test_the_oldest_held_end_of_a_turn_without_its_id_ends_done_when_too_many_are_held(pushed, lock_screen, monkeypatch):
    reporter, posts = lock_screen
    reporter.turn_started("s2")
    monkeypatch.setattr(plugin, "_unnamed_ends", {})
    monkeypatch.setattr(plugin, "MAX_TURN_STARTS", 1)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: object())
    plugin._post_llm_call(session_id="s", assistant_response="One.")
    plugin._post_llm_call(session_id="s2", assistant_response="Two.")
    reporter.flush()
    assert _ends(posts) == ["done"]
    assert list(plugin._unnamed_ends) == ["s2"]


def test_a_call_id_with_a_trailing_newline_is_refused():
    assert events._CALL_ID.match("a" * 8)
    assert events._CALL_ID.match("a" * 8 + "\n") is None


# conduit_notify (#1415): Hermes writes its turn's push.

notify_tool = sys.modules["conduit_push.notify_tool"]


@pytest.fixture
def paired(monkeypatch):
    monkeypatch.setattr(notify_tool.client, "load_state", lambda: {"paired": True})
    monkeypatch.setattr(notify_tool, "_kept", {})
    monkeypatch.setattr(notify_tool, "_taken", {})


def test_a_written_notification_replaces_the_replys_opening(pushed, paired):
    answer = notify_tool.handle({"title": "Report finished", "body": "All  150 products\nanalysed."}, session_id="s")
    assert '"ok": true' in answer
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Here is the full report: ...")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    [event] = pushed
    assert event["title"] == "Report finished"
    assert event["headline"] == "Report finished"
    assert event["body"] == "All 150 products analysed."
    assert event["title_label"] == "response.ready"

    plugin._post_llm_call(session_id="s", turn_id="t2", assistant_response="Next answer.")
    plugin._on_session_end(session_id="s", turn_id="t2", completed=True, failed=False, interrupted=False)
    assert pushed[1]["body"] == "Next answer." and "headline" not in pushed[1], "taken once"


def test_a_silent_reply_drops_the_written_notification(pushed, paired):
    notify_tool.handle({"title": "Done", "body": "Nothing changed."}, session_id="s")
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="[SILENT]")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    assert pushed == []
    assert notify_tool.take("s") is None, "it doesn't wait for a later turn"


def test_conduit_notify_refuses_what_cant_notify(paired, monkeypatch):
    assert "missing_text" in notify_tool.handle({"title": "Done"}, session_id="s")
    assert "subagent" in notify_tool.handle({"title": "a", "body": "b"}, session_id="kid", is_child=lambda s: s == "kid")
    monkeypatch.setattr(notify_tool.client, "load_state", lambda: None)
    assert "not_paired" in notify_tool.handle({"title": "a", "body": "b"}, session_id="s")
    assert notify_tool.take("s") is None


def test_a_written_notification_goes_stale(paired):
    notify_tool.keep("s", "Done", "Result.", now=0)
    assert notify_tool.take("s", now=notify_tool.KEEP_S + 1) is None


def test_a_turn_that_ends_without_a_reply_drops_its_written_notification(pushed, paired):
    notify_tool.handle({"title": "Report finished", "body": "All done."}, session_id="s")
    plugin._on_session_end(session_id="s", turn_id="t", interrupted=True)
    plugin._post_llm_call(session_id="s", turn_id="t2", assistant_response="Next answer.")
    plugin._on_session_end(session_id="s", turn_id="t2", completed=True, failed=False, interrupted=False)
    [event] = [e for e in pushed if e["type"] == "response.ready"]
    assert event["body"] == "Next answer." and "headline" not in event


def test_a_turn_reported_twice_keeps_its_written_notification(pushed, paired):
    notify_tool.handle({"title": "Report finished", "body": "All done."}, session_id="s")
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="First.")
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Second.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    [event] = pushed
    assert event["headline"] == "Report finished" and event["body"] == "All done."


def test_only_the_newest_written_notifications_are_kept(paired, monkeypatch):
    monkeypatch.setattr(notify_tool, "MAX_KEPT", 2)
    for session in ("a", "b", "c"):
        notify_tool.keep(session, "Done", session)
    assert notify_tool.take("a") is None
    assert notify_tool.take("c") == notify_tool.Headline("Done", "c")


@pytest.fixture
def runs(monkeypatch):
    """No turn's slot left over from another test."""
    monkeypatch.setattr(plugin, "_turn_slots", {})


def test_a_keyed_notification_replaces_the_profiles_earlier_one_from_any_chat(pushed, paired, runs):
    # #1579: "Report in progress" from a chat, "Report ready" from a
    # scheduled job, both keyed "report": one slot on the phone.
    answer = notify_tool.handle({"title": "Report in progress", "body": "Half done.", "key": " Report "}, session_id="s")
    assert '"key": "report"' in answer
    plugin._post_llm_call(session_id="s", turn_id="t", assistant_response="Working on it.")
    plugin._on_session_end(session_id="s", turn_id="t", completed=True, failed=False, interrupted=False)
    notify_tool.handle({"title": "Report ready", "body": "All 150 analysed.", "key": "report", "quiet": True},
                       session_id="cron_daily_20261031_120000")
    plugin._post_llm_call(session_id="cron_daily_20261031_120000", turn_id="t2", assistant_response="Done.")
    plugin._on_session_end(session_id="cron_daily_20261031_120000", turn_id="t2", completed=True, failed=False,
                           interrupted=False)
    first, second = pushed
    assert first["slot"] == second["slot"] == events.slot_token("key:default:report")
    assert "level" not in first, "a keyed notification still rings unless it's quiet"
    assert second["level"] == "passive"


def test_each_scheduled_job_keeps_one_notification(pushed, runs):
    # Every run of a job is its own chat; its runs share the job's slot.
    for n, run in enumerate(("cron_daily_20261031_120000", "cron_daily_20261101_120000")):
        plugin._post_llm_call(session_id=run, turn_id=run, task_id=f"cron:daily:{n}", assistant_response="News.")
        plugin._on_session_end(session_id=run, turn_id=run, task_id=f"cron:daily:{n}", completed=True, failed=False,
                               interrupted=False)
    plugin._on_session_end(session_id="local-1", task_id="cron:daily:3", turn_id="t3", failed=True)
    plugin._post_llm_call(session_id="chat", turn_id="t4", assistant_response="Hi.")
    plugin._on_session_end(session_id="chat", turn_id="t4", completed=True, failed=False, interrupted=False)
    assert _types(pushed) == ["response.ready", "response.ready", "turn.failed", "response.ready"]
    job = events.slot_token("cron:default:daily")
    assert [event.get("slot") for event in pushed] == [job, job, job, None], "a chat keeps its own"


def test_a_key_is_kept_short_and_plain(paired):
    assert notify_tool.slot_key("  Weekly  Report!! ") == "weekly-report"
    assert notify_tool.slot_key("a" * 80) == "a" * notify_tool.MAX_KEY_CHARS
    assert notify_tool.slot_key("a" * 39 + " b") == "a" * 39, "no dash left at the cut"
    assert notify_tool.slot_key(None) == notify_tool.slot_key("!!!") == ""
    assert notify_tool.slot_key("___") == notify_tool.slot_key("...") == notify_tool.slot_key("_._") == ""
    for junk in ("📊", "___", "..."):
        assert "invalid_key" in notify_tool.handle({"title": "a", "body": "b", "key": junk}, session_id="s")
    assert notify_tool.take("s") is None


def test_a_failed_turn_replaces_the_keyed_notification_it_wrote(pushed, paired):
    notify_tool.handle({"title": "Report in progress", "body": "Half done.", "key": "report"}, session_id="s")
    plugin._on_session_end(session_id="s", turn_id="t", completed=False, failed=True, interrupted=False)
    [failed] = pushed
    assert failed["type"] == "turn.failed"
    assert failed["slot"] == events.slot_token("key:default:report")


def test_a_gateway_owned_runs_background_task_joins_its_jobs_slot(pushed, runs):
    # The run's chat (local-…) names the job only through the turn's task id.
    plugin._pre_llm_call(session_id="local-run", turn_id="t")
    plugin._post_llm_call(session_id="local-run", turn_id="t", task_id="cron:daily:7", assistant_response="Started.")
    plugin._on_session_end(session_id="local-run", turn_id="t", task_id="cron:daily:7", completed=True,
                           failed=False, interrupted=False)
    _subagent(parent="local-run")
    reply, task = pushed
    assert task["type"] == "background_task.finished"
    assert reply["slot"] == task["slot"] == events.slot_token("cron:default:daily")


def test_a_muted_gateway_owned_runs_background_task_stays_quiet(pushed, runs, monkeypatch):
    monkeypatch.setattr(plugin.cron_runs, "muted", lambda job: job == "daily")
    plugin._pre_llm_call(session_id="local-run", turn_id="t")
    plugin._post_llm_call(session_id="local-run", turn_id="t", task_id="cron:daily:7", assistant_response="Started.")
    plugin._on_session_end(session_id="local-run", turn_id="t", task_id="cron:daily:7", completed=True,
                           failed=False, interrupted=False)
    _subagent(parent="local-run")
    assert pushed == []


def test_conduit_notify_registers_on_a_hermes_without_display_fields():
    registered = []

    class OlderHermes:
        def register_tool(self, name, toolset, schema, handler, check_fn):
            registered.append((name, check_fn))

    notify_tool.register(OlderHermes(), is_child=lambda _: False)
    assert registered == [("conduit_notify", notify_tool.available)]


def test_only_an_answered_turn_checks_for_a_voice_job_to_archive(pushed, tmp_path, monkeypatch):
    # hermes-conduit#1124: a voice job's chat leaves Desktop's list once answered.
    checked = []
    monkeypatch.setattr(plugin.voice_archive, "archive_answered_job",
                        lambda home, session_id: checked.append((home, session_id)) or True)
    plugin._on_session_end(session_id="job", turn_id="t1", completed=True, failed=False, interrupted=False)
    plugin._on_session_end(session_id="job", turn_id="t2", completed=False, failed=True, interrupted=False)
    plugin._on_session_end(session_id="job", turn_id="t3", completed=False, failed=False, interrupted=True)
    assert checked == [(tmp_path, "job")]

    def broken(home, session_id):
        raise RuntimeError("store gone")
    monkeypatch.setattr(plugin.voice_archive, "archive_answered_job", broken)
    plugin._on_session_end(session_id="job", turn_id="t4", completed=True, failed=False, interrupted=False)


def test_a_follow_up_in_an_older_run_keeps_its_own_chats_slot(pushed, runs):
    # Only a run's own turn carries the job's task id; the user's follow-up
    # in an older run's chat doesn't, so the newest run's report stays.
    for n, run in enumerate(("cron_weekly_20261030_120000", "cron_weekly_20261031_120000")):
        plugin._post_llm_call(session_id=run, turn_id=f"r{n}", task_id=f"cron:weekly:{n}", assistant_response="News.")
        plugin._on_session_end(session_id=run, turn_id=f"r{n}", task_id=f"cron:weekly:{n}", completed=True,
                               failed=False, interrupted=False)
    plugin._post_llm_call(session_id="cron_weekly_20261030_120000", turn_id="f", assistant_response="Answer.")
    plugin._on_session_end(session_id="cron_weekly_20261030_120000", turn_id="f", completed=True, failed=False,
                           interrupted=False)
    job = events.slot_token("cron:default:weekly")
    assert [event.get("slot") for event in pushed] == [job, job, None]


def test_a_silent_gateway_owned_run_still_hands_its_job_to_a_later_background_task(pushed, runs):
    plugin._pre_llm_call(session_id="local-quiet", turn_id="t")
    plugin._post_llm_call(session_id="local-quiet", turn_id="t", task_id="cron:nightly:1", assistant_response="[SILENT]")
    plugin._on_session_end(session_id="local-quiet", turn_id="t", task_id="cron:nightly:1", completed=True,
                           failed=False, interrupted=False)
    _subagent(parent="local-quiet")
    [task] = pushed
    assert task["slot"] == events.slot_token("cron:default:nightly")
    plugin._post_llm_call(session_id="local-quiet", turn_id="t2", assistant_response="A follow-up.")
    plugin._on_session_end(session_id="local-quiet", turn_id="t2", completed=True, failed=False, interrupted=False)
    assert "slot" not in pushed[-1], "the user's own follow-up keeps the chat's slot"


def test_a_keyed_runs_background_task_replaces_the_keys_notification(pushed, paired, runs):
    notify_tool.handle({"title": "Backup running", "body": "Started.", "key": "backup"}, session_id="local-keyed")
    plugin._pre_llm_call(session_id="local-keyed", turn_id="t")
    plugin._post_llm_call(session_id="local-keyed", turn_id="t", task_id="cron:nightly:2", assistant_response="Started.")
    plugin._on_session_end(session_id="local-keyed", turn_id="t", task_id="cron:nightly:2", completed=True,
                           failed=False, interrupted=False)
    _subagent(parent="local-keyed")
    reply, task = pushed
    assert reply["slot"] == task["slot"] == events.slot_token("key:default:backup")


def test_a_background_task_from_another_turn_keeps_the_chats_slot(pushed, paired, runs):
    notify_tool.handle({"title": "Report in progress", "body": "Started.", "key": "report"}, session_id="p")
    plugin._pre_llm_call(session_id="p", turn_id="t")
    plugin._post_llm_call(session_id="p", turn_id="t", assistant_response="Started.")
    plugin._on_session_end(session_id="p", turn_id="t", completed=True, failed=False, interrupted=False)
    _subagent(parent="p", parent_turn="other")
    reply, task = pushed
    assert reply["slot"] == events.slot_token("key:default:report") and "slot" not in task
