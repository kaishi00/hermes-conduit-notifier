"""Pulse slice 0: the home chat delivery probe (designs/proactivity.md 6.4.1, 6.4.2, 7.6)
and the outcome ledger (3.6).

The home chat is a Conduit (Desktop-source) chat, reached through the
dashboard's session host; the host is faked here with its two methods.

The Hermes reads run against two databases: a fixture with only the columns
Pulse reads, and, when a Hermes checkout or install is available, one built
from Hermes's own schema (set HERMES_AGENT_SRC to a checkout). The second is
the contract test: it fails when Hermes renames what Pulse depends on.
"""
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
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
store = _load("conduit_push.pulse_store", ROOT / "pulse_store.py")
hermes = _load("conduit_push.pulse_hermes", ROOT / "pulse_hermes.py")
delivery = _load("conduit_push.pulse_delivery", ROOT / "pulse_delivery.py")
pulse = _load("conduit_push.pulse", ROOT / "pulse.py")
cli = _load("conduit_push.cli", ROOT / "cli.py")
host = _load("conduit_push.pulse_host", ROOT / "pulse_host.py")
ledger = _load("conduit_push.pulse_ledger", ROOT / "pulse_ledger.py")

SID = "20261019_165824_560887"  # a Conduit chat's stored id, as Eric's host showed it

