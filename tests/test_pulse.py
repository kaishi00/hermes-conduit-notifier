"""Pulse slice 0: the home chat delivery probe (designs/proactivity.md 6.4.1, 6.4.2, 7.6)
the outcome ledger (3.6), and the daily shadow report (13, slice 0 (d)).

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
import re
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
report = _load("conduit_push.pulse_report", ROOT / "pulse_report.py")
memory = _load("conduit_push.pulse_memory", ROOT / "pulse_memory.py")

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

    def session(self, sid=SID, title="Pulse", pinned=1, archived=0, started=1000.0, parent=None, source="desktop",
                hidden=0):
        # Conduit's chats: source desktop, no gateway session key.
        self._exec("INSERT INTO sessions(id, source, session_key, parent_session_id, started_at, last_activity_at, "
                   "title, pinned, archived, hidden) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (sid, source, None, parent, started, started, title, pinned, archived, hidden))

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


def test_a_turn_compressed_midway_is_followed_into_the_new_session(db, tmp_path):
    db.session()
    db.message("user", "Pulse shadow report pBBBB-1.", display_kind="hidden")
    db.session(sid="tip", parent=SID)
    db.message("assistant", "an older reply carried over", sid="tip")
    db.message("user", "Pulse shadow report pBBBB-1.", sid="tip", display_kind="hidden")  # carried
    db.message("assistant", "1. [date] Mum's birthday Friday", sid="tip")
    db.message("user", "1 yes", sid="tip")
    turn = hermes.find_turn(tmp_path, chat_id=SID, tag="pBBBB-1", since=0)
    assert (turn.session_id, turn.reply) == ("tip", "1. [date] Mum's birthday Friday")
    assert hermes.user_text_after(tmp_path, session_id="tip", tag="pBBBB-1") == ["1 yes"]


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
    monkeypatch.setattr(pulse, "_last_ratings", 0.0)
    monkeypatch.setattr(pulse, "_zone_cache", (0.0, None))
    sent = lambda: [params["text"] for name, params in fake.calls if name == host.SUBMIT]
    pulse_store = store.open_store(tmp_path)
    # The daily report runs on the clock; its own tests turn it on.
    pulse_store.set("daily_report", False)
    pulse_store.set("memory", False)  # likewise memory, which needs a model
    return types.SimpleNamespace(ctx=ctx, fake=fake, sent=sent, store=pulse_store)


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


def test_a_process_with_an_older_build_leaves_the_loop(live, db, monkeypatch, capsys):
    db.session()
    live.store.set("enabled", True)
    live.store.set("probe_requested", True)
    monkeypatch.setattr(pulse, "build_stamp", lambda: "newer")  # installed over since this process started
    pulse.tick()
    assert live.sent() == [] and live.store.get(store.LEADER) is None
    monkeypatch.setattr(pulse, "_build", "newer")  # a process started on the new build
    pulse.tick()
    assert len(live.sent()) == 1 and live.store.get(store.LEADER)["build"] == "newer"


def test_status_names_a_loop_on_an_older_build(live, db, capsys, monkeypatch):
    db.session()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    live.store.set("enabled", True)
    live.store.set("last_tick_at", time.time())
    status = lambda: cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    live.store.claim_leader("Hermes:16293", pulse.LEADER_TTL_S, time.time())  # an old build writes no stamp
    status()
    assert "run by process Hermes:16293, which loaded an older build" in capsys.readouterr().out
    live.store.claim_leader("Hermes:16293", pulse.LEADER_TTL_S, time.time(), build=pulse.build_stamp())
    status()
    assert "older build" not in capsys.readouterr().out


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


def test_urls_lose_their_credentials(recording):
    plugin._post_tool_call(**_call(tool_name="web", args={"url": "https://me:tok@example.com/a?api_key=s#frag"},
                                   result=json.dumps({"url": "https://example.com:8443/b?token=s"})))
    [row] = recording.store.recent_outcomes()
    assert row["label"] == "https://example.com/a"
    assert json.loads(row["ids_json"]) == {"url": "https://example.com:8443/b"}
    assert "tok" not in json.dumps(row) and "api_key" not in json.dumps(row)


def test_a_digest_line_has_no_control_characters():
    row = {"tool": "t", "action": "", "label": "a\x1b[31mred", "status": "error", "error": "x\x07", "ids_json": "{}"}
    assert ledger.line(row) == "t 'a[31mred' → error: x"


@pytest.mark.parametrize("value, expected", [
    ("example.com/x?api_key=k", "example.com/x"),
    ("https://[::1]:8443/b?t=1", "https://[::1]:8443/b"),
    ("plain words? fine", "plain words? fine"),
])
def test_url_like_values_are_cleaned(value, expected):
    assert ledger._safe_url(value) == expected


def test_a_float_duration_is_kept(recording):
    plugin._post_tool_call(**_call(duration_ms=12.7))
    assert recording.store.recent_outcomes()[0]["duration_ms"] == 12


# The daily shadow report -------------------------------------------------------------

REPLY = """Here's what I would have told you today:
1. [commitment] Send the deck to Mara before Friday (you said so in Work)
2. [promise] I said I'd check the build; it failed (tool calls)
3. [weird] Something else
Rate them: reply like 1 yes, 2 no."""


def _report_day(db, now):
    db.session()
    db.session("work", title="Work", pinned=0)
    db.session("cronrun", title="Nightly", pinned=0, source="cron")
    db.session("secret", title="Hidden", pinned=0, hidden=1)
    db.message("user", "I'll send the deck to Mara before Friday", sid="work", at=now - 3600)
    db.message("assistant", "Noted.", sid="work", at=now - 3500)
    db.message("user", "Ancient history", sid="work", at=now - 3 * 86400)
    db.message("user", "nightly job text", sid="cronrun", at=now - 60)
    db.message("user", "hidden chat text", sid="secret", at=now - 60)
    db.message("user", "internal note", sid="work", display_kind="hidden", at=now - 60)
    db.message("user", "home chat text", at=now - 60)


def test_the_report_context_is_the_recent_real_chats(live, db, tmp_path):
    now = time.time()
    _report_day(db, now)
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "USER.md").write_text("Eric likes short answers.")
    ledger.forget_cache()
    live.store.set("enabled", True)
    plugin._post_tool_call(**_call(tool_name="build", status="error", error_message="exit 1", args={}))
    context, since = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    assert since == report._local(now - report.WINDOW_S, report.zone_for(None))  # no report yet: the last day
    assert "Eric" not in report.REPORT_TEXT + report.REPORT_NOTE
    assert "### Work" in context and "User: I'll send the deck to Mara before Friday" in context
    assert "Hermes: Noted." in context
    for absent in ("Ancient history", "nightly job text", "hidden chat text", "internal note", "home chat text"):
        assert absent not in context
    assert "Eric likes short answers." in context
    assert "build → error: exit 1" in context
    text = report.report_text("pabc-1", context, since)
    fence = re.search(r"<<<context (\w+)\n", text).group(1)
    assert text.endswith(f"context {fence}>>>") and report.report_text("pabc-1", context, since) != text
    assert f"covering {since} onward" in text and text.startswith("Pulse shadow report pabc-1.") and context in text
    ledger.forget_cache()


def test_a_report_looks_back_to_the_last_delivered_one(pulse_store):
    now = time.time()
    assert report.window_start(pulse_store, now) == now - report.WINDOW_S
    pulse_store.add_attempt(attempt_id="r:1", item_id="r", attempt=1, kind="report", tag="pr-1", text="x",
                            session_key=SID)
    pulse_store.update_attempt("r:1", state=store.DELIVERED)
    assert report.window_start(pulse_store, now) == now - report.MIN_WINDOW_S  # just sent: at least 4 h back
    with sqlite3.connect(pulse_store.path) as conn:
        conn.execute("UPDATE attempts SET created_at=?", (now - 6 * 3600,))
    assert report.window_start(pulse_store, now) == now - 6 * 3600
    pulse_store.add_attempt(attempt_id="r2:1", item_id="r2", attempt=1, kind="report", tag="pr2-1", text="x",
                            session_key=SID)
    pulse_store.update_attempt("r2:1", state=store.NOT_ADMITTED)  # a failed one doesn't count
    assert report.window_start(pulse_store, now) == now - 6 * 3600


def test_the_report_reads_every_profile(live, db, tmp_path, request):
    now = time.time()
    db.session()
    db.message("user", "home chat text", at=now - 60)
    bot_home = tmp_path / "profiles" / "nahida"
    bot_home.mkdir(parents=True)
    bot = StateDB(bot_home, request.node.callspec.params["db"])
    bot.session("b1", title="Bot Chat", pinned=0)
    bot.message("user", "Remind me to call the landlord", sid="b1", at=now - 600)
    (bot_home / "memories").mkdir()
    (bot_home / "memories" / "USER.md").write_text("Prefers mornings.")
    (bot_home / "cron").mkdir()
    (bot_home / "cron" / "jobs.json").write_text(json.dumps([{"id": "j", "name": "Rent", "prompt": "Pay rent"}]))
    (tmp_path / "profiles" / "empty").mkdir()  # no state.db: not a profile with chats
    assert report.profile_homes(tmp_path) == [("nahida", bot_home)]
    assert report.profile_homes(bot_home) == [("default", tmp_path)]
    context, _ = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    assert "### [nahida] Bot Chat\n" in context and "User: Remind me to call the landlord" in context
    assert "home chat text" not in context
    assert "(USER.md, profile nahida)\nPrefers mornings." in context and "- [nahida] Rent" in context
    live.store.set("report_profiles", "this")
    context, _ = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    assert "nahida" not in context


def test_the_report_lists_active_cron_jobs(tmp_path):
    assert report.cron_jobs(tmp_path) == ""
    (tmp_path / "cron").mkdir()
    jobs = [
        {"id": "a1", "name": "Check the build", "prompt": "Look at CI and tell me", "schedule_display": "once at 09:00",
         "next_run_at": "2026-10-21T10:30:00+00:00", "enabled": True, "state": "scheduled"},
        {"id": "b2", "name": "Old job", "prompt": "x", "enabled": False, "state": "paused"},
        {"id": "c3", "name": "Morning news", "prompt": "News", "schedule_display": "every day at 07:00",
         "next_run_at": "2026-10-21T07:00:00-04:00"},
    ]
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    text = report.cron_jobs(tmp_path)
    assert text.splitlines() == [
        "## Hermes's scheduled jobs",
        "- Check the build (once at 09:00, next 2026-10-21T10:30:00+00:00): Look at CI and tell me",
        "- Morning news (every day at 07:00, next 2026-10-21T07:00:00-04:00): News",
    ]  # 10:30 UTC is 06:30 in New York: before 07:00 there, though it sorts after it as text
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": {job["id"]: job for job in jobs}}))
    assert report.cron_jobs(tmp_path) == text  # keyed by id
    for broken in ("{broken", '{"jobs": null}', '{"jobs": 5}'):
        (tmp_path / "cron" / "jobs.json").write_text(broken)
        assert report.cron_jobs(tmp_path) == ""


def test_the_report_context_stays_within_its_budget(db, tmp_path, pulse_store):
    now = time.time()
    db.session()
    for n in range(40):
        db.session(f"c{n}", title=f"Chat {n}", pinned=0)
        for m in range(20):
            db.message("user", "x" * 1000, sid=f"c{n}", at=now - 3600 + n * 10 + m)
    chats = report.recent_chats(tmp_path, home_chat_id=SID, since=now - 86400, zone=report.zone_for("UTC"))
    assert len(chats) <= report.CHATS_BUDGET + 200
    assert chats.startswith("### Chat 39")  # the most recent chat first


def test_items_and_ratings_are_read_from_the_text():
    items = report.report_items(REPLY)
    assert items == {1: "commitment", 2: "promise", 3: "other"}
    assert report.report_items("1. [Commitment] Call Mum") == {1: "commitment"}
    ratings = report.parse_ratings("1 yes, 2: no, 3 y, 7 yes, 1 n", items)
    assert ratings == [report.Rating(1, "commitment", False), report.Rating(2, "promise", False),
                       report.Rating(3, "other", True)]
    assert report.parse_ratings("2 👍", items) == [report.Rating(2, "promise", True)]
    assert report.parse_ratings("I said no 1 time", items) == []


def _due_report(live):
    live.store.set("enabled", True)
    live.store.set("daily_report", True)
    live.store.set("time_zone", "UTC")
    live.store.set("probe_interval_hours", 1000)
    live.store.add_attempt(attempt_id="old:1", item_id="old", attempt=1, kind=pulse.PROBE, tag="pold-1", text="x",
                           session_key=SID)
    live.store.update_attempt("old:1", state=store.DELIVERED)  # no probe due


def test_the_report_goes_out_once_after_its_time(live, db):
    db.session()
    _due_report(live)
    now = time.time()
    today = time.strftime("%H:%M", time.gmtime(now - 60))
    live.store.set("report_time", time.strftime("%H:%M", time.gmtime(now + 3600)))
    pulse.tick(now=now)
    assert live.sent() == []  # not time yet
    live.store.set("report_time", today)
    pulse.tick(now=now)
    [text] = live.sent()
    assert text.startswith("Pulse shadow report")
    row = live.store.in_flight()[0]
    assert row["kind"] == pulse.REPORT
    pulse.tick(now=now + 60)
    assert len(live.sent()) == 1  # in flight
    live.store.update_attempt(row["attempt_id"], state=store.DELIVERED)
    pulse.tick(now=now + 120)
    assert len(live.sent()) == 1  # delivered today


def test_a_failed_report_is_tried_again_later_that_day(live, db):
    db.session()
    _due_report(live)
    now = time.time()
    live.store.set("report_time", time.strftime("%H:%M", time.gmtime(now - 60)))
    step = pulse.REPORT_RETRY_S + 30
    for n in range(pulse.REPORT_TRIES):
        pulse.tick(now=now + n * step)
        row = live.store.in_flight()[0]
        live.store.update_attempt(row["attempt_id"], state=store.NOT_ADMITTED)
        with sqlite3.connect(live.store.path) as conn:  # as if it was marked at the tick's time
            conn.execute("UPDATE attempts SET updated_at=? WHERE attempt_id=?", (now + n * step, row["attempt_id"]))
        pulse.tick(now=now + n * step + 60)  # too soon to retry
        assert len(live.sent()) == n + 1
    pulse.tick(now=now + 10 * step)
    assert len(live.sent()) == pulse.REPORT_TRIES


def test_a_report_long_past_its_time_waits_for_the_next_one(live, db):
    db.session()
    _due_report(live)
    evening = time.time() - time.time() % 86400 + 20 * 3600  # 20:00 UTC, 12 h after 08:00
    live.store.set("report_time", "08:00")
    pulse.tick(now=evening)
    assert live.sent() == []
    pulse.tick(now=evening - 12 * 3600 + pulse.REPORT_WINDOW_S - 60)  # inside the window
    assert len(live.sent()) == 1


def test_a_report_turn_gets_the_report_note_and_no_tools(live, db):
    db.session()
    _due_report(live)
    live.store.set("report_requested", True)
    pulse.tick()
    [text] = live.sent()
    assert pulse.pre_llm_call(SID, "t9", text) == report.REPORT_NOTE
    assert pulse.pre_tool_call(SID, "t9") == {"action": "block", "message": pulse.BLOCK_MESSAGE}


def test_a_probe_and_a_report_due_together_go_on_separate_ticks(live, db):
    db.session()
    live.store.set("enabled", True)
    live.store.set("probe_requested", True)
    live.store.set("report_requested", True)
    pulse.tick()
    assert len(live.sent()) == 1 and live.sent()[0].startswith("Pulse delivery check")
    pulse.tick()
    assert len(live.sent()) == 2 and live.sent()[1].startswith("Pulse shadow report")


def test_ratings_are_read_back_and_counted(live, db):
    db.session()
    _due_report(live)
    live.store.set("report_requested", True)
    pulse.tick()
    row = live.store.in_flight()[0]
    db.message("user", row["text"], display_kind="hidden")
    db.message("assistant", REPLY)
    db.message("user", "1 yes, 2 no")
    db.message("user", "Pulse delivery check pzz-1.", display_kind="hidden")
    db.message("user", "3 yes")  # belongs to the next Pulse turn
    pulse.tick(now=time.time() + 1)
    assert live.store.attempt_by_tag(row["tag"])["state"] == store.DELIVERED
    pulse._last_ratings = 0.0
    pulse.tick(now=time.time() + 2)
    assert pulse.rating_counts(live.store) == {"commitment": (1, 0), "promise": (0, 1)}


def test_report_command(live, db, capsys, monkeypatch):
    db.session()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="": cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="report",
                                                              value=value))
    assert run() == 1  # Pulse is off
    live.store.set("enabled", True)
    assert run("now") == 0 and live.store.get("report_requested") is True
    assert run("7:30") == 0 and live.store.get("report_time") == "07:30"
    assert run("25:00") == 2
    assert run("Europe/Paris") == 0 and live.store.get("time_zone") == "Europe/Paris"
    assert run("Mars/Olympus") == 2
    assert run("on") == 0 and live.store.get("daily_report") is True
    assert run("this-profile") == 0 and live.store.get("report_profiles") == "this"
    assert run("all-profiles") == 0 and live.store.get("report_profiles") == "all"
    capsys.readouterr()
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Shadow reports at 07:30 (Europe/Paris); next " in capsys.readouterr().out
    assert run("8:00, 17:00,12:30") == 0 and live.store.get("report_time") == "08:00,12:30,17:00"
    assert run("8:00,24:00") == 2 and live.store.get("report_time") == "08:00,12:30,17:00"
    assert run("off") == 0 and live.store.get("daily_report") is False
    (live.store.path).unlink()  # never turned on: report settings still stick
    assert run("off") == 0 and store.open_store(live.store.path.parent).get("daily_report") is False


def test_the_report_times():
    zone = report.zone_for("UTC")
    midnight = 1_760_000_000 - (1_760_000_000 % 86400)
    hours = lambda h: midnight + h * 3600
    assert report.report_times("18:00, 8:00,bad,08:00") == [(8, 0), (18, 0)]
    assert report.clock_text("bad") == "08:00, 13:00, 18:00"
    assert report.last_slot(hours(6), zone, "08:00") == (hours(8 - 24), hours(8 - 48))
    assert report.next_time(hours(6), zone, "08:00") == hours(8)
    assert report.last_slot(hours(9), zone, "08:00") == (hours(8), hours(8 - 24))
    assert report.next_time(hours(9), zone, "08:00") == hours(8 + 24)
    times = "08:00,13:00,18:00"
    assert report.last_slot(hours(6), zone, times) == (hours(18 - 24), hours(13 - 24))
    assert report.last_slot(hours(13), zone, times) == (hours(13), hours(8))
    assert report.next_time(hours(13), zone, times) == hours(18)
    assert report.next_time(hours(19), zone, times) == hours(8 + 24)


def test_each_report_time_sends_once(live, db):
    db.session()
    _due_report(live)
    midnight = time.time() - time.time() % 86400
    live.store.set("report_time", "08:00,13:00,18:00")

    def deliver_at(hour):
        pulse.tick(now=midnight + hour * 3600)
        rows = live.store.in_flight()
        for row in rows:
            live.store.update_attempt(row["attempt_id"], state=store.DELIVERED)
            with sqlite3.connect(live.store.path) as conn:  # as if sent at the tick's time
                conn.execute("UPDATE attempts SET created_at=? WHERE attempt_id=?",
                             (midnight + hour * 3600, row["attempt_id"]))
        return len(rows)

    assert [deliver_at(h) for h in (7, 8.1, 9, 12.9, 13.1, 13.5, 16)] == [0, 1, 0, 0, 1, 0, 0]
    live.store.set("report_requested", True)
    assert deliver_at(17) == 1  # an early one, asked for
    assert deliver_at(18.1) == 0  # stands in for 18:00


def test_a_finished_report_keeps_only_its_first_line(live, db):
    db.session()
    _due_report(live)
    live.store.set("report_requested", True)
    pulse.tick()
    row = live.store.in_flight()[0]
    assert "\n" in row["text"]
    live.store.update_attempt(row["attempt_id"], state=store.DELIVERED)
    pulse.tick(now=time.time() + 1)
    assert live.store.attempt_by_tag(row["tag"])["text"] == f"Pulse shadow report {row['tag']}."


def test_an_early_report_stands_in_for_the_scheduled_one(live, db):
    db.session()
    _due_report(live)
    now = time.time()
    live.store.set("report_requested", True)
    pulse.tick(now=now - 60)
    live.store.update_attempt(live.store.in_flight()[0]["attempt_id"], state=store.DELIVERED)
    live.store.set("report_time", time.strftime("%H:%M", time.gmtime(now - 30)))
    pulse.tick(now=now)
    assert len(live.sent()) == 1


def test_the_whole_home_chain_is_left_out_of_the_report(db, tmp_path):
    db.session("root", title="Pulse")
    db.session(SID, title="Pulse", parent="root")  # the home chat set by id to a compression tip
    db.session("tip", title="Pulse", parent=SID)
    db.session("branch", title="Pulse", parent="root")  # another branch of the same chat
    db.session("orphan", title="Pulse", parent="deleted")  # a chain whose root is gone
    db.session("orphan-tip", title="Pulse", parent="orphan")
    db.session("w", title="Work", pinned=0)
    now = time.time()
    for sid in ("root", SID, "tip", "branch", "orphan", "orphan-tip", "w"):
        db.message("user", f"from {sid}", sid=sid, at=now - 60)
    rows = hermes.recent_messages(tmp_path, exclude_chat=SID, since=0)
    assert [row["content"] for row in rows] == ["from orphan", "from orphan-tip", "from w"]
    rows = hermes.recent_messages(tmp_path, exclude_chat="orphan-tip", since=0)
    assert "from orphan" not in [row["content"] for row in rows]


def test_recent_messages_are_bounded(db, tmp_path):
    db.session()
    db.session("w", title="Work", pinned=0)
    now = time.time()
    for n in range(30):
        db.message("user", f"{n:02d}" + "y" * 5000, sid="w", at=now - 100 + n)
    rows = hermes.recent_messages(tmp_path, exclude_chat=SID, since=0, max_chars=50, limit=10)
    assert [row["content"][:2] for row in rows] == [f"{n:02d}" for n in range(20, 30)]
    assert all(len(row["content"]) == 50 for row in rows)


# Memory (slice 1) -------------------------------------------------------------------

UTC = report.zone_for("UTC")
DAY = 1_792_454_400.0  # 2026-10-20 00:00 UTC, a Tuesday


def _row(n, role, content, at):
    return {"id": n, "role": role, "content": content, "timestamp": at}


class FakeModel:
    """The memory model: returns the queued replies in turn, and keeps what it was shown."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.shown = []

    def __call__(self, instructions, text):
        self.shown.append(text)
        reply = self.replies.pop(0) if self.replies else {"items": []}
        if isinstance(reply, Exception):
            raise reply
        return reply, 100


