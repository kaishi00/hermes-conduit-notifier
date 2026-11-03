"""A cron job delivered to a Bot Chat just shows its report there, unless the bot may act on it (cron_bot_chat)."""
import importlib
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg

cron_bot_chat = importlib.import_module("conduit_push.cron_bot_chat")
CronStore = importlib.import_module("conduit_push.cron_store").CronStore

JOB = {"id": "a1b2c3d4e5f6", "name": "Morning \"news\"", "execution_id": "run-7"}
HEADER = '[Cron job "Morning \'news\'" report: shown in the chat, no reply asked for]'


def _module(monkeypatch, name, **names):
    module = types.ModuleType(name)
    module.__dict__.update(names)
    monkeypatch.setitem(sys.modules, name, module)
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """Hermes' cron delivery, its local-chat note and the profile's Bot Chat, as far as cron_bot_chat uses them."""
    for name in ("cron", "gateway", "tools", "hermes_cli"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    calls = types.SimpleNamespace(turns=[], notes=[], written=[], owned=True, warnings=True, refusals=0,
                                  local_refuses=False, chat="bot-chat-tip")

    def _deliver_to_bot_chat(job, content, profile, *, for_failure=False):
        calls.turns.append((job["id"], content, profile, for_failure))
        return None

    def deliver(job, content, profile, for_failure=False):
        # As Hermes' delivery loop calls it: by its module name.
        return delivery._deliver_to_bot_chat(job, content, profile, for_failure=for_failure)

    delivery = _module(monkeypatch, "cron.scheduler_delivery", _deliver_to_bot_chat=_deliver_to_bot_chat,
                       deliver=deliver, BOT_CHAT_POLICY_PLATFORM="tui",
                       _redact_cron_payload=lambda text, what: text.replace("sk-secret", "[REDACTED]"))

    def deliver_to_local_chat(home, chat_id, text, *, diagnostic=False):
        if calls.local_refuses:
            return f"local chat {chat_id}: delivery not confirmed: storage_unavailable"
        calls.notes.append((str(home), chat_id, text, diagnostic))
        return None

    _module(monkeypatch, "gateway.session_cron", deliver_to_local_chat=deliver_to_local_chat,
            owner_for_home=lambda home: object() if calls.owned else None)
    _module(monkeypatch, "gateway.warning_notifications",
            warning_notifications_enabled=lambda platform, config: calls.warnings)
    _module(monkeypatch, "hermes_cli.config_effective", load_user_config_effective=lambda path: {})
    _module(monkeypatch, "hermes_cli.profiles", get_profile_dir=lambda name: tmp_path / "profiles" / name)
    _module(monkeypatch, "hermes_constants", get_hermes_home=lambda: tmp_path)
    _module(monkeypatch, "tools.bot_live_delivery", _bot_chat_tip=lambda home: calls.chat)

    class SessionDB:
        def __init__(self, db_path, read_only=False):
            self.path = db_path

        def append_completion_note(self, session_id, admission_id, content, display_kind, metadata):
            if calls.refusals:
                calls.refusals -= 1
                raise RuntimeError("active_turn")
            calls.written.append((session_id, admission_id, content, display_kind, metadata))
            return 1

        def close(self):
            pass

    _module(monkeypatch, "hermes_state", SessionDB=SessionDB)
    calls.delivery = delivery
    calls.home = tmp_path
    yield calls
    cron_bot_chat.uninstall()


def test_a_report_shows_in_the_bot_chat_without_a_turn(hermes):
    assert cron_bot_chat.install()
    job = dict(JOB)
    assert hermes.delivery.deliver(job, "Rain at 9. sk-secret", "") is None
    assert hermes.turns == []
    assert hermes.notes == [(str(hermes.home.resolve()), "bot-chat-tip", HEADER + "\n\nRain at 9. [REDACTED]", False)]
    assert job["_bot_chat_delivery_receipts"]["bot-chat:(own)"]["status"] == "shown"


def test_a_job_that_lets_its_bot_act_keeps_hermes_turn(hermes):
    CronStore(hermes.home).set_act(JOB["id"], True)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "furina") is None
    assert hermes.turns == [(JOB["id"], "Rain at 9.", "furina", False)]
    assert hermes.notes == []


def test_another_profiles_bot_chat_gets_the_note_in_its_history_once_its_turn_ends(hermes, monkeypatch):
    hermes.owned = False
    hermes.refusals = 1
    monkeypatch.setattr(cron_bot_chat.time, "sleep", lambda seconds: None)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "furina", for_failure=True) is None
    (chat, key, text, kind, metadata), = hermes.written
    assert (chat, key, kind) == ("bot-chat-tip", f"conduit-cron:{JOB['id']}:run-7", "internal_notification")
    assert text.startswith(HEADER) and metadata["notification_category"] == "diagnostic"
    assert hermes.turns == []


