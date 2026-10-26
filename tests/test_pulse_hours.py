"""Pulse at the right time (pulse_hours, designs/pulse-right-time.md): the
night, the parts of the day, when news is useful, busy holds and Wrong
time."""

import json
import sys
import time
import types
from datetime import datetime, timezone

import pytest

from test_pulse import ROOT, _load, cli, control, pulse, store
from test_pulse_followup import check_now, commitment, followup, live, llm_saying, promise, tell  # noqa: F401
from test_phone import Phone, _batch, _event, phone

hours = _load("conduit_push.pulse_hours", ROOT / "pulse_hours.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")
candidates = _load("conduit_push.pulse_candidates", ROOT / "pulse_candidates.py")
UTC = timezone.utc
LONDON = pulse.report.zone_for("Europe/London")


def at(day, hour, minute=0, zone=UTC):
    """A time in October 2026: the 26th is a Monday, the 31st a Saturday."""
    return datetime(2026, 10, day, hour, minute, tzinfo=zone).timestamp()


def next_at(hour, minute=0):
    """The next hour:minute UTC from the real time on, for a test that ticks."""
    now = time.time()
    moment = datetime.fromtimestamp(now, UTC).replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    return moment if moment > now else moment + 86400


@pytest.fixture
def ps(tmp_path):
    return store.open_store(tmp_path)


def nights(live):
    """The default night, in UTC, with the brief at its default 07:00."""
    live.store.set("night", ["22:00", "07:00"])
    live.store.set("time_zone", "UTC")
    live.store.set("brief_time", "07:00")


# The night and the parts of the day --------------------------------------------

def test_the_night_is_from_22_to_7_unless_set(ps):
    assert hours.night(ps) == ((22, 0), (7, 0)) and hours.night_text(ps) == ["22:00", "07:00"]
    ps.set("night", ["23:30", "06:15"])
    assert hours.night(ps) == ((23, 30), (6, 15)) and hours.night_text(ps) == ["23:30", "06:15"]
    ps.set("night", [])
    assert hours.night(ps) is None and hours.night_text(ps) == [] and hours.bounds(ps) == hours.DEFAULT_BOUNDS
    ps.set("night", ["nonsense"])  # unreadable: the default, never none
    assert hours.night(ps) == hours.DEFAULT_BOUNDS


@pytest.mark.parametrize("value, parsed", [
    (["22:00", "07:00"], ["22:00", "07:00"]), (["7:05", "23:00"], ["07:05", "23:00"]), ([], []),
    (["22:00", "22:00"], []), (["24:00", "07:00"], None), (["22:60", "07:00"], None), (["22:00"], None),
    (["22:00", "07:00", "08:00"], None), ("22:00-07:00", None), ([22, 7], None), (None, None),
    (["22:00:00", "07:00"], None), (["+2:30", "07:00"], None)])
def test_a_night_is_two_times_or_none(value, parsed):
    assert hours.parse_night(value) == parsed


@pytest.mark.parametrize("hour, minute, name", [
    (22, 0, "night"), (23, 59, "night"), (3, 0, "night"), (6, 59, "night"), (7, 0, "morning"), (11, 59, "morning"),
    (12, 0, "midday"), (16, 59, "midday"), (17, 0, "evening"), (21, 59, "evening")])
def test_the_parts_of_the_day(hour, minute, name):
    assert hours.part(hours.DEFAULT_BOUNDS, UTC, at(27, hour, minute)) == name


def test_a_night_that_ends_after_noon_leaves_no_morning():
    late = ((2, 0), (13, 0))
    assert [hours.part(late, UTC, at(27, hour)) for hour in (1, 2, 12, 13, 17, 23)] == [
        "evening", "night", "night", "midday", "evening", "evening"]


