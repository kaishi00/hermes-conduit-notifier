"""Pulse made the same cards again and again (Eric, 2026-11-08: three
copies of the SeaBag and audit-cron cards from one run, and the audit card
back the day after he marked it Done). Three causes: every plugin copy
Hermes loads into one process ran the loop; an item closed with its time
as text failed every candidate list, so reports went out with no check
against earlier cards; and a card the user closed didn't keep the same
words out."""

import importlib.util
import sqlite3
import sys
import time
from datetime import timezone

from test_pulse import ROOT, pulse, report
from test_pulse_followup import commitment, live  # noqa: F401
from test_pulse_next import candidate, candidates, done_in_chat

UTC = timezone.utc
AUDIT = ("The Hermes Conduit autonomous code audit cron has failed its last 2 runs and is currently interrupted - "
         "worth checking if the audit needs to be restarted.")
SEABAG = "App Store Connect sent a SeaBag iOS review completion notice for build 2.1.2+111."


def card(live, news, *, created_at, topic="watch", ref=None, **more):
    links = []
    if ref:  # made from a candidate about a phone reminder or calendar entry
        links = live.store.add_candidates("run-1", [{**candidate(kind=topic, text=news), "ref": ref,
                                                     "decision": candidates.SURFACED}], now=created_at)
    [card_id] = live.store.add_cards([{"kind": "noticed", "level": "screen", "topic": topic, "news": news,
                                       "title": news, "created_at": created_at, **more}], links=links)
    return card_id


def decide(live, *texts, kind="watch", now):
    rows = [candidate(kind=kind, text=text, value=4) for text in texts]
    candidates.decide(live.store, rows, now=now)
    return [row["skip"] for row in rows]


def test_only_one_plugin_copy_in_a_process_runs_the_loop(monkeypatch):
    slot = type(sys)("slot")
    monkeypatch.setitem(sys.modules, pulse._SLOT, slot)
    assert pulse._process_slot()
    # Another profile's copy, loaded into the same process, waits.
    monkeypatch.setattr(pulse, "_me", "hermes_plugins.conduit_push__home_7ecc6991116a.pulse")
    assert not pulse._process_slot()
    pulse._leave_slot()  # not its slot: nothing changes
    assert slot.holder[0] != pulse._me
    # Once the copy that ticked stops ticking, it takes over.
    slot.at -= pulse.LEADER_TTL_S + 1
    assert pulse._process_slot() and slot.holder == (pulse._me, pulse.threading.get_ident())
    pulse._leave_slot()
    assert slot.holder is None


def test_two_copies_loaded_from_one_folder_are_two_copies(monkeypatch):
    # A profile whose plugin folder links to the main install: the same file
    # loaded under another module name is its own copy, and waits.
    slot = type(sys)("slot")
    monkeypatch.setitem(sys.modules, pulse._SLOT, slot)
    name = "conduit_push.pulse_home_0123456789ab"
    spec = importlib.util.spec_from_file_location(name, ROOT / "pulse.py")
    other = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, other)
    spec.loader.exec_module(other)
    assert other._me != pulse._me
    assert pulse._process_slot()
    assert not other._process_slot()
    pulse._leave_slot()


def test_a_loop_started_again_waits_for_the_old_one(monkeypatch):
    # After an unload, register starts a new loop under the same module name
    # while the old thread may still be in a tick.
    slot = type(sys)("slot")
    monkeypatch.setitem(sys.modules, pulse._SLOT, slot)
    slot.holder, slot.at = (pulse._me, -1), pulse.time.monotonic()  # the old loop's thread
    assert not pulse._process_slot()
    pulse._leave_slot()  # the new thread can't let go of the old one's slot
    assert slot.holder == (pulse._me, -1)


def test_a_copy_without_the_slot_sends_nothing(live, monkeypatch):
    slot = type(sys)("slot")
    slot.holder, slot.at = "hermes_plugins.conduit_push__home_bd60a4de7372.pulse", time.monotonic()
    monkeypatch.setitem(sys.modules, pulse._SLOT, slot)
    live.store.set("report_requested", True)
    pulse.tick()
    assert live.sent() == []
    assert live.store.get("leader") is None


def test_a_close_time_stored_as_text_no_longer_fails_the_report(live):
    since = time.time() - 1
    flights = commitment(live, "The user will book the flights to Lisbon.", words="I need to book flights to Lisbon")
    done_in_chat(live, flights, "Booked the flights for the 12th")
    [item] = report.done_items(live.store, since)
    # Something outside Pulse wrote one close time as text on the host.
    assert "done Tue 03 Nov 15:43" in report.done_section([{**item, "closed_at": "2026-11-03 15:43:11"}], UTC)[0]
    assert "done ?" in report.done_section([{**item, "closed_at": "soon"}], UTC)[0]
    assert "done ?" in report.done_section([{**item, "closed_at": None}], UTC)[0]
    # Nor is it "done since" every report for ever, as SQLite sorts text after numbers.
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE items SET closed_at='2026-11-03 15:43:11' WHERE id=?", (flights,))
    assert report.done_items(live.store, time.time() + 86400) == []


def test_the_same_line_twice_in_one_run_comes_up_once(live):
    now = time.time()
    assert decide(live, SEABAG, f"  {SEABAG.upper()} ", now=now) == ["", "raised"]


