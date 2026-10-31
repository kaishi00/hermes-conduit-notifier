"""Bot workstreams (work_store, work_broker, work_tool;
designs/bot-mode-coordinator): a bot's Bot Chat coordinates work sessions
of its own, names them by ref only, and keeps to its policy."""
import contextvars
import json
import pathlib
import sys
import types

import pytest

from test_turn_end_pushes import ROOT, _load, plugin

store_mod = _load("conduit_push.work_store", ROOT / "work_store.py")
broker_mod = _load("conduit_push.work_broker", ROOT / "work_broker.py")
work = _load("conduit_push.work_tool", ROOT / "work_tool.py")
titles = sys.modules["conduit_push.session_titles"]

BOT_CHAT = "20261031_120000_botchat"
OTHER_CHAT = "20261031_120000_other"


# A stand-in for Hermes' session API (tui_gateway.server and .transport) ----------

class FakeHermes:
    """The parts of tui_gateway the broker uses: dispatch binds the transport
    it's given, the method table uses whatever is bound, prompt.submit
    rebinds a live session to the caller's transport as Hermes does."""

    def __init__(self, home):
        self.home = home
        self._sessions = {}
        self.calls = []
        self._current = contextvars.ContextVar("transport", default=None)
        self.created = 0
        self._methods = {name: self._method(name) for name in (
            "session.create", "session.resume", "prompt.submit", "session.steer", "session.redirect",
            "session.interrupt")}
        self.transport_module = types.SimpleNamespace(bind_transport=self._current.set,
                                                      current_transport=self._current.get)
        self.refuse = {}

    def _method(self, name):
        def run(rid, params):
            self.calls.append((name, dict(params), self._current.get()))
            if name in self.refuse:
                return {"jsonrpc": "2.0", "id": rid, "error": self.refuse[name]}
            return {"jsonrpc": "2.0", "id": rid, "result": getattr(self, name.replace(".", "_"))(params)}
        return run

    def dispatch(self, request, transport):
        token = self._current.set(transport)
        try:
            return self._methods[request["method"]](request["id"], request["params"])
        finally:
            self._current.reset(token)

    def live(self, stored, *, transport=None, running=False):
        runtime = f"rt-{stored}"
        self._sessions[runtime] = {"session_key": stored, "transport": transport, "running": running,
                                   "profile_home": str(self.home)}
        return runtime

    def session_create(self, params):
        self.created += 1
        stored = f"20261031_work_{self.created}"
        runtime = f"rt-{stored}"
        self._sessions[runtime] = {"session_key": stored, "transport": self._current.get(), "running": False,
                                   "profile_home": str(self.home)}
        return {"session_id": runtime, "stored_session_id": stored,
                "profile": params.get("profile", "default")}

    def session_resume(self, params):
        runtime = self.live(params["session_id"], transport=self._current.get())
        return {"session_id": runtime, "stored_session_id": params["session_id"],
                "info": {"profile_name": params.get("profile", "default")}}

    def prompt_submit(self, params):
        session = self._sessions[params["session_id"]]
        if self._current.get() is not None:
            session["transport"] = self._current.get()
        if session["running"]:
            return {"status": "queued"}
        session["running"] = True
        return {"status": "streaming"}

    def session_steer(self, params):
        return {"status": "queued" if self._sessions[params["session_id"]]["running"] else "rejected"}

    def session_redirect(self, params):
        return {"status": "redirected" if self._sessions[params["session_id"]]["running"] else "not_running"}

    def session_interrupt(self, params):
        self._sessions[params["session_id"]]["running"] = False
        return {"status": "interrupted"}

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


@pytest.fixture
def home(tmp_path):
    return tmp_path / "hermes"


