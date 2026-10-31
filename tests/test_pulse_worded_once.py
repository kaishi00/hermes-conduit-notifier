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
    ("Renew the car insurance by 12 November.", "The car insurance renewal is due 12 November.", False),
    (TUESDAY, "Kwan Cheung Ho", False),
    (TUESDAY, "Kwan Cheung Ho's passport renewal is due 20 November.", False),
])
def test_same_subject(said, text, same):
    assert candidates.same_subject(said, text) is same
