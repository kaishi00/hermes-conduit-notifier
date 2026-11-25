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


def _job_reader(monkeypatch, deliver="bot-chat:"):
    """Hermes' cron job reader, as the quiet rule and the note push read it."""
    jobs = types.ModuleType("cron.jobs")
    jobs.get_job = lambda job_id: {"id": job_id, "deliver": deliver}
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs)


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


def test_the_note_sends_the_one_push_for_the_run_and_it_opens_the_bot_chat(hermes, monkeypatch):
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    _job_reader(monkeypatch)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9. sk-secret", "") is None
    (event,) = sent
    events = importlib.import_module("conduit_push.events")
    titles = importlib.import_module("conduit_push.session_titles")
    load = titles._profile_of(titles._home()) or "default"
    assert event["type"] == "response.ready"
    assert event["session_id"] == "bot-chat-tip"
    assert event["title"] == 'Cron job "Morning \'news\'"'
    assert event["body"] == "Rain at 9. [REDACTED]"
    assert event["slot"] == events.slot_token(f"cron:{load}:a1b2c3d4e5f6")
    assert event["title_label"] == "response.ready"


def test_a_job_whose_notifications_are_off_sends_no_push(hermes, monkeypatch):
    cron_runs = importlib.import_module("conduit_push.cron_runs")
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    _job_reader(monkeypatch)
    # The mute is read in the push, through cron_runs' home binding.
    monkeypatch.setattr(cron_runs, "get_hermes_home", lambda: hermes.home)
    assert cron_bot_chat.install()
    CronStore(hermes.home).set_notify(JOB["id"], False)
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    assert hermes.notes != []  # the note still shows: the toggle holds pushes, not the delivery
    assert sent == []


def test_a_failure_notice_pushes_as_a_failure(hermes, monkeypatch):
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    _job_reader(monkeypatch)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "It failed.", "", for_failure=True) is None
    (event,) = sent
    assert event["type"] == "turn.failed"
    assert event["title"] == 'Cron job "Morning \'news\'" failed'
    assert event["title_label"] == "turn.failed"


def test_a_suppressed_failure_notice_and_a_note_that_never_lands_send_no_push(hermes, monkeypatch):
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    assert cron_bot_chat.install()
    hermes.warnings = False
    assert hermes.delivery.deliver(dict(JOB), "It failed.", "", for_failure=True) is None
    assert sent == []
    hermes.warnings, hermes.chat = True, None
    assert "no Bot Chat yet" in hermes.delivery.deliver(dict(JOB), "Rain.", "")
    assert sent == []


def _sent(monkeypatch):
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    return sent


def _run_chats(monkeypatch, **chats):
    """The entry module's record of each job's newest run chat (a run's own turn fills it)."""
    monkeypatch.setattr(sys.modules["conduit_push"], "_job_runs", dict(chats), raising=False)
    # And each run's own, by its task id: JOB's execution is run-7.
    monkeypatch.setattr(sys.modules["conduit_push"], "_run_chats",
                        {f"cron:{job}:run-7": chat for job, chat in chats.items()}, raising=False)


def test_a_report_with_no_bot_chat_to_land_in_pushes_for_the_runs_own_chat(hermes, monkeypatch):
    # Issue 1052: the run chat's push stood down for the note's, so a note
    # that never lands sends the run's one push itself, opening the run's chat.
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)
    _run_chats(monkeypatch, a1b2c3d4e5f6="local-run-1")
    assert cron_bot_chat.install()
    hermes.chat = None
    assert "no Bot Chat yet" in hermes.delivery.deliver(dict(JOB), "Rain at 9. sk-secret", "")
    (event,) = sent
    assert (event["type"], event["session_id"]) == ("response.ready", "local-run-1")
    assert event["title"] == 'Cron job "Morning \'news\'"' and event["body"] == "Rain at 9. [REDACTED]"