@pytest.fixture
def hermes(home, monkeypatch):
    home.mkdir(parents=True, exist_ok=True)
    fake = FakeHermes(home)
    server = types.ModuleType("tui_gateway.server")
    server.dispatch = fake.dispatch
    server._sessions = fake._sessions
    server._methods = fake._methods
    transport = types.ModuleType("tui_gateway.transport")
    transport.bind_transport = fake._current.set
    transport.current_transport = fake._current.get
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    monkeypatch.setitem(sys.modules, "tui_gateway.transport", transport)
    monkeypatch.setattr(work, "_home", lambda: home)
    monkeypatch.setattr(titles, "_read", lambda h, key: "Bot Chat" if key == BOT_CHAT else "Some chat")
    monkeypatch.setattr(titles, "_bot_name", lambda h: "Furina")
    monkeypatch.setattr(work, "WAKE_BATCH_S", 0.01)
    monkeypatch.setattr(work, "_swept", set())
    monkeypatch.setattr(work, "_is_child", lambda session_id: False)
    work._wakes.clear()
    return fake


@pytest.fixture
def on(home, hermes):
    store_mod.Store(home).set_policy("strict")
    return store_mod.Store(home)


def call(name, args=None, session_id=BOT_CHAT):
    return json.loads(work.handle(name, args or {}, session_id=session_id))


def start(title="Login bug", instructions="Find out why the login page is slow.", **extra):
    return call("work_start", {"title": title, "instructions": instructions, **extra})


# The registry ---------------------------------------------------------------------

def test_refs_count_per_kind_and_only_refs_name_work(home):
    store = store_mod.Store(home)
    first = store.add("workstream", title="One")
    second = store.add("workstream", title="Two")
    ask = store.add("ask_out", title="Ask Nahida", status="waiting")
    assert (first.ref, second.ref, ask.ref) == ("W1", "W2", "A1")
    assert store.get(" w2 ").title == "Two"
    store.update("W1", session_id="20261031_abc")
    assert store.get("20261031_abc") is None
    assert store.get("W9") is None
    assert store.by_session("20261031_abc").ref == "W1"
    assert first.idempotency_key != second.idempotency_key


def test_every_relationship_field_is_kept(home):
    store = store_mod.Store(home)
    row = store.add("ask_in", title="Research", requested_by={"bot": "raiden", "work": "W4"},
                    executor={"bot": "nahida", "work": "W7"}, reply_to={"bot": "raiden", "work": "W4"},
                    ask_id="ask-1", status="running")
    again = store.get(row.ref)
    assert again.requested_by == {"bot": "raiden", "work": "W4"}
    assert again.executor == {"bot": "nahida", "work": "W7"}
    assert again.reply_to == {"bot": "raiden", "work": "W4"}
    assert again.ask_id == "ask-1"


def test_seen_moves_without_marking_the_row_changed(home):
    store = store_mod.Store(home)
    store.add("workstream", title="One")
    store.update("W1", status="done", last_result="All good")
    assert store.get("W1").changed_since_seen
    store.mark_seen(["W1"])
    assert not store.get("W1").changed_since_seen


def test_the_journal_closes_a_step_by_its_call_id(home):
    store = store_mod.Store(home)
    store.add("workstream", title="One")
    store.step_started("W1", "s1", "terminal", tool_call_id="c1", summary="command: ls")
    store.step_started("W1", "s1", "terminal", tool_call_id="c2", summary="command: rm x")
    assert store.step_ended("W1", tool="terminal", tool_call_id="c2", outcome="ok")
    assert [step["tool_call_id"] for step in store.steps("W1", open_only=True)] == ["c1"]


def test_a_restart_marks_running_work_interrupted(home):
    store = store_mod.Store(home)
    store.add("workstream", title="Live")
    store.add("workstream", title="Gone")
    store.update("W1", session_id="a", status="running")
    store.update("W2", session_id="b", status="running")
    assert store.interrupt_running({"a"}) == ["W2"]
    assert store.get("W2").status == "interrupted"
    assert store.get("W1").status == "running"


def test_the_outbox_keeps_what_hermes_never_took(home):
    store = store_mod.Store(home)
    sent = store.outbox_add("W1", "send", "s1", "hello")
    lost = store.outbox_add("W1", "send", "s1", "again")
    store.outbox_done(sent, "accepted")
    assert [item["id"] for item in store.outbox_pending()] == [lost]


# Who may call the tools -----------------------------------------------------------

