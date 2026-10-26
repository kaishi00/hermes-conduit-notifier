"""Quiet times (pulse_quiet): weekends, a vacation, quiet for today."""

import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from test_pulse import ROOT, _load, cli, control, pulse  # noqa: F401
from test_pulse_followup import live, tell  # noqa: F401
from test_pulse_learning import brief_due_now

quiet = _load("conduit_push.pulse_quiet", ROOT / "pulse_quiet.py")
UTC = timezone.utc


def at(*parts):
    return datetime(*parts, tzinfo=UTC).timestamp()


def test_why_it_is_quiet_and_when_that_ends(live):
    live.store.set("quiet_days", [5, 6])
    saturday, friday, monday = at(2026, 10, 31, 15), at(2026, 10, 30, 15), at(2026, 11, 2)
    assert quiet.why(live.store, UTC, saturday) == quiet.DAY and quiet.ends_at(live.store, UTC, saturday) == monday
    assert quiet.why(live.store, UTC, friday) == "" and quiet.ends_at(live.store, UTC, friday) is None
    # A spell that ends on a quiet day ends when the quiet days do.
    live.store.set("quiet_until", at(2026, 10, 31, 12))
    assert quiet.why(live.store, UTC, friday) == quiet.UNTIL and quiet.ends_at(live.store, UTC, friday) == monday
    live.store.set("quiet_until", at(2026, 10, 30, 18))
    assert quiet.ends_at(live.store, UTC, friday) == at(2026, 10, 30, 18)
    assert quiet.why(live.store, UTC, at(2026, 10, 30, 18)) == ""  # over
    live.store.set("quiet_days", list(range(7)))
    assert quiet.why(live.store, UTC, friday) == quiet.UNTIL and quiet.ends_at(live.store, UTC, friday) is None
    live.store.set("quiet_days", ["sat", 7, True, 6])  # only weekdays count
    assert quiet.days(live.store) == [6]


def test_the_settings_take_quiet_days_and_a_spell_until_today_or_a_day(live):
    live.store.set("time_zone", "UTC")
    control.update(live.store, {"quiet_days": [6, 5, 5]}, live.home)
    assert live.store.get("quiet_days") == [5, 6]
    for bad in ([7], ["sat"], [True], "sat", [0] * 8):
        with pytest.raises(control.Refused):
            control.update(live.store, {"quiet_days": bad}, live.home)
    now = time.time()
    control.update(live.store, {"quiet_until": "today"}, live.home)
    end = live.store.get("quiet_until")
    assert end in (quiet.next_midnight(UTC, now), quiet.next_midnight(UTC, time.time()))  # midnight may pass
    assert now < end <= now + 2 * 86400
    day = (datetime.fromtimestamp(now, UTC) + timedelta(days=3)).strftime("%Y-%m-%d")
    control.update(live.store, {"quiet_until": day}, live.home)
    assert live.store.get("quiet_until") == quiet.day_start(UTC, day) == datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=UTC).timestamp()
    # Quiet for today never shortens the longer spell already set.
    control.update(live.store, {"quiet_until": "today"}, live.home)
    assert live.store.get("quiet_until") == quiet.day_start(UTC, day)
    control.update(live.store, {"quiet_until": now + 7200}, live.home)
    assert live.store.get("quiet_until") == now + 7200
    today = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    for bad in ("yesterday", today, now - 1, now + 400 * 86400, True, "2026-13-40", [], float("nan"), 10 ** 400):
        with pytest.raises(control.Refused):
            control.update(live.store, {"quiet_until": bad}, live.home)
    assert live.store.get("quiet_until") == now + 7200  # a refusal changes nothing
    state = control.state(live.store, live.home)
    assert state["quiet_days"] == [5, 6] and state["quiet_until"] == now + 7200 and state["quiet_now"] == quiet.UNTIL
    assert state["quiet_ends_at"] >= now + 7200
    control.update(live.store, {"quiet_until": None, "quiet_days": []}, live.home)
    state = control.state(live.store, live.home)
    assert state["quiet_until"] is None and state["quiet_now"] == "" and state["quiet_ends_at"] is None


def test_while_quiet_nothing_goes_on_its_own_but_what_is_asked_for_does(live):
    live.store.set("time_zone", "UTC")
    live.store.set("daily_report", True)
    now = time.time()
    live.store.set("report_time", time.strftime("%H:%M", time.gmtime(now - 120)))  # a report due
    live.store.set("quiet_until", now + 3600)
    pulse.tick()
    assert live.sent() == []
    live.store.set("report_requested", True)
    pulse.tick()
    [asked] = live.sent()
    assert asked.startswith("Pulse report")