def test_when_news_is_useful():
    names = hours.DEFAULT_BOUNDS
    useful = lambda when, moment: hours.useful(when, names, UTC, moment)
    # Office hours: weekdays, 09:00 to 17:00. The 30th is a Friday.
    assert useful(hours.OFFICE_HOURS, at(30, 9)) and useful(hours.OFFICE_HOURS, at(30, 16, 59))
    assert not useful(hours.OFFICE_HOURS, at(30, 17)) and not useful(hours.OFFICE_HOURS, at(30, 8, 59))
    assert not useful(hours.OFFICE_HOURS, at(31, 10))
    assert useful(hours.MORNING, at(30, 8)) and not useful(hours.MORNING, at(30, 12))
    assert useful(hours.EVENING, at(30, 18)) and not useful(hours.EVENING, at(30, 22))
    assert useful(hours.ANY, at(30, 3))


def test_the_first_time_a_push_may_go():
    first = lambda now, **kwargs: hours.next_allowed(hours.DEFAULT_BOUNDS, UTC, now, **kwargs)
    assert first(at(27, 10)) == at(27, 10)
    assert first(at(27, 23)) == at(28, 7)  # when the night ends
    assert first(at(27, 23), night_on=False) == at(27, 23)
    assert first(at(30, 18), when=hours.OFFICE_HOURS) == datetime(2026, 11, 2, 9, tzinfo=UTC).timestamp()  # Monday
    assert first(at(27, 8), closed={"morning"}) == at(27, 12)
    days = {"morning", "midday", "evening"}
    assert first(at(27, 8), closed=days) is None  # never within a week: the brief brings it
    assert first(at(27, 8), closed=days, night_on=False) == at(27, 22)


def test_when_a_push_may_go(ps):
    timing = lambda now, brief_at=at(28, 7), **kwargs: hours.timing(ps, UTC, now, brief_at=brief_at, **kwargs)
    assert timing(at(27, 10)) == hours.Timing(hours.NOW)
    # At night it's held for the morning brief, or until the night ends when the brief comes long after.
    assert timing(at(27, 23)) == hours.Timing(hours.BRIEF, held_by=hours.NIGHT)
    assert timing(at(27, 23), brief_at=at(28, 10)) == hours.Timing(hours.HOLD, until=at(28, 7), held_by=hours.NIGHT)
    # Unless it can't wait: urgent, or due before it would come.
    assert timing(at(27, 23), urgent=True) == hours.Timing(hours.NOW, held_by=hours.NIGHT, cant_wait=True)
    assert timing(at(27, 23), due_end=at(27, 23, 30)).cant_wait
    assert timing(at(27, 23), due_end=at(28, 12)).action == hours.BRIEF
    assert timing(at(27, 23), due_end=at(27, 22)).action == hours.BRIEF  # already past: the morning will do
    # News for a time of day waits for it.
    assert timing(at(27, 18), when=hours.OFFICE_HOURS) == hours.Timing(hours.HOLD, until=at(28, 9),
                                                                       held_by="when:office_hours")
    assert timing(at(27, 18), when="whenever") == hours.Timing(hours.NOW)
    # So does a push in a part of the day Wrong time closed for its kind.
    assert timing(at(27, 8), closed={"morning"}) == hours.Timing(hours.HOLD, until=at(27, 12), held_by="part:morning")
    assert timing(at(27, 18), closed={"evening"}) == hours.Timing(hours.BRIEF, held_by="part:evening")
    # No night: any hour may push, and news for the morning waits for it, checked again then.
    ps.set("night", [])
    assert timing(at(27, 23)) == hours.Timing(hours.NOW)
    assert timing(at(27, 23), when=hours.MORNING) == hours.Timing(hours.HOLD, until=at(28, 7),
                                                                  held_by="when:morning")


def test_a_hold_lands_on_the_local_clock_across_a_time_change(ps):
    # The clocks went back on the 25th: 23:00 on the 24th is summer time, 07:00 on the 25th winter time.
    held = hours.timing(ps, LONDON, at(24, 23, zone=LONDON), brief_at=at(25, 10, zone=LONDON))
    assert held == hours.Timing(hours.HOLD, until=at(25, 7, zone=LONDON), held_by=hours.NIGHT)