def test_off_by_default_and_nothing_is_written(home, hermes):
    assert call("work_start", {"title": "x", "instructions": "y"})["error"] == "off"
    assert not store_mod.exists(home)
    assert not work.available()


def test_only_the_bots_own_bot_chat_may_call_them(on, hermes):
    for session_id in (OTHER_CHAT, "", None):
        assert call("work_start", {"title": "x", "instructions": "y"}, session_id=session_id)["error"] == "not_bot_chat"
    assert on.list() == [] and hermes.calls == []


def test_a_subagent_or_fork_of_the_bot_chat_is_refused(on, hermes, monkeypatch):
    monkeypatch.setattr(work, "_is_child", lambda session_id: True)
    assert start()["error"] == "not_bot_chat"
    assert on.list() == [] and hermes.calls == []


def test_a_session_id_or_another_bots_ref_names_nothing(on, hermes):
    start()
    hermes.calls.clear()
    for ref in ("20261031_work_1", "rt-20261031_work_1", "W2", "nahida:W1", ""):
        answer = call("work_send", {"ref": ref, "text": "hi"})
        assert answer["error"] == "no_such_work"
    assert hermes.calls == []


def test_asks_between_bots_say_not_yet(on, hermes):
    assert call("work_ask", {"bot": "nahida", "text": "?"})["error"] == "not_yet"
    assert call("work_reply", {"ref": "A1", "text": "!"})["error"] == "not_yet"
    assert hermes.calls == []


def test_a_plugin_host_answers_with_the_reason(on, monkeypatch):
    monkeypatch.setattr(work, "_home", lambda: on.home)
    monkeypatch.setattr(titles, "_read", lambda h, key: "Bot Chat")
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    answer = start()
    assert answer["error"] == "unavailable" and "plugins.isolation: host" in answer["note"]
    assert on.list() == []
    assert "can't run here" in work.digest(BOT_CHAT)


# Starting, sending, stopping ------------------------------------------------------

def test_start_makes_a_hidden_titled_session_on_the_bots_own_profile(on, hermes):
    answer = start()
    assert answer["ok"] and answer["ref"] == "W1" and answer["status"] == "running"
    (name, params, transport), = hermes.named("session.create")
    assert params["hidden"] is True and params["title"] == "Login bug"
    assert params["idempotency_key"] == on.get("W1").idempotency_key
    assert "profile" not in params  # the default profile
    assert transport is broker_mod._transport
    (_, submitted, submit_transport), = hermes.named("prompt.submit")
    assert "Find out why the login page is slow." in submitted["text"] and "W1" in submitted["text"]
    assert submit_transport is broker_mod._transport
    row = on.get("W1")
    assert row.session_id == "20261031_work_1" and row.coordinator == BOT_CHAT
    assert row.requested_by["via"] == BOT_CHAT and row.reply_to == {"to": "coordinator", "session": BOT_CHAT}
    assert answer["link"] == "[Open W1](conduit://session/20261031_work_1)"
    assert on.outbox_pending() == []


def test_a_secondary_bot_starts_on_its_own_profile(tmp_path, monkeypatch, hermes):
    home = tmp_path / "root" / "profiles" / "nahida"
    home.mkdir(parents=True)
    hermes.home = home
    monkeypatch.setattr(work, "_home", lambda: home)
    store_mod.Store(home).set_policy("strict")
    assert start()["ok"]
    (_, params, _), = hermes.named("session.create")
    assert params["profile"] == "nahida"


def test_a_session_on_another_profile_is_refused(on, hermes, monkeypatch):
    monkeypatch.setattr(hermes, "session_create", lambda params: {"session_id": "rt-x", "stored_session_id": "x",
                                                                   "profile": "raiden"})
    answer = start()
    assert not answer["ok"] and "raiden" in answer["note"]
    assert on.get("W1").status == "failed" and hermes.named("prompt.submit") == []


def test_start_offers_lookalikes_first(on, hermes):
    start("Login page slow")
    answer = start("Slow login page")
    assert answer["error"] == "similar_work" and answer["similar"][0]["ref"] == "W1"
    assert hermes.created == 1
    assert start("Slow login page", new=True)["ref"] == "W2"