def test_a_quiet_day_holds_the_report_for_the_next_scheduled_one(live):
    live.store.set("time_zone", "UTC")
    live.store.set("daily_report", True)
    now = time.time()
    live.store.set("report_time", time.strftime("%H:%M", time.gmtime(now - 120)))
    live.store.set("quiet_days", [datetime.fromtimestamp(now, UTC).weekday()])
    pulse.tick()
    assert live.sent() == []
    live.store.set("quiet_days", [])
    pulse.tick()
    [sent] = live.sent()
    assert sent.startswith("Pulse report")


def test_a_scheduled_report_on_its_way_when_pulse_goes_quiet_doesnt_go(live):
    live.store.set("time_zone", "UTC")
    now = time.time()
    live.store.set("quiet_until", now + 3600)  # quiet for today, tapped while the report was being made
    live.store.set(pulse.REPORT_RUN, {"id": "r1", "state": pulse.RUN_FAILED, "started_at": now - 60})
    pulse.tick()
    assert live.sent() == [] and live.store.get(pulse.REPORT_RUN)["state"] == pulse.RUN_DROPPED
    # One the user asked for still goes.
    live.store.set(pulse.REPORT_RUN, {"id": "r2", "state": pulse.RUN_FAILED, "started_at": now - 60, "asked": True})
    pulse.tick()
    [sent] = live.sent()
    assert sent.startswith("Pulse report")


def test_a_report_or_brief_time_that_fell_while_quiet_isnt_sent_late_once_it_ends(live):
    # Reports at 19:00 and the brief at 18:00: the first minutes of Sunday
    # aren't a late Saturday report or brief when Saturday was quiet.
    live.store.set("time_zone", "UTC")
    live.store.set("daily_report", True)
    live.store.set("report_time", "19:00")
    live.store.set("brief_time", "18:00")
    sunday_night = at(2026, 11, 1, 0, 5)
    # Without quiet times, a missed one still goes, late.
    assert pulse._report_due(live.store, live.home, sunday_night)
    assert pulse.brief.next_attempt(live.store, sunday_night, UTC, "18:00") is not None
    for setting, value, cleared in (("quiet_days", [5], []), ("quiet_until", at(2026, 11, 1), None)):
        live.store.set(setting, value)  # Saturday's a quiet day, or it was quiet for the day
        assert quiet.why(live.store, UTC, sunday_night) == ""  # over
        assert not pulse._report_due(live.store, live.home, sunday_night)
        assert not pulse._maybe_brief(live.store, live.home, sunday_night) and live.sent() == []
        live.store.set(setting, cleared)


def test_a_quiet_day_holds_a_due_follow_up_and_the_brief_for_the_first_brief_after(live):
    brief_due_now(live)
    live.store.set("quiet_days", [datetime.fromtimestamp(time.time(), UTC).weekday()])
    tell(live)  # a follow-up to push now
    pulse.tick()
    assert live.sent() == [] and len(live.store.held_cards(10)) == 1  # held, not pushed
    pulse.tick()
    assert live.sent() == []  # and no brief on a quiet day
    live.store.set("quiet_days", [])
    pulse.tick()
    [text] = live.sent()
    assert text.startswith("Pulse morning brief ") and "everything is green" in text


def test_quiet_command(live, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    live.store.set("time_zone", "UTC")

    def run(value="", extra=""):
        return cli._pulse(types.SimpleNamespace(pulse_action="quiet", value=value, extra=extra))

    assert run("days", "sat,sun") == 0 and live.store.get("quiet_days") == [5, 6]
    assert "Quiet days: Sat, Sun" in capsys.readouterr().out
    assert run("until", "2026-13-40") == 2 and "YYYY-MM-DD" in capsys.readouterr().out
    before = time.time()
    assert run("today") == 0
    assert live.store.get("quiet_until") in (quiet.next_midnight(UTC, before), quiet.next_midnight(UTC, time.time()))
    out = capsys.readouterr().out
    assert "Quiet until " in out and "Pulse is quiet now: nothing on its own until " in out
    cli._pulse(types.SimpleNamespace(pulse_action="status", value="", extra=""))
    assert "Quiet days: Sat, Sun" in capsys.readouterr().out
    assert run("off") == 0 and live.store.get("quiet_until") is None
    assert run("until", "yesterday") == 2 and "a day ahead" in capsys.readouterr().out
    day = (datetime.fromtimestamp(time.time(), UTC) + timedelta(days=10)).strftime("%Y-%m-%d")
    assert run("until", day) == 0 and live.store.get("quiet_until") == quiet.day_start(UTC, day)
    assert run("days", "funday") == 2 and run("sometime") == 2
    assert run("days", "none") == 0 and live.store.get("quiet_days") == []
    assert run("off") == 0 and run() == 0
    out = capsys.readouterr().out
    assert "Quiet days: none" in out and "quiet now" not in out.splitlines()[-1]
    cli._pulse(types.SimpleNamespace(pulse_action="status", value="", extra=""))
    assert "Quiet" not in capsys.readouterr().out  # nothing set: status says nothing of it