def test_memory_chunks_split_on_dates_gaps_and_size():
    rows = [_row(1, "user", "a", DAY + 3600), _row(2, "assistant", "b", DAY + 3700),
            _row(3, "user", "c", DAY + 3700 + memory.GAP_S + 1),  # a long gap
            _row(4, "user", "d", DAY + 86400 + 60),  # the next day
            _row(5, "assistant", "e", DAY + 86400 + 120)]
    assert [[r["id"] for r in c] for c in memory.chunks(rows, UTC, DAY + 3 * 86400)] == [[1, 2], [3], [4, 5]]
    big = [_row(n, "user", "x" * 5000, DAY + n) for n in range(1, 6)]
    assert [len(c) for c in memory.chunks(big, UTC, DAY + 86400)] == [2, 2, 1]


def test_an_unanswered_message_waits_for_its_reply():
    rows = [_row(1, "user", "a", DAY), _row(2, "assistant", "b", DAY + 60), _row(3, "user", "are you there?", DAY + 120)]
    assert [[r["id"] for r in c] for c in memory.chunks(rows, UTC, DAY + 3600)] == [[1, 2]]
    assert [[r["id"] for r in c] for c in memory.chunks(rows, UTC, DAY + memory.HOLD_S + 200)] == [[1, 2, 3]]


