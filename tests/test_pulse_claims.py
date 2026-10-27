"""Hermes's claims, kept to judge (designs/pulse-what-comes-next.md):
memory notes what Hermes said it had just done with a tool, and code looks
in the outcome ledger for the call behind it. Nothing is shown yet: `pulse
claims` lists them."""

import json
import sys
import time
import types

import pytest

from test_pulse import (DAY, ROOT, SID, UTC, FakeModel, StateDB, _load, _remember, _row, cli, db, live,  # noqa: F401
                        memory, pulse, remembering)

claims = _load("conduit_push.pulse_claims", ROOT / "pulse_claims.py")
control = _load("conduit_push.pulse_control", ROOT / "pulse_control.py")

ALL = set(claims.ACTIONS)


def claim(action, text, evidence, line):
    return {"action": action, "text": text, "evidence": evidence, "line": line}


def called(pulse_store, at, tool, action="", label="", status="ok", error="", sid="work"):
    """One call in the ledger, as pulse_ledger.record writes it."""
    pulse_store.add_outcome(dict(at=at, session_id=sid, turn_id="t", task_id="", tool_call_id="", tool=tool,
                                 action=action, label=label, args_digest="", status=status, error_type="",
                                 error=error, ids_json="{}", duration_ms=1))


@pytest.fixture
def judging(remembering):
    """Memory reading chats, with the ledger recording since before them."""
    remembering.store.set(claims.FROM, DAY - 3600)
    return remembering


def test_memory_asks_for_what_hermes_said_it_did():
    assert '"claims": [...]' in memory.INSTRUCTIONS and '"claims": []' in memory.INSTRUCTIONS
    for action in claims.ACTIONS:
        assert f'"{action}"' in memory.INSTRUCTIONS


def test_a_claim_is_in_hermess_own_words():
    chunk = [_row(1, "user", "Remind me to call the dentist at 5pm, set a reminder for 5pm please", DAY + 60),
             _row(2, "assistant", "Done! I've set a reminder for 5pm to call the dentist.", DAY + 120)]
    model = FakeModel({"items": [], "claims": [
        claim("reminder", "Hermes set a reminder for 5pm to call the dentist.", "I've set a reminder for 5pm", 2),
        claim("event", "x", "Remind me to call the dentist", 1),  # the user's words
        claim("event", "x", "I've added it to your calendar", 2),  # never said
        claim("dance", "x", "I've set a reminder", 2),  # no such action
        claim("reminder", "Again.", "set a reminder for 5pm to call", 2),  # that line and action again
        claim("job", "Hermes scheduled it.", "set a reminder for 5pm", 1),  # the user said it too: Hermes's line
    ]})
    found = []
    items, closed, tokens = memory.extract(model, chunk, UTC, claims=found)
    assert (items, closed, tokens) == ([], [], 100)
    assert found == [
        {"action": "reminder", "text": "Hermes set a reminder for 5pm to call the dentist.",
         "evidence": "I've set a reminder for 5pm", "message_id": 2, "said_at": DAY + 120},
        {"action": "job", "text": "Hermes scheduled it.", "evidence": "set a reminder for 5pm", "message_id": 2,
         "said_at": DAY + 120}]
    # At most MAX_CLAIMS are read, and a reply without claims has none.
    many = [claim("reminder", "x", "nothing like it", 2)] * claims.MAX_CLAIMS + [
        claim("reminder", "Hermes set it.", "I've set a reminder", 2)]
    found = []
    memory.extract(FakeModel({"items": [], "claims": many}), chunk, UTC, claims=found)
    assert found == []
    memory.extract(FakeModel({"items": [], "claims": "none"}), chunk, UTC, claims=found)
    assert found == []