def test_how_loudly_a_reports_pick_may_come_up(ps):
    cap = lambda now, **kwargs: hours.push_cap(ps, UTC, now, brief_at=at(28, 7), **kwargs)
    assert cap(at(27, 10)) == "push"
    assert cap(at(27, 23)) == "brief"
    assert cap(at(27, 23), due_end=at(27, 23, 45)) == "push"
    assert cap(at(27, 13), closed={"midday"}) == "screen"


def test_when_the_night_starts_and_a_day_ends(ps):
    assert hours.night_starts(ps, UTC, at(27, 20)) == at(27, 22)
    assert hours.night_starts(ps, UTC, at(27, 23)) == at(27, 23)
    # Due on a day, with no time: it's of use until that day's night starts.
    assert hours.due_end(ps, UTC, at(27, 0), timed=False) == at(27, 22)
    assert hours.due_end(ps, UTC, at(27, 15), timed=True) == at(27, 15)
    ps.set("night", ["01:00", "08:00"])  # a night that starts after midnight: the day ends at midnight
    assert hours.due_end(ps, UTC, at(27, 0), timed=False) == at(28, 0)
    ps.set("night", ["02:00", "13:00"])  # a night over noon started that morning: the day ends at midnight
    assert hours.due_end(ps, UTC, at(27, 0), timed=False) == at(28, 0)
    ps.set("night", ["12:00", "20:00"])
    assert hours.due_end(ps, UTC, at(27, 0), timed=False) == at(28, 0)
    ps.set("night", [])
    assert hours.night_starts(ps, UTC, at(27, 20)) is None
    assert hours.due_end(ps, UTC, at(27, 0), timed=False) == at(28, 0)


# Busy ------------------------------------------------------------------------------

def phone_home(home, **batch):
    """A Hermes home with one phone synced (test_phone's batch: 08:00 in
    London on the 14th, a standup and a design review that morning, lunch
    marked free, an all-day holiday, a Work Focus that holds calls)."""
    home.mkdir(parents=True, exist_ok=True)
    phones = phone.PhoneStore(home, clock=lambda: datetime(2026, 10, 14, 7, 0, tzinfo=UTC).timestamp())
    handset = Phone()
    handset.accept(phones.register_key(handset.key_request()))
    phones.ingest(handset.seal(_batch(**batch)))
    return home


def test_busy_is_an_event_on_now_driving_or_a_focus_that_holds_calls(tmp_path):
    london = lambda hour, minute=0: datetime(2026, 10, 14, hour, minute, tzinfo=LONDON).timestamp()
    events = _batch()["events"]
    declined = _event("e_optional", title="Optional sync", start="2026-10-14T14:00:00+01:00",
                      end="2026-10-14T15:00:00+01:00", my_status="declined", attendees=[], notes=None)
    home = phone_home(tmp_path / "busy", events={**events, "upsert": events["upsert"] + [declined]})
    # The standup and the design review overlap: busy until the later ends.
    assert hours.busy(home, london(9, 50)) == (hours.EVENT, london(11))
    # Lunch is marked free, the holiday is all day and the sync declined: none of them is busy. The Focus is.
    assert hours.busy(home, london(12, 30)) == (hours.FOCUS, None)
    assert hours.busy(home, london(14, 30)) == (hours.FOCUS, None)
    free = {"at": "2026-10-14T07:30:00+01:00", "active": False, "label": "", "hold_calls": False}
    assert hours.busy(phone_home(tmp_path / "free", focus=free), london(8)) is None
    status = {**_batch()["status"], "carplay": True, "activity": "automotive"}
    driving = phone_home(tmp_path / "driving", focus=free, status=status)
    assert hours.busy(driving, london(8)) == (hours.DRIVING, None)
    # The phone said so 21 minutes ago: too long to count as driving now.
    assert hours.busy(driving, london(8, 20)) is None
    assert hours.busy(None, london(8)) is None and hours.busy(tmp_path / "nothing", london(8)) is None