def test_long_code_is_cut_and_tool_outcomes_sit_where_they_happened():
    chunk = [_row(1, "user", "set it up", DAY + 60), _row(2, "assistant", "```\n" + "x\n" * 300 + "```done", DAY + 180)]
    outcome = {"at": DAY + 120, "tool": "conduit_phone", "action": "add_reminder", "label": "Dentist",
               "status": "ok", "ids_json": "{}"}
    text = memory.render(chunk, UTC, [outcome])
    assert text.split("\n") == ["[1] Tue 2026-10-20 00:01 User: set it up",
                                "    (tool) conduit_phone add_reminder 'Dentist' → ok",
                                "[2] Tue 2026-10-20 00:03 Hermes: [code, 302 lines]done"]


def test_only_items_said_in_the_chat_are_kept():
    chunk = [_row(11, "user", "I’ll call the dentist on Friday, promise", DAY + 60),
             _row(12, "assistant", "I'll check the build tomorrow morning and tell you.", DAY + 120)]
    model = FakeModel({"items": [
        {"kind": "commitment", "text": "The user will call the dentist.", "evidence": "I'll call the dentist on Friday",
         "line": 1, "explicit": True, "due": "2026-10-23", "due_words": "on Friday"},
        {"kind": "promise", "text": "Hermes will check the build.", "evidence": "\"I'll check the build ... tell you\"",
         "line": 7, "explicit": True, "due": "2026-10-21T09:00", "due_words": "at nine"},  # never said
        {"kind": "fact", "text": "The user's sister lives in Lyon.", "evidence": "my sister lives in Lyon", "line": 1},
        {"kind": "mood", "text": "x", "evidence": "promise", "line": 1},
        "not an item"]})
    items, tokens = memory.extract(model, chunk, UTC)
    assert tokens == 100 and "[1] Tue 2026-10-20 00:01 User: I’ll call the dentist" in model.shown[0]
    assert [(i["kind"], i["message_id"], i["explicit"], i["due"]) for i in items] == [
        ("commitment", 11, 1, "2026-10-23"), ("promise", 12, 1, "")]
    assert items[0]["due_at"] == DAY + 3 * 86400 and items[0]["mentioned_at"] == DAY + 60
    with pytest.raises(memory.BadReply):
        memory.extract(FakeModel(["items"]), chunk, UTC)