def test_a_note_its_owner_refuses_past_the_wait_pushes_for_the_run_and_a_failure_says_so(hermes, monkeypatch):
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)
    _run_chats(monkeypatch, a1b2c3d4e5f6="local-run-1")
    monkeypatch.setattr(cron_bot_chat, "NOTE_WAIT_S", 0.0)
    hermes.owned, hermes.refusals = False, 99
    assert cron_bot_chat.install()
    assert "not written" in hermes.delivery.deliver(dict(JOB), "It failed.", "", for_failure=True)
    (event,) = sent
    assert (event["type"], event["session_id"]) == ("turn.failed", "local-run-1")
    # The same run delivered again (a second target, a retry) is the same push.
    assert "not written" in hermes.delivery.deliver(dict(JOB), "It failed.", "", for_failure=True)
    assert [again["event_id"] for again in sent] == [event["event_id"]] * 2


def test_an_unlanded_report_opens_the_bot_chat_when_the_runs_chat_isnt_known_here(hermes, monkeypatch):
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)
    monkeypatch.setattr(cron_bot_chat, "NOTE_WAIT_S", 0.0)
    hermes.owned, hermes.refusals = False, 99
    assert cron_bot_chat.install()
    assert "not written" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "")
    assert [event["session_id"] for event in sent] == ["bot-chat-tip"]


def test_an_unlanded_report_opens_its_own_runs_chat_not_a_newer_runs(hermes, monkeypatch):
    # From #1056's reviews: the delivery can wait a minute, and another run of
    # the job may end meanwhile. The push opens the chat of the run it reports.
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)
    monkeypatch.setattr(sys.modules["conduit_push"], "_job_runs", {JOB["id"]: "local-run-newer"}, raising=False)
    monkeypatch.setattr(sys.modules["conduit_push"], "_run_chats",
                        {f"cron:{JOB['id']}:run-7": "local-run-1", f"cron:{JOB['id']}:run-8": "local-run-newer"},
                        raising=False)
    assert cron_bot_chat.install()
    hermes.chat = None
    assert "no Bot Chat yet" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "")
    # A run this process didn't see end has no chat of its own here: nothing to open without a Bot Chat.
    assert "no Bot Chat yet" in hermes.delivery.deliver(dict(JOB, execution_id="run-9"), "Rain at 9.", "")
    # A delivery that names no execution takes the job's newest run.
    job = {key: value for key, value in JOB.items() if key != "execution_id"}
    assert "no Bot Chat yet" in hermes.delivery.deliver(job, "Rain at 9.", "")
    assert [event["session_id"] for event in sent] == ["local-run-1", "local-run-newer"]


def test_a_local_note_that_raises_still_goes_to_the_history(hermes, monkeypatch):
    # From #1056's reviews: Hermes' own write can raise instead of answering
    # with its error; the history write and the push follow all the same.
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)

    def raises(home, chat_id, text, *, diagnostic=False):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sys.modules["gateway.session_cron"], "deliver_to_local_chat", raises)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    assert len(hermes.written) == 1 and [event["session_id"] for event in sent] == ["bot-chat-tip"]


def test_an_unlanded_report_for_another_profile_or_a_muted_job_sends_nothing(hermes, monkeypatch):
    cron_runs = importlib.import_module("conduit_push.cron_runs")
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)
    _run_chats(monkeypatch, a1b2c3d4e5f6="local-run-1")
    monkeypatch.setattr(cron_runs, "get_hermes_home", lambda: hermes.home)
    monkeypatch.setattr(cron_bot_chat, "NOTE_WAIT_S", 0.0)
    hermes.owned, hermes.refusals = False, 99
    assert cron_bot_chat.install()
    # Another profile's Bot Chat: the run chat's push never stood down.
    assert "not written" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "furina")
    CronStore(hermes.home).set_notify(JOB["id"], False)
    assert "not written" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "")
    assert sent == []