# Only the columns Pulse reads, as Hermes names them (hermes_state_common.py).
FIXTURE_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, session_key TEXT, parent_session_id TEXT,
    started_at REAL NOT NULL, last_activity_at REAL, title TEXT, archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
    timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0,
    display_kind TEXT
);
"""


def _hermes_schema():
    """Hermes's own SCHEMA_SQL, read in a separate interpreter so its imports
    never meet this file's stubs. None when no Hermes is importable."""
    env = dict(os.environ)
    source = os.environ.get("HERMES_AGENT_SRC")
    if source:
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    try:
        result = subprocess.run([sys.executable, "-c", "import hermes_state_common as c; print(c.SCHEMA_SQL)"],
                                capture_output=True, text=True, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and "CREATE TABLE IF NOT EXISTS messages" in result.stdout else None


HERMES_SCHEMA = _hermes_schema()


class StateDB:
    """A Hermes state.db with helpers that write rows the way Hermes does."""

    def __init__(self, home, schema):
        self.path = home / "state.db"
        conn = sqlite3.connect(self.path)
        conn.executescript(schema)
        conn.commit()
        conn.close()

    def _exec(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def session(self, sid=SID, title="Pulse", pinned=1, archived=0, started=1000.0, parent=None):
        # Conduit's chats: source desktop, no gateway session key.
        self._exec("INSERT INTO sessions(id, source, session_key, parent_session_id, started_at, last_activity_at, "
                   "title, pinned, archived) VALUES(?,?,?,?,?,?,?,?,?)",
                   (sid, "desktop", None, parent, started, started, title, pinned, archived))

    def message(self, role, content, *, sid=SID, display_kind=None, active=1, at=None):
        return self._exec("INSERT INTO messages(session_id, role, content, timestamp, display_kind, active, compacted) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (sid, role, content, time.time() if at is None else at, display_kind, active,
                           0 if active else 1))


SCHEMAS = [pytest.param(FIXTURE_SCHEMA, id="fixture"),
           pytest.param(HERMES_SCHEMA, id="hermes",
                        marks=pytest.mark.skipif(HERMES_SCHEMA is None, reason="no Hermes to read the schema from"))]


@pytest.fixture(params=SCHEMAS)
def db(request, tmp_path):
    return StateDB(tmp_path, request.param)


@pytest.fixture
def pulse_store(tmp_path):
    return store.open_store(tmp_path)


def _sent(status="streaming"):
    return lambda chat_id, text: host.Sent(True, status)


# The store -------------------------------------------------------------------

def test_store_defaults_and_settings(pulse_store):
    assert pulse_store.get("enabled") is False
    assert pulse_store.get("home_title") == "Pulse"
    pulse_store.set("enabled", True)
    assert pulse_store.get("enabled") is True


def test_probe_request_is_taken_once(pulse_store):
    assert pulse_store.take_probe_request() is False
    pulse_store.set("probe_requested", True)
    assert pulse_store.take_probe_request() is True
    assert pulse_store.take_probe_request() is False


def test_attempts_in_flight_until_final(pulse_store):
    pulse_store.add_attempt(attempt_id="i:1", item_id="i", attempt=1, kind="probe", tag="pi-1", text="t",
                            session_key=SID)
    assert [row["tag"] for row in pulse_store.in_flight()] == ["pi-1"]
    pulse_store.update_attempt("i:1", state=store.RUNNING, turn_id="turn")
    assert pulse_store.attempt_by_tag("pi-1")["turn_id"] == "turn"
    pulse_store.update_attempt("i:1", state=store.DELIVERED, detail="ok")
    assert pulse_store.in_flight() == []
    assert pulse_store.counts("probe") == {store.DELIVERED: 1}


def test_one_process_holds_the_loop(pulse_store):
    assert pulse_store.claim_leader("a", 120, now=1000)
    assert not pulse_store.claim_leader("b", 120, now=1060)
    assert pulse_store.claim_leader("a", 120, now=1060)  # renewed
    assert pulse_store.claim_leader("b", 120, now=1200)  # a's lease ran out


# Hermes reads ----------------------------------------------------------------

def test_home_chat_is_a_started_chat_with_the_title(db, tmp_path):
    db.session()
    db.session(sid="old", pinned=0, started=500.0)
    db.session(sid="tip", parent=SID)  # compression continues under the same title
    db.session(sid="arch", archived=1)
    db.session(sid="other", title="Pulse notes")
    chats = hermes.find_home_chat(tmp_path, "Pulse")
    assert [chat.chat_id for chat in chats] == [SID, "old"]
    assert chats[0].pinned
    assert hermes.chat_by_id(tmp_path, "old").title == "Pulse"
    assert hermes.chat_by_id(tmp_path, "arch") is None


def test_turn_rows_follow_the_tagged_user_row(db, tmp_path):
    db.session()
    db.message("user", "earlier")
    db.message("assistant", "earlier reply")
    db.message("user", "Pulse delivery check pAAAA-1.", display_kind="hidden", active=0)
    db.message("assistant", "")
    db.message("assistant", "Check arrived.", active=0)
    turn = hermes.find_turn(tmp_path, chat_id=SID, tag="pAAAA-1", since=0)
    assert turn == hermes.TurnRows(SID, "Check arrived.", False, False)
    db.message("user", "next")
    assert hermes.find_turn(tmp_path, chat_id=SID, tag="pAAAA-1", since=0).closed


def test_a_turn_after_compression_is_found_in_the_tip(db, tmp_path):
    db.session()
    db.session(sid="tip", parent=SID)
    db.message("user", "Pulse delivery check pAAAA-1.", sid="tip")
    db.message("assistant", "Check arrived.", sid="tip")
    assert hermes.find_turn(tmp_path, chat_id=SID, tag="pAAAA-1", since=0).session_id == "tip"


def test_rows_from_before_the_attempt_dont_count(db, tmp_path):
    db.session()
    db.message("user", "Pulse delivery check pAAAA-1.", at=100.0)
    assert hermes.find_turn(tmp_path, chat_id=SID, tag="pAAAA-1", since=200.0) is None


def test_a_failed_turn_is_seen(db, tmp_path):
    db.session()
    db.message("user", "Pulse delivery check pAAAA-1.")
    db.message("assistant", "Your request was not processed.", display_kind="failed_turn")
    turn = hermes.find_turn(tmp_path, chat_id=SID, tag="pAAAA-1", since=0)
    assert turn.failed and turn.reply == ""


def test_missing_state_db_is_unreadable_not_empty(tmp_path):
    with pytest.raises(hermes.HermesUnreadable):
        hermes.find_home_chat(tmp_path, "Pulse")


# Deriving an attempt's state ---------------------------------------------------

TURN = hermes.TurnRows
S = store


@pytest.mark.parametrize("turn, state, age, earlier, prior, unchanged, expected", [
    (None, S.SUBMITTED, 5, False, "", 0, S.SUBMITTED),
    (None, S.RUNNING, 5, False, "", 0, S.RUNNING),
    (None, S.SUBMITTED, 500, False, "", 0, S.NOT_ADMITTED),
    (None, S.SUBMITTED, 5, True, "", 0, S.NOT_ADMITTED),
    (None, S.QUEUED, 900, False, "", 0, S.QUEUED),
    (None, S.QUEUED, delivery.STUCK_S, False, "", 0, S.FAILED),
    (None, S.QUEUED, 5, True, "", 0, S.NOT_ADMITTED),
    (TURN(SID, "ok", False, False), S.RUNNING, 9, False, "", 0, S.DELIVERED),
    (TURN(SID, "ok", False, False), S.SUBMITTED, 9, True, "", 0, S.DELIVERED),
    (TURN(SID, "sorry", True, False), S.RUNNING, 9, False, "", 0, S.FAILED),
    (TURN(SID, "", False, True), S.RUNNING, 9, False, "", 0, S.FAILED),
    (TURN(SID, "", False, False), S.RUNNING, 9, False, "", 0, S.RUNNING),
    (TURN(SID, "", False, False), S.RUNNING, 9, False, delivery.ENDED, 10, S.RUNNING),
    (TURN(SID, "", False, False), S.RUNNING, 90, False, delivery.ENDED, delivery.REPLY_GRACE_S, S.FAILED),
    (TURN(SID, "", False, False), S.RUNNING, delivery.STUCK_S, False, "", 0, S.FAILED),
])
def test_state_comes_from_hermes_rows(turn, state, age, earlier, prior, unchanged, expected):
    got, _detail = delivery.derive_state(turn, state=state, age_s=age, sent_by_earlier_process=earlier,
                                         prior_detail=prior, unchanged_s=unchanged)
    assert got == expected


# Submitting and reconciling ----------------------------------------------------

def test_attempt_is_recorded_before_it_is_sent(pulse_store):
    seen_in_store = []

    def send(chat_id, text):
        seen_in_store.append(bool(pulse_store.in_flight()))
        return host.Sent(True, "streaming")

    row = delivery.submit(pulse_store, send, kind="probe", chat_id=SID, text=lambda tag: f"check {tag}")
    assert seen_in_store == [True]
    assert row["state"] == store.SUBMITTED and row["tag"] in row["text"] and row["session_key"] == SID


def test_a_turn_behind_another_is_queued(pulse_store):
    row = delivery.submit(pulse_store, _sent("queued"), kind="probe", chat_id=SID, text=lambda tag: tag)
    assert row["state"] == store.QUEUED


@pytest.mark.parametrize("send", [lambda chat_id, text: host.Sent(False, "", "couldn't open the chat: gone"),
                                  lambda chat_id, text: (_ for _ in ()).throw(RuntimeError("down"))])
def test_a_refused_send_is_final(pulse_store, send):
    row = delivery.submit(pulse_store, send, kind="probe", chat_id=SID, text=lambda tag: tag)
    assert row["state"] == store.REFUSED
    assert pulse_store.in_flight() == []


def test_reconcile_follows_one_attempt_to_delivered(db, tmp_path, pulse_store):
    db.session()
    row = delivery.submit(pulse_store, _sent(), kind="probe", chat_id=SID,
                          text=lambda tag: f"Pulse delivery check {tag}.")
    reconcile = lambda: delivery.reconcile(pulse_store, tmp_path, process_started_at=row["created_at"] - 1,
                                           now=row["created_at"] + 1)
    assert reconcile() == []
    db.message("user", row["text"], display_kind="hidden")
    assert [r["state"] for r in reconcile()] == [store.RUNNING]
    db.message("assistant", "Check arrived.")
    assert [r["state"] for r in reconcile()] == [store.DELIVERED]
    assert pulse_store.attempt_by_tag(row["tag"])["detail"] == "Check arrived."


def test_an_attempt_from_before_a_restart_with_no_trace_was_not_admitted(db, tmp_path, pulse_store):
    db.session()
    row = delivery.submit(pulse_store, _sent(), kind="probe", chat_id=SID, text=lambda tag: tag)
    changed = delivery.reconcile(pulse_store, tmp_path, process_started_at=row["created_at"] + 1,
                                 now=row["created_at"] + 2)
    assert [r["state"] for r in changed] == [store.NOT_ADMITTED]


def test_unreadable_state_leaves_attempts_alone(tmp_path, pulse_store):
    delivery.submit(pulse_store, _sent(), kind="probe", chat_id=SID, text=lambda tag: tag)
    assert delivery.reconcile(pulse_store, tmp_path, process_started_at=0, now=time.time() + 999) == []
    assert pulse_store.in_flight()[0]["state"] == store.SUBMITTED


# Sending through the session host ------------------------------------------------

class FakeHost:
    """The two host methods, as tui_gateway.server's method table has them."""

    def __init__(self, resume=None, submit=None):
        self.calls = []
        self.resume = resume or {"result": {"session_id": "ui-1"}}
        self.submit = submit or {"result": {"status": "streaming"}}

    def install(self, monkeypatch):
        server = types.ModuleType("tui_gateway.server")
        server._methods = {
            host.RESUME: lambda rid, params: self.calls.append((host.RESUME, params)) or self.resume,
            host.SUBMIT: lambda rid, params: self.calls.append((host.SUBMIT, params)) or self.submit,
        }
        monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
        return self


def test_send_reopens_the_chat_and_submits_a_hidden_queued_turn(monkeypatch):
    fake = FakeHost().install(monkeypatch)
    assert host.send(SID, "Pulse delivery check p1-1.", profile="work") == host.Sent(True, "streaming", "streaming")
    assert fake.calls == [
        (host.RESUME, {"session_id": SID, "source": "desktop", "profile": "work"}),
        (host.SUBMIT, {"session_id": "ui-1", "text": "Pulse delivery check p1-1.", "display_kind": "hidden",
                       "queued": True}),
    ]


def test_send_reports_a_refusal(monkeypatch):
    FakeHost(resume={"error": {"code": 4007, "message": "session not found"}}).install(monkeypatch)
    sent = host.send(SID, "x")
    assert not sent.ok and "session not found" in sent.detail


def test_no_host_means_unavailable(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    assert not host.available()
    assert not host.send(SID, "x").ok


# Hooks and the loop ------------------------------------------------------------

@pytest.fixture
def live(tmp_path, monkeypatch):
    """Pulse as the dashboard process runs it, with Hermes's home in tmp_path."""
    fake = FakeHost().install(monkeypatch)
    ctx = types.SimpleNamespace(plugin_id="conduit_push", profile_name="default",
                                _manager=types.SimpleNamespace(has_tui_message_injector=True))
    monkeypatch.setattr(pulse, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(pulse, "_ctx", ctx)
    monkeypatch.setattr(pulse, "_started_at", time.time() - 10)
    monkeypatch.setattr(pulse, "_turns", {})
    monkeypatch.setattr(pulse, "_last_problem", "")
    sent = lambda: [params["text"] for name, params in fake.calls if name == host.SUBMIT]
    return types.SimpleNamespace(ctx=ctx, fake=fake, sent=sent, store=store.open_store(tmp_path))


def test_nothing_is_sent_while_pulse_is_off(live, db):
    db.session()
    pulse.tick()
    assert live.sent() == []


def test_nothing_is_sent_outside_the_chat_host(live, db, monkeypatch):
    db.session()
    live.store.set("enabled", True)
    live.ctx._manager.has_tui_message_injector = False  # the messaging gateway or the CLI
    pulse.tick()
    monkeypatch.delitem(sys.modules, "tui_gateway.server")
    live.ctx._manager.has_tui_message_injector = True
    pulse.tick()
    assert live.sent() == []


def test_a_probe_goes_to_the_home_chat_once_at_a_time(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    assert len(live.sent()) == 1
    assert live.fake.calls[0][1]["session_id"] == SID
    live.store.set("probe_requested", True)
    pulse.tick()
    assert len(live.sent()) == 1  # the first one is still in flight


def test_a_second_host_process_stays_quiet(live, db, monkeypatch):
    db.session()
    live.store.set("enabled", True)
    live.store.claim_leader("other-host:1", pulse.LEADER_TTL_S, time.time())
    pulse.tick()
    assert live.sent() == []


def test_no_home_chat_means_no_probe(live, db):
    live.store.set("enabled", True)
    pulse.tick()
    assert live.sent() == []
    chat, problem = pulse.home_chat(live.store, pulse.get_hermes_home())
    assert chat is None and "titled" in problem


def test_a_pulse_turn_gets_its_note_and_no_tools(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    text = live.sent()[0]
    result = plugin._pre_llm_call(session_id=SID, turn_id="t1", user_message=text, conversation_history=[])
    assert result == {"context": pulse.PROBE_NOTE}
    assert live.store.in_flight()[0]["state"] == store.RUNNING
    assert plugin._pre_tool_call(session_id=SID, turn_id="t1", tool_name="terminal", args={}) == {
        "action": "block", "message": pulse.BLOCK_MESSAGE}
    # Another turn in the same chat is the user's own.
    assert plugin._pre_tool_call(session_id=SID, turn_id="t2", tool_name="read_file", args={}) is None


def test_a_lookalike_tag_is_not_a_pulse_turn(live, db):
    db.session()
    live.store.set("enabled", True)
    assert pulse.pre_llm_call(SID, "t1", "Pulse delivery check p12345678-1.") is None
    assert not pulse.is_pulse_turn(SID, "t1")


def test_quoting_a_live_tag_is_not_a_pulse_turn(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    tag = live.store.in_flight()[0]["tag"]
    assert pulse.pre_llm_call(SID, "t1", f"Pulse sent me {tag}, what is it?") is None
    assert not pulse.is_pulse_turn(SID, "t1")


def test_a_pulse_turn_sends_no_push(live, db, monkeypatch):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    sent = []
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    plugin._pre_llm_call(session_id=SID, turn_id="t1", user_message=live.sent()[0], conversation_history=[])
    plugin._post_llm_call(session_id=SID, turn_id="t1", assistant_response="Check arrived.")
    plugin._on_session_end(session_id=SID, turn_id="t1", completed=True, failed=False, interrupted=False)
    plugin._pre_llm_call(session_id=SID, turn_id="t2", user_message=live.sent()[0], conversation_history=[])
    plugin._on_session_end(session_id=SID, turn_id="t2", completed=False, failed=True, interrupted=False)
    assert sent == []


def test_the_end_of_a_pulse_turn_is_noted_on_its_attempt(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    pulse.pre_llm_call(SID, "t1", live.sent()[0])
    pulse.turn_ended(SID, "t1")
    assert live.store.in_flight()[0]["detail"] == delivery.ENDED


def test_a_stuck_probe_makes_way_for_the_next(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    row = live.store.in_flight()[0]
    db.message("user", row["text"], display_kind="hidden")
    pulse.tick(now=row["created_at"] + 6 * 3600 + 1)  # stuck, and the next one is due
    assert live.store.attempt_by_tag(row["tag"])["state"] == store.FAILED
    assert len(live.sent()) == 2


def test_the_loop_leaves_no_file_while_never_turned_on(live, tmp_path):
    (tmp_path / store.DB_NAME).unlink()
    pulse.tick()
    assert not (tmp_path / store.DB_NAME).exists()


def test_the_loop_stops_on_unload(monkeypatch):
    monkeypatch.setattr(pulse, "_thread", None)
    ctx = types.SimpleNamespace(plugin_id="conduit_push", _manager=types.SimpleNamespace())
    pulse.register(ctx)
    thread = pulse._thread
    pulse.register(ctx)  # loaded again: still one loop
    assert pulse._thread is thread and thread.is_alive()
    pulse.stop(timeout=2.0)
    assert not thread.is_alive()


# The command ---------------------------------------------------------------------

def test_pulse_command(live, db, capsys, monkeypatch):
    db.session()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda *argv: cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action=argv[0],
                                                           value=argv[1] if len(argv) > 1 else ""))
    assert run("probe") == 1
    assert run("on") == 0 and live.store.get("enabled") is True
    assert run("probe") == 0 and live.store.get("probe_requested") is True
    assert run("status") == 0
    out = capsys.readouterr().out
    assert f"Home chat: Pulse ({SID})" in out
    assert "hasn't run Pulse" in out  # no tick yet
    pulse.tick()
    assert run("status") == 0 and "Dashboard last checked" in capsys.readouterr().out
    assert run("home", "other-chat") == 0 and live.store.get("home_session_key") == "other-chat"
    assert run("status") == 0 and "no open chat has the id other-chat" in capsys.readouterr().out
    assert run("home", "auto") == 0 and live.store.get("home_session_key") == ""
    assert run("off") == 0 and live.store.get("enabled") is False


def test_reading_pulse_before_it_was_ever_on_leaves_no_file(live, tmp_path, monkeypatch):
    (tmp_path / store.DB_NAME).unlink()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    for action in ("status", "off", "probe"):
        cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action=action, value=""))
    assert not (tmp_path / store.DB_NAME).exists()


# The outcome ledger ---------------------------------------------------------------

@pytest.fixture
def recording(live):
    ledger.forget_cache()
    live.store.set("enabled", True)
    yield live
    ledger.forget_cache()


def _call(**overrides):
    kwargs = dict(tool_name="conduit_phone", args={"action": "add_reminder", "title": "Call  the\ndentist",
                                                   "notes": "secret"},
                  result=json.dumps({"ok": True, "reminder": {"id": "R123", "title": "Call the dentist"}}),
                  session_id=SID, turn_id="t1", task_id="task", tool_call_id="call-1", status="ok",
                  error_type=None, error_message=None, duration_ms=42)
    kwargs.update(overrides)
    return kwargs


def test_a_tool_call_leaves_one_outcome_row(recording):
    plugin._post_tool_call(**_call())
    [row] = recording.store.recent_outcomes()
    assert (row["tool"], row["action"], row["label"], row["status"]) == (
        "conduit_phone", "add_reminder", "Call the dentist", "ok")
    assert (row["session_id"], row["turn_id"], row["tool_call_id"], row["duration_ms"]) == (SID, "t1", "call-1", 42)
    assert json.loads(row["ids_json"]) == {"reminder.id": "R123"}
    assert "secret" not in json.dumps(row)  # arguments are digested, not kept
    assert ledger.line(row) == "conduit_phone add_reminder 'Call the dentist' → ok, reminder.id R123"


def test_a_failed_call_keeps_its_error(recording):
    plugin._post_tool_call(**_call(status="error", error_type="tool_error", error_message="phone unreachable",
                                   result=json.dumps({"error": "phone unreachable"})))
    [row] = recording.store.recent_outcomes()
    assert row["status"] == "error" and row["error"] == "phone unreachable"
    assert ledger.line(row).endswith("→ error: phone unreachable")


def test_without_hermes_status_the_result_decides(recording):
    plugin._post_tool_call(**_call(status=None, result=json.dumps({"error": "no such job"})))
    plugin._post_tool_call(**_call(status=None, result="plain text output"))
    rows = recording.store.recent_outcomes()
    assert [(r["status"], r["error"]) for r in rows] == [("ok", ""), ("error", "no such job")]


def test_nothing_is_recorded_while_pulse_is_off(live):
    ledger.forget_cache()
    plugin._post_tool_call(**_call())
    assert live.store.recent_outcomes() == []


def test_the_ledger_leaves_no_file_while_never_turned_on(live, tmp_path):
    ledger.forget_cache()
    (tmp_path / store.DB_NAME).unlink()
    plugin._post_tool_call(**_call())
    assert not (tmp_path / store.DB_NAME).exists()


def test_a_ledger_failure_never_breaks_the_call(recording, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store.PulseStore, "add_outcome", boom)
    plugin._post_tool_call(**_call())  # no exception


def test_subagent_calls_count_too(recording, monkeypatch):
    monkeypatch.setattr(plugin, "_is_child", lambda session_id: True)
    plugin._post_tool_call(**_call(session_id="child"))
    assert [r["session_id"] for r in recording.store.recent_outcomes()] == ["child"]


@pytest.mark.parametrize("result, expected", [
    ({"id": 7, "path": "/tmp/a.txt", "ok": True}, {"id": "7", "path": "/tmp/a.txt"}),
    ('{"job_id": "j1", "watchId": "w2", "valid": true}', {"job_id": "j1", "watchId": "w2"}),
    ({"created": [{"event_id": "E9"}]}, {"created[0].event_id": "E9"}),
    ({"id": True, "data": {"url": "https://example.com/x"}}, {"data.url": "https://example.com/x"}),
    ("not json", {}),
    (None, {}),
    ({f"k{i}_id": str(i) for i in range(20)}, {f"k{i}_id": str(i) for i in range(ledger.MAX_IDS)}),
])
def test_identifiers_come_from_the_result(result, expected):
    assert ledger.extract_ids(result) == expected


def test_same_arguments_give_the_same_digest():
    assert ledger.describe_call({"a": 1, "b": [2]})[2] == ledger.describe_call({"b": [2], "a": 1})[2]
    assert ledger.describe_call({"a": 1})[2] != ledger.describe_call({"a": 2})[2]
    assert ledger.describe_call(None) == ("", "", ledger.describe_call({})[2])


def test_old_outcomes_are_pruned(recording):
    now = time.time()
    plugin._post_tool_call(**_call(tool_call_id="old"))
    with sqlite3.connect(recording.store.path) as conn:
        conn.execute("UPDATE outcomes SET at = ?", (now - ledger.RETENTION_S - 1,))
    assert recording.store.prune_outcomes(before=now - ledger.RETENTION_S, keep=ledger.MAX_ROWS) == 1
    for n in range(5):
        plugin._post_tool_call(**_call(tool_call_id=f"c{n}"))
    assert recording.store.prune_outcomes(before=0, keep=3) == 2
    assert [r["tool_call_id"] for r in recording.store.recent_outcomes()] == ["c4", "c3", "c2"]


def test_outcomes_command(recording, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="": cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="outcomes",
                                                              value=value))
    assert run() == 0 and "No tool outcomes recorded yet." in capsys.readouterr().out
    plugin._post_tool_call(**_call())
    plugin._post_tool_call(**_call(session_id="other", tool_name="terminal", args={}))
    assert run() == 0
    out = capsys.readouterr().out
    assert out.index("conduit_phone") < out.index("terminal")  # oldest first
    assert run("other") == 0 and "conduit_phone" not in capsys.readouterr().out
    assert run("1") == 0 and capsys.readouterr().out.count("\n") == 1
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Tool outcomes in the last 24 hours: 2 ok" in capsys.readouterr().out
