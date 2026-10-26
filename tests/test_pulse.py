"""Pulse slice 0: the home chat delivery probe (designs/proactivity.md 6.4.1, 6.4.2, 7.6)
the outcome ledger (3.6), and the daily shadow report (13, slice 0 (d)).

The home chat is a Conduit (Desktop-source) chat, reached through the
dashboard's session host; the host is faked here with its two methods.

The Hermes reads run against two databases: a fixture with only the columns
Pulse reads, and one built from Hermes's own schema. The second is the
contract test: it fails when Hermes renames what Pulse depends on. It reads
the schema from Hermes when a checkout or install is available (set
HERMES_AGENT_SRC to a checkout), and otherwise from the copy in
hermes_state_schema.sql, so CI runs it too; with Hermes available, a test
checks the copy is current.
"""
import contextlib
import importlib.util
import json
import logging
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import datetime

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
    pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, model TEXT, model_config TEXT,
    billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT
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


SCHEMA_COPY = ROOT / "tests" / "hermes_state_schema.sql"
LIVE_HERMES_SCHEMA = _hermes_schema()
HERMES_SCHEMA = LIVE_HERMES_SCHEMA or SCHEMA_COPY.read_text()


def _without_comments(sql):
    return [line.rstrip() for line in sql.splitlines() if line.strip() and not line.lstrip().startswith("--")]


@pytest.mark.skipif(LIVE_HERMES_SCHEMA is None, reason="no Hermes to read the schema from")
def test_the_schema_copy_matches_hermes():
    assert _without_comments(SCHEMA_COPY.read_text()) == _without_comments(LIVE_HERMES_SCHEMA), \
        "Hermes changed SCHEMA_SQL: refresh tests/hermes_state_schema.sql from hermes_state_common.SCHEMA_SQL"


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

    def model(self, sid=SID, *, model, config=None, billing=None):
        # A chat's model as Hermes keeps it: the model column, the route in model_config, the provider billed.
        self._exec("UPDATE sessions SET model=?, model_config=?, billing_provider=? WHERE id=?",
                   (model, None if config is None else json.dumps(config), billing, sid))

    def message(self, role, content, *, sid=SID, display_kind=None, active=1, at=None):
        return self._exec("INSERT INTO messages(session_id, role, content, timestamp, display_kind, active, compacted) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (sid, role, content, time.time() if at is None else at, display_kind, active,
                           0 if active else 1))


SCHEMAS = [pytest.param(FIXTURE_SCHEMA, id="fixture"),
           pytest.param(HERMES_SCHEMA, id="hermes")]


@pytest.fixture(autouse=True)
def _no_lcm_settings(monkeypatch):
    """The report's budget reads hermes-lcm's threshold from the environment."""
    monkeypatch.delenv(report.LCM_THRESHOLD, raising=False)


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


def test_a_lease_on_another_build_is_left_to_the_new_one(pulse_store):
    assert pulse_store.claim_leader("a", 120, now=1000, build="old")
    assert not pulse_store.claim_leader("b", 120, now=1010, build="old")  # same build: a still runs it
    assert not pulse_store.claim_leader("b", 120, now=1010)  # no stamp to compare: wait it out
    assert pulse_store.claim_leader("b", 120, now=1010, build="new")  # a stopped when "new" landed
    assert not pulse_store.claim_leader("c", 120, now=1020, build="new")
    # Another host has its own install, so its own stamp: its lease is waited out.
    assert pulse_store.claim_leader("other:1", 120, now=2000, build="theirs")
    assert not pulse_store.claim_leader("here:2", 120, now=2010, build="ours")
    assert pulse_store.claim_leader("here:2", 120, now=2200, build="ours")


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
    assert turn == hermes.TurnRows(SID, "Check arrived.", False, False, "Pulse delivery check pAAAA-1.", 5)
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


class Routes:
    """Hermes's side of a chat's model: the session host's reader of the
    route a chat was left on (tui_gateway.server's
    _stored_session_runtime_overrides; a provider in ``dropped`` is no longer
    set up, so it comes back as None, as Hermes drops it), and the binding
    that lends a model to auxiliary calls (agent.auxiliary_client's
    scoped_runtime_main), in ``bound`` while it holds. Needs FakeHost."""

    def __init__(self, monkeypatch, dropped=()):
        self.read, self.bound, self.dropped = [], [], set(dropped)
        monkeypatch.setattr(sys.modules["tui_gateway.server"], "_stored_session_runtime_overrides", self.read_back,
                            raising=False)
        auxiliary = types.ModuleType("agent.auxiliary_client")
        auxiliary.scoped_runtime_main = self.scoped_runtime_main
        monkeypatch.setitem(sys.modules, "agent", sys.modules.get("agent") or types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)

    def read_back(self, row):
        self.read.append(dict(row))
        config = json.loads(row.get("model_config") or "{}")
        provider = config.get("provider") or row.get("billing_provider")
        if provider in self.dropped:
            provider, config = None, {}
        return {"model_override": {"model": row["model"], "provider": provider, "base_url": config.get("base_url"),
                                   "api_mode": config.get("api_mode")}} if row.get("model") else {}

    @contextlib.contextmanager
    def scoped_runtime_main(self, runtime):
        self.bound.append(runtime)
        try:
            yield runtime
        finally:
            self.bound.pop()


# The Pulse chat as Eric's host had it, pinned to Volcengine while Hermes's main model ran on zai.
VOLCENGINE = {"provider": "volcengine", "model": "deepseek-v4-1-flash-261017",
              "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3", "api_mode": "chat_completions"}
VOLCENGINE_CONFIG = {key: VOLCENGINE[key] for key in ("provider", "base_url", "api_mode")}


def test_send_reopens_the_chat_and_submits_a_hidden_queued_turn(monkeypatch):
    fake = FakeHost().install(monkeypatch)
    assert host.send(SID, "Pulse delivery check p1-1.", profile="work") == host.Sent(True, "streaming", "streaming")
    assert fake.calls == [
        (host.RESUME, {"session_id": SID, "source": "desktop", "profile": "work"}),
        (host.SUBMIT, {"session_id": "ui-1", "text": "Pulse delivery check p1-1.", "display_kind": "hidden",
                       "queued": True}),
    ]


def test_send_puts_the_chat_on_pulses_model_first(monkeypatch):
    fake = FakeHost().install(monkeypatch)
    sys.modules["tui_gateway.server"]._methods[host.CONFIG_SET] = \
        lambda rid, params: fake.calls.append((host.CONFIG_SET, params)) or {"result": {"value": "m"}}
    model = {"provider": "openrouter", "model": "google/gemini-flash"}
    assert host.send(SID, "x", model=model).ok
    assert [call[0] for call in fake.calls] == [host.RESUME, host.CONFIG_SET, host.SUBMIT]
    assert fake.calls[1][1] == {"key": "model", "session_id": "ui-1", "confirm_expensive_model": True,
                                "value": "google/gemini-flash --provider openrouter --session"}
    # Already on it: no switch.
    fake.calls.clear()
    fake.resume = {"result": {"session_id": "ui-1", "info": {"model": "google/gemini-flash", "provider": "openrouter"}}}
    assert host.send(SID, "x", model=model).ok
    assert [call[0] for call in fake.calls] == [host.RESUME, host.SUBMIT]
    # A refused switch still sends, on the chat's own model.
    fake.calls.clear()
    fake.resume = {"result": {"session_id": "ui-1"}}
    sys.modules["tui_gateway.server"]._methods[host.CONFIG_SET] = lambda rid, params: {"error": {"message": "no"}}
    assert host.send(SID, "x", model=model).ok and fake.calls[-1][0] == host.SUBMIT


def test_the_pulse_model_setting(pulse_store, tmp_path):
    store_ = pulse_store
    control.update(store_, {"model": {"provider": "openrouter", "model": "google/gemini-flash"}})
    assert store_.get(pulse.MODEL) == {"provider": "openrouter", "model": "google/gemini-flash"}
    for bad in ({"model": "flash"}, {"model": {"model": ""}}, {"model": {"model": "a b"}},
                {"model": {"model": "--provider"}}, {"model": {"model": "m", "provider": "-x"}},
                {"model": {"model": 'x"'}}, {"model": {"model": "x\x00"}},
                {"model": {"model": "m", "base_url": "x"}}):
        with pytest.raises(control.Refused):
            control.update(store_, bad)
    control.update(store_, {"model": None})
    assert store_.get(pulse.MODEL) is None
    store_.set(pulse.MODEL, {"provider": "", "model": "x --base-url y"})
    assert pulse.turn_model(store_) is None and control.state(store_, tmp_path)["model"] is None