# Follow-ups ---------------------------------------------------------------------

def test_a_check_says_when_news_is_useful_and_whether_its_urgent():
    told = lambda **reply: followup.check(lambda instructions, text: (
        {"decision": "tell", "news": "n", "why": "w", **reply}, 1), "material")[0]
    assert (told()["when"], told()["urgent"]) == ("any", False)
    assert (told(when="office_hours", urgent=True)["when"], told(urgent=True)["urgent"]) == ("office_hours", True)
    assert (told(when="lunchtime")["when"], told(urgent="yes")["urgent"]) == ("any", False)
    for instructions in (followup.INSTRUCTIONS, followup.COMMITMENT_INSTRUCTIONS, followup.WATCH_INSTRUCTIONS):
        assert '"when"' in instructions and '"urgent"' in instructions


def test_news_at_night_waits_for_the_morning_brief(live):
    nights(live)
    item_id = tell(live)
    assert followup.next_send(live.store, next_at(23), live.home, zone=UTC) is None
    [card] = live.store.cards()
    assert card["level"] == "brief" and card["item_id"] == item_id
    assert card["why"] == "the chat shows it finished. Held for the morning brief: it came up at night."
    assert live.store.followups()[0]["decision"] == "held"


def test_news_that_cant_wait_goes_at_night_and_says_why(live, monkeypatch):
    nights(live)
    promise(live)
    check_now(live, llm_saying(decision="tell", news="The deploy failed.", why="it broke", urgent=True),
              now=time.time() + followup.FIRST_CHECK_S + 1)
    tonight = next_at(23)
    monkeypatch.setattr(time, "time", lambda: tonight)
    pulse.tick(now=tonight)
    assert len(live.sent()) == 1
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick(now=tonight + 60)
    [card] = live.store.cards()
    assert card["level"] == "push" and card["why"] == "it broke. Sent at night: it couldn't wait for the morning."


def test_news_for_office_hours_waits_for_them_and_is_checked_again_first(live):
    live.store.set("time_zone", "UTC")
    item_id = promise(live)
    check_now(live, llm_saying(decision="tell", news="Call the bank back.", why="they asked", when="office_hours"),
              now=time.time() + followup.FIRST_CHECK_S + 1)
    evening = next_at(18)
    while datetime.fromtimestamp(evening, UTC).weekday() > 3:  # Monday to Thursday: tomorrow is a weekday
        evening += 86400
    morning = evening + 15 * 3600
    assert followup.next_send(live.store, evening, live.home, zone=UTC) is None
    [row] = live.store.followups()
    assert row["decision"] == followup.TIMED and row["next_check_at"] == morning
    timing = followup.followup_timing(row)
    assert (timing["held"], timing["held_clock"], timing["when"]) == ("when:office_hours", "09:00", "office_hours")
    # `followups now` leaves it be: its news is decided, and it's checked when it may go.
    live.store.set(followup.NOW_REQUESTED, evening)
    followup.check_all_now(live.store, evening)
    assert live.store.followups()[0]["next_check_at"] == morning
    # Checked again then; another time of day the check gives doesn't hold it again.
    again = llm_saying(decision="tell", news="Call the bank back today.", why="still open", when="evening")
    check_now(live, again, now=morning + 1)
    assert len(again.calls) == 1
    item, row, attempt = followup.next_send(live.store, morning + 2, live.home, zone=UTC)
    assert item["id"] == item_id and row["news"] == "Call the bank back today." and attempt == 1
    assert followup.held_note(followup.followup_timing(live.store.followups()[0]), "push") == \
        "Held until 09:00, for office hours."


