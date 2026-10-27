"""How a Pulse push goes (pulse_push): its level and whether it's private.
designs/pulse-where-you-are.md, "Push levels" and "Surprises show no text on
the Lock Screen"."""

import json
import sqlite3
import sys
import time
import types
from datetime import datetime, timezone

import pytest

from test_pulse import SID, ROOT, _load, cli, control, plugin, pulse
from test_pulse_followup import check_now, followup, live, llm_saying, promise  # noqa: F401
from test_pulse_dates import fact

push = _load("conduit_push.pulse_push", ROOT / "pulse_push.py")
hours = _load("conduit_push.pulse_hours", ROOT / "pulse_hours.py")
UTC = pulse.report.zone_for("UTC")
# Tuesday 27 October 2026, 15:00 UTC.
NOW = datetime(2026, 10, 27, 15, tzinfo=timezone.utc).timestamp()


# Private words ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Pick up Sam's gift on the way home", "Her present is hidden in the garage", "Sam’s present came",
    "Plan the surprise party", "Sam's birthday is on Friday", "Ana's bday", "Our anniversary dinner",
    "Sam turns 40 on Wednesday", "Dentist at 3", "Call the doctor's office", "Refill the prescription",
    "Therapy moved to Thursday", "The blood test came back", "The bank called about the mortgage",
    "Pay the bills before Friday", "Your credit card is due", "Dan owes you £20", "File your taxes",
])
def test_words_about_a_gift_a_surprise_someones_day_health_or_money_are_private(text):
    assert push.private_words(text)


@pytest.mark.parametrize("text", [
    "Dan will present the roadmap", "The presentation is at 4", "Pick up batteries at Costco",
    "The Bills play tonight", "Book a taxi to the airport", "The deploy finished and everything is green",
    "Your Netflix trial ends Friday", "", None, 42,
])
def test_other_words_are_not(text):
    assert not push.private_words(text)


def test_a_birthday_or_anniversary_and_a_picked_persons_birthday_are_private(live):
    wedding = fact(live, "Our wedding anniversary is 28 October.", "we married on 28 October", "--10-28",
                   "anniversary")
    moved = fact(live, "The user moved into the flat on 1 May.", "we moved in on 1 May", "--05-01", "other")
    assert push.private_item(live.store.item(wedding)) and not push.private_item(live.store.item(moved))
    assert not push.private_item(None)
    assert push.private_row(live.store, {"origin": "date", "item_id": None, "text": "Wednesday, two weeks to plan",
                                         "ref": "date:person:p_sam:2026-10-28:ahead"})
    assert push.private_row(live.store, {"origin": "date", "item_id": None, "text": "On Wednesday",
                                         "ref": f"date:fact:{wedding}:2026-10-28:eve"})
    assert not push.private_row(live.store, {"origin": "date", "item_id": None, "text": "On Friday",
                                             "ref": f"date:fact:{moved}:2027-05-01:day"})
    gift = promise(live, "Hermes will remind the user to order Ana's gift.")
    assert push.private_row(live.store, {"origin": "memory", "item_id": gift, "text": "Ana, by Friday"})
    assert not push.private_row(live.store, {"origin": "model", "item_id": None, "text": "The deploy is green"})


# Levels ----------------------------------------------------------------------------

def level(live, *, moment=False, cant_wait=False, quiet=False, busy=None, now=NOW, zone=UTC):
    return push.level(live.store, moment=moment, cant_wait=cant_wait, quiet=quiet, busy=busy, zone=zone, now=now)


def test_a_push_is_as_loud_as_what_it_is_and_whether_the_user_is_busy(live):
    assert level(live) == "active"
    assert level(live, quiet=True) == "passive"
    assert level(live, busy=(hours.EVENT, NOW + 600)) == "passive"
    # It went while busy because it couldn't wait: into Notification Center.
    assert level(live, busy=(hours.FOCUS, None), cant_wait=True) == "passive"
    assert level(live, busy=(hours.DRIVING, None)) == "active"  # driving doesn't count
    assert level(live, cant_wait=True) == "time-sensitive"
    # A place's moment comes only where Pulse may speak up: driving away from work, say.
    assert level(live, moment=True, busy=(hours.DRIVING, None)) == "time-sensitive"


def test_at_most_three_time_sensitive_pushes_a_day_in_the_users_time_zone(live):
    assert [level(live, moment=True) for _ in range(4)] == ["time-sensitive"] * 3 + ["active"]
    assert level(live, cant_wait=True) == "active"
    # 00:00 on Wednesday in Tokyo: a new day there.
    assert level(live, moment=True, zone=pulse.report.zone_for("Asia/Tokyo")) == "time-sensitive"
    assert level(live, moment=True, now=NOW + 86400) == "time-sensitive"
    live.store.set(push.TIME_SENSITIVE_KEY, {"day": "2026-10-29", "count": "lots"})
    assert level(live, moment=True, now=NOW + 2 * 86400) == "active"  # a count that can't be read is a full day's


def news(live, text="Hermes will tell the user when the deploy finishes.", **timing):
    """A follow-up whose news is ready to tell, with ``timing`` as next_send
    left it: the attempt that tells it."""
    item_id = promise(live, text)
    check_now(live, llm_saying(decision="tell", news="It's done.", why="the chat says so"),
              now=time.time() + followup.FIRST_CHECK_S + 1)
    row = live.store.followup(item_id)
    if timing:
        live.store.decide_followup(item_id, decision="tell", news=row["news"], why=row["why"], checks=row["checks"],
                                   next_check_at=None, now=time.time(), timing=timing)
    return {"kind": pulse.FOLLOWUP, "item_id": f"0000beef-f{item_id}"}