def test_pulse_turns_go_out_on_pulses_model(pulse_store, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(host, "send", lambda chat_id, body, **kw: sent.append(kw["model"]))
    pulse._sender(pulse_store)("c", "x")
    control.update(pulse_store, {"model": {"provider": "openrouter", "model": "anthropic/claude-haiku-4.5"}})
    pulse._sender(pulse_store)("c", "x")
    assert sent == [None, {"provider": "openrouter", "model": "anthropic/claude-haiku-4.5"}]
    assert control.state(pulse_store, tmp_path)["model"] == sent[1]


def test_send_reports_a_refusal(monkeypatch):
    FakeHost(resume={"error": {"code": 4007, "message": "session not found"}}).install(monkeypatch)
    sent = host.send(SID, "x")
    assert not sent.ok and "session not found" in sent.detail


NOT_OWNED = {"error": {"code": 4090, "message": "This chat is open in another Hermes window/terminal.",
                       "data": {"reason": "SESSION_NOT_OWNED"}}}


@pytest.mark.parametrize("status", ["taken_over", "free"])
def test_a_chat_held_elsewhere_is_taken_over_and_sent_again(monkeypatch, status):
    fake = FakeHost().install(monkeypatch)
    replies, rids = [NOT_OWNED, {"result": {"status": "streaming"}}], []
    monkeypatch.setitem(sys.modules["tui_gateway.server"]._methods, host.SUBMIT,
                        lambda rid, params: rids.append(rid) or fake.calls.append((host.SUBMIT, params))
                        or replies.pop(0))
    taken = []
    monkeypatch.setattr(host, "take_over", lambda ids: taken.append(ids) or status)
    assert host.send(SID, "x") == host.Sent(True, "streaming", "streaming")
    assert taken == [[SID, "ui-1"]]
    assert [name for name, _ in fake.calls] == [host.RESUME, host.SUBMIT, host.SUBMIT]
    assert len(set(rids)) == 2  # the resend is a new request


def test_a_taken_over_chat_is_put_on_pulses_model_again(monkeypatch):
    fake = FakeHost().install(monkeypatch)
    methods = sys.modules["tui_gateway.server"]._methods
    switches = [{"error": {"message": "held elsewhere"}}, {"result": {"value": "m"}}]
    methods[host.CONFIG_SET] = lambda rid, params: fake.calls.append((host.CONFIG_SET, params)) or switches.pop(0)
    replies = [NOT_OWNED, {"result": {"status": "queued"}}]
    methods[host.SUBMIT] = lambda rid, params: fake.calls.append((host.SUBMIT, params)) or replies.pop(0)
    monkeypatch.setattr(host, "take_over", lambda ids: "taken_over")
    assert host.send(SID, "x", model={"provider": "", "model": "m"}).ok
    assert [name for name, _ in fake.calls] == [host.RESUME, host.CONFIG_SET, host.SUBMIT, host.CONFIG_SET, host.SUBMIT]


def test_one_takeover_waits_on_hermes_at_a_time(monkeypatch):
    import threading

    takeover = _load("conduit_push.chat_takeover", ROOT / "chat_takeover.py")
    release = threading.Event()
    monkeypatch.setattr(takeover, "modules", lambda: (object(), object()))
    monkeypatch.setattr(takeover, "home", lambda: "home")
    monkeypatch.setattr(takeover, "take_over_session", lambda ids, **_: release.wait(5) and {"status": "free"})
    monkeypatch.setattr(host, "TAKEOVER_TIMEOUT_S", 0.05)
    assert host.take_over([SID]) == "takeover timed out"
    assert host.take_over([SID]) == "a takeover is still waiting on Hermes"
    release.set()
    host._worker.join(2)
    monkeypatch.setattr(host, "TAKEOVER_TIMEOUT_S", 2)
    assert host.take_over([SID]) == "free"


def test_the_dashboard_and_pulse_copies_share_one_takeover_lock():
    pulse_copy = _load("conduit_push.chat_takeover", ROOT / "chat_takeover.py")
    dashboard_copy = _load("conduit_push_dashboard_pkg.chat_takeover", ROOT / "chat_takeover.py")
    assert pulse_copy is not dashboard_copy
    assert pulse_copy._in_process is dashboard_copy._in_process


def test_another_refusal_after_the_takeover_names_it(monkeypatch):
    fake = FakeHost().install(monkeypatch)
    replies = [NOT_OWNED, {"error": {"code": 4009, "message": "nope"}}]
    monkeypatch.setitem(sys.modules["tui_gateway.server"]._methods, host.SUBMIT,
                        lambda rid, params: fake.calls.append((host.SUBMIT, params)) or replies.pop(0))
    sent = host.send(SID, "x", taking_over=lambda ids: "taken_over")
    assert not sent.ok and sent.detail == "the chat refused the turn after Pulse took it over (taken_over): nope"


def test_a_chat_busy_elsewhere_stays_refused(monkeypatch):
    fake = FakeHost(submit=NOT_OWNED).install(monkeypatch)
    sent = host.send(SID, "x", taking_over=lambda ids: "busy")
    assert not sent.ok and "couldn't take it over: busy" in sent.detail
    assert [name for name, _ in fake.calls] == [host.RESUME, host.SUBMIT]


def test_another_refusal_takes_nothing_over(monkeypatch):
    FakeHost(submit={"error": {"code": 4009, "message": "nope"}}).install(monkeypatch)
    taken = []
    assert not host.send(SID, "x", taking_over=lambda ids: taken.append(ids) or "taken_over").ok
    assert taken == []


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
    monkeypatch.setattr(pulse, "turns_marked", True)
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
    brief_off_the_clock(pulse_store, tmp_path)
    return types.SimpleNamespace(ctx=ctx, fake=fake, sent=sent, store=pulse_store)


# For a test that ticks at set UTC hours from 07:00 to 20:00: brief_off_the_clock
# keys off the real time, so its brief could fall due at one of them.
BRIEF_OUT_OF_REACH = "21:00"


def brief_off_the_clock(pulse_store, home):
    """The morning brief goes out every day on the clock; its own tests set
    its time. Ten hours ahead, its last slot is always too old to send at
    the real time (BRIEF_OUT_OF_REACH for ticks at set hours)."""
    zone = pulse.report_zone(pulse_store, home)
    pulse_store.set("brief_time", datetime.fromtimestamp(time.time() + 10 * 3600, zone).strftime("%H:%M"))


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


def test_nothing_is_sent_where_pulse_turns_cant_be_recognized(live, db, monkeypatch):
    db.session()
    live.store.set("enabled", True)
    monkeypatch.setattr(pulse, "turns_marked", False)  # a Hermes without pre_llm_call
    pulse.tick()
    assert live.sent() == []
    monkeypatch.setattr(pulse, "turns_marked", True)
    pulse.tick()
    assert len(live.sent()) == 1


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


def test_a_restarted_dashboard_takes_over_from_an_older_build_at_once(live, db, capsys, monkeypatch):
    # The dashboard a plugin update restarted has a new pid; the one it
    # replaced stamped the lease with the old build and no longer ticks.
    db.session()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    live.store.set("enabled", True)
    live.store.set("probe_requested", True)
    live.store.claim_leader(f"{socket.gethostname()}:16293", pulse.LEADER_TTL_S, time.time(), build="older")
    pulse.tick()
    assert len(live.sent()) == 1
    lease = live.store.get(store.LEADER)
    assert lease["owner"] == pulse._owner and lease["build"] == pulse.build_stamp()
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "older build" not in capsys.readouterr().out


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
    if os.name == "nt":
        return  # no signal-0 probe there: a gone owner still reads as running
    # The dashboard a plugin update restarted: the old process is gone and
    # its lease, still unexpired, no longer reads as an older copy running.
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    live.store.set(store.LEADER, {"owner": f"{socket.gethostname()}:{gone.pid}", "until": time.time() + 60,
                                  "build": "older"})
    status()
    assert "older build" not in capsys.readouterr().out
    assert not pulse.lease_on_older_build(live.store.get(store.LEADER))


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
    ("notes?token=x", "notes"),
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
    assert f"covering {since} onward" in text and text.startswith("Pulse report pabc-1.") and context in text
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


def test_the_report_context_stays_within_its_budget(db, tmp_path):
    now = time.time()
    db.session()
    for n in range(40):
        db.session(f"c{n}", title=f"Chat {n}", pinned=0)
        for m in range(20):
            db.message("user", "x" * 1000, sid=f"c{n}", at=now - 3600 + n * 10 + m)
    chats = report.recent_chats(tmp_path, home_chat_id=SID, since=now - 86400, zone=report.zone_for("UTC"))
    assert len(chats) <= report.CHATS_BUDGET
    assert chats.startswith("### Chat 39")  # the most recent chat first
    small = report.recent_chats(tmp_path, home_chat_id=SID, since=now - 86400, zone=report.zone_for("UTC"),
                                budget=1500)
    assert small.startswith("### Chat 39") and len(small) <= 1500  # a bigger chat is cut, not dropped
    none = report.recent_chats(tmp_path, home_chat_id=SID, since=now - 86400, zone=report.zone_for("UTC"),
                               budget=10)
    assert none == "(Chats left out: no room in this report.)"


def test_the_whole_report_message_stays_under_its_budget(db, tmp_path, live, monkeypatch):
    # A host swapped a 22k-character report for a file pointer the turn couldn't open.
    monkeypatch.setenv(report.LCM_THRESHOLD, "50000")  # LCM's cap above Pulse's own
    now = time.time()
    db.session()
    for n in range(40):
        db.session(f"c{n}", title=f"Chat {n}", pinned=0)
        for m in range(20):
            db.message("user", "x" * 1000, sid=f"c{n}", at=now - 3600 + n * 10 + m)
    (tmp_path / "memories").mkdir(exist_ok=True)
    (tmp_path / "memories" / "USER.md").write_text("u" * 9000)
    (tmp_path / "memories" / "MEMORY.md").write_text("m" * 9000)
    context, since = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    assert len(report.report_text("p12345678-1", context, since)) <= report.REPORT_BUDGET
    assert "### Chat 39" in context  # the chats still get their share


def test_the_report_stays_under_lcms_externalization_threshold(db, tmp_path, live, monkeypatch):
    now = time.time()
    db.session()
    for n in range(40):
        db.session(f"c{n}", title=f"Chat {n}", pinned=0)
        for m in range(20):
            db.message("user", "x" * 1000, sid=f"c{n}", at=now - 3600 + n * 10 + m)
    (tmp_path / "memories").mkdir(exist_ok=True)
    # LCM's default threshold counts even when nothing here says it's on.
    assert report.report_budget() == report.LCM_DEFAULT_THRESHOLD - report.LCM_MARGIN
    context, since = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    assert len(report.report_text("p12345678-1", context, since)) < report.LCM_DEFAULT_THRESHOLD
    assert "### Chat 39" in context
    # The user's note to Pulse takes its share of the message first: the
    # context is gathered to fit beside it, so the newest chat lines stay.
    long_note = "n" * control.NOTE_LIMIT
    live.store.set("note", long_note)
    context, since = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    text = report.report_text("p12345678-1", context, since, note=long_note)
    assert context in text and long_note in text and len(text) <= report.report_budget(long_note)
    assert "### Chat 39" in context
    live.store.set("note", "")
    monkeypatch.setenv(report.LCM_THRESHOLD, "50000")
    assert report.report_budget() == report.REPORT_BUDGET
    monkeypatch.setenv(report.LCM_THRESHOLD, "junk")
    assert report.report_budget() == report.LCM_DEFAULT_THRESHOLD - report.LCM_MARGIN
    monkeypatch.setenv(report.LCM_THRESHOLD, "1")
    assert report.report_budget() == len(report.REPORT_TEXT) + report.BAR_ROOM + 1_000
    assert report.report_budget("note") == len(report.REPORT_TEXT) + report.BAR_ROOM + len(report.note_block("note")) + 1_000
    # The message itself never goes over, whatever the context.
    monkeypatch.delenv(report.LCM_THRESHOLD)
    for name in ("USER.md", "MEMORY.md"):
        (tmp_path / "memories" / name).write_text("z" * 9000)
    live.store.set("report_profiles", "this")
    monkeypatch.setattr(report, "MEMORY_BUDGET", 12_000)  # the other sections alone fill the report
    context, since = report.build(tmp_path, live.store, home_chat_id=SID, now=now)
    assert "(cut here: no room in this report)" in context and "### Chat 39" in context
    assert len(report.report_text("p12345678-1", context, since)) <= report.report_budget()
    monkeypatch.setenv(report.LCM_THRESHOLD, "1")
    text = report.report_text("p12345678-12", "y" * 50_000, "Tue 20 Oct 18:00")
    assert len(text) <= report.report_budget() and text.endswith(">>>")
    text = report.report_text("p12345678-12", "y" * 50_000, "Tue 20 Oct 18:00", note="z" * 1_000)
    assert len(text) <= report.report_budget("z" * 1_000) and text.endswith(">>>") and "z" * 1_000 in text


def test_items_and_ratings_are_read_from_the_text():
    items = report.report_items(REPLY)
    assert items == {1: "commitment", 2: "promise", 3: "other"}
    assert report.report_items("1. [Commitment] Call Mum") == {1: "commitment"}
    ratings = report.parse_ratings("1 yes, 2: no, 3 y, 7 yes, 1 n", items)
    assert ratings == [report.Rating(1, "commitment", False), report.Rating(2, "promise", False),
                       report.Rating(3, "other", True)]
    assert report.parse_ratings("2 👍", items) == [report.Rating(2, "promise", True)]
    assert report.parse_ratings("I said no 1 time", items) == []


def test_report_lines_name_the_remembered_item_they_came_from():
    reply = ("Here's what I would have brought up:\n1. [commitment] Send the deck (said in Work) #12\n"
             "2. [promise] Check the build (tool calls)\n3. [follow-up] Ask about #4 tickets\n"
             "4. [date] Mum's birthday (#7).\n1. [fact] A second line one #9\nRate them: reply like 1 yes, 2 no.")
    sources = report.report_sources(reply)
    assert sources == {1: 12, 4: 7}  # a "#4" inside the sentence isn't a source
    ratings = report.parse_ratings("1 yes, 2 no, 4 no", report.report_items(reply), sources)
    assert ratings == [report.Rating(1, "commitment", True, 12), report.Rating(2, "promise", False),
                       report.Rating(4, "date", False, 7)]
    assert report.ratings_from_json(report.ratings_json(ratings)) == ratings
    assert report.ratings_from_json('[[1, "promise", true]]') == [report.Rating(1, "promise", True)]  # older rows
    assert report.ratings_from_json('[[1], "x"]') == [] and report.ratings_from_json("{") == []
    assert report.ratings_from_json('[["a", "x", true], [2, "fact", false, 7]]') == [report.Rating(2, "fact", False, 7)]
    shown = report.shown_items("## What Pulse remembers\n#12 [commitment] Send the deck\n#3 [fact] x")
    assert shown == {12, 3} and report.report_sources(reply, shown) == {1: 12}  # #7 wasn't in the report
    assert report.report_items("1. [fact] The vet") == {1: "fact"}  # a remembered kind keeps its name


def test_the_report_reads_what_memory_remembers(live, db, tmp_path):
    now = time.time()
    _report_day(db, now)
    db.message("user", "Book the vet next week", sid="work", at=now - 1800)
    store_ = live.store
    store_.set("memory", True)
    mark = max(row["id"] for row in hermes.recent_messages(tmp_path, exclude_chat=SID, since=now - 7200)
               if row["content"].startswith("Noted"))
    item = lambda text, kind="commitment", **more: {
        "kind": kind, "text": text, "evidence": text, "explicit": 1, "due": "", "due_at": None, "message_id": mark,
        "mentioned_at": now - 3600, **more}
    ids = store_.advance("", "work", message_id=mark, at=now - 3500, items=[
        item("The user will send the deck to Mara.", due="2026-10-23", due_at=now + 86400),
        item("The user's sister is Ana.", kind="fact"), item("Hermes will check the build.", kind="promise")])
    store_.set_ratings("a1", json.dumps({1: "fact"}), report.ratings_json([report.Rating(1, "fact", False, ids[1])]))
    context, _ = report.build(tmp_path, store_, home_chat_id=SID, now=now)
    remembered = context.split("## What Pulse remembers")[1].split("\n## ")[0]
    assert remembered.index(f"#{ids[0]} [commitment]") < remembered.index(f"#{ids[2]} [promise]")  # dated first
    assert "(due 2026-10-23)" in remembered and "sister" not in remembered  # rated "no"
    assert "## How the user rated earlier reports\n- fact: 0 useful, 1 not" in context
    chats = context.split("that memory hasn't read yet")[1]
    assert "Book the vet next week" in chats and "send the deck to Mara before Friday" not in chats
    store_.set("memory_since", now - 2000)  # memory never read what came before it was turned on
    context, _ = report.build(tmp_path, store_, home_chat_id=SID, now=now)
    assert "send the deck to Mara before Friday" in context.split("that memory hasn't read yet")[1]
    store_.set("memory", False)
    context, _ = report.build(tmp_path, store_, home_chat_id=SID, now=now)
    assert "What Pulse remembers" not in context and "send the deck to Mara before Friday" in context


def test_a_dated_item_said_long_ago_still_leads_the_report(live, db, tmp_path):
    now = time.time()
    db.session()
    store_ = live.store
    store_.set("memory", True)
    item = lambda n, **more: {"kind": "interest", "text": f"The user likes topic {n}.", "evidence": "x",
                              "explicit": 1, "due": "", "due_at": None, "message_id": 1,
                              "mentioned_at": now - 3600 + n, **more}
    [old] = store_.advance("", "a", message_id=1, at=now - 90 * 86400, items=[item(
        0, kind="fact", text="The user's mum's birthday is 3 March.", due="2027-03-03", due_at=now + 130 * 86400,
        mentioned_at=now - 90 * 86400)])
    store_.advance("", "b", message_id=1, at=now, items=[item(n) for n in range(1, report.MAX_REPORT_ITEMS * 3 + 10)])
    remembered = report.remembered(store_, UTC, now)
    assert remembered.split("\n")[1].startswith(f"#{old} [fact] The user's mum's birthday")
    [late] = store_.advance("", "c", message_id=1, at=now, items=[item(
        0, due="2026-01-01", due_at=now - 30 * 86400, mentioned_at=now - 30 * 86400)])
    assert f"#{late} " not in report.remembered(store_, UTC, now)  # long past due, waiting for expiry
    [again] = store_.advance("", "d", message_id=1, at=now, items=[item(0, due="2026-01-01", due_at=now - 30 * 86400,
                                                                   mentioned_at=now)])
    assert f"#{again} " in report.remembered(store_, UTC, now)  # said again since: expiry keeps it, so does the report


def test_memory_shows_when_an_item_was_said_again():
    item = {"id": 3, "profile": "", "text": "The user will renew the passport.", "due": "", "explicit": 1,
            "evidence": "I need to renew my passport", "valid_from": DAY, "mentioned_at": DAY + 86400}
    assert memory.describe(item, UTC).endswith('"I need to renew my passport" (20 Oct, said again 21 Oct)')
    assert memory.describe({**item, "mentioned_at": DAY + 365 * 86400}, UTC).endswith("(20 Oct 2026, said again 20 Oct 2027)")
    assert memory.describe({**item, "mentioned_at": DAY + 600}, UTC).endswith("(20 Oct)")


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
    assert text.startswith("Pulse report")
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
    live.store.set("brief_time", BRIEF_OUT_OF_REACH)  # these ticks are at set hours, not now
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
    assert len(live.sent()) == 2 and live.sent()[1].startswith("Pulse report")


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
    assert pulse.rating_counts(live.store) == {}  # split into cards: rated there, not by typing
    _unsplit(live.store)  # as a report from before item cards
    pulse._last_ratings = 0.0
    pulse.tick(now=time.time() + 2)
    assert pulse.rating_counts(live.store) == {"commitment": (1, 0), "promise": (0, 1)}



def _unsplit(pulse_store):
    """Makes the store's reports read as from before item cards: no split marks."""
    pulse_store.set(store.SPLIT_REPORTS, [])
    with sqlite3.connect(pulse_store.path) as conn:
        conn.execute("DELETE FROM report_ratings WHERE items_json=?", (store.SPLIT_ITEMS,))


def test_a_no_on_a_remembered_item_keeps_it_out_of_later_reports(live, db):
    db.session()
    _due_report(live)
    live.store.set("memory", True)
    now = time.time()
    [kept] = live.store.advance("", "work", message_id=1, at=now - 3600, items=[{
        "kind": "commitment", "text": "The user will send the deck.", "evidence": "I'll send the deck",
        "explicit": 1, "due": "", "due_at": None, "message_id": 1, "mentioned_at": now - 3600}])
    live.store.set("report_requested", True)
    pulse.tick()
    row = live.store.in_flight()[0]
    assert f"#{kept} [commitment]" in row["text"]
    db.message("user", row["text"], display_kind="hidden")  # Hermes keeps the whole message; Pulse trims its copy
    db.message("assistant", f"1. [commitment] Send the deck #{kept}\n2. [follow-up] Chase PR #99")
    db.message("user", "1 no, 2 no")
    pulse.tick(now=time.time() + 1)
    assert f"#{kept}" not in live.store.attempt_by_tag(row["tag"])["text"]  # trimmed once delivered
    _unsplit(live.store)  # as a report from before item cards, rated by typing
    pulse._last_ratings = 0.0
    pulse.tick(now=time.time() + 2)
    assert set(report.rated_not_useful(live.store)) == {kept}  # #99 wasn't shown, so it names nothing
    assert report.remembered(live.store, UTC, time.time()) == ""
    live.store.advance("", "work", message_id=2, at=time.time() + 3, after_id=1, items=[{
        "kind": "commitment", "text": "The user will send the deck.", "evidence": "the deck", "explicit": 1, "due": "",
        "due_at": None, "message_id": 2, "mentioned_at": time.time() + 3, "same_as": kept}])
    assert f"#{kept} " in report.remembered(live.store, UTC, time.time())  # brought up again: back in the report


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
    out = capsys.readouterr().out
    assert "Shadow reports at 07:30 (Europe/Paris); next " in out
    live.store.set("brief_time", "06:15")
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Morning brief at 06:15 (Europe/Paris); next " in capsys.readouterr().out
    live.store.set("enabled", False)
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Morning brief: off while Pulse is" in capsys.readouterr().out
    live.store.set("enabled", True)
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
    live.store.set("brief_time", BRIEF_OUT_OF_REACH)  # these ticks are at set hours, not now

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
    assert live.store.attempt_by_tag(row["tag"])["text"] == f"Pulse report {row['tag']}."


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
         "line": 2.0, "explicit": True, "due": "2026-10-21T09:00", "due_words": "at nine"},  # never said
        {"kind": "fact", "text": "The user's sister lives in Lyon.", "evidence": "my sister lives in Lyon", "line": 1},
        {"kind": "mood", "text": "x", "evidence": "promise", "line": 1},
        {"kind": "fact", "text": "x", "evidence": "I ... on", "line": 1},  # too little to place
        "not an item"]})
    items, _, tokens = memory.extract(model, chunk, UTC)
    assert tokens == 100 and "[1] Tue 2026-10-20 00:01 User: I’ll call the dentist" in model.shown[0]
    assert [(i["kind"], i["message_id"], i["explicit"], i["due"]) for i in items] == [
        ("commitment", 11, 1, "2026-10-23"), ("promise", 12, 1, "")]
    assert items[0]["due_at"] == DAY + 3 * 86400 and items[0]["mentioned_at"] == DAY + 60
    with pytest.raises(memory.BadReply):
        memory.extract(FakeModel(["items"]), chunk, UTC)
    items, _, _ = memory.extract(FakeModel({"items": [{"kind": "fact", "text": "x", "evidence": "call the dentist",
                                                        "line": 10 ** 400}]}), chunk, UTC)
    assert items[0]["message_id"] == 11  # a wild line number is only a wrong hint


