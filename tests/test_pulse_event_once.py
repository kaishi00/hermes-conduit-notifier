"""One card per calendar entry: an entry on the phone's calendar the
candidate list or the look ahead brings up gets one card, as a phone
reminder does (Eric, 2026-10-29: three cards for one birthday on the
calendar, the look ahead's on the Tuesday and the Thursday and the report's
of the one held for a card shop)."""

import time

import pytest

from test_pulse import SID, report
from test_pulse_candidates import gathered  # noqa: F401
from test_pulse_dates import Model, ahead, at, last_run, look, looking, picked
from test_pulse_followup import commitment, live  # noqa: F401
from test_pulse_hours import phone_home
from test_pulse_next import UTC, candidate, candidates
from test_pulse_reminder_once import card_for
from test_phone import _event

KWAN = {"id": "e_kwan", "title": "Kwan Cheung Ho’s Birthday", "start": "2026-11-06T00:00:00+00:00",
        "end": "2026-11-07T00:00:00+00:00", "all_day": True}
FOUND = {"kind": "date", "bring_up": True, "value": 4, "skip": "", "actionable": True,
         "line": "Kwan Cheung Ho's 41st birthday is on Friday 6 November, worth having a card in hand by then.",
         "why": "It's on your calendar.", "source": "calendar", "area": "social", "due": "2026-11-06"}
REF = "event:e_kwan:2026-11-06"


def follows(events=None, upcoming=()):
    return report.Gathered("the context", "Thu 29 Oct 08:00", [], [], {}, UTC, events={} if events is None else events,
                           upcoming=list(upcoming))


def test_the_list_gives_each_calendar_entry_a_key_and_reads_two_weeks_for_titles(live):
    phone_home(live.home)  # 08:00 in London on the 14th: the standup, the review, lunch, the holiday; the dentist on the 16th
    now = at(14, 7)
    found = report.gather(live.home, live.store, home_chat_id=SID, now=now, keyed=True)
    assert found.events and all(key.startswith("v") for key in found.events)
    for key, event in found.events.items():
        assert f"\n- {key} {event['title']}" in found.text
    assert "e_dentist" not in {event["id"] for event in found.events.values()}  # past the report's two days
    assert "e_dentist" in {event["id"] for event in found.upcoming}  # but named by its title
    plain = report.gather(live.home, live.store, home_chat_id=SID, now=now)
    assert plain.events == {} and plain.upcoming == [] and "\n- Standup (" in plain.text


@pytest.mark.parametrize("key, ref", [("v1", REF), (" V1 ", REF), ("v2", None), ("", None), (None, None)])
def test_a_found_candidate_about_a_listed_entry_carries_it(key, ref):
    row = candidates.found_row({**FOUND, "line": "A card for the party.", "event": key}, {},
                               follows=follows({"v1": KWAN}))
    assert row.get("ref") == ref
    if ref:
        assert row["source"] == "calendar"


@pytest.mark.parametrize("entry, ref", [
    ({}, REF),  # its title's words are all in the line, the curly apostrophe too
    ({"source": "chat"}, REF),  # whatever source the model gave
    ({"line": "Get a card for Kwan.", "why": "Kwan Cheung Ho's birthday is on your calendar."}, REF),
    ({"line": "Get a card for Kwan's birthday."}, None),  # not every word of the title
])
def test_one_with_no_key_is_matched_by_the_title_of_an_entry_in_the_next_two_weeks(entry, ref):
    row = candidates.found_row({**FOUND, **entry}, {}, follows=follows(upcoming=[KWAN]))
    assert row.get("ref") == ref