def test_a_due_date_must_be_said_near_its_item():
    chunk = [_row(1, "user", "Book the vet", DAY + 60), _row(2, "assistant", "When?", DAY + 70),
             _row(3, "user", "Thursday works", DAY + 80), _row(4, "user", "Also my sister visits", DAY + 90)]
    item = lambda evidence: {"kind": "commitment", "text": "x", "evidence": evidence, "due": "2026-10-22",
                             "due_words": "Thursday"}
    items, _ = memory.extract(FakeModel({"items": [item("Book the vet"), item("my sister visits")]}), chunk, UTC)
    assert [i["due"] for i in items] == ["", "2026-10-22"]  # two lines away, then the line just after


def test_chat_reads_match_hermes(db, tmp_path):
    db.session()
    db.session("work", title="Work", pinned=0)
    db.session("work2", title="Work", pinned=0, parent="work")
    db.session("cronrun", title="Nightly", pinned=0, source="cron")
    db.message("user", "home chat text", at=DAY + 10)
    first = db.message("user", "hello", sid="work", at=DAY + 20)
    db.message("tool", "tool output", sid="work", at=DAY + 25)
    db.message("assistant", "hi", sid="work", at=DAY + 30)
    db.message("user", "later", sid="work2", at=DAY + 40)
    db.message("user", "nightly", sid="cronrun", at=DAY + 50)
    rows = hermes.chat_activity(tmp_path, exclude_chat=SID, since=DAY)
    assert [(r["chat_id"], r["parent"], r["last_at"]) for r in rows] == [("work", None, DAY + 30),
                                                                        ("work2", "work", DAY + 40)]
    rows = hermes.chat_messages(tmp_path, chat_id="work", after_id=first, since=0, max_chars=1, limit=10)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "h")]