def test_a_follow_up_at_its_place_or_that_cant_wait_is_time_sensitive_unless_the_user_is_busy(live, monkeypatch):
    monkeypatch.setattr(pulse.hours, "busy", lambda home, now: None)
    assert pulse._push_how(news(live), "It's done.", []) == {"level": "active"}
    assert pulse._push_how(news(live, moment={"at": time.time()}), "It's done.", []) == {"level": "time-sensitive"}
    assert pulse._push_how(news(live, urgent=True), "It's done.", []) == {"level": "time-sensitive"}
    assert pulse._push_how(news(live, cant_wait=True), "It's done.", []) == {"level": "time-sensitive"}
    monkeypatch.setattr(pulse.hours, "busy", lambda home, now: (hours.EVENT, now + 600))
    assert pulse._push_how(news(live, urgent=True), "It's done.", []) == {"level": "passive"}


def test_a_reports_quiet_picks_go_passive(live, monkeypatch):
    monkeypatch.setattr(pulse.hours, "busy", lambda home, now: None)
    run = {"kind": pulse.REPORT, "item_id": "run-1"}
    interest = {"origin": "model", "item_id": None, "kind": "interest", "band": "days", "text": "A new Rust release"}
    later = {"origin": "model", "item_id": None, "kind": "follow-up", "band": "later", "text": "Plan the offsite"}
    soon = {"origin": "model", "item_id": None, "kind": "follow-up", "band": "soon", "text": "The window opens"}
    assert pulse._push_how(run, "A new Rust release · Plan the offsite", [interest, later]) == {"level": "passive"}
    assert pulse._push_how(run, "A new Rust release · The window opens", [interest, soon]) == {"level": "active"}
    # The morning brief is active.
    assert pulse._push_how({"kind": pulse.BRIEF, "item_id": ""}, "A calm day.", []) == {"level": "active"}


# Private pushes --------------------------------------------------------------------

def test_a_private_push_says_only_that_pulse_has_something(live, monkeypatch):
    monkeypatch.setattr(pulse.hours, "busy", lambda home, now: None)
    hidden = {"level": "active", "private": True, "body": "Pulse has something for you."}
    # Its item, its words or a pick in it.
    assert pulse._push_how(news(live, "Hermes will remind the user to order Ana's gift."), "Order it today.", []) \
        == hidden
    attempt = news(live)
    assert pulse._push_how(attempt, "The bank called back.", []) == hidden
    birthday = {"origin": "date", "item_id": None, "kind": "date", "band": "days", "text": "Wednesday",
                "ref": "date:person:p_sam:2026-10-28:ahead"}
    assert pulse._push_how({"kind": pulse.REPORT, "item_id": "run-1"}, "Wednesday", [birthday]) == hidden
    # Hide private reminders off: its words, as any push.
    live.store.set(push.PRIVATE_SETTING, False)
    assert pulse._push_how(attempt, "The bank called back.", []) == {"level": "active"}


def test_private_words_hide_a_push_even_when_pulse_cant_read_its_store(live, monkeypatch):
    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pulse, "open_store", locked)
    brief = {"kind": pulse.BRIEF, "item_id": ""}
    assert pulse._push_how(brief, "Dentist at 3, then a calm day.", []) == {"private": True,
                                                                         "body": "Pulse has something for you."}
    assert pulse._push_how(brief, "A calm day.", []) == {}  # at the default level


def test_the_reply_event_carries_the_level_and_the_private_copy(live, monkeypatch):
    monkeypatch.setattr(pulse.hours, "busy", lambda home, now: None)
    promise(live, "Hermes will remind the user to book the dentist.")
    check_now(live, llm_saying(decision="tell", news="Dr Lee has Friday at 3.", why="the chat says so"),
              now=time.time() + followup.FIRST_CHECK_S + 1)
    pulse.tick()
    [text] = live.sent()
    pulse.pre_llm_call(SID, "t1", text)
    queued = []
    monkeypatch.setattr(plugin, "enqueue", queued.append)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    monkeypatch.setattr(plugin, "_desktop_watched", lambda *args: False)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    plugin._pre_llm_call(session_id=SID, turn_id="t1", user_message=text, conversation_history=[])
    plugin._post_llm_call(session_id=SID, turn_id="t1", assistant_response="Book it: Dr Lee has Friday at 3.")
    plugin._on_session_end(session_id=SID, turn_id="t1", completed=True, failed=False, interrupted=False)
    [event] = queued
    assert (event["title_label"], event["body"], event["level"], event["private"]) == \
        ("pulse.followup", "Pulse has something for you.", "active", True)
    assert "Dr Lee" not in json.dumps(event)


def test_hide_private_reminders_is_a_setting_on_by_default(live, capsys, monkeypatch):
    assert control.state(live.store, live.home)["private_pushes"] is True
    control.update(live.store, {"private_pushes": False})
    assert control.state(live.store, live.home)["private_pushes"] is False
    with pytest.raises(control.Refused):
        control.update(live.store, {"private_pushes": "yes"})
    assert cli._pulse_private(live.store, "on") == 0 and push.hides_private(live.store)
    assert cli._pulse_private(live.store, "") == 0
    assert 'the Lock Screen says only "Pulse has something for you."' in capsys.readouterr().out
    assert cli._pulse_private(live.store, "maybe") == 2
    # `pulse status` says it, and today's time-sensitive pushes.
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: live.home)
    assert level(live, moment=True, now=time.time()) == "time-sensitive"
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value=""))
    out = capsys.readouterr().out
    assert "Private reminders: the Lock Screen says only" in out
    assert "Time-sensitive pushes today: 1 of 3" in out