def test_a_title_of_one_word_or_two_entries_named_match_nothing():
    standup = {"id": "e_standup", "title": "Standup", "start": "2026-10-30T09:30:00+00:00"}
    row = candidates.found_row({**FOUND, "line": "Standup moved to 10."}, {}, follows=follows(upcoming=[standup]))
    assert "ref" not in row
    party = {"id": "e_party", "title": "Kwan Cheung Ho’s Birthday", "start": "2026-11-07T19:00:00+00:00"}
    row = candidates.found_row(FOUND, {}, follows=follows(upcoming=[KWAN, party]))
    assert "ref" not in row


def test_a_repeating_entry_is_matched_on_the_day_it_is_due():
    weekly = [{"id": "e_yoga", "title": "Yoga class", "start": f"2026-11-0{day}T18:00:00+00:00"} for day in (2, 9)]
    row = candidates.found_row({**FOUND, "line": "Bring your mat to yoga class.", "due": "2026-11-09"}, {},
                               follows=follows(upcoming=weekly))
    assert row["ref"] == "event:e_yoga:2026-11-09"
    row = candidates.found_row({**FOUND, "line": "Bring your mat to yoga class.", "due": ""}, {},
                               follows=follows(upcoming=weekly))
    assert row["ref"] == "event:e_yoga:2026-11-02"  # the soonest


def test_a_next_step_keeps_what_it_follows():
    gathered_ = report.Gathered("x", "y", [], [], {}, UTC, {}, {"e1": {"id": "e_x"}}, {}, {"v1": KWAN}, [KWAN])
    row = candidates.found_row({**FOUND, "kind": "next", "after": "e1", "event": "v1"}, {}, follows=gathered_)
    assert row["ref"] == "after:event:e_x"


def decided(live, now, ref=REF):
    rows = [candidate(kind="date", ref=ref, value=5), candidate(kind="date", ref="event:e_other:2026-11-01", value=5)]
    candidates.decide(live.store, rows, now=now)
    return [(row["decision"], row["skip"]) for row in rows]


def test_an_entry_coming_closer_comes_up_once_a_week(live):
    now = time.time()
    card_id = card_for(live, REF, created_at=now - 2 * 86400)
    live.store.mark_done(card_id, None, closed_by="user", now=now - 86400)
    assert decided(live, now) == [(candidates.SKIPPED, "raised"), (candidates.SURFACED, "")]
    assert decided(live, now + candidates.REMINDER_AGAIN_S) == [(candidates.SURFACED, "")] * 2


def test_an_entry_held_for_a_shop_isnt_brought_up_again_meanwhile(live):
    # The look ahead held it for a card shop at 04:04; the 08:00 report found it again.
    now = time.time()
    card_for(live, REF, created_at=now - 4 * 3600, level="moment")
    assert decided(live, now)[0] == (candidates.SKIPPED, "raised")


def test_an_entry_whose_card_is_still_open_comes_up_again_only_on_its_day(live):
    now = time.time()
    card_for(live, REF, created_at=now - 20 * 86400)
    assert decided(live, now)[0] == (candidates.SKIPPED, "raised")
    rows = [candidate(kind="date", ref=REF, value=5, due_at=now + 3600)]
    candidates.decide(live.store, rows, now=now)
    assert (rows[0]["decision"], rows[0]["skip"]) == (candidates.SURFACED, "")
    card_for(live, REF, created_at=now - 600)  # that one went out: not again that day
    rows = [candidate(kind="date", ref=REF, value=5, due_at=now + 3600)]
    candidates.decide(live.store, rows, now=now)
    assert rows[0]["skip"] == "raised"


@pytest.mark.parametrize("more", [{"rating": "down"}, {"dismissed_at": 1.0}])
def test_an_entry_whose_card_was_put_away_stays_away(live, more):
    now = time.time()
    card_for(live, REF, created_at=now - 30 * 86400, **more)
    assert decided(live, now)[0] == (candidates.SKIPPED, "raised")


def test_the_list_asks_which_entry_each_is_about():
    seen = {}

    def llm(instructions, text):
        seen.update(instructions=instructions)
        return {"remembered": [], "found": []}, 1

    candidates.ask(llm, gathered(), nudge="balanced")
    assert '"event": the key of the calendar entry it is about (like v1)' in seen["instructions"]