@pytest.mark.parametrize("tool, action, backs", [
    ("conduit_calendar", "add_reminder", {"reminder"}),
    ("conduit_calendar", "update_event", {"event"}),
    ("conduit_calendar", "reminders", set()),  # a read
    ("conduit_phone", "set_alarm", {"reminder"}),
    ("conduit_phone", "status", set()),
    ("conduit_call_watch", "set", {"reminder"}),
    ("conduit_call_watch", "list", set()),
    ("cronjob_manage", "create", {"reminder", "job"}),
    ("cronjob_manage", "list", set()),
    ("cronjob", "update", {"reminder", "job"}),
    ("schedule_cronjob", "", {"reminder", "job"}),  # an older Hermes's
    ("list_cronjobs", "", set()),
    ("mcp_gmail_send_message", "", {"message"}),
    ("mcp_gmail_search_messages", "", set()),
    ("mcp_slack_send_to_target", "", {"message"}),
    ("mcp_google_calendar_create_event", "", {"event"}),
    ("createEvent", "", {"event"}),
    ("send_message", "send", {"message"}),
    ("send_message", "list", set()),
    ("delete_reminder", "", set()),  # undoing it isn't doing it
    ("mcp_google_calendar_delete_event", "", set()),
    ("cancelScheduledMessage", "", set()),
    ("terminal", "", ALL),  # it runs other tools or code: it could have
    ("delegate_task", "", ALL),
    ("execute_code", "", ALL),
    ("web_search", "", set()),
    ("conduit_location", "", set()),
])
def test_what_could_have_done_what_hermes_said(tool, action, backs):
    assert {kind for kind in claims.ACTIONS if claims.does({"tool": tool, "action": action}, kind)} == backs


def test_a_claim_with_no_call_in_its_turn_is_kept_to_judge(judging, db):
    store_ = judging.store
    db.message("user", "Remind me to call the dentist at 5pm", sid="work", at=DAY + 60)
    dentist_line = db.message("assistant", "Done! I've set a reminder for 5pm to call the dentist.", sid="work",
                              at=DAY + 120)
    db.message("user", "And remind me about the vet tomorrow", sid="work", at=DAY + 300)
    db.message("assistant", "Done, I've added a reminder for the vet.", sid="work", at=DAY + 360)
    called(store_, DAY + 30, "conduit_calendar", "add_reminder", "Gym")  # the turn before
    called(store_, DAY + 90, "web_search", label="dentist hours")  # its turn's, but no reminder
    called(store_, DAY + 100, "conduit_calendar", "add_reminder", "Dentist", sid="elsewhere")  # another chat's
    called(store_, DAY + 330, "conduit_calendar", "add_reminder", "Vet")  # the next turn's
    model = FakeModel({"items": [], "claims": [
        claim("reminder", "Hermes set a reminder for 5pm to call the dentist.", "I've set a reminder for 5pm", 2),
        claim("reminder", "Hermes set a reminder for the vet.", "I've added a reminder for the vet", 4)]})
    assert _remember(judging, model, DAY + 360 + memory.IDLE_S)["chunks"] == 1
    kept = {kept["evidence"]: kept for kept in store_.claims()}
    dentist, vet = kept["I've set a reminder for 5pm"], kept["I've added a reminder for the vet"]
    assert (dentist["chat_id"], dentist["message_id"], dentist["action"], dentist["said_at"], dentist["turn_from"],
            dentist["turn_to"], dentist["backed_by"]) == ("work", dentist_line, "reminder", DAY + 120, DAY + 60,
                                                          DAY + 300, "")
    assert json.loads(dentist["calls_json"]) == ["web_search 'dentist hours' → ok"]
    assert (vet["backed_by"], vet["turn_from"], vet["turn_to"]) == (
        "conduit_calendar add_reminder 'Vet' → ok", DAY + 300, None)
    assert [kept["id"] for kept in store_.claims(unbacked=True)] == [dentist["id"]]
    assert store_.item_counts() == {}  # nothing remembered from them


def test_a_call_that_failed_backs_nothing(judging, db):
    store_ = judging.store
    db.message("user", "Add the flight to my calendar", sid="work", at=DAY + 60)
    db.message("assistant", "I've added your flight to the calendar.", sid="work", at=DAY + 120)
    called(store_, DAY + 90, "conduit_calendar", "add_event", "Flight", status="error", error="calendar is read only")
    model = FakeModel({"items": [], "claims": [
        claim("event", "Hermes added the flight to the calendar.", "I've added your flight to the calendar", 2)]})
    _remember(judging, model, DAY + 120 + memory.IDLE_S)
    [kept] = store_.claims(unbacked=True)
    assert json.loads(kept["calls_json"]) == ["conduit_calendar add_event 'Flight' → error: calendar is read only"]