def test_a_due_date_must_be_said_near_its_item():
    chunk = [_row(1, "user", "Book the vet", DAY + 60), _row(2, "assistant", "When?", DAY + 70),
             _row(3, "user", "Thursday works", DAY + 80), _row(4, "user", "Also my sister visits", DAY + 90)]
    item = lambda evidence: {"kind": "commitment", "text": "x", "evidence": evidence, "due": "2026-10-22",
                             "due_words": "Thursday"}
    items, _, _ = memory.extract(FakeModel({"items": [item("Book the vet"), item("my sister visits")]}), chunk, UTC)
    assert [i["due"] for i in items] == ["", "2026-10-22"]  # two lines away, then the line just after
    wrong = lambda due: {"kind": "commitment", "text": "x", "evidence": "Thursday works", "due": due,
                         "due_words": "Thursday"}
    dues = [memory.extract(FakeModel({"items": [wrong(due)]}), chunk, UTC)[0][0]["due"]
            for due in ("2026-10-23", "2026-10-15", "26-10-22", "2026-10-29T09:30")]
    # not a Thursday, before it was said, a short year; a later Thursday stays
    assert dues == ["", "", "", "2026-10-29T09:30"]
    chunk = [_row(1, "user", "Remind me at 9:00 today", DAY + 14 * 3600)]
    early = {"kind": "commitment", "text": "x", "evidence": "Remind me at 9:00 today", "due_words": "9:00 today"}
    items, _, _ = memory.extract(FakeModel({"items": [{**early, "due": "2026-10-20T09:00"}]}), chunk, UTC)
    assert items[0]["due"] == ""  # earlier the same day it was said
    items, _, _ = memory.extract(FakeModel({"items": [{**early, "due": "2026-10-20T19:00"}]}), chunk, UTC)
    assert items[0]["due"] == "2026-10-20T19:00"
    chunk = [_row(1, "user", "Renew the lease next month", DAY + 60)]
    items, _, _ = memory.extract(FakeModel({"items": [{"kind": "commitment", "text": "x", "evidence": "Renew the lease",
                                                    "due": "2026-11-20", "due_words": "next month"}]}), chunk, UTC)
    assert items[0]["due"] == "2026-11-20"  # "month" names no Monday


def test_each_kind_comes_from_its_own_speaker():
    chunk = [_row(1, "user", "I'll renew the lease and my sister is called Ana", DAY + 60),
             _row(2, "assistant", "Kept for you to purge when settled: the rollback tag", DAY + 70)]
    raw = lambda kind, evidence: {"kind": kind, "text": "x", "evidence": evidence, "line": 1}
    items, _, _ = memory.extract(FakeModel({"items": [
        raw("commitment", "I'll renew the lease"), raw("commitment", "Kept for you to purge"),
        raw("promise", "I'll renew the lease"), raw("fact", "my sister is called Ana"),
        raw("fact", "the rollback tag")]}), chunk, UTC)
    # Hermes's words aren't the user's commitment, nor the user's Hermes's promise
    assert [(i["kind"], i["message_id"]) for i in items] == [("commitment", 1), ("fact", 1), ("fact", 2)]


def test_only_a_do_it_cards_reminder_sent_after_it_counts_as_handed_on(live):
    live.store.add_card({"kind": "followup", "level": "push", "news": "Call Sam about the Friday dinner booking.",
                         "created_at": DAY})
    live.store.add_card({"kind": "followup", "level": "push", "news": "Call Sam.", "created_at": DAY})  # too short
    live.store.add_card({"kind": "brief", "level": "push", "news": "A calm day: renew the parking permit online.",
                         "created_at": DAY})  # no Do it on a brief
    # Do it leaves a card's news, or its title when it has none: a title with news is never handed on.
    live.store.add_card({"kind": "followup", "level": "push", "title": "Book the dentist for a cleaning",
                         "news": "You said you'd book the dentist this week.", "created_at": DAY})
    live.store.add_card({"kind": "noticed", "level": "screen", "title": "Passport renewals open on Monday",
                         "news": "", "created_at": DAY})
    texts = memory.handoff_texts(live.store)
    rows = [_row(1, "user", "call sam about the friday dinner booking.", DAY - 60),  # before the card
            _row(2, "assistant", "Sure.", DAY - 50),
            _row(3, "user", "Pulse reminded me: Call Sam about the Friday  dinner booking.\n\nLet's take care of it now.",
                 DAY + 60),
            _row(4, "assistant", "Calling now.", DAY + 70), _row(5, "assistant", "Done, booked for 8.", DAY + 80),
            _row(6, "user", "Call Sam.", DAY + 90), _row(7, "assistant", "Again?", DAY + 100),
            _row(8, "user", "Renew the parking permit online.", DAY + 110),
            _row(9, "user", "I need to book the dentist for a cleaning soon", DAY + 120),
            _row(10, "assistant", "I'll find you a slot tomorrow.", DAY + 130),
            _row(11, "user", "Pulse reminded me: Passport renewals open on Monday", DAY + 140),
            _row(12, "assistant", "I'll book you a slot.", DAY + 150)]
    assert memory.handoff_rows(rows, texts) == {3, 4, 5, 11, 12}
    text = memory.render(rows[2:6], UTC, handoffs={3, 4, 5})
    assert "User (Pulse's reminder): Pulse reminded me" in text
    assert "Hermes (answering Pulse's reminder): Calling now." in text and "User: Call Sam." in text


def test_a_reminder_sent_on_with_do_it_and_its_answer_close_items_but_never_add_one(remembering, db):
    news = "The few weeks are up and the relay.json backup is still there. Say the word and it's gone."
    remembering.store.add_card({"kind": "followup", "level": "push", "news": news, "created_at": DAY})
    [prune] = remembering.store.advance("", "work", message_id=0, at=DAY, items=[
        {"kind": "promise", "text": "Hermes will prune the August backup if the user says so.",
         "evidence": "say the word if you want the Aug 15 one pruned too", "explicit": 1, "due": "",
         "due_at": None, "message_id": 0, "mentioned_at": DAY - 86400}])
    db.message("user", f"Pulse reminded me: {news}\n\nLet's take care of it now.", sid="work", at=DAY + 60)
    db.message("assistant", "Already done: both backups were shredded, the August one too. If that reminder job "
               "keeps replaying completed items, say the word and I'll inspect its cron config.", sid="work",
               at=DAY + 70)
    db.message("user", "Thanks. I'll renew my passport on Friday.", sid="work", at=DAY + 80)
    db.message("assistant", "Noted.", sid="work", at=DAY + 90)
    model = FakeModel({"items": [
        {"kind": "commitment", "text": "The user will take care of the backup now.",
         "evidence": "Let's take care of it now.", "line": 1, "explicit": True},
        {"kind": "promise", "text": "Hermes will inspect the Pulse reminder job's cron config.",
         "evidence": "say the word and I'll inspect its cron config", "line": 2, "explicit": True},
        {"kind": "commitment", "text": "The user will renew their passport.", "evidence": "I'll renew my passport",
         "line": 3, "explicit": True}],
        "closed": [{"number": prune, "status": "done", "evidence": "both backups were shredded, the August one too",
                    "line": 2}]})
    _remember(remembering, model, DAY + 90 + memory.IDLE_S)
    chat = model.shown[0].split("<<<chat")[1]
    assert "User (Pulse's reminder): Pulse reminded me" in chat and "Hermes (answering Pulse's reminder):" in chat
    assert [item["text"] for item in remembering.store.open_items()] == ["The user will renew their passport."]
    closed = remembering.store.item(prune)
    assert closed["status"] == "done" and "both backups were shredded" in closed["closed_by"]