@pytest.fixture
def remembering(live, db, monkeypatch):
    live.store.set("memory_since", DAY)
    db.session()
    db.session("work", title="Work", pinned=0)
    return live


def _remember(live, model, now, sources=None):
    return memory.run(live.store, sources or [("", live.store.path.parent, SID)], model, zone=UTC, now=now)


def test_a_quiet_chat_is_read_once_from_where_it_stopped(remembering, db):
    db.message("user", "home chat text", at=DAY + 10)
    db.message("user", "Remind me to renew my passport", sid="work", at=DAY + 60)
    db.message("assistant", "Will do.", sid="work", at=DAY + 70)
    model = FakeModel({"items": [{"kind": "commitment", "text": "The user will renew their passport.",
                                  "evidence": "renew my passport", "line": 1, "explicit": True}]})
    assert _remember(remembering, model, DAY + 600)["chunks"] == 0  # still going
    done = _remember(remembering, model, DAY + 70 + memory.IDLE_S)
    assert (done["chunks"], done["items"], done["tokens"]) == (1, 1, 100)
    assert "home chat text" not in model.shown[0]
    [item] = remembering.store.open_items()
    assert (item["profile"], item["chat_id"], item["kind"], item["status"]) == ("", "work", "commitment", "open")
    assert _remember(remembering, model, DAY + 7200)["chunks"] == 0 and len(model.shown) == 1
    db.message("user", "Also book the vet", sid="work", at=DAY + 7300)
    db.message("assistant", "Sure.", sid="work", at=DAY + 7310)
    _remember(remembering, model, DAY + 7310 + memory.IDLE_S)
    assert "renew my passport" not in model.shown[1] and "Also book the vet" in model.shown[1]
    assert remembering.store.item_counts() == {"commitment": 1}