def test_a_claim_said_after_its_turn_was_read_still_finds_the_users_message(judging, db):
    store_ = judging.store
    db.message("user", "Every Monday, send me the week's agenda", sid="work", at=DAY + 60)
    db.message("assistant", "Working on it.", sid="work", at=DAY + 70)
    model = FakeModel({"items": []}, {"items": [], "claims": [
        claim("job", "Hermes scheduled the Monday agenda.", "I've scheduled the Monday agenda", 1)]})
    _remember(judging, model, DAY + 70 + memory.IDLE_S)
    later = DAY + 70 + memory.IDLE_S + 60
    called(store_, DAY + 65, "cronjob_manage", "create", "Monday agenda")
    db.message("assistant", "Done: I've scheduled the Monday agenda for 8am.", sid="work", at=later)
    _remember(judging, model, later + memory.IDLE_S)
    [kept] = store_.claims()
    assert "I've scheduled the Monday agenda" in model.shown[1] and "Every Monday" not in model.shown[1]
    assert (kept["turn_from"], kept["backed_by"]) == (DAY + 60, "cronjob_manage create 'Monday agenda' → ok")


@pytest.mark.parametrize("why", ["from before the ledger recorded", "pulse was never on since"])
def test_a_turn_the_ledger_may_have_missed_is_not_judged(judging, db, why):
    store_ = judging.store
    if why == "pulse was never on since":
        claims.stop(store_)
    else:
        store_.set(claims.FROM, DAY + 60 - 10)  # the ledger recorded only in every process 30 seconds on
    db.message("user", "Remind me at 5pm", sid="work", at=DAY + 60)
    db.message("assistant", "I've set a reminder for 5pm.", sid="work", at=DAY + 120)
    model = FakeModel({"items": [], "claims": [claim("reminder", "Hermes set it.", "I've set a reminder", 2)]})
    assert _remember(judging, model, DAY + 120 + memory.IDLE_S)["chunks"] == 1
    assert store_.claims() == []


def test_another_profiles_chats_and_older_ones_are_not_judged(judging, db, tmp_path, request):
    store_ = judging.store
    bot_home = tmp_path / "profiles" / "nahida"
    bot_home.mkdir(parents=True)
    bot = StateDB(bot_home, request.node.callspec.params["db"])
    bot.session("b1", title="Bot Chat", pinned=0)
    bot.message("user", "Remind me at 5pm", sid="b1", at=DAY + 60)
    bot.message("assistant", "I've set a reminder for 5pm.", sid="b1", at=DAY + 70)
    said = FakeModel({"items": [], "claims": [claim("reminder", "Hermes set it.", "I've set a reminder", 2)]})
    _remember(judging, said, DAY + 70 + memory.IDLE_S, [("", tmp_path, SID), ("nahida", bot_home, "")])
    assert len(said.shown) == 1 and store_.claims() == []  # its calls aren't in this profile's ledger
    # A backfill's turns are older than the ledger.
    db.message("user", "Remind me at 5pm", sid="work", at=DAY - 5 * 86400)
    db.message("assistant", "I've set a reminder for 5pm.", sid="work", at=DAY - 5 * 86400 + 10)
    now = DAY + 70 + memory.IDLE_S + 600
    store_.set(claims.FROM, DAY - 30 * 86400)
    store_.set("memory_backfill", {"from": now - 30 * 86400, "to": DAY, "active": True, "done": False})
    said = FakeModel({"items": [], "claims": [claim("reminder", "Hermes set it.", "I've set a reminder", 2)]})
    assert _remember(judging, said, now)["chunks"] == 1 and len(said.shown) == 1
    assert store_.claims() == []