def test_a_reminder_sent_on_at_the_end_of_one_chunk_still_marks_the_answer_in_the_next(remembering, db):
    news = "The few weeks are up and the relay.json backup is still there. Say the word and it's gone."
    remembering.store.add_card({"kind": "followup", "level": "push", "news": news, "created_at": DAY})
    # Sent just before midnight and answered just after: the date change splits the chunks.
    db.message("user", f"Pulse reminded me: {news}\n\nLet's take care of it now.", sid="work", at=DAY + 86400 - 30)
    db.message("assistant", "Already done. If that reminder job keeps replaying completed items, say the word and "
               "I'll inspect its cron config.", sid="work", at=DAY + 86400 + 30)
    model = FakeModel({"items": []}, {"items": [
        {"kind": "promise", "text": "Hermes will inspect the Pulse reminder job's cron config.",
         "evidence": "say the word and I'll inspect its cron config", "line": 1, "explicit": True}]})
    _remember(remembering, model, DAY + 86400 + 30 + memory.IDLE_S)
    assert len(model.shown) == 2
    assert "Hermes (answering Pulse's reminder):" in model.shown[1].split("<<<chat")[1]
    assert remembering.store.open_items() == []


def test_a_reminder_one_run_read_still_marks_the_answer_a_later_run_reads(remembering, db):
    news = "The few weeks are up and the relay.json backup is still there. Say the word and it's gone."
    remembering.store.add_card({"kind": "followup", "level": "push", "news": news, "created_at": DAY})
    db.message("user", f"Pulse reminded me: {news}\n\nLet's take care of it now.", sid="work", at=DAY + 60)
    db.message("assistant", "Checking the server now.", sid="work", at=DAY + 70)
    # The chat goes quiet while Hermes works, so a run reads it this far.
    _remember(remembering, FakeModel(), DAY + 70 + memory.IDLE_S)
    answer = DAY + 70 + 2 * memory.IDLE_S
    db.message("assistant", "Already done. If that reminder job keeps replaying completed items, say the word and "
               "I'll inspect its cron config.", sid="work", at=answer)
    model = FakeModel({"items": [
        {"kind": "promise", "text": "Hermes will inspect the Pulse reminder job's cron config.",
         "evidence": "say the word and I'll inspect its cron config", "line": 1, "explicit": True}]})
    _remember(remembering, model, answer + memory.IDLE_S)
    chat = model.shown[0].split("<<<chat")[1]
    assert "Pulse reminded me" not in chat and "Hermes (answering Pulse's reminder): Already done." in chat
    assert remembering.store.open_items() == []


def test_the_report_marks_an_answer_whose_reminder_it_leaves_out(remembering, db):
    news = "The few weeks are up and the relay.json backup is still there. Say the word and it's gone."
    remembering.store.add_card({"kind": "followup", "level": "push", "news": news, "created_at": DAY})
    asked = db.message("user", f"Pulse reminded me: {news}\n\nLet's take care of it now.", sid="work", at=DAY + 60)
    db.message("assistant", "Already done. If that reminder job keeps replaying completed items, say the word and "
               "I'll inspect its cron config.", sid="work", at=DAY + 3600)
    home, texts = remembering.store.path.parent, memory.handoff_texts(remembering.store)
    marked = "] Hermes (answering Pulse's reminder): Already done."
    # Memory read the reminder but not the answer yet.
    chats = report.recent_chats(home, home_chat_id=SID, since=DAY, zone=UTC, read={("", "work"): asked},
                                read_since=DAY, handoffs=texts)
    assert "Pulse reminded me" not in chats and marked in chats
    # The report's window starts after the reminder.
    chats = report.recent_chats(home, home_chat_id=SID, since=DAY + 600, zone=UTC, handoffs=texts)
    assert "Pulse reminded me" not in chats and marked in chats


def _open(n, kind, text, evidence, chat="work", at=DAY, due=""):
    return {"id": n, "kind": kind, "text": text, "evidence": evidence, "chat_id": chat, "mentioned_at": at,
            "due": due, "due_at": None}


def test_the_model_sees_what_is_remembered_and_can_repeat_replace_or_close_it():
    remembered = [_open(3, "commitment", "The user will renew the lease.", "I'll renew the lease"),
                  _open(4, "promise", "Hermes will check the build.", "I'll check the build"),
                  _open(5, "fact", "The user's sister is Ana.", "my sister is Ana")]
    chunk = [_row(1, "user", "Lease renewed! Also I'll renew the lease for the garage, and Ana moved to Lyon", DAY + 60),
             _row(2, "assistant", "Nice. I'll check the build tomorrow too.", DAY + 70)]
    model = FakeModel({"items": [
        {"kind": "promise", "text": "Hermes will check the build tomorrow.", "evidence": "I'll check the build tomorrow",
         "line": 2, "same_as": 4},
        {"kind": "fact", "text": "Ana lives in Lyon.", "evidence": "Ana moved to Lyon", "line": 1, "replaces": "#5"},
        {"kind": "commitment", "text": "x", "evidence": "renew the lease for the garage", "line": 1, "same_as": 5},
        {"kind": "commitment", "text": "x", "evidence": "I'll renew the lease", "line": 1},  # the same words again
    ], "closed": [{"number": 3, "status": "done", "evidence": "Lease renewed!", "line": 1},
                  {"number": 5, "status": "done", "evidence": "Ana moved", "line": 1},  # a fact doesn't close
                  {"number": 9, "status": "done", "evidence": "Lease renewed!", "line": 1},  # never shown
                  {"number": 4, "status": "done", "evidence": "never said", "line": 2}]})
    items, closed, _ = memory.extract(model, chunk, UTC, remembered=remembered)
    assert "#4 promise: Hermes will check the build. (said 2026-10-20: \"I'll check the build\")" in model.shown[0]
    assert model.shown[0].index("<<<remembered") < model.shown[0].index("<<<chat")
    assert [(i["kind"], i["same_as"], i["replaces"]) for i in items] == [
        ("promise", 4, None), ("fact", None, 5), ("commitment", None, None), ("commitment", 3, None)]
    assert [(c["id"], c["status"]) for c in closed] == [(3, "done")]
    items, _, _ = memory.extract(FakeModel({"items": [
        {"kind": "promise", "text": "x", "evidence": "I'll check the build tomorrow", "line": 2, "same_as": 4.0}]}),
        chunk, UTC, remembered=remembered)
    assert items[0]["same_as"] == 4  # a whole-number float, as JSON may give it


def test_a_replacement_of_a_replaced_item_replaces_the_current_one(remembering):
    item = lambda evidence, **more: {"kind": "fact", "text": evidence, "evidence": evidence, "explicit": 1, "due": "",
                                     "due_at": None, "message_id": 1, "mentioned_at": DAY, **more}
    [old] = remembering.store.advance("", "work", message_id=1, at=DAY, items=[item("lives in Paris")])
    [mid] = remembering.store.advance("", "work", message_id=2, at=DAY, after_id=1,
                                      items=[item("lives in Lyon", replaces=old)])
    [new] = remembering.store.advance("", "work", message_id=3, at=DAY, after_id=2,
                                      items=[item("lives in Rome", replaces=old)])  # the model named the first one
    assert [i["id"] for i in remembering.store.open_items()] == [new]
    with remembering.store._connect() as conn:
        assert conn.execute("SELECT superseded_by FROM items WHERE id=?", (mid,)).fetchone()[0] == new
    assert remembering.store.advance("", "work", message_id=4, at=DAY + 9, after_id=3,
                                     items=[item("still Rome", same_as=old, mentioned_at=DAY + 9)]) == []
    assert remembering.store.open_items()[0]["mentioned_at"] == DAY + 9  # the repeat landed on the current item
    remembering.store.advance("", "work", message_id=6, at=DAY, after_id=4, items=[
        item("Rome by Friday", same_as=new, due="2026-10-23", due_at=DAY + 3 * 86400)])
    assert remembering.store.open_items()[0]["due"] == "2026-10-23"  # a date said with the repeat
    remembering.store.advance("", "work", message_id=7, at=DAY, after_id=6, items=[
        item("Rome", same_as=new, due="2026-10-21", due_at=DAY + 86400)])
    assert remembering.store.open_items()[0]["due"] == "2026-10-23"  # an earlier one doesn't move it back
    assert remembering.store.advance("", "work", message_id=7, at=DAY, after_id=7, closed=[
        {"id": old, "status": "done", "evidence": "x", "message_id": 7}]) == []  # through the chain to the current one
    assert remembering.store.open_items() == []
    with remembering.store._connect() as conn:
        conn.execute("UPDATE items SET status='open', closed_by='', closed_at=NULL WHERE id=?", (new,))
        conn.commit()
    remembering.store.close_item(new, status="done", closed_by="user")
    assert remembering.store.advance("", "work", message_id=8, at=DAY, after_id=7,
                                     items=[item("Rome again", same_as=new)]) == []  # closed: already on record
    assert remembering.store.open_items() == []


def test_candidates_are_this_chats_items_then_the_closest_ones():
    items = [_open(1, "fact", "The user likes jazz.", "I love jazz", chat="other", at=DAY + 3),
             _open(2, "commitment", "The user will renew the passport.", "renew my passport", chat="other", at=DAY + 2),
             _open(3, "commitment", "The user will call mum.", "call mum", at=DAY + 1)]
    chunk = [_row(1, "user", "Did I renew my passport yet?", DAY + 60)]
    assert [i["id"] for i in memory.candidates(items, chunk, "work")] == [3, 2]


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


def test_tokens_spent_before_an_unreadable_state_db_are_kept(remembering, db, monkeypatch):
    db.session("more", title="More", pinned=0)
    db.message("user", "Remind me to renew my passport", sid="work", at=DAY + 60)
    db.message("assistant", "Will do.", sid="work", at=DAY + 60)
    db.message("user", "Book the dentist", sid="more", at=DAY + 60)
    db.message("assistant", "Booked.", sid="more", at=DAY + 60)
    model = FakeModel({"items": []})
    read = hermes.chat_messages
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise hermes.HermesUnreadable("locked")
        return read(*args, **kwargs)

    monkeypatch.setattr(hermes, "chat_messages", flaky)
    done = {}
    with pytest.raises(hermes.HermesUnreadable):
        memory.run(remembering.store, [("", remembering.store.path.parent, SID)], model, zone=UTC,
                   now=DAY + 60 + memory.IDLE_S, done=done)
    assert (done["chunks"], done["tokens"]) == (1, 100)


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
    chat = model.shown[1].split("<<<chat")[1]
    assert "renew my passport" not in chat and "Also book the vet" in chat  # read once (it's remembered, above)
    assert remembering.store.item_counts() == {"commitment": 1}


def test_repeats_update_one_item_and_changes_supersede_it(remembering, db):
    db.message("user", "Remind me to renew my passport", sid="work", at=DAY + 60)
    db.message("assistant", "Will do.", sid="work", at=DAY + 70)
    item = {"kind": "commitment", "text": "The user will renew their passport.", "evidence": "renew my passport",
            "line": 1, "explicit": True}
    _remember(remembering, FakeModel({"items": [item]}), DAY + 70 + memory.IDLE_S)
    [first] = remembering.store.open_items()
    db.message("user", "Remind me to renew my passport, it's urgent", sid="work", at=DAY + 9000)
    db.message("assistant", "Noted.", sid="work", at=DAY + 9010)
    model = FakeModel({"items": [{**item, "same_as": first["id"]}]})
    done = _remember(remembering, model, DAY + 9010 + memory.IDLE_S)
    assert done["items"] == 0 and f"#{first['id']} commitment" in model.shown[0]
    [again] = remembering.store.open_items()
    assert again["id"] == first["id"] and again["mentioned_at"] == DAY + 9000
    db.message("user", "Actually I'll renew it on Friday instead", sid="work", at=DAY + 20000)
    db.message("assistant", "Got it.", sid="work", at=DAY + 20010)
    _remember(remembering, FakeModel({"items": [{**item, "evidence": "I'll renew it on Friday instead",
                                                 "replaces": first["id"]}], "closed": []}),
              DAY + 20010 + memory.IDLE_S)
    [now_open] = remembering.store.open_items()
    assert now_open["id"] != first["id"]
    with remembering.store._connect() as conn:
        old = dict(conn.execute("SELECT * FROM items WHERE id=?", (first["id"],)).fetchone())
    assert old["superseded_by"] == now_open["id"] and old["valid_to"] == DAY + 20000  # kept, not deleted
    db.message("user", "Passport renewed, done", sid="work", at=DAY + 30000)
    db.message("assistant", "Great.", sid="work", at=DAY + 30010)
    _remember(remembering, FakeModel({"items": [], "closed": [
        {"number": now_open["id"], "status": "done", "evidence": "Passport renewed", "line": 1}]}),
        DAY + 30010 + memory.IDLE_S)
    assert remembering.store.open_items() == []
    assert not remembering.store.close_item(first["id"], status="cancelled", closed_by="user")  # superseded already
    with remembering.store._connect() as conn:
        closed = dict(conn.execute("SELECT * FROM items WHERE id=?", (now_open["id"],)).fetchone())
    assert closed["status"] == "done" and "Passport renewed" in closed["closed_by"]