def test_held_news_done_meanwhile_closes_instead(live):
    live.store.set("time_zone", "UTC")
    item_id = promise(live)
    check_now(live, llm_saying(decision="tell", news="Call the bank back.", why="they asked", when="office_hours"),
              now=time.time() + followup.FIRST_CHECK_S + 1)
    saturday = next_at(10)
    while datetime.fromtimestamp(saturday, UTC).weekday() != 5:
        saturday += 86400
    assert followup.next_send(live.store, saturday, live.home, zone=UTC) is None
    monday = saturday + 47 * 3600  # 09:00
    assert live.store.followups()[0]["next_check_at"] == monday
    check_now(live, llm_saying(decision="done", why="they called on Sunday"), now=monday + 1)
    assert live.store.followups()[0]["decision"] == "done" and live.store.item(item_id)["status"] == "done"


def test_a_reminder_held_for_its_time_takes_one_of_the_days_reminders_once(live):
    live.store.set("time_zone", "UTC")
    commitment(live, said_at=time.time() - 2 * 86400)
    first = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm_saying(decision="tell", news="Renew the passport.", why="due", when="office_hours"), now=first)
    assert len(live.store.get(followup.REMINDERS_TOLD)) == 1
    evening = next_at(18)
    while datetime.fromtimestamp(evening, UTC).weekday() > 3:
        evening += 86400
    assert followup.next_send(live.store, evening, live.home, zone=UTC) is None
    check_now(live, llm_saying(decision="tell", news="Renew the passport.", why="due"), now=evening + 15 * 3600 + 1)
    [row] = live.store.followups()
    assert row["decision"] == "tell" and len(live.store.get(followup.REMINDERS_TOLD)) == 1


def test_a_push_waits_while_youre_busy(live, monkeypatch):
    item_id = tell(live)
    busy = [(hours.EVENT, None)]
    monkeypatch.setattr(followup.hours, "busy", lambda home, now: busy[0])
    now = time.time() + followup.FIRST_CHECK_S + 2
    busy[0] = (hours.EVENT, now + 3600)
    assert followup.next_send(live.store, now, live.home, zone=UTC) is None
    [row] = live.store.followups()
    assert row["decision"] == "tell" and followup.followup_timing(row)["busy_since"] == now
    assert followup.next_send(live.store, now + 600, live.home, zone=UTC) is None
    # The event over, it goes, and its Why? says what it waited for.
    busy[0] = None
    item, row, _ = followup.next_send(live.store, now + 3601, live.home, zone=UTC)
    assert item["id"] == item_id
    assert followup.held_note(followup.followup_timing(row), "push") == "Waited until your event ended."


def test_a_push_busy_for_over_three_hours_shows_without_one(live, monkeypatch):
    tell(live)
    monkeypatch.setattr(followup.hours, "busy", lambda home, now: (hours.DRIVING, None))
    now = time.time() + followup.FIRST_CHECK_S + 2
    assert followup.next_send(live.store, now, live.home, zone=UTC) is None
    assert followup.next_send(live.store, now + hours.BUSY_MAX_S, live.home, zone=UTC) is None
    [card] = live.store.cards()
    assert card["level"] == "screen"
    assert card["why"].endswith("Shown without a notification: you were driving for over three hours.")
    # An event known to run past three hours doesn't make it wait at all.
    promise(live, "Hermes will send the invoice.")
    check_now(live, llm_saying(decision="tell", news="The invoice went out.", why="sent"), now=now)
    monkeypatch.setattr(followup.hours, "busy", lambda home, now: (hours.EVENT, now + 4 * 3600))
    assert followup.next_send(live.store, now, live.home, zone=UTC) is None
    assert [card["level"] for card in live.store.cards()] == ["screen", "screen"]


def test_a_push_due_before_youre_free_or_urgent_goes_anyway(live, monkeypatch):
    now = time.time() + followup.FIRST_CHECK_S + 2
    item_id = commitment(live, due="2026-10-27T15:00", due_at=now + 1800, said_at=now - 86400)
    check_now(live, llm_saying(decision="tell", news="The call is at three.", why="due soon"), now=now)
    monkeypatch.setattr(followup.hours, "busy", lambda home, moment: (hours.EVENT, now + 3600))
    item, _, _ = followup.next_send(live.store, now, live.home, zone=UTC)
    assert item["id"] == item_id


