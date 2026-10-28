"""Dates that come back every year (pulse_dates) and the daily look ahead
(pulse_ahead): designs/pulse-dates.md."""

import sys
import threading
import time
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from test_pulse import SID, ROOT, _load, cli, control, memory, pulse, report, store
from test_pulse_followup import followup, live  # noqa: F401
from test_pulse_hours import phone_home
from test_phone import _batch, _event, _sharing

dates = _load("conduit_push.pulse_dates", ROOT / "pulse_dates.py")
ahead = _load("conduit_push.pulse_ahead", ROOT / "pulse_ahead.py")
candidates = _load("conduit_push.pulse_candidates", ROOT / "pulse_candidates.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")
UTC = report.zone_for("UTC")
SAM = {"id": "p_sam", "name": "Sam Taylor", "nickname": "Sam", "relation": "wife", "birthday": "1986-10-28"}
ALEX = {"id": "p_alex", "name": "Alex Kim", "relation": "boss"}


def at(day, hour=0, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def picked(home, *people, events=None):
    """A phone synced on 14 October sharing ``people`` (Settings > Calendar &
    phone > People) and test_phone's calendar, plus ``events``."""
    batch = _batch()
    extra = {"events": {**batch["events"], "upsert": batch["events"]["upsert"] + list(events or [])}}
    return phone_home(home, sharing=_sharing(contacts=True), contacts=list(people), **extra)


def said(live, item, when, chat="work"):
    """``item`` as memory keeps it from the next line of ``chat``; its id."""
    mark = live.store.watermark("", chat)
    after = mark["message_id"] if mark else 0
    ids = live.store.advance("", chat, message_id=after + 1, at=when, after_id=after, items=[{
        "explicit": 1, "due": "", "due_at": None, "message_id": after + 1, "mentioned_at": when, **item}])
    return ids[0] if ids else None


def fact(live, text, evidence, every_year, occasion, *, said_at=None):
    return said(live, {"kind": "fact", "text": text, "evidence": evidence, "every_year": every_year,
                       "occasion": occasion}, at(1, 12, month=9) if said_at is None else said_at)


def looking(live):
    live.store.set("time_zone", "UTC")
    live.store.set("brief_time", "07:00")
    live.store.set("look_ahead", True)


class Model:
    """The look ahead's model: the queued replies in turn (an exception is
    raised), keeping what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked = []

    def __call__(self, instructions, text):
        self.asked.append((instructions, text))
        reply = self.replies.pop(0) if self.replies else {"dates": [], "ahead": []}
        if isinstance(reply, Exception):
            raise reply
        return reply, 50


def look(live, now, model=None, *, budget=100_000):
    done = {}
    ahead.run(live.store, live.home, model or Model(RuntimeError("no model")), zone=UTC, now=now,
              still_leader=lambda: True, budget=budget, done=done)
    return done


def last_run(live):
    return live.store.run_candidates(live.store.get(ahead.LAST)["run"])


# The dates ---------------------------------------------------------------------

def test_a_yearly_date_comes_round_every_year_with_its_lead_times():
    assert dates.parse("03-08") == dates.parse("--03-08") == (None, 3, 8)
    assert dates.parse("1986-03-08") == (1986, 3, 8)
    assert dates.parse("1987-02-29") == (None, 2, 29)  # no 29 February that year: the year goes, the day stays
    assert dates.parse("1850-03-08") == (None, 3, 8)
    for bad in ("02-30", "13-01", "3-8", "", None, 38, "2026-10-14T00:00"):
        assert dates.parse(bad) is None
    today = date(2026, 10, 14)
    assert dates.next_on(10, 14, today) == today and dates.next_on(10, 13, today) == date(2027, 10, 13)
    assert dates.next_on(2, 29, today) == date(2027, 2, 28)  # the 28th in other years
    assert dates.next_on(2, 29, date(2027, 3, 1)) == date(2028, 2, 29)
    sam = dates.Yearly("person:p_sam", dates.BIRTHDAY, 10, 28, 1986, who="Sam", relation="wife")
    trip = dates.Yearly("fact:4", dates.OTHER, 10, 28, None, item={"text": "The user's annual trip to Kyoto.",
                                                                  "evidence": "we go to Kyoto every 28 October"})
    due = lambda found, day: [(entry.days, entry.stage) for entry in dates.coming([found], date(2026, 10, day))]
    assert due(sam, 13) == []  # over two weeks out
    assert [due(sam, day) for day in (14, 26, 27, 28)] == [[(14, "ahead")], [(2, "ahead")], [(1, "eve")], [(0, "day")]]
    assert [due(trip, day) for day in (14, 26, 27, 28)] == [[(14, "")], [(2, "")], [(1, "eve")], [(0, "day")]]
    [entry] = dates.coming([sam], date(2026, 10, 14))
    assert entry.years == 40 and entry.ref == "date:person:p_sam:2026-10-28:ahead"
    assert dates.line(entry) == "Sam's birthday is coming up on Wednesday 28 October (turning 40)."
    assert dates.why(entry) == ("Sam (wife) is one of the people you picked in Settings. Pulse brings up birthdays "
                                "and anniversaries two weeks ahead, the day before and on the day.")
    [eve] = dates.coming([sam], date(2026, 10, 27))
    assert dates.line(eve) == "Sam's birthday is tomorrow, Wednesday 28 October (turning 40)."
    [day] = dates.coming([trip], date(2026, 10, 28))
    assert dates.line(day) == "Coming up today, as you said: “we go to Kyoto every 28 October”"
    assert dates.why(day, "1 September 2026") == ("You said it on 1 September 2026. Pulse brings up dates that come "
                                                  "back every year the day before and on the day.")
    wedding = dates.Yearly("fact:5", dates.ANNIVERSARY, 11, 3, 2025, item={"text": "x", "evidence": "y"})
    assert dates.years_words(dates.coming([wedding], date(2026, 11, 2))[0]) == " (1 year)"
    assert dates.words("2016-06-03") == "3 June, since 2016" and dates.words("06-03") == "3 June"


def test_the_next_lead_time_skips_the_ones_already_decided():
    sam = dates.Yearly("person:p_sam", dates.BIRTHDAY, 10, 28, None, who="Sam")
    today = date(2026, 10, 14)
    refs = dates.refs_round(sam, today)
    assert dates.next_due(sam, today) == (today, dates.AHEAD)
    assert dates.next_due(sam, date(2026, 10, 1)) == (today, dates.AHEAD)
    assert dates.next_due(sam, today, refs[:1]) == (date(2026, 10, 27), dates.EVE)
    assert dates.next_due(sam, date(2026, 10, 27), refs[:2]) == (date(2026, 10, 28), dates.DAY)
    assert dates.next_due(sam, date(2026, 10, 28), refs) == (date(2027, 10, 14), dates.AHEAD)


def test_a_birthday_said_in_a_chat_and_a_picked_persons_count_once(live):
    picked(live.home, SAM, ALEX)
    birthday = fact(live, "Sam's birthday is 28 October.", "Sam's birthday is the 28th of October", "10-28", "birthday")
    wedding = fact(live, "The user's wedding anniversary is 3 November.", "we married on 3 November 2016",
                   "2016-11-03", "anniversary")
    found = dates.yearly(live.store, live.home)
    assert [(entry.key, entry.occasion, entry.year) for entry in found] == [
        ("person:p_sam", "birthday", 1986), (f"fact:{wedding}", "anniversary", 2016)]
    assert found[0].what == "Sam's birthday" and found[0].relation == "wife"
    assert f"fact:{birthday}" not in [entry.key for entry in found]
    assert dates.people(live.home / "nothing") == []  # no phone synced


# Memory ------------------------------------------------------------------------

def test_a_fact_about_a_yearly_date_stays_open_and_said_again_picks_up_its_date(live):
    now = time.time()
    old = said(live, {"kind": "fact", "text": "The user's mum's birthday is 3 March.",
                      "evidence": "Mum's birthday is 3 March", "due": "2027-03-03", "due_at": now - 10 * 86400},
               now - 90 * 86400)
    # Said again with its yearly date: it no longer has a due date, so it never expires.
    said(live, {"kind": "fact", "text": "x", "evidence": "y", "same_as": old, "every_year": "03-03",
                "occasion": "birthday"}, now)
    item = live.store.item(old)
    assert (item["every_year"], item["occasion"], item["due"], item["due_at"]) == ("03-03", "birthday", "", None)
    live.store.expire_items(due_before=now + 400 * 86400, now=now)
    assert live.store.item(old)["status"] == "open"
    assert [entry["id"] for entry in live.store.yearly_facts()] == [old]
    # A repeat that gives it a date once ("the party's on the 6th") leaves it yearly, with no date to expire on.
    said(live, {"kind": "fact", "text": "x", "evidence": "y", "same_as": old, "due": "2027-03-06",
                "due_at": now + 30 * 86400}, now + 1)
    item = live.store.item(old)
    assert (item["every_year"], item["due"], item["due_at"], item["mentioned_at"]) == ("03-03", "", None, now + 1)
    live.store.expire_items(due_before=now + 400 * 86400, now=now)
    assert live.store.item(old)["status"] == "open"
    # Done on a card drawn from it marks the card: the date comes round again.
    [card] = live.store.add_cards([{"kind": learning.NOTICED, "level": "screen", "item_id": old, "news": "n",
                                    "created_at": now}])
    assert live.store.mark_done(card, old, closed_by=learning.DONE_BY, now=now)
    assert live.store.item(old)["status"] == "open" and live.store.done_marks([card])
    assert f"#{old} [fact] The user's mum's birthday is 3 March. (every year on 3 March) " \
        in report.remembered(live.store, UTC, now)
    # `pulse memory` and What Pulse remembers in the app say so too.
    assert memory.describe(live.store.item(old), UTC).startswith(
        f"#{old} The user's mum's birthday is 3 March. (every year on 3 March)\n")
    [listed] = [entry for entry in control.items(live.store) if entry["id"] == old]
    assert (listed["every_year"], listed["occasion"], listed["due"]) == ("03-03", "birthday", "")


# The look ahead ----------------------------------------------------------------

def test_it_runs_once_a_day_from_an_hour_before_the_brief_and_not_while_quiet(live):
    looking(live)
    assert not ahead.due(live.store, UTC, at(14, 5, 59)) and ahead.due(live.store, UTC, at(14, 6))
    live.store.set(ahead.DAY_KEY, "2026-10-14")
    assert not ahead.due(live.store, UTC, at(14, 9)) and ahead.due(live.store, UTC, at(15, 6))
    assert ahead.due(live.store, UTC, at(14, 9), asked=True)  # `pulse ahead now`
    live.store.set("quiet_days", [3])  # Thursdays
    assert not ahead.due(live.store, UTC, at(15, 6)) and ahead.due(live.store, UTC, at(16, 6))
    assert ahead.due(live.store, UTC, at(15, 6), asked=True)  # asked, it goes while quiet, as an asked report does
    live.store.set("look_ahead", False)
    assert not ahead.due(live.store, UTC, at(16, 6)) and not ahead.due(live.store, UTC, at(16, 6), asked=True)


def test_a_birthday_two_weeks_out_comes_up_in_codes_words_without_a_model(live):
    looking(live)
    picked(live.home, SAM, ALEX)
    done = look(live, at(14, 6, 30))
    [card] = live.store.cards()
    assert (card["kind"], card["level"], card["topic"], card["item_id"], card["chat_id"]) == (
        learning.NOTICED, "brief", "date", None, "")
    assert card["news"] == "Sam's birthday is coming up on Wednesday 28 October (turning 40)."
    assert card["title"] == card["news"] and card["why"].startswith("Sam (wife) is one of the people you picked")
    [row] = last_run(live)
    assert (row["origin"], row["decision"], row["floor"], row["card_id"], row["ref"]) == (
        candidates.DATE, candidates.SURFACED, 1, card["id"], "date:person:p_sam:2026-10-28:ahead")
    last = live.store.get(ahead.LAST)
    assert (last["dates"], last["picked"], last["held"], last["error"]) == (1, 1, True, "RuntimeError")
    assert done["failed"] == 1 and done["tokens"] > 0
    # Just made: the held check reads it hours on, not in the same run.
    assert followup._times(live.store.get(followup.HELD_CHECKED)) == {str(card["id"]): at(14, 6, 30)}
    assert live.store.get(ahead.DAY_KEY) == "2026-10-14"
    # Each lead time comes up once: not again the next day, then the day before and on the day.
    look(live, at(15, 6, 30))
    assert len(live.store.cards()) == 1
    look(live, at(27, 6, 30))
    look(live, at(28, 6, 30))
    news = [card["news"] for card in live.store.cards()]
    assert news[:2] == ["Sam's birthday is today (turning 40).",
                        "Sam's birthday is tomorrow, Wednesday 28 October (turning 40)."]


def test_a_date_the_model_answers_without_words_comes_up_in_codes(live):
    looking(live)
    picked(live.home, SAM)
    look(live, at(14, 6, 30), Model({"dates": [{"ref": "D1", "bring_up": True, "line": 42},
                                               {"ref": "D1", "line": "Twice."}], "ahead": "none"}))
    [card] = live.store.cards()
    assert card["news"] == "Sam's birthday is coming up on Wednesday 28 October (turning 40)."
    [row] = last_run(live)
    assert (row["decision"], row["value"], row["text"]) == (candidates.SURFACED, 4, card["news"])
    assert live.store.get(ahead.LAST)["error"] == ""


def test_the_model_words_the_dates_and_adds_what_needs_getting_ready_for(live):
    looking(live)
    kyoto = _event("e_kyoto", title="Kyoto", all_day=True, start="2026-10-24T00:00:00+01:00",
                   end="2026-11-01T00:00:00+00:00", location=None, lat=None, lon=None, notes=None, attendees=[])
    picked(live.home, SAM, events=[kyoto])
    wedding = fact(live, "The user's wedding anniversary is 21 October.", "we married on 21 October 2016",
                   "2016-10-21", "anniversary")
    now = at(14, 6, 30)
    suit = said(live, {"kind": "commitment", "text": "The user will pick up the suit before the trip.",
                       "evidence": "pick up the suit before Kyoto", "due": "2026-10-23", "due_at": at(23, 23, 59)},
                now - 3600)
    model = Model({"dates": [
        {"ref": "D2", "bring_up": True, "value": 5, "skip": "minor", "line": "Sam turns 40 on Wednesday 28 October."},
        {"ref": "D1", "bring_up": False, "skip": "done", "line": "Your anniversary is next Wednesday."}],
        "ahead": [
        {"kind": "date", "bring_up": True, "value": 4, "line": "Kyoto starts Saturday 24 October: time to pack.",
         "why": "Your calendar has Kyoto from Saturday.", "due": "2026-10-24", "source": "calendar", "area": "travel"},
        {"kind": "commitment", "bring_up": True, "value": 4, "line": "Pick up the suit before Kyoto.",
         "why": "You said you would.", "ref": f"#{suit}"},
        {"kind": "other", "bring_up": True, "value": 4, "line": ""}]})
    done = look(live, now, model)
    [(instructions, text)] = model.asked
    assert '"dates"' in instructions and text.startswith("Pulse look ahead.")
    assert ('D1 [anniversary] The user\'s wedding anniversary is 21 October. (the user said: "we married on 21 '
            'October 2016", 1 Sep 2026): Wednesday 21 October, in 7 days (10 years).') in text
    assert ("D2 [birthday] Sam's birthday (wife, one of the people the user picked in Conduit): Wednesday 28 "
            "October, in 14 days (turning 40). Bring it up today: two weeks ahead.") in text
    assert "- Sat 24 Oct, all day, to Sat 31 Oct: Kyoto" in text and "- Fri 16 Oct 15:00: Dentist" in text
    assert "- Buy milk (due Thu 15 Oct 09:00)" in text and "File taxes" not in text  # due after the two weeks
    assert f"#{suit} [commitment] The user will pick up the suit before the trip. (due 2026-10-23)" in text
    assert f"#{wedding} " not in text.split("## What Pulse remembers")[-1]  # the dates' own section has it
    assert done == {"tokens": 50, "checks": 1}
    cards = {card["news"]: card for card in live.store.cards()}
    # "minor" isn't a reason to leave a birthday out: it's still a floor, in the model's words.
    assert set(cards) == {"Sam turns 40 on Wednesday 28 October.", "Kyoto starts Saturday 24 October: time to pack.",
                          "Pick up the suit before Kyoto."}
    birthday = cards["Sam turns 40 on Wednesday 28 October."]
    assert birthday["level"] == "brief" and birthday["why"].startswith("Sam (wife) is one of the people")
    trip = cards["Kyoto starts Saturday 24 October: time to pack."]
    assert (trip["item_id"], trip["topic"], trip["why"]) == (None, "date", "Your calendar has Kyoto from Saturday.")
    reminder = cards["Pick up the suit before Kyoto."]
    assert (reminder["item_id"], reminder["chat_id"], reminder["topic"]) == (suit, "work", "commitment")
    rows = {row["text"]: row for row in last_run(live)}
    assert rows["Your anniversary is next Wednesday."]["decision"] == candidates.SKIPPED
    assert rows["Your anniversary is next Wednesday."]["ref"] == f"date:fact:{wedding}:2026-10-21:ahead"
    # What it left out is listed as the look ahead's, and comes up with Bring it up.
    [left] = [entry for entry in control.left_out(live.store, live.home, now=now)
              if entry["text"] == "Your anniversary is next Wednesday."]
    assert left["from"] == control.LOOK_AHEAD and left["decision"] == candidates.SKIPPED
    card = control.bring_up(live.store, left["id"], live.home, now=now)
    assert card["chat_id"] == "" and card["news"] == "Your anniversary is next Wednesday."


def test_a_fact_dates_card_points_at_the_chat_it_was_said_in_without_the_fact(live):
    looking(live)
    wedding = fact(live, "The user's wedding anniversary is 15 October.", "our anniversary is 15 October",
                   "10-15", "anniversary")
    look(live, at(14, 6, 30))
    [card] = live.store.cards()
    assert card["news"] == "Coming up tomorrow, Thursday 15 October, as you said: “our anniversary is 15 October”"
    assert (card["item_id"], card["chat_id"], card["message_id"], card["quote"]) == (
        None, "work", None, "our anniversary is 15 October")
    assert card["why"] == ("You said it on 1 September 2026. Pulse brings up birthdays and anniversaries two weeks "
                           "ahead, the day before and on the day.")
    # Learned about as a date, by its area, not by that chat.
    [row] = last_run(live)
    assert (row["chat_id"], row["place"], row["area"]) == ("", "area:personal", "personal")
    assert live.store.mark_done(card["id"], card["item_id"], closed_by=learning.DONE_BY, now=at(14, 7))
    assert live.store.item(wedding)["status"] == "open"


def test_after_the_brief_went_what_it_brings_up_goes_on_the_screen(live, monkeypatch):
    looking(live)
    # Noon today, the store's clock too: an hour either side stays today whenever the suite runs (between 00:00
    # and 01:00 UTC "an hour ago" was yesterday, so the brief was still ahead).
    now = datetime.combine(datetime.now(timezone.utc).date(), datetime.min.time(), timezone.utc).timestamp() \
        + 12 * 3600
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: now))
    live.store.set("brief_time", datetime.fromtimestamp(now - 3600, timezone.utc).strftime("%H:%M"))
    assert ahead.brief_ahead(live.store, UTC, now)  # within the brief's window, none sent yet
    live.store.add_attempt(attempt_id="b:1", item_id="b", attempt=1, kind=pulse.BRIEF, tag="t", text="x",
                           session_key=SID)
    assert not ahead.brief_ahead(live.store, UTC, now)  # one on its way
    live.store.update_attempt("b:1", state=store.DELIVERED)
    assert not ahead.brief_ahead(live.store, UTC, now)
    live.store.set("quiet_until", now + 86400)
    live.store.set("brief_time", datetime.fromtimestamp(now + 3600, timezone.utc).strftime("%H:%M"))
    assert not ahead.brief_ahead(live.store, UTC, now)  # quiet at its time: no brief today
    live.store.set("quiet_until", None)
    assert ahead.brief_ahead(live.store, UTC, now) or datetime.fromtimestamp(now + 3600, timezone.utc).day != \
        datetime.fromtimestamp(now, timezone.utc).day
    live.store.set("brief_time", datetime.fromtimestamp(now - 3600, timezone.utc).strftime("%H:%M"))
    today = datetime.fromtimestamp(now, timezone.utc).date()
    fact(live, "The user's anniversary.", "our anniversary is tomorrow", (today + timedelta(days=1)).strftime("%m-%d"),
         "anniversary")
    look(live, now)
    [card] = live.store.cards()
    assert card["level"] == "screen" and live.store.get(ahead.LAST)["held"] is False


def test_with_the_budget_spent_the_dates_still_come_up_and_nothing_is_asked(live):
    looking(live)
    picked(live.home, SAM)
    model = Model()
    done = look(live, at(27, 6, 30), model, budget=0)
    assert model.asked == [] and done == {}
    [card] = live.store.cards()
    assert card["news"] == "Sam's birthday is tomorrow, Wednesday 28 October (turning 40)."
    assert live.store.get(ahead.LAST)["error"] == "budget"


def test_nothing_ahead_asks_nothing_and_a_run_that_stops_leading_gives_the_day_back(live):
    looking(live)
    model = Model()
    look(live, at(14, 6, 30), model)
    assert model.asked == [] and live.store.cards() == [] and live.store.get(ahead.LAST)["run"] == ""
    picked(live.home, SAM)
    live.store.set(ahead.DAY_KEY, "2026-10-13")
    ahead.run(live.store, live.home, model, zone=UTC, now=at(14, 6, 30), still_leader=lambda: False, budget=1e9,
              done={})
    assert model.asked == [] and live.store.cards() == [] and live.store.get(ahead.DAY_KEY) == "2026-10-13"


def test_a_held_look_ahead_card_no_brief_took_goes_to_the_screen_at_midnight(live):
    looking(live)
    now = time.time()
    yesterday = datetime.fromtimestamp(now - 86400, timezone.utc)
    live.store.set("brief_time", (yesterday + timedelta(hours=1)).strftime("%H:%M"))
    fact(live, "The user's anniversary.", "our anniversary is tomorrow",
         (yesterday + timedelta(days=1)).strftime("%m-%d"), "anniversary")
    look(live, now - 86400)
    [card] = live.store.cards()
    held = live.store.add_cards([{"kind": "followup", "level": "brief", "news": "held", "created_at": now - 86400}])
    assert card["level"] == "brief"
    live.store.set("brief_time", datetime.fromtimestamp(now + 10 * 3600, timezone.utc).strftime("%H:%M"))
    pulse.tick()
    assert live.store.card(card["id"])["level"] == "screen"
    assert live.store.card(held[0])["level"] == "brief"  # not the look ahead's: it waits for a brief


def test_the_memory_run_looks_ahead_once_a_day_on_the_follow_up_budget(live, monkeypatch):
    looking(live)
    live.store.set("memory", True)
    live.store.set("followups", False)
    now = time.time()
    live.store.set("brief_time", datetime.fromtimestamp(now + 1800, timezone.utc).strftime("%H:%M"))
    today = datetime.fromtimestamp(now, timezone.utc).date()
    fact(live, "The user's anniversary.", "our anniversary", (today + timedelta(days=1)).strftime("%m-%d"),
         "anniversary")
    asked = []
    monkeypatch.setattr(pulse.memory, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse, "_memory_llm", lambda instructions, text, purpose="", route=None: (
        asked.append(purpose), ({"dates": [], "ahead": []}, 70))[1])
    monkeypatch.setattr(pulse, "_leads", lambda pulse_store: True)
    if datetime.fromtimestamp(now + 1800, timezone.utc).date() != today:
        pytest.skip("the brief's time is tomorrow: the look ahead isn't due until an hour before it")
    pulse._memory_run(live.home, SID, now, threading.Event())
    assert asked == ["pulse look ahead"] and len(live.store.cards()) == 1
    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    assert live.store.get("followup_today") == {"day": day, "tokens": 70}
    assert live.store.get("followup_usage") == {"checks": 1, "tokens": 70, "failed": 0}
    pulse._memory_run(live.home, SID, now + 300, threading.Event())
    assert asked == ["pulse look ahead"]  # once a day
    live.store.set(ahead.REQUESTED, True)
    pulse._memory_run(live.home, SID, now + 600, threading.Event())
    assert asked == ["pulse look ahead"] * 2 and len(live.store.cards()) == 1  # asked again: the date came up already


# Settings and commands ---------------------------------------------------------

def test_the_look_ahead_is_a_setting_and_its_state(live):
    assert control.state(live.store, live.home)["look_ahead"] is False  # the tests keep it off the clock
    control.update(live.store, {"look_ahead": True}, live.home)
    assert live.store.get("look_ahead") is True and control.state(live.store, live.home)["look_ahead"] is True
    with pytest.raises(control.Refused):
        control.update(live.store, {"look_ahead": "yes"}, live.home)
    assert store.DEFAULTS["look_ahead"] is True


def test_the_commands_list_the_dates_and_run_the_look_ahead(live, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    looking(live)
    live.store.set("memory", True)
    run = lambda action, value="": cli._pulse(types.SimpleNamespace(pulse_action=action, value=value, extra=""))
    assert run("dates") == 0
    assert "Pulse knows no dates that come back every year yet" in capsys.readouterr().out
    today = datetime.fromtimestamp(time.time(), UTC).date()
    soon = today + timedelta(days=20)
    item_id = fact(live, "The user's wedding anniversary.", "we married in 2016", f"2016-{soon:%m-%d}", "anniversary")
    assert run("dates") == 0
    out = capsys.readouterr().out
    years = soon.year - 2016
    assert (f"- The user's wedding anniversary. (remembered #{item_id}): {dates.day_words(soon)} {soon.year} "
            f"({years} years); comes up {dates.day_words(soon - timedelta(days=14))}, two weeks ahead") in out
    assert run("ahead", "off") == 0 and live.store.get("look_ahead") is False
    assert "The look ahead is off" in capsys.readouterr().out
    assert run("ahead", "now") == 1 and "Off: the look ahead" in capsys.readouterr().out
    assert run("dates") == 0 and "They come up with the look ahead. Look ahead: off" in capsys.readouterr().out
    assert run("ahead", "on") == 0 and live.store.get("look_ahead") is True
    assert run("ahead", "now") == 0
    assert live.store.get(ahead.REQUESTED) is True and live.store.get("memory_requested") is True
    capsys.readouterr()
    assert run("ahead") == 0 and "Look ahead: on; it hasn't run yet" in capsys.readouterr().out
    look(live, time.time())
    assert run("ahead") == 0
    out = capsys.readouterr().out
    assert "Look ahead: on; last ran " in out and "nothing brought up" in out
    assert run("ahead", "soon") == 2
    run("status")
    assert "Look ahead: on; last ran " in capsys.readouterr().out
