"""The morning brief leaves out what a card still says (Eric, 2026-10-30:
the RAPIDS appointment left open on Thursday came back in Friday's brief,
and the Volcengine plan's end with it, neither due that day)."""

import time

from test_pulse import SID, pulse
from test_pulse_dates import at
from test_pulse_followup import commitment, live  # noqa: F401
from test_pulse_hours import phone_home
from test_pulse_next import UTC
from test_pulse_reminder_once import card_for


def brief(live, now):
    return pulse.brief.context(live.home, live.store, [], home_chat_id=SID, now=now, zone=UTC)


def card_about(live, item_id, created_at):
    [card] = live.store.add_cards([{"kind": pulse.store.NOTICED, "item_id": item_id, "level": "screen",
                                    "title": "x", "news": "x", "why": "x", "created_at": created_at}])
    return card


def test_a_remembered_item_whose_card_is_open_stays_out_of_the_brief(live):
    live.store.set("memory", True)
    now = time.time()
    rapids = commitment(live, "The user will get their RAPIDS ID card at the appointment.", due="2026-12-01",
                        due_at=now + 30 * 86400, words="I have my RAPIDS ID card appointment")
    passport = commitment(live, said_at=now - 7200)
    card = card_about(live, rapids, now - 20 * 3600)
    text = brief(live, now)
    assert "RAPIDS" not in text and "renew their passport" in text
    live.store.mark_done(card, None, closed_by="user", now=now - 60)  # its card done, the item still open
    assert "RAPIDS" in brief(live, now)
    assert passport


def test_on_its_day_it_comes_in_the_brief_once(live):
    live.store.set("memory", True)
    now = time.time()
    rapids = commitment(live, "The user will get their RAPIDS ID card at the appointment.",
                        due="2026-10-30T15:00", due_at=now + 3 * 3600, words="I have my RAPIDS ID card appointment")
    card_about(live, rapids, now - 3 * 86400)
    assert "RAPIDS" in brief(live, now)  # its day: once more
    card_about(live, rapids, now - 3600)  # that came today already
    assert "RAPIDS" not in brief(live, now)


def test_what_a_card_says_isnt_related_to_todays_events_either(live, monkeypatch):
    live.store.set("memory", True)
    now = time.time()
    rapids = commitment(live, "The user will get their RAPIDS ID card.", due_at=now + 30 * 86400,
                        words="RAPIDS ID card")
    monkeypatch.setattr(pulse.places, "today_events", lambda *a: [{"title": "RAPIDS ID card", "start": ""}])
    assert pulse.places.RELATED_TITLE in brief(live, now)
    card_about(live, rapids, now - 20 * 3600)
    text = brief(live, now)
    assert pulse.places.RELATED_TITLE not in text and "RAPIDS" not in text


def test_a_phone_reminder_or_calendar_entry_whose_card_is_open_stays_out(live):
    phone_home(live.home)  # 07:00 UTC on the 14th: Buy milk due the 15th, File taxes the 31st; standup and review today
    now = at(14, 7)
    text = brief(live, now)
    assert "File taxes" in text and "Buy milk" in text and "Design review" in text and "Standup" in text
    card_for(live, "reminder:r_tax", created_at=now - 2 * 86400)
    card_for(live, "event:e_review:2026-10-14", created_at=now - 600)  # its day's card came already
    text = brief(live, now)
    assert "File taxes" not in text and "Design review" not in text
    assert "Buy milk" in text and "Standup" in text
    # Each list says some are on cards, so a list never reads as emptier than it is.
    assert text.count(pulse.report.ON_CARDS.format(count=1)) == 2


def test_a_list_left_empty_by_cards_doesnt_read_as_empty(live):
    phone_home(live.home)
    now = at(14, 7)
    for event in ("e_standup", "e_review", "e_lunch", "e_holiday"):
        card_for(live, f"event:{event}:2026-10-14", created_at=now - 600)
    for reminder in ("r_milk", "r_tax"):
        card_for(live, f"reminder:{reminder}", created_at=now - 600)
    text = brief(live, now)
    assert "Nothing on the calendar" not in text and "## Open reminders\nNone." not in text
    assert pulse.report.ON_CARDS.format(count=2) in text


def test_a_reminder_put_away_for_good_stays_out(live):
    phone_home(live.home)
    now = at(14, 7)
    card = card_for(live, "reminder:r_tax", created_at=now - 2 * 86400)
    live.store.dismiss_card(card, True, now - 3600)  # put away for good: no rest
    assert "File taxes" not in brief(live, now)


def test_one_on_a_card_isnt_related_to_today_however_many_are_open(live, monkeypatch):
    live.store.set("memory", True)
    now = time.time()
    for n in range(45):  # due before it, so the report's first ones are all these
        commitment(live, f"The user will file form {n}.", due_at=now + (n + 1) * 3600, words=f"form {n}")
    rapids = commitment(live, "The user will get their RAPIDS ID card.", words="RAPIDS ID card")
    assert rapids not in {item["id"] for item in pulse.report.remembered_items(live.store, now)[0]}
    monkeypatch.setattr(pulse.places, "today_events", lambda *a: [{"title": "RAPIDS ID card", "start": ""}])
    assert "RAPIDS" in brief(live, now)
    card_about(live, rapids, now - 20 * 3600)
    assert "RAPIDS" not in brief(live, now)


def test_items_on_cards_dont_crowd_out_one_that_isnt(live):
    live.store.set("memory", True)
    now = time.time()
    for n in range(45):  # all due first, all on cards
        card_about(live, commitment(live, f"The user will file form {n}.", due_at=now + (n + 1) * 3600,
                                    words=f"form {n}"), now - 20 * 3600)
    commitment(live, "The user will get their RAPIDS ID card.", words="RAPIDS ID card")
    text = brief(live, now)
    assert "RAPIDS" in text and "file form" not in text and pulse.brief.NOTHING_NEW_OPEN not in text


def test_when_all_it_keeps_open_is_on_cards_the_brief_says_so(live):
    live.store.set("memory", True)
    now = time.time()
    rapids = commitment(live, "The user will get their RAPIDS ID card.", due_at=now + 30 * 86400)
    card_about(live, rapids, now - 20 * 3600)
    text = brief(live, now)
    assert pulse.brief.NOTHING_NEW_OPEN in text and "RAPIDS" not in text