def test_a_push_that_goes_while_youre_busy_doesnt_say_it_waited(live, monkeypatch):
    noon = next_at(12)
    item_id = commitment(live, due="2026-10-27T15:00", due_at=noon + 1800, said_at=noon - 86400)
    row = {"item_id": item_id, "news": "The call is at three.", "why": "due soon", "checks": 1,
           "timing": json.dumps({"busy": hours.DRIVING, "busy_since": noon - 600})}
    driving = [(hours.DRIVING, None)]
    monkeypatch.setattr(followup.hours, "busy", lambda home, moment: driving[0])

    def timed(now):
        return followup._timed(live.store, live.store.item(item_id), row, now=now, zone=UTC, home=live.home)

    # Due before you'd be free, it goes while you drive: its Why? doesn't say it waited.
    loudness, timing = timed(noon)
    assert loudness == "push" and "busy" not in timing and followup.held_note(timing, "push") == ""
    # Nor when it can't wait for the morning.
    nights(live)
    row["timing"] = json.dumps({"urgent": True, "busy": hours.DRIVING, "busy_since": noon - 600})
    loudness, timing = timed(noon + 11 * 3600)
    assert loudness == "push" and followup.held_note(timing, "push") == \
        "Sent at night: it couldn't wait for the morning."
    # Once you're free, it says what it waited for.
    driving[0] = None
    row["timing"] = json.dumps({"busy": hours.DRIVING, "busy_since": noon - 600})
    loudness, timing = timed(noon)
    assert loudness == "push" and followup.held_note(timing, "push") == "Waited until you'd stopped driving."


def test_held_notes():
    note = followup.held_note
    assert note({}, "push") == "" and note({"when": "any"}, "screen") == ""
    assert note({"held": followup.QUIET}, "brief") == "Held for the morning brief: it came during your quiet time."
    assert note({"held": "part:evening"}, "brief") == \
        "Held for the morning brief: you said the evening was the wrong time for these."
    assert note({"held": "when:morning"}, "brief") == "Held for the morning brief: it's for the morning."
    assert note({"held": "part:midday", "cant_wait": True}, "push") == \
        "Sent in the middle of the day, which you said was the wrong time for these: it couldn't wait."
    assert note({"held": "when:office_hours", "cant_wait": True}, "push") == \
        "Sent now, though it's for office hours: it couldn't wait."
    assert note({"held": "night", "held_clock": "07:00"}, "push") == "Held until 07:00, since it came up at night."
    assert note({"held": "part:morning", "held_clock": "12:00", "busy": "focus"}, "push") == \
        "Held until 12:00, since you said the morning was the wrong time for these. Waited until your Focus ended."
    assert note({"busy": "event", "busy_long": "event"}, "screen") == \
        "Shown without a notification: your calendar was busy for over three hours."


# Not now and Wrong time ----------------------------------------------------------

def test_later_today_ends_when_the_night_starts(live):
    nights(live)
    rest = lambda choice, now: followup.rest_time(choice, now, UTC, live.store)
    assert rest("later_today", at(27, 10)) == at(27, 13)
    assert rest("later_today", at(27, 20)) == at(27, 22)
    assert rest("later_today", at(27, 21, 45)) == at(28, 6, 50)  # under half an hour before it: tomorrow
    assert rest("later_today", at(27, 23)) == at(28, 6, 50)  # at night: in time for the morning brief
    assert rest("later_today", at(28, 2)) == at(28, 6, 50)
    live.store.set("night", ["23:30", "06:00"])
    assert rest("later_today", at(27, 21)) == at(27, 23)  # by 23:00 still


def test_a_card_put_away_for_the_wrong_time_comes_back_at_a_better_one(live):
    nights(live)
    back = lambda now: followup.wrong_time_rest(live.store, UTC, now)
    assert back(at(27, 9)) == at(27, 12)  # in three hours
    assert back(at(27, 14)) == at(27, 17)
    assert back(at(27, 18)) == at(28, 6, 50)  # in the evening or at night: before the morning brief
    assert back(at(27, 23)) == at(28, 6, 50)