def test_start_stops_at_the_bots_running_limit(on, hermes):
    start("One thing")
    start("Second matter")
    answer = start("Third question")
    assert answer["error"] == "busy" and len(answer["running"]) == 2
    assert hermes.created == 2


@pytest.mark.parametrize("how, running, method, outcome", [
    ("auto", False, "prompt.submit", "started"),
    ("auto", True, "session.redirect", "redirected"),
    ("add", True, "session.steer", "steered"),
    ("after", True, "prompt.submit", "queued"),
])
def test_send_takes_hermes_own_path(on, hermes, how, running, method, outcome):
    start()
    hermes._sessions["rt-20261031_work_1"]["running"] = running
    hermes.calls.clear()
    answer = call("work_send", {"ref": "w1", "text": "Don't change anything, just investigate.", "how": how})
    assert answer["ok"] and answer["outcome"] == outcome, answer
    assert hermes.calls[-1][0] == method
    assert "Don't change anything" in hermes.calls[-1][1]["text"]
    assert hermes.created == 1


def test_send_to_a_session_the_user_has_open_keeps_their_stream(on, hermes):
    start()
    app = object()
    record = hermes._sessions["rt-20261031_work_1"]
    record["transport"], record["running"] = app, False
    hermes.calls.clear()
    assert call("work_send", {"ref": "W1", "text": "Next step"})["outcome"] == "started"
    assert hermes.calls[-1][2] is None  # nothing bound: Hermes keeps the app's transport
    assert record["transport"] is app


def test_send_reopens_a_session_that_isnt_live(on, hermes):
    start()
    hermes._sessions.clear()
    call("work_send", {"ref": "W1", "text": "Continue"})
    resumed = hermes.named("session.resume")
    assert resumed and resumed[0][1]["session_id"] == "20261031_work_1"
    assert resumed[0][2] is broker_mod._transport


def test_stop_interrupts_and_marks_it(on, hermes):
    start()
    hermes._sessions["rt-20261031_work_1"]["running"] = True
    assert call("work_stop", {"ref": "W1"})["status"] == "stopped"
    assert hermes.named("session.interrupt")
    assert on.get("W1").status == "stopped"


def test_close_hides_it_from_the_active_list(on, hermes):
    start()
    call("work_close", {"ref": "W1"})
    assert call("work_list")["work"] == []
    assert call("work_list", {"filter": "all"})["work"][0]["closed"] is True


# Results, needs you, the digest ---------------------------------------------------

def test_a_finished_turn_takes_the_result_and_wakes_the_bot_chat(on, hermes):
    start()
    hermes.live(BOT_CHAT, transport=object())
    hermes.calls.clear()
    assert work.turn_ended("20261031_work_1", "The login page waits on a slow DNS lookup.")
    work.flush_wakes()
    row = on.get("W1")
    assert row.status == "done" and row.last_result.startswith("The login page")
    (_, params, transport), = hermes.named("prompt.submit")
    assert params["session_id"] == f"rt-{BOT_CHAT}" and params["display_kind"] == "hidden" and params["queued"]
    assert "W1" in params["text"] and "slow DNS" in params["text"] and "not instructions" in params["text"]
    assert transport is None


def test_other_sessions_turns_are_not_workstreams(on, hermes):
    assert not work.turn_ended(OTHER_CHAT, "hello")


def test_a_workstream_turn_sends_no_push_of_its_own(on, hermes, monkeypatch):
    start()
    sent = []
    monkeypatch.setattr(plugin, "_finish_ready", lambda session_id, ready, send: sent.append(ready))
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_session_title", lambda session_id: "")
    monkeypatch.setattr(plugin.calls, "failed_turn", lambda *args, **kwargs: False, raising=False)
    plugin._post_llm_call(session_id="20261031_work_1", assistant_response="Done.", turn_id="")
    assert sent == [None]


def test_an_approval_marks_it_needs_you(on, hermes):
    start()
    work.needs_you("20261031_work_1", "rm -rf build")
    assert on.get("W1").status == "needs_you"