def test_a_chunk_two_processes_read_is_kept_once(remembering, db):
    db.message("user", "Remind me to renew my passport", sid="work", at=DAY + 60)
    db.message("assistant", "Will do.", sid="work", at=DAY + 70)
    reply = {"items": [{"kind": "commitment", "text": "x", "evidence": "renew my passport", "line": 1}]}

    class Racing(FakeModel):
        def __call__(self, instructions, text):
            if not self.shown:  # the other process finishes the same chunk during this call
                _remember(remembering, FakeModel(reply), DAY + 70 + memory.IDLE_S)
            return super().__call__(instructions, text)

    done = _remember(remembering, Racing(reply), DAY + 70 + memory.IDLE_S)
    assert done["chunks"] == 0 and len(remembering.store.open_items()) == 1


def test_tool_outcomes_show_once_across_chunks(remembering, db):
    remembering.store.set("memory_since", DAY)
    db.message("user", "add it", sid="work", at=DAY + 60)
    db.message("assistant", "done", sid="work", at=DAY + 120)
    db.message("user", "next one", sid="work", at=DAY + 120 + memory.GAP_S + 60)
    db.message("assistant", "ok", sid="work", at=DAY + 120 + memory.GAP_S + 70)
    remembering.store.add_outcome(dict(at=DAY + 100, session_id="work", turn_id="t", task_id="", tool_call_id="",
                                       tool="conduit_phone", action="add_reminder", label="Vet", args_digest="",
                                       status="ok", error_type="", error="", ids_json="{}", duration_ms=1))
    model = FakeModel()
    _remember(remembering, model, DAY + memory.GAP_S + memory.IDLE_S + 200)
    assert ["add_reminder" in text for text in model.shown] == [True, False]