def told_card(live):
    tell(live)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    [card] = live.store.cards()
    return card


def test_wrong_time_closes_that_part_of_the_day_for_the_kind_without_a_thumb(live):
    live.store.set("time_zone", "UTC")
    card = told_card(live)
    part = hours.part_of(live.store, UTC, card["created_at"])
    control.rate(live.store, card["id"], "down", now=time.time())  # first a plain thumbs down: a signal
    assert [signal["what"] for signal in live.store.signals() if signal["card_id"] == card["id"]] == ["thumb"]
    now = time.time()
    [lesson] = control.rate(live.store, card["id"], "down", "wrong_time", now=now, home=live.home)
    assert (lesson["what"], lesson["value"], lesson["cause"]) == ("part", part, "wrong time")
    assert lesson["text"] == f"Follow-ups: no pushes {learning.PART_WORDS[part]} (wrong time)"
    # The timing was wrong, not the card: the thumb goes, and a sync doesn't put it back.
    assert [signal for signal in live.store.signals() if signal["card_id"] == card["id"]] == []
    assert [signal for signal in store.open_store(live.home).signals() if signal["card_id"] == card["id"]] == []
    # The card is put away until a better time.
    rated = live.store.card(card["id"])
    assert rated["dismissed_at"] == now and rated["rest_until"] == followup.wrong_time_rest(live.store, UTC, now)
    assert learning.closed_parts(live.store, "followup") == {part}
    # Pushes of that kind wait for the next part of the day.
    assert hours.timing(live.store, UTC, card["created_at"], brief_at=card["created_at"] + 86400,
                        closed=learning.closed_parts(live.store, "followup")).action != hours.NOW
    # Again for the same part: nothing new to learn. Undo opens it again.
    assert control.rate(live.store, card["id"], "down", "wrong_time", now=now + 1, home=live.home) == []
    assert learning.undo(live.store, lesson["id"], now + 2)
    assert learning.closed_parts(live.store, "followup") == set()


def test_wrong_time_on_a_report_card_doesnt_keep_it_out_of_reports(live):
    live.store.set("time_zone", "UTC")
    item_id = promise(live)
    card_id = live.store.add_card({"kind": "noticed", "item_id": item_id, "level": "push", "topic": "promise",
                                   "title": "Hermes owes the deploy note", "news": "Hermes owes the deploy note",
                                   "created_at": time.time()})
    control.rate(live.store, card_id, "down", "wrong_time", home=live.home)
    # The timing was wrong, not the card: the report isn't told it was rated down.
    assert pulse.report.rating_counts(live.store) == {}
    assert item_id not in pulse.report.rated_not_useful(live.store)
    recent = candidates.recent_section(live.store, UTC, time.time())
    assert "Hermes owes the deploy note (remembered #%d) (the user said it came at the wrong time)" % item_id in recent
    assert "rated not useful" not in recent
    # It rests while its card is away until a better time, and the report offers it again once the card is back.
    offered = lambda: [(item["id"], why) for item, why in pulse.report.remembered_items(live.store, time.time())[1]]
    assert offered() == [(item_id, pulse.report.PUT_AWAY)]
    back = live.store.card(card_id)["rest_until"]
    assert live.store.return_card(card_id, back, back)
    items, resting = pulse.report.remembered_items(live.store, time.time())
    assert [item["id"] for item in items] == [item_id] and resting == []


def test_wrong_time_on_a_card_that_didnt_push_teaches_nothing(live):
    live.store.set("time_zone", "UTC")
    item_id = promise(live)
    card_id = live.store.add_card({"kind": "followup", "item_id": item_id, "level": "screen", "news": "n",
                                   "created_at": time.time()})
    assert control.rate(live.store, card_id, "down", "wrong_time", home=live.home) == []
    assert live.store.card(card_id)["rest_until"] is not None
    with pytest.raises(control.Refused):
        control.rate(live.store, card_id, "up", "wrong_time", home=live.home)