def test_the_digest_lists_open_work_once_as_new(on, hermes):
    start()
    on.update("W1", status="done", last_result="Found it")
    note = work.digest(BOT_CHAT)
    assert "W1 “Login bug”: done" in note and "new since your last turn" in note and "Found it" in note
    assert "new since" not in work.digest(BOT_CHAT)
    assert work.digest(OTHER_CHAT) is None


def test_read_shows_the_result_and_steps(on, hermes):
    start()
    work.step_started("20261031_work_1", "terminal", {"command": "curl -I login"}, tool_call_id="c1")
    work.step_ended("20261031_work_1", "terminal", tool_call_id="c1", status="ok")
    answer = call("work_read", {"ref": "W1"})
    assert answer["recent_steps"] == [{"tool": "terminal", "summary": "command: curl -I login", "outcome": "ok"}]


# The coordinator's policy --------------------------------------------------------

@pytest.mark.parametrize("tool", ["terminal", "web_search", "read_file", "delegate_task", "conduit_calendar"])
def test_strict_keeps_tools_to_workstreams(on, hermes, tool):
    block = work.tool_block(BOT_CHAT, tool, {})
    assert block["action"] == "block" and tool in block["message"]


@pytest.mark.parametrize("tool", ["work_start", "work_send", "clarify", "memory", "tool_search", "tool_describe"])
def test_strict_allows_the_coordinators_own_tools(on, hermes, tool):
    assert work.tool_block(BOT_CHAT, tool, {}) is None


def test_lookups_allows_read_only_lookups(on, hermes):
    on.set_policy("lookups")
    assert work.tool_block(BOT_CHAT, "web_search", {}) is None
    assert work.tool_block(BOT_CHAT, "terminal", {})["action"] == "block"


def test_a_bridged_call_is_checked_by_what_it_calls(on, hermes):
    assert work.tool_block(BOT_CHAT, "tool_call", {"name": "work_start", "arguments": {}}) is None
    assert work.tool_block(BOT_CHAT, "tool_call", {"name": "terminal", "arguments": {}})["action"] == "block"


def test_message_agent_is_refused_for_work_ask(on, hermes):
    assert "work_ask" in work.tool_block(BOT_CHAT, "message_agent", {})["message"]


def test_the_policy_touches_no_other_chat_and_nothing_while_off(home, hermes):
    store_mod.Store(home).set_policy("strict")
    assert work.tool_block(OTHER_CHAT, "terminal", {}) is None
    store_mod.Store(home).set_policy("off")
    assert work.tool_block(BOT_CHAT, "terminal", {}) is None


def test_the_plugins_tool_hook_applies_the_policy(on, hermes, monkeypatch):
    monkeypatch.setattr(plugin, "_room_tool_block", lambda kwargs: None)
    block = plugin._pre_tool_call(session_id=BOT_CHAT, tool_name="terminal", args={"command": "ls"}, turn_id="t1",
                                  tool_call_id="c1")
    assert block and block["action"] == "block"


def test_the_plugins_tool_hooks_journal_a_workstream(on, hermes, monkeypatch):
    start()
    monkeypatch.setattr(plugin, "_room_tool_block", lambda kwargs: None)
    plugin._pre_tool_call(session_id="20261031_work_1", tool_name="terminal", args={"command": "ls"}, turn_id="t1",
                          tool_call_id="c1")
    assert on.steps("W1", open_only=True)[0]["summary"] == "command: ls"
    plugin._post_tool_call(session_id="20261031_work_1", tool_name="terminal", tool_call_id="c1", status="ok")
    assert on.steps("W1", open_only=True) == []


def test_the_cli_turns_it_on_and_off(home, monkeypatch, capsys):
    cli = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: home)
    args = types.SimpleNamespace(work_action="status", policy=None, max_running=None)
    assert cli._work(args) == 0 and not store_mod.exists(home)
    cli._work(types.SimpleNamespace(work_action="on", policy="lookups", max_running=3))
    assert store_mod.Store(home).policy() == "lookups" and store_mod.Store(home).max_running() == 3
    cli._work(types.SimpleNamespace(work_action="off", policy=None, max_running=None))
    assert store_mod.Store(home).policy() == "off"
    assert "Bot workstreams: off" in capsys.readouterr().out
