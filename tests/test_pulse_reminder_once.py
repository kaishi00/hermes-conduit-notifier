"""One card per phone reminder: an open reminder the candidate list brings
up gets one card, however often it's moved (Eric, 2026-10-27: three cards
for one cipher-lock reminder moved back each morning)."""

import sqlite3
import time

import pytest

from test_pulse import SID, report
from test_pulse_candidates import gathered  # noqa: F401
from test_pulse_followup import live  # noqa: F401
from test_pulse_hours import phone_home
from test_pulse_next import UTC, candidate, candidates

REMINDERS = {"r1": {"id": "r_lock", "title": "Call the building manager about the 64D cipher lock"}}
FOUND = {"kind": "reminder", "bring_up": True, "value": 4, "skip": "", "actionable": True,
         "line": "Your cipher lock follow-up is due tomorrow at 9:00 AM.", "why": "From your open reminders.",
         "source": "reminders", "area": "personal", "due": "2026-10-28T09:00", "asked": True}


def follows(reminders=None):
    return report.Gathered("the context", "Mon 26 Oct 08:00", [], [], {}, UTC, reminders=REMINDERS
                           if reminders is None else reminders)


def test_the_list_gives_each_open_reminder_a_key(live):
    phone_home(live.home)  # Buy milk and File taxes open, Call mum done
    now = time.time()
    found = report.gather(live.home, live.store, home_chat_id=SID, now=now, keyed=True)
    assert sorted(reminder["id"] for reminder in found.reminders.values()) == ["r_milk", "r_tax"]
    for key, reminder in found.reminders.items():
        assert f"\n- {key} {reminder['title']}" in found.text
    # The report as it was, the brief and the checks read them unkeyed.
    plain = report.gather(live.home, live.store, home_chat_id=SID, now=now)
    assert plain.reminders == {} and "\n- Buy milk" in plain.text
    assert "\n- Buy milk" in report.phone_context(live.home, now)[0]


@pytest.mark.parametrize("key, ref", [("r1", "reminder:r_lock"), (" R1 ", "reminder:r_lock"), ("r2", None),
                                      ("", None), (None, None)])
def test_a_found_candidate_about_a_listed_reminder_carries_it(key, ref):
    row = candidates.found_row({**FOUND, "reminder": key}, {}, follows=follows())
    assert row.get("ref") == ref
    if ref:
        assert row["source"] == "reminders"


def test_a_next_step_keeps_what_it_follows():
    row = candidates.found_row({**FOUND, "kind": "next", "after": "e1", "reminder": "r1"}, {},
                               follows=report.Gathered("x", "y", [], [], {}, UTC, {}, {"e1": {"id": "e_x"}},
                                                       REMINDERS))
    assert row["ref"] == "after:event:e_x"


def test_the_list_asks_which_reminder_each_is_about():
    seen = {}

    def llm(instructions, text):
        seen.update(instructions=instructions)
        return {"remembered": [], "found": []}, 1

    candidates.ask(llm, gathered(), nudge="balanced")
    assert '"reminder": the key of the open reminder it is about (like r2)' in seen["instructions"]


def test_a_phone_reminder_due_soon_still_comes_up_once(live):
    # Pulse keeps the phone's reminders with what else it brings up (Eric): one
    # due within a day comes up whatever learning says, the first time only.
    now = time.time()
    rows = lambda: [candidate(kind="reminder", ref="reminder:r_lock", asked=True, due_at=now + 3600, value=1,
                              bring_up=False)]
    first = rows()
    candidates.decide(live.store, first, now=now)
    assert first[0]["floor"] and first[0]["decision"] == candidates.SURFACED
    card_for(live, "reminder:r_lock", created_at=now - 86400)
    again = rows()
    candidates.decide(live.store, again, now=now)
    assert not again[0]["floor"] and again[0]["decision"] == candidates.SKIPPED