@pytest.mark.parametrize("status, pushes", [(None, 1), ("failed", 1), ("queued", 0), ("claimed", 0), ("ambiguous", 0)])
def test_a_turn_hermes_didnt_admit_for_a_job_the_bot_acts_on_pushes_for_the_run(hermes, monkeypatch, status, pushes):
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch, deliver="bot-chat:furina")
    _run_chats(monkeypatch, a1b2c3d4e5f6="local-run-1")

    def refused(job, content, profile, *, for_failure=False):
        if status:
            job.setdefault("_bot_chat_delivery_receipts", {})[f"bot-chat:{profile}"] = {"status": status}
        return f"bot-chat:{profile} unverified"

    monkeypatch.setattr(hermes.delivery, "_deliver_to_bot_chat", refused)
    CronStore(hermes.home).set_act(JOB["id"], True)
    assert cron_bot_chat.install()
    assert "unverified" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "furina")
    assert [event["session_id"] for event in sent] == ["local-run-1"] * pushes


def test_the_notes_slot_is_spelled_as_the_run_chats(hermes, monkeypatch):
    # Issue 1052: one spelling of the load's profile for the job's slot, the
    # entry module's, so the note's push and a run chat's replace each other.
    sent = _sent(monkeypatch)
    _job_reader(monkeypatch)
    monkeypatch.setattr(sys.modules["conduit_push"], "_profile", "Atlas", raising=False)
    events = importlib.import_module("conduit_push.events")
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    (event,) = sent
    assert event["slot"] == events.slot_token(f"cron:Atlas:{JOB['id']}") and event["profile"] == "Atlas"


def test_another_profiles_bot_chat_keeps_the_run_chats_push_instead(hermes, monkeypatch):
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "furina") is None
    assert hermes.notes != []  # the note still shows
    assert sent == []  # but this load can't seal a push for another profile's pairing


def test_a_hermes_without_the_job_reader_keeps_the_run_chats_push_alone(hermes, monkeypatch):
    # The straddle: the note path without cron.jobs.get_job. The push rides
    # the same delivery read the quiet rule makes, so it stands down and the
    # run chat's push is the one notification, as before this.
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    monkeypatch.setitem(sys.modules, "cron.jobs", None)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    assert hermes.notes != []  # the note still lands
    assert sent == []  # but the push stands down with the quiet rule's read


TASK = f"cron:{JOB['id']}:{JOB['execution_id']}"


def _quiet_then_the_job_goes(hermes, monkeypatch, *, failure=False, raises=False):
    """The run's turn ends while the job can be read (the run chat's push stands down), and the
    job is deleted, or its store unreadable, before the delivery's push (notifier issue 1059)."""
    cron_runs = importlib.import_module("conduit_push.cron_runs")
    monkeypatch.setattr(cron_runs, "get_hermes_home", lambda: hermes.home)
    monkeypatch.setattr(cron_runs, "_stood_down", {})
    _job_reader(monkeypatch)
    assert cron_runs.quiet("run-chat", TASK, "Rain at 9.", delivered=True, failure=failure) is True

    def gone(job_id):
        if raises:
            raise OSError("jobs.json is being rewritten")
        return None

    sys.modules["cron.jobs"].get_job = gone
    return cron_runs


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_a_job_deleted_after_its_runs_push_stood_down_still_notifies_once(hermes, monkeypatch, failure, raises):
    sent = _sent(monkeypatch)
    _quiet_then_the_job_goes(hermes, monkeypatch, failure=failure, raises=raises)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "", for_failure=failure) is None
    assert hermes.notes != [] and [event["session_id"] for event in sent] == ["bot-chat-tip"]


def test_a_job_deleted_after_its_runs_push_stood_down_notifies_for_a_report_that_didnt_land(hermes, monkeypatch):
    sent = _sent(monkeypatch)
    _run_chats(monkeypatch, a1b2c3d4e5f6="local-run-1")
    _quiet_then_the_job_goes(hermes, monkeypatch)
    monkeypatch.setattr(cron_bot_chat, "NOTE_WAIT_S", 0.0)
    hermes.owned, hermes.refusals = False, 99
    assert cron_bot_chat.install()
    assert "not written" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "")
    assert [event["session_id"] for event in sent] == ["local-run-1"]