def test_a_continued_chat_starts_where_its_parent_stopped(remembering, db):
    db.message("user", "first part", sid="work", at=DAY + 60)
    db.message("assistant", "ok", sid="work", at=DAY + 70)
    model = FakeModel()
    _remember(remembering, model, DAY + 70 + memory.IDLE_S)
    db.session("work2", title="Work", pinned=0, parent="work")
    db.message("user", "first part", sid="work2", at=DAY + 60)  # the tail compression carried over
    db.message("user", "second part", sid="work2", at=DAY + 4000)
    db.message("assistant", "ok", sid="work2", at=DAY + 4010)
    _remember(remembering, model, DAY + 4010 + memory.IDLE_S)
    assert "first part" not in model.shown[1] and "second part" in model.shown[1]


def test_a_failing_chunk_is_retried_then_skipped(remembering, db):
    db.message("user", "hello", sid="work", at=DAY + 60)
    db.message("assistant", "hi", sid="work", at=DAY + 70)
    model = FakeModel(TimeoutError(), TimeoutError(), TimeoutError())
    now = DAY + 70 + memory.IDLE_S
    assert _remember(remembering, model, now)["failed"] == 1
    assert _remember(remembering, model, now + 60)["failed"] == 0  # waits for its retry
    _remember(remembering, model, now + memory.RETRY_S)
    _remember(remembering, model, now + 2 * memory.RETRY_S)
    assert len(model.shown) == 3 and _remember(remembering, model, now + 3 * memory.RETRY_S)["failed"] == 0
    assert len(model.shown) == 3  # skipped after the third failure, not tried forever
    assert remembering.store.watermark("", "work")["failures"] == 0