def test_a_report_pick_comes_up_no_louder_than_the_time_allows(live):
    nights(live)

    def pick(kind="date", due_at=None):
        return {"origin": candidates.MODEL, "item_id": None, "kind": kind, "profile": "", "chat_id": "",
                "source": "calendar", "area": "", "due_at": due_at, "actionable": True, "value": 5,
                "bring_up": True, "skip": "", "text": f"{kind} thing", "why": "", "asked": None}

    learned = learning.prefs.EMPTY
    late = [pick(), pick(due_at=at(27, 23, 30))]
    candidates.decide(live.store, late, now=at(27, 23), learned=learned, zone=UTC)
    assert [row["level"] for row in late] == ["brief", "push"]  # due before morning: it can't wait
    day = [pick()]
    candidates.decide(live.store, day, now=at(27, 10), learned=learned, zone=UTC)
    assert day[0]["level"] == "push"
    live.store.add_lesson({"kind": "date", "what": "part", "value": "morning", "profile": "", "chat_id": "",
                           "card_id": None, "cause": "wrong time", "created_at": at(27, 10)})
    day = [pick(), pick("promise")]
    candidates.decide(live.store, day, now=at(27, 10), learned=learned, zone=UTC)
    assert [row["level"] for row in day] == ["screen", "push"]
    unzoned = [pick()]
    candidates.decide(live.store, unzoned, now=at(27, 23), learned=learned)
    assert unzoned[0]["level"] == "push"  # no zone, no time cap: as before


# Settings and the CLI --------------------------------------------------------------

def test_the_night_is_a_setting(live):
    control.update(live.store, {"night": ["21:30", "06:45"]}, live.home)
    assert live.store.get("night") == ["21:30", "06:45"]
    state = control.state(live.store, live.home)
    assert state["night"] == ["21:30", "06:45"] and isinstance(state["night_now"], bool)
    feed = control.feed(live.store, live.home)
    assert feed["night"] == ["21:30", "06:45"] and isinstance(feed["night_now"], bool)
    for bad in (["21:30"], ["25:00", "06:00"], "21:30", None):
        with pytest.raises(control.Refused) as refused:
            control.update(live.store, {"night": bad}, live.home)
        assert refused.value.data["field"] == "night"
    assert live.store.get("night") == ["21:30", "06:45"]
    control.update(live.store, {"night": []}, live.home)
    assert control.state(live.store, live.home)["night"] == [] and not control.state(live.store, live.home)["night_now"]


def test_night_command(live, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    live.store.set("time_zone", "UTC")
    run = lambda value="", extra="": cli.dispatch(types.SimpleNamespace(
        conduit_push_action="pulse", pulse_action="night", value=value, extra=extra))
    assert run() == 0 and "Night: none" in capsys.readouterr().out
    assert run("22:30", "6:45") == 0 and live.store.get("night") == ["22:30", "06:45"]
    assert "Night: 22:30 to 06:45 (UTC): nothing pushes then" in capsys.readouterr().out
    assert run("22:30") == 2 and run("25:00", "07:00") == 2 and run("off", "07:00") == 2
    assert live.store.get("night") == ["22:30", "06:45"]
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value="", extra=""))
    assert "Night: 22:30 to 06:45 (UTC)" in capsys.readouterr().out
    assert run("off") == 0 and live.store.get("night") == []


def test_rate_command_takes_wrong_time(live, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    live.store.set("time_zone", "UTC")
    card = told_card(live)
    capsys.readouterr()
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="rate",
                                              value=str(card["id"]), extra="wrong_time")) == 0
    out = capsys.readouterr().out
    assert out.startswith("Rated. Put away until ") and "Pulse learned: Follow-ups: no pushes" in out
    assert live.store.card(card["id"])["reason"] == "wrong_time"