def test_the_loop_judges_from_when_pulse_is_on_and_off_starts_over(live, db, monkeypatch):
    db.session()
    live.store.set("enabled", True)
    now = time.time()
    pulse.tick(now)
    assert live.store.get(claims.FROM) == now and claims.judged_from(live.store) == now + 30.0
    pulse.tick(now + 60)
    assert live.store.get(claims.FROM) == now  # from when it started
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="off", value="")) == 0
    assert live.store.get(claims.FROM) is None and claims.judged_from(live.store) is None
    live.store.set("enabled", True)
    pulse.tick(now + 120)
    assert live.store.get(claims.FROM) == now + 120
    control.update(live.store, {"enabled": False})
    assert live.store.get(claims.FROM) is None
    # A setting that isn't a time starts it over.
    live.store.set("enabled", True)
    live.store.set(claims.FROM, True)
    pulse.tick(now + 180)
    assert live.store.get(claims.FROM) == now + 180


def test_claims_are_kept_once_and_forgotten_after_a_month(judging, db):
    store_ = judging.store
    db.message("user", "Remind me at 5pm", sid="work", at=DAY + 60)
    db.message("assistant", "I've set a reminder for 5pm.", sid="work", at=DAY + 120)
    model = FakeModel({"items": [], "claims": [claim("reminder", "Hermes set it.", "I've set a reminder", 2)]})
    now = DAY + 120 + memory.IDLE_S
    _remember(judging, model, now)
    [kept] = store_.claims()
    # A second process that read the same chunk keeps nothing twice.
    assert store_.advance("", "work", message_id=2, at=DAY + 120, after_id=0, claims=[{
        **{key: kept[key] for key in ("message_id", "action", "text", "evidence", "said_at", "turn_from", "turn_to",
                                      "backed_by", "calls_json", "kept_at")}}]) is None
    assert len(store_.claims()) == 1
    _remember(judging, model, now + claims.KEEP_S - 1)
    assert len(store_.claims()) == 1
    _remember(judging, model, now + claims.KEEP_S + 1)
    assert store_.claims() == []


def test_the_loop_forgets_claims_after_a_month_with_memory_off(live, db):
    db.session()
    live.store.set("enabled", True)
    now = time.time()
    assert live.store.advance("", "work", message_id=2, at=now, claims=[{
        "message_id": 2, "action": "reminder", "text": "Hermes set it.", "evidence": "I've set a reminder",
        "said_at": now, "turn_from": now - 60, "turn_to": None, "backed_by": "", "calls_json": "[]",
        "kept_at": now}]) is not None
    pulse.tick(now + claims.KEEP_S - 1)
    assert len(live.store.claims()) == 1
    pulse.tick(now + claims.KEEP_S + 1)
    assert live.store.claims() == []


def test_pulse_claims_lists_what_no_call_backed(judging, db, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="": cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="claims",
                                                              value=value))
    judging.store.set("time_zone", "UTC")
    assert run() == 0 and "No claims without a call behind them" in capsys.readouterr().out
    db.message("user", "Remind me to call the dentist at 5pm", sid="work", at=DAY + 60)
    db.message("assistant", "Done! I've set a reminder for 5pm to call the dentist.", sid="work", at=DAY + 120)
    db.message("user", "And remind me about the vet tomorrow", sid="work", at=DAY + 300)
    db.message("assistant", "Done, I've added a reminder for the vet.", sid="work", at=DAY + 360)
    called(judging.store, DAY + 90, "web_search", label="dentist hours")
    called(judging.store, DAY + 330, "conduit_calendar", "add_reminder", "Vet")
    _remember(judging, FakeModel({"items": [], "claims": [
        claim("reminder", "Hermes set a reminder for 5pm to call the dentist.", "I've set a reminder for 5pm", 2),
        claim("reminder", "Hermes set a reminder for the vet.", "I've added a reminder for the vet", 4)]}),
        DAY + 360 + memory.IDLE_S)
    assert run() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "What Hermes said it did with no call behind it in that turn (kept to judge; Conduit shows none " \
                     "of it yet):"
    assert out[1].endswith(' Tue 20 Oct 00:02 in "Work" (reminder): Hermes set a reminder for 5pm to call the '
                           'dentist.')
    assert out[2:] == ["  Said: \"I've set a reminder for 5pm\"", "  That turn's calls: web_search 'dentist hours' → ok"]
    assert run("all") == 0
    out = capsys.readouterr().out
    assert "Backed by: conduit_calendar add_reminder 'Vet' → ok" in out and "Said: \"I've set a reminder" in out
    assert run("everything") == 2