def test_items_long_past_their_due_date_expire(remembering, db):
    remembering.store.advance("", "work", message_id=1, at=DAY, items=[
        {"kind": "commitment", "text": "x", "evidence": "y", "explicit": 1, "due": "2026-10-20", "due_at": DAY,
         "message_id": 1, "mentioned_at": DAY - 60},
        {"kind": "fact", "text": "z", "evidence": "w", "explicit": 1, "due": "", "due_at": None,
         "message_id": 1, "mentioned_at": DAY - 60}])
    _remember(remembering, FakeModel(), DAY + memory.EXPIRE_S - 60)
    assert len(remembering.store.open_items()) == 2
    _remember(remembering, FakeModel(), DAY + memory.EXPIRE_S + 60)
    assert [i["kind"] for i in remembering.store.open_items()] == ["fact"]


def test_a_commitment_learned_after_its_time_is_kept_as_expired(remembering, db):
    db.message("user", "Remind me at 16:00 today to repair my wallet", sid="work", at=DAY + 5 * 3600)
    db.message("assistant", "I'll set a reminder for 4:00 PM.", sid="work", at=DAY + 5 * 3600 + 10)
    db.message("user", "I'll file the expense report by Saturday", sid="work", at=DAY + 5 * 3600 + 20)
    db.message("assistant", "Noted. I'll check the build tomorrow at 9 too.", sid="work", at=DAY + 5 * 3600 + 30)
    model = FakeModel({"items": [
        {"kind": "commitment", "text": "The user wants a reminder at 16:00 to repair their wallet.",
         "evidence": "Remind me at 16:00 today to repair my wallet", "line": 1, "due": "2026-10-20T16:00",
         "due_words": "16:00 today"},
        {"kind": "commitment", "text": "The user will file the expense report.", "evidence": "file the expense report",
         "line": 3, "due": "2026-10-24", "due_words": "by Saturday"},
        {"kind": "promise", "text": "Hermes will check the build.", "evidence": "I'll check the build tomorrow at 9",
         "line": 4, "due": "2026-10-21T09:00", "due_words": "tomorrow at 9"}]})
    # Memory started the day after, so a backfill of older chats reads it, five days on: Sunday 05:00.
    remembering.store.set("memory_since", DAY + 86400)
    remembering.store.set("memory_backfill", {"from": DAY - 86400, "to": DAY + 86400, "active": True, "done": False})
    now = DAY + 5 * 86400 + 5 * 3600
    _remember(remembering, model, now)
    # Saturday's lasts to its end, five hours ago; a promise past its time is still Hermes's to keep.
    assert sorted(item["kind"] for item in remembering.store.open_items()) == ["commitment", "promise"]
    with remembering.store._connect() as conn:
        wallet = dict(conn.execute("SELECT * FROM items WHERE evidence LIKE '%wallet%'").fetchone())
    assert (wallet["status"], wallet["closed_by"]) == ("expired", store.LATE_BY)
    pulse.followup.schedule(remembering.store, now)
    assert wallet["id"] not in {row["item_id"] for row in remembering.store.followups(-1)}  # never reminded


def test_a_commitment_a_live_read_learns_of_late_is_left_to_the_followup_checks(remembering, db):
    db.message("user", "Remind me at 16:00 today to repair my wallet", sid="work", at=DAY + 5 * 3600)
    db.message("assistant", "I'll set a reminder for 4:00 PM.", sid="work", at=DAY + 5 * 3600 + 10)
    model = FakeModel({"items": [
        {"kind": "commitment", "text": "The user wants a reminder at 16:00 to repair their wallet.",
         "evidence": "Remind me at 16:00 today to repair my wallet", "line": 1, "due": "2026-10-20T16:00",
         "due_words": "16:00 today"}]})
    # Memory fell behind (its daily budget ran out, or the host was off) and reads it the next morning.
    _remember(remembering, model, DAY + 86400 + 9 * 3600)
    assert [item["kind"] for item in remembering.store.open_items()] == ["commitment"]


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
    # A failure on a chunk someone else already read isn't counted against the chat.
    assert remembering.store.chunk_failed("", "work", retry_at=DAY, after_id=0) == 0
    # Nor is a second reader's failure in the same round.
    assert remembering.store.chunk_failed("", "fresh", retry_at=DAY + 100, now=DAY) == 1
    assert remembering.store.chunk_failed("", "fresh", retry_at=DAY + 110, now=DAY + 10) == 0
    assert remembering.store.chunk_failed("", "fresh", retry_at=DAY + 300, now=DAY + 200) == 2
    assert remembering.store.watermark("", "work")["failures"] == 0


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


def test_memory_stops_at_its_token_budget(remembering, db):
    for sid in ("work", "w2"):
        if sid == "w2":
            db.session("w2", title="Other", pinned=0)
        db.message("user", f"hello {sid}", sid=sid, at=DAY + 60)
        db.message("assistant", "hi", sid=sid, at=DAY + 70)
    model = FakeModel()
    done = memory.run(remembering.store, [("", remembering.store.path.parent, SID)], model, zone=UTC,
                      now=DAY + 70 + memory.IDLE_S, budget=50)
    assert done["chunks"] == 1 and len(model.shown) == 1  # the first call used 100 tokens


def test_a_continued_chat_starts_where_its_parent_stopped(remembering, db):
    db.message("user", "first part", sid="work", at=DAY + 60)
    db.message("assistant", "ok", sid="work", at=DAY + 70)
    model = FakeModel()
    _remember(remembering, model, DAY + 70 + memory.IDLE_S)
    db.message("user", "unread when compressed", sid="work", at=DAY + 3000)
    db.message("assistant", "noted", sid="work", at=DAY + 3010)
    db.session("work2", title="Work", pinned=0, parent="work")
    db.message("user", "first part", sid="work2", at=DAY + 60)  # the tail compression carried over
    db.message("user", "unread when compressed", sid="work2", at=DAY + 3000)
    db.message("user", "second part", sid="work2", at=DAY + 4000)
    db.message("assistant", "ok", sid="work2", at=DAY + 4010)
    _remember(remembering, model, DAY + 4010 + memory.IDLE_S)
    _remember(remembering, model, DAY + 4010 + memory.IDLE_S)  # the child waited for its parent
    assert [text.count("unread when compressed") for text in model.shown] == [0, 1, 0]
    assert "first part" not in model.shown[2] and "second part" in model.shown[2]


def test_a_failing_chunk_is_retried_then_skipped(remembering, db):
    db.message("user", "hello", sid="work", at=DAY + 60)
    db.message("assistant", "hi", sid="work", at=DAY + 70)
    model = FakeModel(TimeoutError(), TimeoutError(), ["not", "items"])
    now = DAY + 70 + memory.IDLE_S
    first = _remember(remembering, model, now)
    assert first["failed"] == 1 and 0 < first["tokens"] < memory.MAX_TOKENS  # a failed call counts its input, estimated
    assert _remember(remembering, model, now + 60)["failed"] == 0  # waits for its retry
    _remember(remembering, model, now + memory.RETRY_S)
    last = _remember(remembering, model, now + 2 * memory.RETRY_S)
    assert (last["skipped"], last["tokens"]) == (1, 100)  # a bad reply counts what the model reported
    assert len(model.shown) == 3 and _remember(remembering, model, now + 3 * memory.RETRY_S)["failed"] == 0
    assert len(model.shown) == 3  # skipped after the third failure, not tried forever
    assert remembering.store.watermark("", "work")["failures"] == 0


def test_failed_calls_count_toward_a_runs_limit(remembering, db):
    for n in range(memory.MAX_CHUNKS + 2):
        db.session(f"c{n}", title=f"Chat {n}", pinned=0)
        db.message("user", "hello", sid=f"c{n}", at=DAY + 60)
        db.message("assistant", "hi", sid=f"c{n}", at=DAY + 70)
    model = FakeModel(*[TimeoutError()] * 10)
    _remember(remembering, model, DAY + 70 + memory.IDLE_S)
    assert len(model.shown) == memory.MAX_CHUNKS


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


def test_model_calls_run_in_this_profiles_secret_scope(remembering, monkeypatch):
    # A dashboard that has served another profile raises on an unscoped key read.
    scope = []

    @contextlib.contextmanager
    def config_profile_scope(profile):
        scope.append(profile)
        yield
        scope.append("left")

    class Llm:
        def complete_structured(self, **kwargs):
            assert scope == [None]  # inside this profile's scope
            return types.SimpleNamespace(parsed={"items": []}, usage=types.SimpleNamespace(total_tokens=5))

    module = types.ModuleType("hermes_cli.web_server_profiles")
    module._config_profile_scope = config_profile_scope
    monkeypatch.setitem(sys.modules, "hermes_cli", sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", module)
    remembering.ctx.llm = Llm()
    assert pulse._memory_llm("instructions", "text", purpose="pulse follow-up") == ({"items": []}, 5)
    assert scope == [None, "left"]


def test_the_chats_model_is_read_where_hermes_reads_it(db, tmp_path):
    db.session()
    assert hermes.chat_model_row(tmp_path, SID) is None  # on Hermes's main model
    db.model(model="glm-5.3-flash", config={"provider": "zai"})
    db.session("next", started=2000.0, parent=SID)  # compressed into
    db.model("next", model=VOLCENGINE["model"], config=VOLCENGINE_CONFIG, billing="volcengine")
    db.session("branch", started=3000.0, parent="next")
    db.model("branch", model="gpt-5.5", config={"provider": "openai", "_branched_from": "next"})
    db.session("helper", started=3000.0, parent="next", source="tool")  # a subagent's run
    db.model("helper", model="gpt-5.5", config={"provider": "openai"})
    row = hermes.chat_model_row(tmp_path, SID)
    assert (row["id"], row["model"], row["billing_provider"]) == ("next", VOLCENGINE["model"], "volcengine")
    assert set(row) == set(hermes.MODEL_COLUMNS)
    assert pulse.chat_model(tmp_path, SID) == {"provider": "volcengine", "model": VOLCENGINE["model"]}
    # A continuation carries its parent's markers: one naming another session makes no fork.
    db.session("last", started=4000.0, parent="next")
    db.model("last", model="kimi-k2.6", config={"_branched_from": "elsewhere"}, billing="custom")
    assert hermes.chat_model_row(tmp_path, SID)["id"] == "last"
    assert pulse.chat_model(tmp_path, SID) == {"provider": "", "model": "kimi-k2.6"}  # "custom" names none
    db.model("last", model="kimi-k2.6", config={"provider": "custom:ark", "gateway_runtime": {"provider": "volcengine"}})
    assert pulse.chat_model(tmp_path, SID) == {"provider": "volcengine", "model": "kimi-k2.6"}


def test_pulses_model_calls_run_on_the_pulse_chats_model(remembering, db, tmp_path, monkeypatch):
    # Settings > Pulse's "Same as the Pulse chat" leaves Hermes's "Pulse memory" slot on "auto", which
    # meant Hermes's main model until the Pulse chat's model was lent to the call (Eric, 2026-10-25:
    # report lists and follow-up checks failed while the main model was out of capacity).
    routes = Routes(monkeypatch)
    seen = []

    class Llm:
        def complete_structured(self, **kwargs):
            seen.append(list(routes.bound))
            return types.SimpleNamespace(parsed={"items": []}, usage=types.SimpleNamespace(total_tokens=5))

    remembering.ctx.llm = Llm()
    db.model(model=VOLCENGINE["model"], config=VOLCENGINE_CONFIG)
    route = pulse.chat_route(tmp_path, SID)
    assert route == VOLCENGINE and [row["id"] for row in routes.read] == [SID]
    assert pulse._memory_llm("instructions", "text", route=route) == ({"items": []}, 5)
    assert seen == [[VOLCENGINE]] and routes.bound == []  # lent for the call, and only for it
    assert pulse._memory_llm("instructions", "text") == ({"items": []}, 5)
    assert seen[-1] == []
    # What Hermes leaves out of the chat's route stays out, as it does when it reopens the chat.
    db.model(model=VOLCENGINE["model"], config={"provider": "volcengine"})
    assert pulse.chat_route(tmp_path, SID) == {"provider": "volcengine", "model": VOLCENGINE["model"]}


def test_pulse_stays_on_hermess_main_model_when_the_chat_has_no_model_to_lend(remembering, db, tmp_path,
                                                                                monkeypatch, caplog):
    routes = Routes(monkeypatch, dropped={"volcengine"})
    assert pulse.chat_route(tmp_path, SID) is None and routes.read == []  # no model of its own: nothing to read
    db.model(model=VOLCENGINE["model"], config=VOLCENGINE_CONFIG)
    assert pulse.chat_route(tmp_path, SID) is None  # its provider is gone: Hermes's main one, as "auto" is
    routes.dropped.clear()
    assert pulse.chat_route(tmp_path, "") is None
    server = sys.modules["tui_gateway.server"]
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda row: 1 / 0)
    with caplog.at_level(logging.WARNING, logger=pulse.logger.name):
        assert pulse.chat_route(tmp_path, SID) is None  # a reader that fails, which the log says
    assert "Pulse couldn't read its chat's model" in caplog.text
    monkeypatch.delattr(server, "_stored_session_runtime_overrides")
    assert pulse.chat_route(tmp_path, SID) is None  # a Hermes without the reader
    monkeypatch.delitem(sys.modules, "tui_gateway.server")
    assert pulse.chat_route(tmp_path, SID) is None  # outside the process hosting the chat
    # A Hermes without the binding: the call still goes, on Hermes's main model.
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", types.ModuleType("agent.auxiliary_client"))
    remembering.ctx.llm = types.SimpleNamespace(complete_structured=lambda **kwargs: types.SimpleNamespace(
        parsed={"items": []}, usage=types.SimpleNamespace(total_tokens=3)))
    assert pulse._memory_llm("instructions", "text", route=VOLCENGINE) == ({"items": []}, 3)


