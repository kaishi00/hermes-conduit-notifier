"""An open card with nothing to key on (no remembered item, no reminder or
calendar ref) still keeps a candidate saying the same thing out (Eric,
2026-10-31: Kwan's birthday came back at 18:14 with Tuesday's card open)."""

import time

import pytest

from test_pulse_followup import live  # noqa: F401
from test_pulse_next import candidate, candidates

TUESDAY = "Kwan Cheung Ho's 41st birthday is on Friday 6 November, ten days from today."
TONIGHT = "Kwan Cheung Ho's 41st birthday is Friday 6 November — a card needs to be in hand by then."


def open_card(live, news, *, created_at, topic="date", **more):
    [card_id] = live.store.add_cards([{"kind": "noticed", "level": "screen", "topic": topic, "news": news,
                                       "created_at": created_at, **more}])
    return card_id


def skip(live, text, now, kind="date", **more):
    rows = [candidate(kind=kind, text=text, value=4, **more)]
    candidates.decide(live.store, rows, now=now)
    return rows[0]["skip"]


def test_a_refless_card_still_open_keeps_the_same_birthday_out(live):
    now = time.time()
    open_card(live, TUESDAY, created_at=now - 4 * 86400)
    assert skip(live, TONIGHT, now) == "raised"


def test_someone_else_on_the_same_day_still_comes_up(live):
    now = time.time()
    open_card(live, TUESDAY, created_at=now - 86400)
    assert skip(live, "Anna Reyes's birthday is on Friday 6 November.", now) == ""
    # Family share a surname: Long's appointment isn't Kwan's birthday.
    assert skip(live, "Long Cheung Ho's 41st birthday is Friday 6 November.", now) == ""


def test_only_an_open_card_on_the_same_kind_counts(live):
    now = time.time()
    card_id = open_card(live, TUESDAY, created_at=now - 86400)
    assert skip(live, TONIGHT, now, kind="commitment") == ""
    live.store.mark_done(card_id, None, closed_by="user", now=now - 3600)
    assert skip(live, TONIGHT, now) == ""


def test_a_card_from_long_ago_doesnt_count(live):
    now = time.time()
    open_card(live, TUESDAY, created_at=now - candidates.SAID_WINDOW_S - 60)
    assert skip(live, TONIGHT, now) == ""


def test_its_day_still_brings_it_once(live):
    now = time.time()
    open_card(live, TUESDAY, created_at=now - 4 * 86400)
    assert skip(live, TONIGHT, now, due_at=now + 3600) == ""
    open_card(live, TONIGHT, created_at=now - 600)
    assert skip(live, TONIGHT, now, due_at=now + 3600) == "raised"


@pytest.mark.parametrize("said, text, same", [
    (TUESDAY, TONIGHT, True),
    (TUESDAY, "Kwan's birthday is Friday 6 November.", True),
    (TUESDAY, "Remember Kwan's birthday on Friday 6 November.", True),
    (TUESDAY, "Anna's birthday is Friday 6 November.", False),
    (TUESDAY, "Long Cheung Ho's RAPIDS ID card appointment is on Tuesday 10 November at 18:00.", False),
    ("File the RAPIDS form by Friday 6 November.", "File taxes by Friday 6 November.", False),
    ("File taxes by Friday 6 November.", "File the RAPIDS form by Friday 6 November.", False),
    ("Renew passport by 20 November.", "The user will renew their passport by 20 November.", True),
    ("call the dentist on 15 October", "pay the rent on 15 October", False),
    (TUESDAY, "Kwan's birthday, the 6th.", True),
    ("Kwan's visa may lapse.", "Kwan may need a visa.", False),
    ("Ho's birthday is Friday 6 November.", "Anna Ho's birthday is Friday 6 November.", False),
    ("Kwan's birthday, 6 November 2026.", "Kwan's passport is due 20 November 2026.", False),
    ("Kwan's party at 18:00 on 6 November.", "Kwan's flight lands at 18:00 on 9 November.", False),
    ("Kwan's birthday is 2026-11-06.", "Kwan's passport is due 2026-11-20.", False),
    ("Kwan's birthday is 2026-11-06.", "Kwan's birthday is Friday 6 November.", True),
    (TUESDAY, "Kwan Cheung Ho", False),
    (TUESDAY, "Kwan Cheung Ho's passport renewal is due 20 November.", False),
    ("Anna Ho's birthday is Friday 6 November.", "Ho's birthday is Friday 6 November.", False),
    ("Send Mara the deck by Friday.", "Send Mara the budget by Friday.", False),
    ("Send Mara the deck by Friday.", "Mara's deck is due Friday.", True),
    ("Kwan's birthday, Friday 6 November.", "Remind Kwan's birthday, Friday 6 November.", True),
    ("Kwan's party is 2026-11-06T18:00.", "Kwan's party is Friday 6 November.", True),
    ("Kwan's party is 2026-11-11T18:00.", "Kwan's party is Friday 6 November.", False),
    ("Kwan's birthday is 6 November.", "Kwan's dentist appointment is 6 December.", False),
    ("Kwan's birthday is 6 Nov.", "Kwan's birthday, 6 November.", True),
    ("Kwan's birthday is Friday 6 November.", "Kwan's piano lesson is on Friday.", False),
    ("Kwan's birthday is Friday 6 November.", "Kwan's birthday is on Friday.", True),
    ("Kwan's passport renewal in Nov.", "Kwan's passport renewal, November.", True),
    ("Dentist appointment for Kwan, Friday 6 November.", "Kwan's dentist appointment is on Friday 6 November.", True),
    ("Dentist appointment for Kwan, Friday 6 November.", "Anna's dentist appointment is on Friday 6 November.", False),
    ("Kwan's birthday is Friday 6 November.", "Kwan's birthday is Fri 6 November.", True),
    ("Kwan's party is Friday 6 November.", "Kwan's party is 2026-11-06T18:00.", True),
    ("May's birthday is Friday 6 November.", "May's 41st birthday is Friday 6 November.", True),
    ("May's birthday is Friday 6 November.", "Kwan's birthday is in May.", False),
    ("Kwan's 21st birthday is Friday 6 November.", "Kwan's flight is on the 21st.", False),
    ("Kwan's birthday is 6 November.", "Kwan's flight at 6 pm on 9 November.", False),
    ("Kwan's birthday is 6 November.", "Kwan's flight at 18.06 on 9 November.", False),
])
def test_same_subject(said, text, same):
    assert candidates.same_subject(said, text) is same



def test_a_remembered_item_of_its_own_only_counts_a_card_with_nothing_to_key_on(live):
    now = time.time()
    open_card(live, "Send Mara the deck by Friday 6 November.", created_at=now - 3600, topic="commitment",
              item_id=901)
    row = {"origin": candidates.MEMORY, "item_id": 902, "kind": "commitment", "ref": "",
           "text": "Send Mara the deck by Friday 6 November.", "due_at": None}
    assert candidates.still_open(live.store, [row], now) == []
    open_card(live, "Send Mara the deck by Friday 6 November.", created_at=now - 600, topic="commitment")
    assert candidates.still_open(live.store, [row], now) == [row]