def test_a_job_deleted_after_its_runs_push_stood_down_notifies_when_hermes_admits_no_turn(hermes, monkeypatch):
    sent = _sent(monkeypatch)
    _run_chats(monkeypatch, a1b2c3d4e5f6="local-run-1")
    CronStore(hermes.home).set_act(JOB["id"], True)
    _quiet_then_the_job_goes(hermes, monkeypatch)
    monkeypatch.setattr(hermes.delivery, "_deliver_to_bot_chat",
                        lambda job, content, profile, *, for_failure=False: "bot-chat: unverified")
    assert cron_bot_chat.install()
    assert "unverified" in hermes.delivery.deliver(dict(JOB), "Rain at 9.", "")
    assert [event["session_id"] for event in sent] == ["local-run-1"]


def test_a_run_whose_push_never_stood_down_keeps_it_alone_after_an_earlier_run_that_did(hermes, monkeypatch):
    # The next run's turn ends with the job unreadable: its own chat's push goes out, so the
    # delivery's stays down, whatever the run before recorded.
    sent = _sent(monkeypatch)
    cron_runs = _quiet_then_the_job_goes(hermes, monkeypatch)
    later = f"cron:{JOB['id']}:run-8"
    assert cron_runs.quiet("run-chat-2", later, "Rain at 9.", delivered=True) is False
    assert not cron_runs.stood_down(JOB["id"], "run-8") and not cron_runs.stood_down(JOB["id"], None)
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver({**JOB, "execution_id": "run-8"}, "Rain at 9.", "") is None
    assert sent == []


def test_what_is_kept_of_runs_that_stood_down_is_bounded_and_keeps_the_newest(monkeypatch):
    # notifier#1070: past _STOOD_DOWN_KEPT keys the oldest go, and a key noted again is newest.
    cron_runs = importlib.import_module("conduit_push.cron_runs")
    monkeypatch.setattr(cron_runs, "_stood_down", {})
    monkeypatch.setattr(cron_runs, "_STOOD_DOWN_KEPT", 6)
    for run in range(5):
        cron_runs._note_stood_down(f"job-{run}", f"cron:job-{run}:run-{run}")
    assert len(cron_runs._stood_down) == 6
    assert not cron_runs.stood_down("job-1", "run-1") and not cron_runs.stood_down("job-1", None)
    assert cron_runs.stood_down("job-2", None) and cron_runs.stood_down("job-4", "run-4")
    cron_runs._note_stood_down("job-2", "cron:job-2:run-2")
    cron_runs._note_stood_down("job-5", "cron:job-5:run-5")
    assert cron_runs.stood_down("job-2", "run-2"), "noted again: newest, kept"
    assert not cron_runs.stood_down("job-3", "run-3")


def test_a_muted_bot_chat_holds_the_notes_push(hermes, monkeypatch):
    ChatMute = importlib.import_module("conduit_push.chat_mute")
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    _job_reader(monkeypatch)
    assert cron_bot_chat.install()
    ChatMute.ChatMuteStore(hermes.home).set_mute(["bot-chat-tip"], None)
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    assert hermes.notes != []  # the note still shows in the muted chat
    assert sent == []  # but the push stays quiet, as any reply's does


def test_a_mute_on_a_pre_compaction_session_holds_the_notes_push_too(hermes, monkeypatch):
    ChatMute = importlib.import_module("conduit_push.chat_mute")
    sent = []
    client = importlib.import_module("conduit_push.client")
    monkeypatch.setattr(client, "enqueue", lambda event: sent.append(event) or True)
    _job_reader(monkeypatch)
    # The ancestor walk lives in the entry module (a copy reads it from there);
    # the test hands the chat one ancestor, the way Hermes' store would.
    monkeypatch.setattr(sys.modules["conduit_push"], "_compaction_ancestors",
                        lambda chat_id: ["old-session"], raising=False)
    assert cron_bot_chat.install()
    ChatMute.ChatMuteStore(hermes.home).set_mute(["old-session"], None)
    assert hermes.delivery.deliver(dict(JOB), "Rain at 9.", "") is None
    assert hermes.notes != []
    assert sent == []


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