def card_for(live, ref, *, created_at, **more):
    """A card a candidate about ``ref`` made; its id."""
    [candidate_id] = live.store.add_candidates("run-1", [{**candidate(kind="reminder"), "ref": ref,
                                                         "decision": candidates.SURFACED}], now=created_at)
    [card_id] = live.store.add_cards([{"kind": "noticed", "level": "screen", "topic": "reminder", "news": "x",
                                       "created_at": created_at}], links=[candidate_id])
    if more:
        with sqlite3.connect(live.store.path) as conn:
            conn.execute(f"UPDATE cards SET {', '.join(f'{key}=?' for key in more)} WHERE id=?",
                         (*more.values(), card_id))
    return card_id


def decided(live, now, ref="reminder:r_lock"):
    rows = [candidate(kind="reminder", ref=ref, value=5), candidate(kind="reminder", ref="reminder:r_other", value=5)]
    candidates.decide(live.store, rows, now=now)
    return [(row["decision"], row["skip"]) for row in rows]


def test_a_reminder_moved_back_each_day_comes_up_once_a_week(live):
    now = time.time()
    card_for(live, "reminder:r_lock", created_at=now - 2 * 86400)
    assert decided(live, now) == [(candidates.SKIPPED, "raised"), (candidates.SURFACED, "")]
    # A week on, if it's still open, once more.
    assert decided(live, now + candidates.REMINDER_AGAIN_S) == [(candidates.SURFACED, "")] * 2


@pytest.mark.parametrize("more", [{"rating": "down"}, {"dismissed_at": 1.0}])
def test_a_reminder_whose_card_was_put_away_or_not_useful_stays_away(live, more):
    now = time.time()
    card_for(live, "reminder:r_lock", created_at=now - 30 * 86400, **more)
    assert decided(live, now)[0] == (candidates.SKIPPED, "raised")


def test_a_reminder_put_off_until_later_can_come_back_after_the_week(live):
    now = time.time()
    card_for(live, "reminder:r_lock", created_at=now - 30 * 86400, dismissed_at=now - 30 * 86400,
             rest_until=now - 29 * 86400)
    assert decided(live, now)[0] == (candidates.SURFACED, "")


def test_a_reminder_put_away_stays_away_once_the_journal_and_cards_drop_it(live):
    now = time.time()
    card_for(live, "reminder:r_lock", created_at=now - 20 * 86400, rating="down")
    assert decided(live, now)[0] == (candidates.SKIPPED, "raised")
    live.store.prune_candidates(now)
    live.store.prune_cards(0)
    assert decided(live, now + 90 * 86400) == [(candidates.SKIPPED, "raised"), (candidates.SURFACED, "")]


@pytest.mark.parametrize("entry, ref", [
    ({"line": "Call the building manager about the 64D cipher lock today."}, "reminder:r_lock"),
    ({"why": "From your reminder: call the building manager about the 64D cipher lock."}, "reminder:r_lock"),
    ({"reminder": "r9"}, None),  # FOUND's line names no reminder's title
    ({"source": "chat"}, None),
])
def test_one_from_the_reminders_with_no_key_is_matched_by_its_title(entry, ref):
    row = candidates.found_row({**FOUND, **entry}, {}, follows=follows())
    assert row.get("ref") == ref


def test_two_reminders_named_match_neither():
    both = {**REMINDERS, "r2": {"id": "r_mgr", "title": "Call the building manager"}}
    row = candidates.found_row({**FOUND, "line": "Call the building manager about the 64D cipher lock."}, {},
                               follows=follows(both))
    assert "ref" not in row


def test_pulse_candidates_says_which_were_about_a_phone_reminder():
    row = {"value": 4, "item_id": None, "ref": "reminder:r_lock", "origin": candidates.MODEL, "source": "reminders",
           "skip": "raised", "why": "", "kind": "reminder", "text": "x", "decision": candidates.SKIPPED,
           "card_id": None}
    assert "(a phone reminder)" in candidates.describe(row)