def test_other_profiles_are_remembered_with_their_name(remembering, db, tmp_path, request):
    bot_home = tmp_path / "profiles" / "nahida"
    bot_home.mkdir(parents=True)
    bot = StateDB(bot_home, request.node.callspec.params["db"])
    bot.session("b1", title="Bot Chat", pinned=0)
    bot.session("bp", title="Pulse", pinned=1)  # that profile's own Pulse chat
    bot.message("user", "Pulse shadow report text", sid="bp", at=DAY + 60)
    bot.message("user", "My mum's birthday is on 3 March", sid="b1", at=DAY + 60)
    bot.message("assistant", "Noted.", sid="b1", at=DAY + 70)
    model = FakeModel({"items": [{"kind": "fact", "text": "The user's mum's birthday is 3 March.",
                                  "evidence": "My mum's birthday is on 3 March", "line": 1, "explicit": True}]})
    _remember(remembering, model, DAY + 70 + memory.IDLE_S,
              [("", tmp_path, SID), ("nahida", bot_home, report.their_home_chat(bot_home)), ("gone", tmp_path / "nowhere", "")])
    [item] = remembering.store.open_items("fact")
    assert (item["profile"], item["chat_id"]) == ("nahida", "b1")
    assert len(model.shown) == 1 and report.their_home_chat(bot_home) == "bp"
    assert memory.describe(item, UTC).startswith("#1 [nahida] The user's mum's birthday is 3 March.\n")


def test_the_loop_runs_memory_on_its_own_thread(remembering, db, monkeypatch):
    remembering.store.set("enabled", True)
    remembering.store.set("memory", True)
    remembering.store.set("memory_since", None)
    calls = []

    class Llm:
        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(parsed={"items": []}, usage=types.SimpleNamespace(total_tokens=7))

    remembering.ctx.llm = Llm()
    monkeypatch.setattr(pulse, "_memory_thread", None)
    monkeypatch.setattr(pulse, "_memory_task", True)
    start = time.time()
    pulse.tick(start)
    pulse._memory_thread.join(5)
    assert remembering.store.get("memory_since") == start  # memory starts now, not from old chats
    db.message("user", "hello", sid="work", at=start + 1)
    db.message("assistant", "hi", sid="work", at=start + 2)
    pulse.tick(start + 60)
    assert pulse._memory_thread.ident and calls == []  # not due again yet
    pulse._memory_thread.join(5)
    monkeypatch.setattr(memory, "IDLE_S", 0.0)
    remembering.store.set("memory_requested", True)
    pulse.tick(start + 120)
    pulse._memory_thread.join(5)
    assert [(c["task"], c["json_mode"]) for c in calls] == [(memory.TASK, True)]
    assert remembering.store.get("memory_usage") == {"chunks": 1, "items": 0, "tokens": 7, "failed": 0}


def test_memory_command(remembering, db, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="", extra="": cli.dispatch(types.SimpleNamespace(
        conduit_push_action="pulse", pulse_action="memory", value=value, extra=extra))
    assert run("on") == 0 and remembering.store.get("memory") is True
    capsys.readouterr()
    assert run() == 0 and "hasn't kept anything" in capsys.readouterr().out
    remembering.store.advance("", "work", message_id=5, at=DAY, items=[dict(
        kind="promise", text="Hermes will check the build.", evidence="I'll check the build", explicit=1,
        due="2026-10-21", due_at=DAY + 86400, message_id=5, mentioned_at=DAY + 60)])
    assert run() == 0
    out = capsys.readouterr().out
    assert "Promise (1)\n  #1 Hermes will check the build. (due 2026-10-21)\n      \"I'll check the build\" (20 Oct)" in out
    assert run("fact") == 0 and "hasn't kept" in capsys.readouterr().out
    assert run("mood") == 2
    assert run("forget", "x") == 2 and run("forget", "#²") == 2 and run("forget", "#9") == 1
    assert run("forget", "#1") == 0 and remembering.store.open_items() == []
    assert run("now") == 1  # Pulse is off
    remembering.store.set("enabled", True)
    assert run("now") == 0 and remembering.store.get("memory_requested") is True
    capsys.readouterr()
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Memory: nothing kept yet (0 chat chunks read, 0 tokens)" in capsys.readouterr().out