def test_a_reports_whole_line_media_tag_shows_as_the_reference_its_viewers_render(hermes):
    # The note is a user row: a raw MEDIA tag reads as the host's path there,
    # so the report's picture comes through as the reference the chat renders
    # (issue 581).
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "MEDIA:/cards/reminder-2026-11-05-1017.png\nQuiz: kapott?", "") is None
    (_home, _chat, text, _diagnostic), = hermes.notes
    assert text == HEADER + "\n\n@image:/cards/reminder-2026-11-05-1017.png\nQuiz: kapott?"


def test_the_history_note_rewrites_media_tags_the_same_way(hermes):
    hermes.owned = False
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), "MEDIA:/cards/quiz.png", "") is None
    (_chat, _key, text, _kind, _metadata), = hermes.written
    assert text.endswith("@image:/cards/quiz.png") and "MEDIA:" not in text


def test_other_files_are_chips_and_paths_are_quoted_or_left_as_written(hermes):
    assert cron_bot_chat.install()
    report = ("MEDIA:/var/bericht.pdf\n"
              'MEDIA:"/var/mit leer.png"\n'
              "MEDIA:C:\\Users\\me\\out.JPG\n"
              'MEDIA:"/var/kaputt".png\n'
              "MEDIA:/a/x.png und MEDIA:/b/y.png\n"
              "Siehe MEDIA:/x.png hier.\n"
              "```\nMEDIA:/example/in-code.png\n```")
    assert hermes.delivery.deliver(dict(JOB), report, "") is None
    (_home, _chat, text, _diagnostic), = hermes.notes
    assert "@file:/var/bericht.pdf" in text
    assert "@image:`/var/mit leer.png`" in text
    assert "@image:C:\\Users\\me\\out.JPG" in text
    assert 'MEDIA:"/var/kaputt".png' in text  # an unquotable path stays as written
    assert "MEDIA:/a/x.png und MEDIA:/b/y.png" in text  # one line, two tags: as written
    assert "Siehe MEDIA:/x.png hier." in text  # a tag inside prose stays prose
    assert "```\nMEDIA:/example/in-code.png\n```" in text  # an example in code stays code


def test_a_withheld_report_has_no_references_to_make():
    assert cron_bot_chat.reference_tags(cron_bot_chat.WITHHELD) == cron_bot_chat.WITHHELD


def test_the_image_references_keep_artifacts_index_extensions():
    import importlib.util

    spec = importlib.util.spec_from_file_location("artifacts_index_581", ROOT / "artifacts_index.py")
    artifacts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifacts)
    assert cron_bot_chat._IMAGE_REFERENCE == frozenset(artifacts._IMAGE)


def test_the_artifacts_list_knows_the_note_by_its_header():
    import importlib.util

    spec = importlib.util.spec_from_file_location("artifacts_index_581_header", ROOT / "artifacts_index.py")
    artifacts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifacts)
    assert cron_bot_chat.HEADER.format(name="x").startswith(artifacts.CRON_NOTE_PREFIX)
    # Names as note_text writes them: it turns a double quote into a single one.
    for name in ("x", "Morning 'news'", ""):
        assert artifacts._CRON_NOTE_HEADER_RE.match(cron_bot_chat.HEADER.format(name=name) + "\n\nreport")


def test_a_path_with_brackets_is_quoted_so_its_reference_holds(hermes):
    assert cron_bot_chat.install()
    assert hermes.delivery.deliver(dict(JOB), 'MEDIA:"/tmp/old(1).png"', "") is None
    (_home, _chat, text, _diagnostic), = hermes.notes
    assert text.endswith("@image:`/tmp/old(1).png`")


def test_markers_directives_and_quote_marks_keep_the_reference_whole():
    report = ("- MEDIA:/cards/a.png\n"
              "  > MEDIA:/cards/b.pdf\n"
              "1. MEDIA:/tmp/report.pdf[[as_document]]\n"
              "MEDIA:\"/tmp/Bob's photo.png\"\n"
              "MEDIA:/tmp/old<1>.png")
    assert cron_bot_chat.reference_tags(report).splitlines() == [
        "@image:/cards/a.png",
        "  > MEDIA:/cards/b.pdf",  # a quoted line is an example
        "@file:/tmp/report.pdf",
        "@image:`/tmp/Bob's photo.png`",
        "@image:`/tmp/old<1>.png`",
    ]