def test_pulse_candidates_says_which_were_about_a_calendar_entry():
    row = {"value": 4, "item_id": None, "ref": REF, "origin": candidates.MODEL, "source": "calendar",
           "skip": "raised", "why": "", "kind": "date", "text": "x", "decision": candidates.SKIPPED, "card_id": None}
    assert "(a calendar entry)" in candidates.describe(row)


# The look ahead -----------------------------------------------------------------

BIRTHDAY = _event("e_kwan", title="Kwan Cheung Ho’s Birthday", all_day=True, start="2026-10-24T00:00:00+01:00",
                  end="2026-10-25T00:00:00+01:00", location=None, lat=None, lon=None, notes=None, url=None,
                  attendees=[], availability="free")
SAID = {"kind": "date", "bring_up": True, "value": 4, "skip": "", "why": "It's on your calendar.",
        "due": "2026-10-24", "ref": None, "source": "calendar", "area": "social"}


def test_the_look_ahead_keys_its_calendar_and_brings_an_entry_up_once(live):
    looking(live)
    picked(live.home, events=[BIRTHDAY])
    first = Model({"dates": [], "ahead": [{**SAID, "event": "v6",
                                           "line": "Kwan Cheung Ho's birthday is on Saturday 24 October."}]})
    look(live, at(14, 6, 30), first)
    assert "\n- v6 Sat 24 Oct, all day: Kwan Cheung Ho\u2019s Birthday" in first.asked[0][1]
    assert '"event" (ahead only): the key of the calendar entry' in first.asked[0][0]
    [row] = last_run(live)
    assert row["ref"] == "event:e_kwan:2026-10-24" and row["decision"] == candidates.SURFACED
    # Two days on it says it again, in other words and with no key: still the same entry.
    again = Model({"dates": [], "ahead": [{**SAID, "line": "Kwan Cheung Ho's birthday is Saturday 24 October: "
                                                         "worth having a card in hand by then."}]})
    look(live, at(16, 6, 30), again)
    [row] = last_run(live)
    assert row["ref"] == "event:e_kwan:2026-10-24"
    assert (row["decision"], row["skip"]) == (candidates.SKIPPED, "raised")


def test_the_look_ahead_lists_entries_unkeyed_where_it_cant_key_them():
    assert ahead._event_line({"title": "Kyoto"}) == "- Kyoto"
    assert ahead._event_line({"title": "Kyoto"}, "v2") == "- v2 Kyoto"


# Remembered items ------------------------------------------------------------------

def remembered(live, item_id, **said):
    return candidates.remembered_row(live.store.item(item_id), {"bring_up": True, "value": 5, "line": "Renew it",
                                                                **said})


@pytest.mark.parametrize("level", ["screen", "push", "brief", "moment"])
def test_a_remembered_item_whose_card_is_still_open_doesnt_come_up_again(live, level):
    # Eric, 2026-10-29: the relay token's "window is opening" in the brief, then "has arrived" as a push.
    now = time.time()
    item_id = commitment(live, due="2026-11-30", due_at=now + 20 * 86400)
    [card] = live.store.add_cards([{"kind": candidates.store.NOTICED, "item_id": item_id, "level": level,
                                    "title": "x", "news": "x", "why": "x", "created_at": now - 33 * 3600}])
    rows = [remembered(live, item_id)]
    candidates.decide(live.store, rows, now=now)
    assert (rows[0]["decision"], rows[0]["skip"]) == (candidates.SKIPPED, "raised")
    live.store.mark_done(card, None, closed_by="user", now=now - 60)  # its card done, the item still open
    rows = [remembered(live, item_id)]
    candidates.decide(live.store, rows, now=now)
    assert rows[0]["decision"] == candidates.SURFACED