def test_memory_and_follow_ups_run_on_the_pulse_chats_model(remembering, monkeypatch):
    store_ = remembering.store
    store_.set("enabled", True)
    store_.set("memory", True)
    monkeypatch.setattr(pulse, "chat_route", lambda home, chat_id: VOLCENGINE if chat_id == SID else None)
    asked = []

    def memory_llm(instructions, text, purpose="pulse memory", route=None):
        asked.append((purpose, route))
        return {"items": []}, 1

    monkeypatch.setattr(pulse, "_memory_llm", memory_llm)
    monkeypatch.setattr(pulse.memory, "run", lambda pulse_store, sources, ask, **kwargs: ask("i", "t"))
    monkeypatch.setattr(pulse.followup, "run", lambda pulse_store, home, ask, **kwargs: ask("i", "t"))
    pulse._memory_run(store_.path.parent, SID, time.time(), threading.Event())
    assert asked == [("pulse memory", VOLCENGINE), ("pulse follow-up", VOLCENGINE)]
    # A model the user gave Hermes's "Pulse memory" slot runs them, with nothing lent.
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config = lambda: {"auxiliary": {memory.TASK: {"provider": "openrouter", "model": "google/gemini-flash"}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    asked.clear()
    pulse._memory_run(store_.path.parent, SID, time.time(), threading.Event())
    assert asked == [("pulse memory", None), ("pulse follow-up", None)]


def test_pulse_status_says_which_model_pulse_runs_on(live, db, capsys, monkeypatch):
    db.session()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    config = {"auxiliary": {memory.TASK: {"provider": "auto", "model": ""}}}
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config = lambda: config
    monkeypatch.setitem(sys.modules, "hermes_cli", sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    live.store.set("enabled", True)

    def status():
        cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
        return [line for line in capsys.readouterr().out.splitlines() if line.startswith("Model: ")]

    every = "for its turns, memory, report lists and follow-up checks"
    assert status() == [f"Model: Hermes's main model, {every}"]
    db.model(model="local-model", billing="custom")  # names no provider, so it lends Pulse none
    assert status() == ["Model: local-model, the Pulse chat's own, for its turns; memory, report lists and "
                        "follow-up checks run on Hermes's main model"]
    db.model(model=VOLCENGINE["model"], config=VOLCENGINE_CONFIG)
    assert status() == [f"Model: {VOLCENGINE['model']} on volcengine, the Pulse chat's own, {every}"]
    config["auxiliary"][memory.TASK] = {"provider": "openrouter", "model": "google/gemini-flash"}
    assert status() == [f"Model: {VOLCENGINE['model']} on volcengine, the Pulse chat's own, for its turns; memory, "
                        "report lists and follow-up checks run on google/gemini-flash on openrouter, "
                        "Hermes's \"Pulse memory\" model"]
    live.store.set(pulse.MODEL, {"provider": "openrouter", "model": "google/gemini-flash"})
    assert status() == [f"Model: google/gemini-flash on openrouter, from Settings > Pulse, {every}"]
    config["auxiliary"][memory.TASK] = {"provider": "nous", "model": ""}
    assert status()[0].endswith("run on nous's default model, Hermes's \"Pulse memory\" model")
    config["auxiliary"][memory.TASK] = {"provider": "openrouter", "model": {"name": "x"}}  # hand-edited
    assert status() == [f"Model: google/gemini-flash on openrouter, from Settings > Pulse, {every}"]
    hermes_config.load_config = lambda: 1 / 0
    assert status() == [f"Model: google/gemini-flash on openrouter, from Settings > Pulse, {every}"]


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
    monkeypatch.setattr(pulse, "_stop", threading.Event())
    start = time.time()
    pulse.tick(start)
    pulse._memory_thread.join(5)
    assert remembering.store.get("memory_since") == start  # memory starts now, not from old chats
    db.message("user", "hello", sid="work", at=start + 1)
    db.message("assistant", "hi", sid="work", at=start + 2)
    pulse.tick(start + 60)
    assert pulse._memory_thread.ident and calls == []  # not due again yet
    stopping = threading.Event()
    assert memory.run(remembering.store, [("", remembering.store.path.parent, SID)],
                      lambda *_: (stopping.set(), ({"items": []}, 1))[1], zone=UTC, now=start + 4000,
                      still_leader=lambda: not stopping.is_set())["chunks"] == 0  # stopped during the call
    assert remembering.store.watermark("", "work") is None
    pulse._memory_thread.join(5)
    monkeypatch.setattr(memory, "IDLE_S", 0.0)
    remembering.store.set("memory_requested", True)
    pulse.tick(start + 120)
    pulse._memory_thread.join(5)
    assert [(c["task"], c["json_mode"]) for c in calls] == [(memory.TASK, True)]
    assert remembering.store.get("memory_usage") == {"chunks": 1, "tokens": 7, "failed": 0, "skipped": 0}
    pulse._stop.set()  # an unloaded plugin's run makes no more calls
    db.message("user", "more", sid="work", at=start + 130)
    db.message("assistant", "ok", sid="work", at=start + 131)
    remembering.store.set("memory_requested", True)
    pulse.tick(start + 180)
    pulse._memory_thread.join(5)
    assert len(calls) == 1


def test_follow_ups_run_on_their_own_budget_after_memory_spent_its(remembering, monkeypatch):
    store_ = remembering.store
    store_.set("enabled", True)
    store_.set("memory", True)
    now = time.time() + 600  # ahead of the clock, so the run's day is this one
    day = datetime.fromtimestamp(now, pulse.report_zone(store_, store_.path.parent)).strftime("%Y-%m-%d")
    store_.set("memory_today", {"day": day, "tokens": store_.get("memory_daily_tokens")})
    ran = []
    monkeypatch.setattr(pulse.memory, "run", lambda *a, **kw: ran.append("memory"))

    def checks(*args, budget, done, **kwargs):
        ran.append(("followups", budget))
        done["tokens"] = 900

    monkeypatch.setattr(pulse.followup, "run", checks)
    pulse._memory_run(store_.path.parent, SID, now, threading.Event())
    assert ran == [("followups", pulse.learning.NUDGES["balanced"]["followup_tokens"])]
    assert store_.get("followup_today") == {"day": day, "tokens": 900}
    assert store_.get("memory_today")["tokens"] == store_.get("memory_daily_tokens")  # memory's day is its own
    store_.set("followup_today", {"day": day, "tokens": pulse.learning.NUDGES["balanced"]["followup_tokens"]})
    ran.clear()
    pulse._memory_run(store_.path.parent, SID, now, threading.Event())
    assert ran == []  # both spent


def test_a_backfill_reads_older_chats_after_new_ones_then_finishes(remembering, db):
    store_ = remembering.store
    db.message("user", "I'll renew my passport next month", sid="work", at=DAY - 5 * 86400)
    db.message("assistant", "Noted.", sid="work", at=DAY - 5 * 86400 + 10)
    db.message("user", "Way too old", sid="work", at=DAY - 40 * 86400)
    db.message("user", "Book the vet", sid="work", at=DAY + 60)
    db.message("assistant", "Sure.", sid="work", at=DAY + 70)
    now = DAY + 70 + memory.IDLE_S
    assert memory.backfill_window(store_, 30, now) == (now - 30 * 86400, DAY)  # back to where memory started
    cost = memory.estimate([("", store_.path.parent, SID)], (now - 30 * 86400, DAY))
    assert (cost["chats"], cost["chunks"]) == (1, 1) and cost["chars"] == len("I'll renew my passport next month") + 6
    store_.set("memory_backfill", {"from": now - 30 * 86400, "to": DAY, "active": True, "done": False})
    model = FakeModel({"items": []}, {"items": [{"kind": "commitment", "text": "The user will renew their passport.",
                                                 "evidence": "renew my passport", "line": 1, "explicit": True}]})
    done = _remember(remembering, model, now)
    assert done["chunks"] == 2 and "Book the vet" in model.shown[0]  # new chats first
    old = model.shown[1].split("<<<chat")[1]
    assert "renew my passport" in old and "Book the vet" not in old and "Way too old" not in old
    [item] = store_.open_items()
    assert item["mentioned_at"] == DAY - 5 * 86400
    assert store_.watermark("", "work")["message_id"] == 5  # the backfill keeps its own marks
    assert [m["message_id"] for m in store_.watermarks("backfill_marks")] == [2]
    assert store_.get("memory_backfill")["active"]  # it finishes on the run that finds nothing left
    assert _remember(remembering, model, now + 300)["chunks"] == 0
    assert store_.get("memory_backfill")["done"] and not memory.active_backfill(store_)
    assert memory.backfill_window(store_, 30, now) is None  # already read that far
    assert memory.backfill_window(store_, 60, now) == (now - 60 * 86400, now - 30 * 86400)  # carries on from there
    assert store_.get("memory_backfill")["scope"] == "all"
    # Started for every profile but finished while memory read only this one: it counts for this one only.
    store_.set("memory_backfill", {"from": now - 30 * 86400, "to": DAY, "active": True, "done": False, "scope": "all"})
    assert memory.run(store_, [("", store_.path.parent, SID)], model, zone=UTC, now=now + 600,
                      reads="this")["chunks"] == 0
    assert store_.get("memory_backfill")["done"] and store_.get("memory_backfill")["scope"] == "this"
    assert memory.backfill_window(store_, 30, now)[1] == DAY  # the other profiles' older chats are still due


def test_a_backfill_reads_the_chat_going_on_when_it_started(remembering, db):
    store_ = remembering.store
    db.message("user", "I'll call the bank", sid="work", at=DAY - 120)  # minutes before memory started
    db.message("assistant", "Ok.", sid="work", at=DAY - 110)
    store_.set("memory_backfill", {"from": DAY - 86400, "to": DAY, "active": True, "done": False})
    model = FakeModel()
    assert _remember(remembering, model, DAY + 60)["chunks"] == 1 and "call the bank" in model.shown[0]
    assert store_.get("memory_backfill")["active"]  # not done before its last chat was read
    assert store_.swap("memory_backfill", {"x": 1}, None) is False and store_.get("memory_backfill")["active"]
    gone = [("", store_.path.parent, SID), ("gone", store_.path.parent / "nowhere", "")]
    assert memory.run(store_, gone, model, zone=UTC, now=DAY + 120)["chunks"] == 0
    assert store_.get("memory_backfill")["active"]  # a profile it couldn't read isn't done
    assert store_.get("memory_backfill_unreadable") == ["gone"]  # `pulse status` says why
    assert memory.estimate(gone, (DAY - 86400, DAY))["unreadable"] == ["gone"]
    _remember(remembering, model, DAY + 180)
    assert store_.get("memory_backfill")["done"] and store_.get("memory_backfill_unreadable") == []


def test_a_backfill_stopped_mid_run_is_left_alone(remembering, db):
    store_ = remembering.store
    db.session("w2", title="Other", pinned=0)
    for n, sid in enumerate(("work", "w2")):
        db.message("user", f"Old thing {n}", sid=sid, at=DAY - (5 - n) * 86400)
        db.message("assistant", "Ok.", sid=sid, at=DAY - (5 - n) * 86400 + 10)
    cost = memory.estimate([("", store_.path.parent, SID)], (DAY - 30 * 86400, DAY))
    assert (cost["chats"], cost["chunks"]) == (2, 2)  # at least one model call per chat, however short
    store_.set("memory_backfill", {"from": DAY - 30 * 86400, "to": DAY, "active": True, "done": False})
    shown = []

    def model(instructions, text):
        shown.append(text)
        store_.set("memory_backfill", {**store_.get("memory_backfill"), "active": False})  # `backfill stop`
        return {"items": []}, 100

    assert _remember(remembering, model, DAY + 3600)["chunks"] == 0 and len(shown) == 1
    assert store_.watermarks("backfill_marks") == []  # the chunk in flight when it stopped isn't kept
    assert store_.get("memory_backfill")["active"] is False and not store_.get("memory_backfill")["done"]
    # A chunk that lands after `cancel` keeps its old window's stamp, so the next window reads that chat afresh.
    assert store_.advance("", "work", message_id=7, at=0, marks="backfill_marks", window=1.0) == []
    assert store_.watermarks("backfill_marks", 2.0) == [] and len(store_.watermarks("backfill_marks", 1.0)) == 1
    assert store_.advance("", "work", message_id=3, at=0, marks="backfill_marks", window=2.0) == []
    assert [mark["message_id"] for mark in store_.watermarks("backfill_marks", 2.0)] == [3]
    assert store_.watermarks("backfill_marks", 1.0) == []
    assert not memory.has_window({"from": 1e30, "to": 2e30}) and not memory.has_window({"from": -1.0, "to": 2.0})


def test_backfill_command(remembering, db, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda extra="": cli.dispatch(types.SimpleNamespace(
        conduit_push_action="pulse", pulse_action="memory", value="backfill", extra=extra))
    status = lambda: (cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status",
                                                          value="")), capsys.readouterr().out)[1]
    store_ = remembering.store
    store_.set("memory_since", time.time() - 60)
    store_.set("home_session_key", SID)
    store_.set("memory", True)
    db.message("user", "I'll renew my passport next month", sid="work", at=time.time() - 3 * 86400)
    assert run("0") == 2 and run("400") == 2 and run("0" * 5000) == 2 and run("soon") == 2 and run("stop") == 1
    capsys.readouterr()
    assert run("7") == 0
    out = capsys.readouterr().out
    assert "Reading back 7 days" in out and "covers 1 chats" in out and "backfill start" in out
    assert store_.get("memory_backfill_days") == 7 and store_.get("memory_backfill") is None
    assert run("start") == 0 and "Memory is reading chats from" in capsys.readouterr().out
    backfill = store_.get("memory_backfill")
    assert backfill["active"] and backfill["days"] == 7 and store_.get("memory_requested") is True
    assert "Backfill: reading chats back to" in status()
    assert run("start") == 1 and "already reading" in capsys.readouterr().out
    store_.advance("", "work", message_id=1, at=0, marks="backfill_marks")
    assert run("stop") == 0 and "Backfill: paused" in status()
    assert run("30") == 0 and "A paused backfill back to" in capsys.readouterr().out  # no wider window yet
    assert memory.backfill_window(store_, 30, time.time()) == (backfill["from"], backfill["to"])
    assert run("start") == 0 and store_.watermarks("backfill_marks")  # carries on: its marks are kept
    store_.set("memory_backfill_unreadable", ["gone"])
    assert run("stop") == 0 and run("cancel") == 0 and store_.watermarks("backfill_marks") == []
    assert store_.get("memory_backfill_unreadable") == []
    cancelled = store_.get("memory_backfill")
    assert cancelled["done"] and cancelled["from"] == backfill["to"] and run("cancel") == 1
    assert "Backfill: cancelled; memory has read back to" in status()
    assert not memory.has_window({"from": 2.0, "to": 1.0}) and not memory.has_window({"from": True, "to": 2.0})
    assert run("start") == 0 and store_.get("memory_backfill")["to"] == backfill["to"]  # starts over from there
    store_.set("memory_backfill", {**store_.get("memory_backfill"), "active": False, "done": True})
    assert "Backfill: read back to" in status()
    assert run("7") == 0 and "already read back 7 days" in capsys.readouterr().out
    # Read under `report this-profile`: once the report reads every profile, the others' older chats are still due.
    finished = store_.get("memory_backfill")
    assert finished["scope"] == "all"
    store_.set("memory_backfill", {**finished, "scope": "this"})
    store_.set("report_profiles", "this")
    assert memory.backfill_window(store_, 7, time.time()) is None and memory.scope(store_) == "this"
    store_.set("report_profiles", "all")
    assert memory.backfill_window(store_, 7, time.time())[1] == store_.get("memory_since")
    assert "read only this profile's older chats" in status()
    assert memory.scope(store_, store_.get("memory_backfill")) == "this"  # carrying one on stays narrow
    store_.set("memory_backfill", finished)
    assert run("30") == 0 and "Reading back 30 days" in capsys.readouterr().out
    store_.advance("", "w9", message_id=1, at=0, marks="backfill_marks", window=-5.0)
    assert run("start") == 0 and store_.watermarks("backfill_marks") == []  # a new, older window starts fresh
    store_.set("memory_backfill", None)
    store_.set("memory", False)
    store_.set("memory_since", None)
    assert run("start") == 1 and "memory is off" in capsys.readouterr().out
    assert store_.get("memory_since") is None  # a refused start doesn't move where live reading starts
    store_.set("memory", True)
    store_.set("memory_since", None)  # memory hasn't run yet
    assert run("start") == 0
    assert store_.get("memory_since") == store_.get("memory_backfill")["to"]  # live reading starts where it ends


def test_memory_command(remembering, db, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="", extra="": cli.dispatch(types.SimpleNamespace(
        conduit_push_action="pulse", pulse_action="memory", value=value, extra=extra))
    remembering.store.path.unlink()
    assert run() == 0 and run("fact") == 0 and run("now") == 1 and run("forget", "1") == 1
    assert not remembering.store.path.exists()  # nothing but on/off leaves a file on a home that never ran Pulse
    assert run("on") == 0 and remembering.store.get("memory") is True
    assert "once Pulse is on" in capsys.readouterr().out  # Pulse itself is still off
    assert run() == 0 and "hasn't kept anything" in capsys.readouterr().out
    remembering.store.advance("", "work", message_id=5, at=DAY, items=[dict(
        kind="promise", text="Hermes will check the build.", evidence="I'll check the build", explicit=1,
        due="2026-10-21", due_at=DAY + 86400, message_id=5, mentioned_at=DAY + 60)])
    assert run() == 0
    out = capsys.readouterr().out
    assert "Promise (1)\n  #1 Hermes will check the build. (due 2026-10-21)\n      \"I'll check the build\" (20 Oct)" in out
    assert run("fact") == 0 and "hasn't kept" in capsys.readouterr().out
    monkeypatch.setattr(cli, "MEMORY_LIST", 0)
    assert run() == 0 and "The 0 most recent of 1:" in capsys.readouterr().out
    monkeypatch.setattr(cli, "MEMORY_LIST", 50)
    assert run("mood") == 2
    assert run("forget", "x") == 2 and run("forget", "#²") == 2 and run("forget", "#9") == 1
    assert run("forget", "#1") == 0 and remembering.store.open_items() == []
    assert run("now") == 1  # Pulse is off
    remembering.store.set("enabled", True)
    remembering.store.set("memory", False)
    assert run("now") == 1
    remembering.store.set("memory", True)
    assert run("now") == 0 and remembering.store.get("memory_requested") is True
    capsys.readouterr()
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Memory: nothing kept yet (0 chat chunks read, 0 tokens)" in capsys.readouterr().out
    remembering.store.set("memory", False)
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    assert "Memory: off; nothing kept yet (" in capsys.readouterr().out


# Settings > Pulse (pulse_control and its dashboard routes) ---------------------

control = _load("conduit_push.pulse_control", ROOT / "pulse_control.py")


def test_pulse_state_and_settings_as_data(remembering, db):
    store_ = remembering.store
    store_.set("home_session_key", SID)
    home = store_.path.parent
    state = control.state(store_, home, now=DAY)
    assert state["enabled"] is False and state["home_chat"]["id"] == SID and state["memory"] is False
    assert state["report_times"] == ["08:00", "13:00", "18:00"] and state["backfill"] == {"state": "none"}
    assert state["backfill_days"] == 30 and state["daily_tokens"] == store.DEFAULTS["memory_daily_tokens"]
    control.update(store_, {"enabled": True, "daily_report": True, "report_times": ["17:30", "9:00"],
                            "report_profiles": "this", "time_zone": "Europe/Paris"})
    state = control.state(store_, home, now=DAY)
    assert state["enabled"] and state["report_times"] == ["09:00", "17:30"] and state["report_profiles"] == "this"
    assert state["time_zone"] == "Europe/Paris" and state["next_report_at"] > DAY
    assert state["note"] == ""
    control.update(store_, {"note": "  Never bring up the gym.\nAlways tell me when a build finishes.  "})
    assert control.state(store_, home, now=DAY)["note"] == "Never bring up the gym.\nAlways tell me when a build finishes."
    for bad in ({"enabled": "yes"}, {"report_times": ["25:00"]}, {"report_times": []}, {"report_profiles": "some"},
                {"time_zone": "Mars/Base"}, {"memory": 1, "enabled": False}, {"report_time": "08:00"}, [],
                {"note": 5}, {"note": "x" * (control.NOTE_LIMIT + 1)}):
        with pytest.raises(control.Refused) as refused:
            control.update(store_, bad)
        assert refused.value.code == "invalid"
    assert store_.get("enabled") is True  # a refused change changes nothing
    assert store_.get("note").startswith("Never bring up the gym.")
    control.update(store_, {"note": ""})
    assert control.state(store_, home, now=DAY)["note"] == ""
    control.request_report(store_)
    assert store_.get("report_requested") is True
    store_.set("enabled", False)
    with pytest.raises(control.Refused, match="pulse_off"):
        control.request_report(store_)


def test_the_report_turn_carries_the_users_note():
    text = report.report_text("pabc-1", "ctx", "Mon 20 Oct 08:00", note=" Never bring up the gym. ")
    assert f"and stop.\n\n{report.NOTE_TITLE}\nNever bring up the gym.\n\nEverything between the context markers" in text
    assert report.NOTE_TITLE not in report.report_text("pabc-1", "ctx", "Mon 20 Oct 08:00")
    assert report.note_block("") == "" and report.note_block(None) == ""


def test_pulse_backfill_steps_as_data(remembering, db):
    store_ = remembering.store
    store_.set("home_session_key", SID)
    home = store_.path.parent
    store_.set("memory", True)
    db.message("user", "I'll renew my passport next month", sid="work", at=DAY - 3 * 86400)
    with pytest.raises(control.Refused, match="not_running"):
        control.backfill_stop(store_)
    cost = control.backfill_estimate(store_, home, 7, now=DAY + 60)
    assert (cost["days"], cost["chats"], cost["to"]) == (7, 1, DAY) and cost["days_needed"] == 1
    started = control.backfill_start(store_, home, now=DAY + 60)  # the days last shown
    assert started["days"] == 7 and not started["resumed"]
    assert control.state(store_, home, now=DAY + 60)["backfill"]["state"] == "reading"
    with pytest.raises(control.Refused) as refused:
        control.backfill_start(store_, home, 20, now=DAY + 60)
    assert refused.value.code == "reading" and refused.value.data["from"] == started["from"]
    assert store_.get("memory_backfill_days") == 7  # a refused start doesn't change the days
    control.backfill_stop(store_)
    with pytest.raises(control.Refused, match="paused"):
        control.backfill_estimate(store_, home, 30, now=DAY + 60)
    assert control.backfill_start(store_, home, now=DAY + 60)["resumed"]
    control.backfill_cancel(store_)
    assert control.state(store_, home, now=DAY + 60)["backfill"]["state"] == "cancelled"
    store_.set("memory", False)
    with pytest.raises(control.Refused, match="memory_off"):
        control.backfill_start(store_, home, now=DAY + 60)


def test_pulse_routes(remembering, db, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_pulse", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    store_ = remembering.store
    store_.set("home_session_key", SID)
    home = store_.path.parent
    modules = {"pulse_control": control, "pulse": pulse}
    monkeypatch.setattr(api, "_plugin_package_module", lambda name: modules[name])
    monkeypatch.setattr(api, "_calls_home", lambda: home)
    # More requests than the minute's limit allows, which this test doesn't test.
    monkeypatch.setattr(api, "_pulse_limiter", api._MintLimiter(1000, api.PULSE_WINDOW_S, message="limit"))
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    http = TestClient(app)
    base = "/api/plugins/conduit_push/pulse"
    got = http.get(base)
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    assert got.json()["ok"] and got.json()["home_chat"]["id"] == SID
    put = http.put(base, json={"enabled": True, "daily_report": False})
    assert put.status_code == 200 and put.json()["enabled"] and put.json()["next_report_at"] is None
    bad = http.put(base, json={"report_times": ["noon"]})
    assert bad.status_code == 400 and bad.json()["detail"] == {"code": "invalid", "field": "report_times"}
    # The note to Pulse fits the body cap in any script: NOTE_LIMIT characters of three bytes each here.
    long_note = "記" * control.NOTE_LIMIT
    noted = http.put(base, json={"note": long_note})
    assert noted.status_code == 200 and noted.json()["note"] == long_note
    over = http.put(base, json={"note": long_note + "記"})
    assert over.status_code == 400 and over.json()["detail"] == {"code": "invalid", "field": "note"}
    assert http.put(base, json={"note": " "}).json()["note"] == ""
    assert http.put(base, json={"note": "n" * control.NOTE_LIMIT + " \n"}).json()["note"] == "n" * control.NOTE_LIMIT
    for bad in ("a\x00b", "a\x1bb"):
        assert http.put(base, json={"note": bad}).json()["detail"] == {"code": "invalid", "field": "note"}
    lone = http.put(base, content=b'{"note": "a\\ud83db"}', headers={"content-type": "application/json"})
    assert lone.json()["detail"] == {"code": "invalid", "field": "note"}
    assert http.put(base, json={"note": "tabs\tand\nlines"}).json()["note"] == "tabs\tand\nlines"
    assert http.put(base, json={"note": ""}).json()["note"] == ""
    stop = http.post(f"{base}/backfill", json={"action": "stop"})
    assert stop.status_code == 409 and stop.json()["detail"]["code"] == "not_running"
    assert http.post(f"{base}/backfill", json={"action": "estimate", "days": 0}).status_code == 400
    assert http.post(f"{base}/backfill", json={"action": "estimate", "days": 3})
    assert http.post(f"{base}/report").json() == {"ok": True}
    item = store_.advance("", "work", message_id=1, at=DAY, items=[{
        "kind": "commitment", "text": "The user will renew their passport.", "evidence": "renew", "explicit": True,
        "due": "", "due_at": None, "mentioned_at": DAY, "message_id": 1}])[0]
    listed = http.get(f"{base}/memory").json()["items"]
    assert [entry["id"] for entry in listed] == [item]
    assert http.post(f"{base}/memory/forget", json={"id": item}).json()["forgotten"] is True
    assert http.post(f"{base}/memory/forget", json={"id": "x"}).status_code == 400
    assert http.get(f"{base}/memory", params={"kind": "nope"}).status_code == 400
    store_.add_followup(item, next_check_at=DAY, now=DAY)
    got = http.get(f"{base}/followups")
    assert got.headers["cache-control"] == "no-store"
    assert [(entry["id"], entry["decision"]) for entry in got.json()["followups"]] == [(item, "waiting")]
    card = store_.add_card({"kind": "followup", "item_id": item, "level": "push", "title": "t", "news": "n",
                            "created_at": DAY})
    feed = http.get(f"{base}/feed").json()
    assert [entry["id"] for entry in feed["cards"]] == [card] and feed["unseen"] == 1
    assert feed["levels"] == {"followup": "push"}
    assert http.post(f"{base}/feed/seen", json={"ids": None}).json()["seen"] == 1
    assert http.post(f"{base}/feed/seen", json={"ids": "all"}).status_code == 400
    assert http.post(f"{base}/feed/seen", json={}).status_code == 400
    assert http.post(f"{base}/feed/seen", json={"ids": []}).json()["seen"] == 0
    assert http.post(f"{base}/rate", json={"card": card, "rating": "sideways"}).json()["detail"] == {
        "code": "invalid", "field": "rating"}
    [lesson] = http.post(f"{base}/rate", json={"card": card, "rating": "down", "reason": "too_often"}).json()["learned"]
    assert lesson["value"] == "brief"
    assert http.get(f"{base}/learned").json()["levels"] == {"followup": "brief"}
    undone = http.post(f"{base}/learned/undo", json={"id": lesson["id"]}).json()
    assert undone["undone"] is True and undone["levels"] == {"followup": "push"}
    assert http.put(f"{base}/levels", json={"kind": "followup", "level": "screen"}).json()["levels"] == {
        "followup": "screen"}
    assert http.put(f"{base}/levels", json={"kind": "followup", "level": "loud"}).status_code == 400
    # What learning holds, reset; what the reports left out, and one brought up.
    assert "pulse_explains" in api.ROUTE_CAPABILITIES
    assert [entry["scope"] for entry in http.get(f"{base}/learned").json()["preferences"]] != []
    reset = http.post(f"{base}/learned/reset", json={"scope": "kind", "value": "followup"}).json()
    assert reset["reset"]["what"] == "reset" and reset["preferences"] == []
    assert reset["lessons"][0]["id"] == reset["reset"]["id"]
    bad = http.post(f"{base}/learned/reset", json={"scope": "everything", "value": "followup"})
    assert bad.status_code == 400 and bad.json()["detail"] == {"code": "invalid", "field": "value"}
    assert http.post(f"{base}/learned/reset", json={"scope": "everything"}).json()["reset"]["kind"] == "everything"
    [left] = store_.add_candidates("r1", [{"origin": "model", "kind": "date", "text": "Dentist Friday at 9",
                                           "decision": "not_picked", "bring_up": False}], now=time.time())
    got = http.get(f"{base}/left_out")
    assert got.headers["cache-control"] == "no-store" and [entry["id"] for entry in got.json()["left_out"]] == [left]
    brought = http.post(f"{base}/left_out/bring_up", json={"id": left}).json()["card"]
    assert brought["news"] == "Dentist Friday at 9" and brought["level"] == "screen"
    assert http.post(f"{base}/left_out/bring_up", json={"id": left}).json()["card"]["id"] == brought["id"]
    assert http.post(f"{base}/left_out/bring_up", json={"id": str(left)}).json()["detail"] == {
        "code": "invalid", "field": "id"}
    assert http.post(f"{base}/left_out/bring_up", json=[left]).status_code == 400
    assert "pulse" in api.ROUTE_CAPABILITIES and "pulse_feed" in api.ROUTE_CAPABILITIES
    assert "pulse_daily_brief" in api.ROUTE_CAPABILITIES  # Settings > Pulse says the brief comes every day
    # A report item Pulse found on its own takes Do it and Done too.
    assert "pulse_found_actions" in api.ROUTE_CAPABILITIES
    assert "pulse_note" in api.ROUTE_CAPABILITIES  # Settings > Pulse shows the note to Pulse
    # Quiet times: weekdays, and a spell until the end of today or a day.
    assert "pulse_quiet" in api.ROUTE_CAPABILITIES
    hushed = http.put(base, json={"quiet_days": [6, 5], "quiet_until": "today"}).json()
    assert hushed["quiet_days"] == [5, 6] and hushed["quiet_until"] > time.time() and hushed["quiet_now"] == "until"
    assert hushed["quiet_ends_at"] >= hushed["quiet_until"]
    feed = http.get(f"{base}/feed").json()
    assert feed["quiet_now"] == "until" and feed["quiet_ends_at"] == hushed["quiet_ends_at"]
    never = http.put(base, json={"quiet_until": "never"})
    assert never.status_code == 400 and never.json()["detail"] == {"code": "invalid", "field": "quiet_until"}
    assert http.put(base, json={"quiet_days": [7]}).status_code == 400
    calm = http.put(base, json={"quiet_days": [], "quiet_until": None}).json()
    assert calm["quiet_days"] == [] and calm["quiet_until"] is None and calm["quiet_now"] == ""
    found = store_.add_card({"kind": "noticed", "level": "screen", "topic": "date", "news": "Dentist Friday",
                             "created_at": DAY})
    assert http.post(f"{base}/acted", json={"card": found}).json()["booked"] is True
    assert http.post(f"{base}/done", json={"card": found, "done": True}).json()["changed"] is True
    assert http.post(f"{base}/acted", json={"card": found}).json()["booked"] is False  # done already
    # Not now until later today, tomorrow or next week: the card comes back then.
    assert "pulse_rest" in api.ROUTE_CAPABILITIES
    rested = store_.add_card({"kind": "noticed", "level": "screen", "topic": "date", "news": "Dentist Friday",
                              "created_at": DAY})
    put_off = http.post(f"{base}/dismiss", json={"card": rested, "dismissed": True, "until": "tomorrow"}).json()
    assert put_off["changed"] is True and put_off["rest_until"] > time.time()
    [entry] = [card for card in http.get(f"{base}/feed").json()["cards"] if card["id"] == rested]
    assert entry["rest_until"] == put_off["rest_until"]  # the cleared row says when it comes back
    assert http.post(f"{base}/dismiss", json={"card": rested, "dismissed": True, "until": "tomorrow"}).json() == {
        "ok": True, "changed": False, "rest_until": put_off["rest_until"]}
    never = http.post(f"{base}/dismiss", json={"card": rested, "dismissed": True, "until": "never"})
    assert never.status_code == 400 and never.json()["detail"] == {"code": "invalid", "field": "until"}
    assert http.post(f"{base}/dismiss", json={"card": rested, "dismissed": False}).json() == {
        "ok": True, "changed": True, "rest_until": None}
    # A host that never turned Pulse on is answered without a file left behind.
    fresh = home / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(api, "_calls_home", lambda: fresh)
    got = http.get(base)
    assert got.status_code == 200 and got.json()["enabled"] is False
    assert http.get(f"{base}/memory").json()["items"] == []
    assert http.get(f"{base}/followups").json()["followups"] == []
    assert http.get(f"{base}/feed").json()["cards"] == []
    assert http.get(f"{base}/learned").json()["lessons"] == []
    assert http.get(f"{base}/learned").json()["preferences"] == []
    assert http.get(f"{base}/left_out").json()["left_out"] == []
    assert http.post(f"{base}/learned/reset", json={"scope": "everything"}).json()["detail"]["code"] == "pulse_off"
    assert http.post(f"{base}/left_out/bring_up", json={"id": 1}).json()["detail"]["code"] == "pulse_off"
    rated = http.post(f"{base}/rate", json={"card": 1, "rating": "up"})
    assert rated.status_code == 409 and rated.json()["detail"]["code"] == "pulse_off"
    assert http.post(f"{base}/report").json()["detail"]["code"] == "pulse_off"
    assert http.post(f"{base}/backfill", json={"action": "stop"}).json()["detail"]["code"] == "not_running"
    assert http.post(f"{base}/memory/forget", json={"id": 1}).json()["forgotten"] is False
    assert not (fresh / store.DB_NAME).exists()


def test_take_over_names_why_it_could_not(monkeypatch):
    takeover = _load("conduit_push.chat_takeover", ROOT / "chat_takeover.py")
    monkeypatch.setattr(takeover, "modules", lambda: (_ for _ in ()).throw(
        takeover.TakeoverError(501, "This Hermes version can't hand a chat over")))
    assert host.take_over([SID]) == "This Hermes version can't hand a chat over"


def test_take_over_turns_any_failure_into_a_reason(monkeypatch):
    takeover = _load("conduit_push.chat_takeover", ROOT / "chat_takeover.py")
    monkeypatch.setattr(takeover, "modules", lambda: (_ for _ in ()).throw(OSError("disk")))
    assert host.take_over([SID]) == "takeover failed: OSError"


def test_a_takeover_that_hangs_is_abandoned(monkeypatch):
    import threading

    takeover = _load("conduit_push.chat_takeover", ROOT / "chat_takeover.py")
    release, seen = threading.Event(), []

    def stuck(ids, *, abandoned, **_):
        release.wait(5)
        seen.append(abandoned.is_set())
        return {"status": "taken_over"}

    monkeypatch.setattr(takeover, "modules", lambda: (object(), object()))
    monkeypatch.setattr(takeover, "home", lambda: "home")
    monkeypatch.setattr(takeover, "take_over_session", stuck)
    monkeypatch.setattr(host, "TAKEOVER_TIMEOUT_S", 0.05)
    assert host.take_over([SID]) == "takeover timed out"
    release.set()
    for _ in range(100):
        if seen:
            break
        time.sleep(0.01)
    assert seen == [True]  # the worker sees it was abandoned, so it writes nothing


def test_a_chat_refused_again_after_the_takeover_says_so(monkeypatch):
    FakeHost(submit=NOT_OWNED).install(monkeypatch)
    sent = host.send(SID, "x", taking_over=lambda ids: "same_host")
    assert not sent.ok and "again after Pulse took it over (same_host)" in sent.detail


def test_do_it_checks_run_with_follow_ups_off(remembering, monkeypatch):
    store_ = remembering.store
    store_.set("enabled", True)
    store_.set("memory", True)
    store_.set("followups", False)
    now = time.time() + 600
    day = datetime.fromtimestamp(now, pulse.report_zone(store_, store_.path.parent)).strftime("%Y-%m-%d")
    store_.set("memory_today", {"day": day, "tokens": store_.get("memory_daily_tokens")})  # memory spent too
    ran = []
    monkeypatch.setattr(pulse.followup, "run", lambda *a, **kw: ran.append("followups"))

    def acts(*args, budget, done, **kwargs):
        ran.append(("acts", budget))
        done.update(tokens=40, checks=1)

    monkeypatch.setattr(pulse.followup, "check_acts", acts)
    pulse._memory_run(store_.path.parent, SID, now, threading.Event())
    assert ran == []  # nothing booked
    store_.act_on_card(1, now=now - 700, check_at=now - 100)
    pulse._memory_run(store_.path.parent, SID, now, threading.Event())
    assert ran == [("acts", float("inf"))]
    assert store_.get("followup_usage")["checks"] == 1
    # Follow-ups on: Do it still goes first, outside their budget.
    store_.set("followups", True)
    store_.set("followup_today", {"day": day, "tokens": 0})
    ran.clear()
    pulse._memory_run(store_.path.parent, SID, now, threading.Event())
    assert ran == [("acts", float("inf")), "followups"]
    assert store_.get("followup_usage")["checks"] == 2  # each act check counted once
    assert store_.get("followup_today") == {"day": day, "tokens": 0}  # nor spent from follow-ups' day
