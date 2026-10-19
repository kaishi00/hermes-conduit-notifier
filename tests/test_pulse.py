"""Pulse slice 0: the home chat delivery probe (designs/proactivity.md 6.4.1, 6.4.2, 7.6).

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

PLUGIN = "conduit_push"
KEY = "agent:main:local:dm:pulse-chat"
SID = "local-abc"

# Only the columns Pulse reads, as Hermes names them (hermes_state_common.py).
FIXTURE_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, session_key TEXT, started_at REAL NOT NULL,
    last_activity_at REAL, title TEXT, archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
    timestamp REAL NOT NULL, platform_message_id TEXT, active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0, display_kind TEXT
);
CREATE TABLE session_admissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, admission_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
    principal_id TEXT NOT NULL, target_session_id TEXT NOT NULL, lineage_json TEXT NOT NULL,
    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL,
    outcome TEXT, owner_epoch INTEGER, generation INTEGER
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
    return result.stdout if result.returncode == 0 and "session_admissions" in result.stdout else None


HERMES_SCHEMA = _hermes_schema()


class StateDB:
    """A Hermes state.db with helpers that write rows the way Hermes does."""

    def __init__(self, home, schema):
        self.path = home / "state.db"
        conn = sqlite3.connect(self.path)
        conn.executescript(schema)
        conn.commit()
        conn.close()
        self.seq = 0

    def _exec(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def session(self, sid=SID, key=KEY, title="Pulse", pinned=1, archived=0, started=1000.0):
        self._exec("INSERT INTO sessions(id, source, session_key, started_at, last_activity_at, title, pinned, "
                   "archived) VALUES(?,?,?,?,?,?,?,?)", (sid, "local", key, started, started, title, pinned, archived))

    def admission(self, text, *, status="queued", outcome=None, key=KEY, sid=SID, plugin_id=PLUGIN):
        self.seq += 1
        request_id = json.dumps(["plugin", plugin_id, key, f"{self.seq:032x}"], separators=(",", ":"))
        started = status == "started"
        self._exec(
            "INSERT INTO session_admissions(admission_id, request_id, principal_id, target_session_id, lineage_json, "
            "payload_json, payload_digest, intent, status, outcome, owner_epoch, generation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"adm-{self.seq}", request_id, "automation:" + key, sid, "[]",
             json.dumps({"text": text, "local_automation_v1": {}}), "d", "queue", status, outcome,
             1 if started else None, 1 if started else None))
        return request_id

    def finish(self, request_id, outcome="completed"):
        self._exec("UPDATE session_admissions SET status='terminal', outcome=? WHERE request_id=?",
                   (outcome, request_id))

    def message(self, role, content, *, sid=SID, platform_message_id=None, display_kind=None, active=1):
        return self._exec("INSERT INTO messages(session_id, role, content, timestamp, platform_message_id, "
                          "display_kind, active, compacted) VALUES(?,?,?,?,?,?,?,?)",
                          (sid, role, content, time.time(), platform_message_id, display_kind, active,
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
                            session_key=KEY)
    assert [row["tag"] for row in pulse_store.in_flight()] == ["pi-1"]
    pulse_store.update_attempt("i:1", state=store.RUNNING, turn_id="turn")
    assert pulse_store.attempt_by_tag("pi-1")["turn_id"] == "turn"
    pulse_store.update_attempt("i:1", state=store.DELIVERED, detail="ok")
    assert pulse_store.in_flight() == []
    assert pulse_store.counts("probe") == {store.DELIVERED: 1}


# Hermes reads ----------------------------------------------------------------

def test_home_chat_is_the_local_chat_with_the_title(db, tmp_path):
    db.session()
    db.session(sid="local-old", key="agent:main:local:dm:old", pinned=0, started=500.0)
    db.session(sid="tg-1", key="agent:main:telegram:dm:1")  # not local
    db.session(sid="local-arch", key="agent:main:local:dm:arch", archived=1)
    db.session(sid="local-other", key="agent:main:local:dm:other", title="Pulse notes")
    chats = hermes.find_home_chat(tmp_path, "Pulse")
    assert [chat.session_key for chat in chats] == [KEY, "agent:main:local:dm:old"]
    assert chats[0].pinned


def test_admission_is_found_by_tag_for_this_plugin_only(db, tmp_path):
    db.session()
    db.admission("Pulse delivery check pAAAA-1.", plugin_id="someone_else")
    request_id = db.admission("Pulse delivery check pAAAA-1.", status="started")
    db.admission("Pulse delivery check pBBBB-1.")
    found = hermes.find_admission(tmp_path, plugin_id=PLUGIN, session_key=KEY, tag="pAAAA-1")
    assert found == hermes.Admission(request_id, SID, "started", None)
    assert hermes.find_admission(tmp_path, plugin_id=PLUGIN, session_key=KEY, tag="pCCCC-1") is None


def test_turn_rows_follow_the_tagged_user_row(db, tmp_path):
    db.session()
    request_id = db.admission("Pulse delivery check pAAAA-1.")
    db.message("user", "earlier")
    db.message("assistant", "earlier reply")
    db.message("user", "Pulse delivery check pAAAA-1.", platform_message_id=request_id, active=0)
    db.message("assistant", "")
    db.message("assistant", "Check arrived.", active=0)
    turn = hermes.find_turn(tmp_path, session_key=KEY, tag="pAAAA-1", request_id=request_id)
    assert turn == hermes.TurnRows(SID, "Check arrived.", False, False)
    db.message("user", "next")
    assert hermes.find_turn(tmp_path, session_key=KEY, tag="pAAAA-1").closed


def test_a_failed_turn_is_seen(db, tmp_path):
    db.session()
    db.message("user", "Pulse delivery check pAAAA-1.")
    db.message("assistant", "Your request was not processed.", display_kind="failed_turn")
    turn = hermes.find_turn(tmp_path, session_key=KEY, tag="pAAAA-1")
    assert turn.failed and turn.reply == ""


def test_missing_state_db_is_unreadable_not_empty(tmp_path):
    with pytest.raises(hermes.HermesUnreadable):
        hermes.find_home_chat(tmp_path, "Pulse")


# Deriving an attempt's state ---------------------------------------------------

ADM = hermes.Admission
TURN = hermes.TurnRows


@pytest.mark.parametrize("seen, age, earlier, prior, expected", [
    (delivery.Seen(None, None), 5, False, "", store.SUBMITTED),
    (delivery.Seen(None, None), 500, False, "", store.NOT_ADMITTED),
    (delivery.Seen(None, None), 5, True, "", store.NOT_ADMITTED),
    (delivery.Seen(ADM("r", SID, "queued", None), None), 900, True, "", store.QUEUED),
    (delivery.Seen(ADM("r", SID, "started", None), None), 900, False, "", store.RUNNING),
    (delivery.Seen(ADM("r", SID, "unknown", None), None), 900, False, "", store.RUNNING),
    (delivery.Seen(ADM("r", SID, "terminal", "interrupted"), None), 9, False, "", store.FAILED),
    (delivery.Seen(ADM("r", SID, "terminal", "completed"), TURN(SID, "ok", False, False)), 9, False, "",
     store.DELIVERED),
    (delivery.Seen(ADM("r", SID, "terminal", "completed"), TURN(SID, "sorry", True, False)), 9, False, "",
     store.FAILED),
    (delivery.Seen(ADM("r", SID, "terminal", "completed"), None), 9, False, "", store.RUNNING),
    (delivery.Seen(ADM("r", SID, "terminal", "completed"), None), 9, False, delivery.AWAITING_REPLY,
     store.FAILED),
    (delivery.Seen(None, TURN(SID, "ok", False, False)), 500, False, "", store.DELIVERED),
    (delivery.Seen(None, TURN(SID, "", False, True)), 500, False, "", store.FAILED),
])
def test_state_comes_from_hermes_rows_only(seen, age, earlier, prior, expected):
    state, _detail = delivery.derive_state(seen, age_s=age, sent_by_earlier_process=earlier, prior_detail=prior)
    assert state == expected


# Submitting and reconciling ----------------------------------------------------

def test_attempt_is_recorded_before_the_gateway_sees_it(pulse_store):
    seen_in_store = []

    def inject(text, session_key):
        seen_in_store.append(bool(pulse_store.in_flight()))
        return True

    row = delivery.submit(pulse_store, inject, kind="probe", session_key=KEY, text=lambda tag: f"check {tag}")
    assert seen_in_store == [True]
    assert row["state"] == store.SUBMITTED and row["tag"] in row["text"]


@pytest.mark.parametrize("inject", [lambda text, session_key: False,
                                    lambda text, session_key: (_ for _ in ()).throw(RuntimeError("down"))])
def test_a_refused_injection_is_final(pulse_store, inject):
    row = delivery.submit(pulse_store, inject, kind="probe", session_key=KEY, text=lambda tag: tag)
    assert row["state"] == store.REFUSED
    assert pulse_store.in_flight() == []


def test_reconcile_follows_one_attempt_to_delivered(db, tmp_path, pulse_store):
    db.session()
    row = delivery.submit(pulse_store, lambda text, session_key: True, kind="probe", session_key=KEY,
                          text=lambda tag: f"Pulse delivery check {tag}.")
    started = row["created_at"] - 1
    reconcile = lambda: delivery.reconcile(pulse_store, tmp_path, plugin_id=PLUGIN, process_started_at=started,
                                           now=row["created_at"] + 1)
    assert reconcile() == []
    request_id = db.admission(row["text"])
    assert [r["state"] for r in reconcile()] == [store.QUEUED]
    db.message("user", row["text"], platform_message_id=request_id)
    db.message("assistant", "Check arrived.")
    db.finish(request_id)
    assert [r["state"] for r in reconcile()] == [store.DELIVERED]
    assert pulse_store.attempt_by_tag(row["tag"])["detail"] == "Check arrived."


def test_an_attempt_from_before_a_restart_with_no_trace_was_not_admitted(db, tmp_path, pulse_store):
    db.session()
    row = delivery.submit(pulse_store, lambda text, session_key: True, kind="probe", session_key=KEY,
                          text=lambda tag: tag)
    changed = delivery.reconcile(pulse_store, tmp_path, plugin_id=PLUGIN, process_started_at=row["created_at"] + 1,
                                 now=row["created_at"] + 2)
    assert [r["state"] for r in changed] == [store.NOT_ADMITTED]


def test_unreadable_state_leaves_attempts_alone(tmp_path, pulse_store):
    delivery.submit(pulse_store, lambda text, session_key: True, kind="probe", session_key=KEY, text=lambda tag: tag)
    assert delivery.reconcile(pulse_store, tmp_path, plugin_id=PLUGIN, process_started_at=0, now=time.time() + 999) == []
    assert pulse_store.in_flight()[0]["state"] == store.SUBMITTED


# Hooks and the loop ------------------------------------------------------------

@pytest.fixture
def live(tmp_path, monkeypatch):
    """Pulse as the gateway process runs it, with Hermes's home in tmp_path."""
    injected = []
    ctx = types.SimpleNamespace(plugin_id=PLUGIN, _manager=types.SimpleNamespace(has_gateway_message_injector=True),
                                inject_message=lambda text, session_key=None: injected.append((text, session_key))
                                or True)
    monkeypatch.setattr(pulse, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(pulse, "_ctx", ctx)
    monkeypatch.setattr(pulse, "_started_at", time.time() - 10)
    monkeypatch.setattr(pulse, "_turns", {})
    return types.SimpleNamespace(ctx=ctx, injected=injected, store=store.open_store(tmp_path))


def test_nothing_is_sent_while_pulse_is_off(live, db):
    db.session()
    pulse.tick()
    assert live.injected == []


def test_nothing_is_sent_outside_the_gateway(live, db):
    db.session()
    live.store.set("enabled", True)
    live.ctx._manager.has_gateway_message_injector = False
    pulse.tick()
    assert live.injected == []


def test_a_probe_goes_to_the_home_chat_once_at_a_time(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    assert [key for _text, key in live.injected] == [KEY]
    live.store.set("probe_requested", True)
    pulse.tick()
    assert len(live.injected) == 1  # the first one is still in flight


def test_no_home_chat_means_no_probe(live, db):
    live.store.set("enabled", True)
    pulse.tick()
    assert live.injected == []
    chat, problem = pulse.home_chat(live.store, pulse.get_hermes_home())
    assert chat is None and "titled" in problem


def test_a_pulse_turn_gets_its_note_and_no_tools(live, db):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    text = live.injected[0][0]
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


def test_a_pulse_turn_sends_no_push(live, db, monkeypatch):
    db.session()
    live.store.set("enabled", True)
    pulse.tick()
    sent = []
    monkeypatch.setattr(plugin, "enqueue", sent.append)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    plugin._pre_llm_call(session_id=SID, turn_id="t1", user_message=live.injected[0][0], conversation_history=[])
    plugin._post_llm_call(session_id=SID, turn_id="t1", assistant_response="Check arrived.")
    plugin._on_session_end(session_id=SID, turn_id="t1", completed=True, failed=False, interrupted=False)
    plugin._pre_llm_call(session_id=SID, turn_id="t2", user_message=live.injected[0][0], conversation_history=[])
    plugin._on_session_end(session_id=SID, turn_id="t2", completed=False, failed=True, interrupted=False)
    assert sent == []


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
    assert f"Home chat: Pulse ({KEY})" in capsys.readouterr().out
    assert run("home", "agent:main:local:dm:x") == 0 and live.store.get("home_session_key") == "agent:main:local:dm:x"
    assert run("home", "auto") == 0 and live.store.get("home_session_key") == ""
    assert run("off") == 0 and live.store.get("enabled") is False


def test_the_loop_stops_on_unload(monkeypatch):
    monkeypatch.setattr(pulse, "_thread", None)
    ctx = types.SimpleNamespace(plugin_id=PLUGIN, _manager=types.SimpleNamespace(has_gateway_message_injector=False))
    pulse.register(ctx)
    thread = pulse._thread
    pulse.register(ctx)  # loaded again: still one loop
    assert pulse._thread is thread and thread.is_alive()
    pulse.stop(timeout=2.0)
    assert not thread.is_alive()