def test_a_card_marked_done_keeps_the_same_words_out(live):
    now = time.time()
    card_id = card(live, AUDIT, created_at=now - 86400)
    live.store.mark_done(card_id, None, closed_by="user", now=now - 86000)
    assert decide(live, AUDIT, now=now) == ["raised"]
    # Something new about it still comes up.
    assert decide(live, AUDIT.replace("last 2 runs", "last 3 runs"), now=now) == [""]
    # A month after it was closed, the same words come up again.
    assert decide(live, AUDIT, now=now + candidates.SAID_WINDOW_S) == [""]
    # However long the card sat open first: the month runs from the Done.
    old = card(live, AUDIT, created_at=now - 3 * candidates.SAID_WINDOW_S)
    live.store.mark_done(old, None, closed_by="user", now=now + candidates.SAID_WINDOW_S - 60)
    assert decide(live, AUDIT, now=now + candidates.SAID_WINDOW_S) == ["raised"]


def test_a_deadline_carded_other_and_done_keeps_its_words_out_of_a_deadline_row(live):
    # A report without a list carded a deadline it didn't show as "other";
    # a later list has the same words as a deadline.
    now = time.time()
    words = "Renew the passport by 20 November"
    done = card(live, words, created_at=now - 86400, topic="other")
    live.store.mark_done(done, None, closed_by="user", now=now - 86000)
    assert decide(live, words, kind="deadline", now=now) == ["raised"]
    assert decide(live, words, kind="promise", now=now) == [""]
    # Still open, the same.
    passport = "Renew the passport at the post office by 20 November"
    card(live, passport, created_at=now - 86400, topic="other")
    assert decide(live, passport, kind="deadline", now=now) == ["raised"]


def test_a_card_put_away_keeps_the_same_words_out(live):
    now = time.time()
    card_id = card(live, AUDIT, created_at=now - 86400)
    live.store.dismiss_card(card_id, True, now - 86000, rest_until=now + 3600)  # Not now: still open
    assert decide(live, AUDIT, now=now) == ["raised"]
    live.store.dismiss_card(card_id, False, now - 600)
    live.store.dismiss_card(card_id, True, now - 300)  # put away for good
    assert decide(live, AUDIT, now=now) == ["raised"]


def test_a_card_rated_not_useful_keeps_the_same_words_out_but_wrong_time_doesnt(live):
    now = time.time()
    card_id = card(live, AUDIT, created_at=now - 86400)
    assert live.store.rate_card(live.store.card(card_id), rating="down", reason="", now=now - 3600)
    assert decide(live, AUDIT, now=now) == ["raised"]
    assert candidates.said_before(live.store, "watch", AUDIT, now)
    # "Wrong time" only puts it off: still open, not closed.
    late = card(live, SEABAG, created_at=now - 86400)
    assert live.store.rate_card(live.store.card(late), rating="down", reason=pulse.store.WRONG_TIME, now=now - 3600)
    assert [row["id"] for row in live.store.closed_cards_on(["watch"], now - 86400)] == [card_id]


def test_a_report_without_a_list_lets_a_closed_reminder_card_come_back(live):
    # A phone reminder or calendar entry keeps its own rules (a reminder's
    # week, an event's day), with a list or without one.
    now = time.time()
    done = card(live, SEABAG, created_at=now - 86400, ref=f"{candidates.REMINDER}abc")
    live.store.mark_done(done, None, closed_by="user", now=now - 86000)
    assert not candidates.said_before(live.store, "watch", SEABAG, now)
    # Still open, it keeps the same words out.
    card(live, AUDIT, created_at=now - 86400, ref=f"{candidates.EVENT}def")
    assert candidates.said_before(live.store, "watch", AUDIT, now)


def test_an_open_card_keeps_the_same_words_out_without_a_date(live):
    now = time.time()
    card(live, SEABAG, created_at=now - 86400, topic="fact")
    assert decide(live, SEABAG, kind="fact", now=now) == ["raised"]


def test_a_report_without_a_list_gets_no_card_for_a_line_already_carded(live):
    now = time.time()
    done = card(live, AUDIT, created_at=now - 86400)
    live.store.mark_done(done, None, closed_by="user", now=now - 86000)
    assert candidates.said_before(live.store, "watch", AUDIT, now)
    assert not candidates.said_before(live.store, "fact", AUDIT, now)
    assert not candidates.said_before(live.store, "watch", SEABAG, now)


def test_other_readers_of_a_text_close_time_count_it_as_long_ago(live):
    flights = commitment(live, "The user will book the flights to Lisbon.", words="I need to book flights to Lisbon")
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE items SET status='done', closed_at='2026-11-03 15:43:11' WHERE id=?", (flights,))
    assert live.store.closed_since(flights, 1.0) is False  # used to raise ValueError
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE items SET status='reminded' WHERE id=?", (flights,))
    assert live.store.remembered_items(limit=None, reminded_since=time.time()) == []
    assert pulse.store.close_time("2026-11-03 15:43:11") == 0.0 and pulse.store.close_time(5) == 5.0


def test_a_loop_that_ends_lets_go_of_the_slot(monkeypatch):
    slot = type(sys)("slot")
    monkeypatch.setitem(sys.modules, pulse._SLOT, slot)
    stopped = pulse.threading.Event()
    monkeypatch.setattr(pulse, "tick", lambda: (pulse._process_slot(), stopped.set(), pulse._wake.set()))
    pulse._run(stopped)
    assert slot.holder is None