def test_a_bot_chat_its_owner_refuses_gets_the_note_in_its_history(hermes):
    hermes.local_refuses = True
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    assert hermes.notes == [] and len(hermes.written) == 1 and hermes.turns == []


def test_a_hidden_failure_notice_and_a_missing_bot_chat(hermes):
    assert cron_bot_chat.install()
    hermes.warnings = False
    job = dict(JOB)
    assert hermes.delivery.deliver(job, "It failed.", "", for_failure=True) is None
    assert job["_notification_all_targets_suppressed"] and hermes.notes == []
    hermes.warnings, hermes.chat = True, None
    assert "no Bot Chat yet" in hermes.delivery.deliver(dict(JOB), "Rain.", "")
    assert hermes.turns == [] and hermes.notes == []


def test_a_hermes_without_the_note_keeps_its_own_delivery(hermes, monkeypatch):
    monkeypatch.delattr(sys.modules["gateway.session_cron"], "deliver_to_local_chat")
    assert not cron_bot_chat.install()
    hermes.delivery.deliver(dict(JOB), "Rain.", "")
    assert len(hermes.turns) == 1


def test_unloading_puts_hermes_own_back_and_a_reload_wraps_it_once(hermes):
    own = hermes.delivery._deliver_to_bot_chat
    assert cron_bot_chat.install()
    cron_bot_chat._installed = None  # a reload: the replacement is still in place
    assert cron_bot_chat.install()
    assert hermes.delivery._deliver_to_bot_chat.__wrapped__ is own
    cron_bot_chat.uninstall()
    assert hermes.delivery._deliver_to_bot_chat is own


def test_the_cron_tool_says_what_conduit_does_with_a_bot_chat_jobs_reports(hermes):
    import json

    created = json.dumps({"success": True, "job_id": JOB["id"], "deliver": "bot-chat:furina", "message": "created"})
    assert cron_bot_chat.annotate("cronjob_manage", created) is None  # not installed: Hermes' turn stands
    assert cron_bot_chat.install()
    assert json.loads(cron_bot_chat.annotate("cronjob_manage", created))["conduit_bot_chat"] == cron_bot_chat.SHOWS
    CronStore(hermes.home).set_act(JOB["id"], True)
    assert json.loads(cron_bot_chat.annotate("cronjob_manage", created))["conduit_bot_chat"] == cron_bot_chat.ACTS
    local = json.dumps({"success": True, "job_id": JOB["id"], "deliver": "local"})
    assert cron_bot_chat.annotate("cronjob_manage", local) is None
    assert cron_bot_chat.annotate("cronjob_manage", json.dumps({"success": False, "error": "x"})) is None
    assert cron_bot_chat.annotate("session_search", created) is None


def test_a_report_hermes_cant_redact_is_withheld(hermes, monkeypatch):
    assert cron_bot_chat.install()

    def broken(text, what):
        raise RuntimeError("redactor changed")

    monkeypatch.setattr(hermes.delivery, "_redact_cron_payload", broken)
    hermes.delivery.deliver(dict(JOB), "key sk-secret", "")
    monkeypatch.delattr(hermes.delivery, "_redact_cron_payload")
    hermes.delivery.deliver(dict(JOB), "key sk-secret", "")
    texts = [note[2] for note in hermes.notes]
    assert len(texts) == 2 and not any("sk-secret" in text for text in texts)
    assert all(text.endswith(cron_bot_chat.WITHHELD) for text in texts)


def test_a_database_that_wont_open_is_waited_for_too(hermes, monkeypatch):
    hermes.owned = False
    db = sys.modules["hermes_state"].SessionDB
    opens = []

    class Locked(db):
        def __init__(self, db_path, read_only=False):
            opens.append(db_path)
            if len(opens) == 1:
                raise RuntimeError("database is locked")
            super().__init__(db_path, read_only)

    monkeypatch.setattr(sys.modules["hermes_state"], "SessionDB", Locked)
    monkeypatch.setattr(cron_bot_chat.time, "sleep", lambda seconds: None)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain.", "") is None
    assert len(opens) == 2 and len(hermes.written) == 1


def test_a_failure_notice_passed_positionally_is_still_one(hermes):
    assert cron_bot_chat.install()
    hermes.warnings = False
    job = dict(JOB)
    assert hermes.delivery._deliver_to_bot_chat(job, "It failed.", "", True) is None
    assert job["_notification_all_targets_suppressed"] and hermes.notes == []
