"""Pulse knows what comes next (designs/pulse-what-comes-next.md): what got
done and the calendar events that ended since the last report, the next
steps the candidate list may find after them, and Hermes's scheduled jobs
as they've been running, a failing one covering nothing."""

import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from test_pulse import ROOT, SID, _load, pulse, report, store
from test_pulse_candidates import Llm, deliver, gathered, reporting  # noqa: F401
from test_pulse_followup import CHAT, check_now, commitment, followup, live, promise  # noqa: F401
from test_pulse_hours import phone_home
from test_phone import _batch, _event

candidates = _load("conduit_push.pulse_candidates", ROOT / "pulse_candidates.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")
push = _load("conduit_push.pulse_push", ROOT / "pulse_push.py")
places = _load("conduit_push.pulse_places", ROOT / "pulse_places.py")

UTC = timezone.utc
LONDON = report.zone_for("Europe/London")
# The phone's batch (test_phone): Wednesday 14 October 2026 in London.
LUNCHTIME = datetime(2026, 10, 14, 13, 30, tzinfo=LONDON).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def calendar(home, *events, now):
    """A phone sharing just these events, its window around ``now``."""
    return phone_home(home, events={"full": True, "from": iso(now - 7 * 86400), "to": iso(now + 30 * 86400),
                                    "upsert": list(events), "delete": []})


def event(event_id, title, start, end, **more):
    return _event(event_id, title=title, start=iso(start), end=iso(end), **{
        "notes": None, "attendees": [], "location": None, "lat": None, "lon": None, "url": None, **more})


def said(live, text, words, kind="fact", *, at=None):
    """An item memory kept from the user's ``words``; its id."""
    at = time.time() - 3600 if at is None else at
    message_id = live.db.message("user", words, sid=CHAT, at=at)
    mark = live.store.watermark("", CHAT)
    [item_id] = live.store.advance("", CHAT, message_id=message_id, at=at,
                                   after_id=mark["message_id"] if mark else 0, items=[{
        "kind": kind, "text": text, "evidence": words, "explicit": 1, "due": "", "due_at": None,
        "message_id": message_id, "mentioned_at": at}])
    return item_id


def done_in_chat(live, item_id, words, *, at=None):
    """Memory reads ``words`` later in the chat and closes the item as done."""
    at = time.time() - 600 if at is None else at
    message_id = live.db.message("user", words, sid=CHAT, at=at)
    mark = live.store.watermark("", CHAT)
    live.store.advance("", CHAT, message_id=message_id, at=at, after_id=mark["message_id"],
                       closed=[{"id": item_id, "status": "done", "message_id": message_id, "evidence": words}])
    return message_id


# Jobs that fail cover nothing ---------------------------------------------------------

@pytest.mark.parametrize("job, health", [
    ({}, ""),  # it hasn't run yet
    ({"last_status": "ok"}, ""),
    ({"last_status": "ok", "failure_streak": 0, "last_failure": {"at": "2026-10-20", "detail": "503"}}, ""),
    ({"last_status": "delivery_queued"}, ""),
    ({"last_status": "error"}, "last run failed"),
    ({"last_status": "error", "last_error": "Provider returned 503"}, "last run failed: Provider returned 503"),
    # An older Hermes keeps no streak; one failure in a row is the last run.
    ({"last_status": "error", "failure_streak": 1, "last_error": "Timeout"}, "last run failed: Timeout"),
    ({"last_status": "error", "failure_streak": 3, "last_error": "Timeout"}, "failed its last 3 runs: Timeout"),
    ({"last_status": "error", "failure_streak": True, "last_error": "Timeout"}, "last run failed: Timeout"),
    ({"last_status": "error", "failure_streak": "3"}, "last run failed"),
    # The run worked; what it said never reached the user.
    ({"last_status": "delivery_failed", "last_delivery_error": "Telegram: chat not found"},
     "its last message didn't reach the user: Telegram: chat not found"),
    ({"last_status": "delivery_failed"}, "its last message didn't reach the user"),
    # Any status but a run that worked is a failure: Hermes's blocked_config, or one this doesn't know yet.
    ({"last_status": "blocked_config", "failure_streak": 2, "last_error": "No delivery platform configured"},
     "failed its last 2 runs: No delivery platform configured"),
    ({"last_status": "timeout"}, "last run failed"),
    ({"last_status": ""}, ""),
    ({"last_status": None}, ""),
])
def test_a_job_says_how_it_has_been_running_when_it_fails(job, health):
    assert report.job_health(job) == health


def test_a_failing_jobs_error_is_one_clipped_line():
    health = report.job_health({"last_status": "error", "last_error": "Traceback:\n  " + "x" * 400})
    assert health.startswith("last run failed: Traceback: xxx") and "\n" not in health and health.endswith("…")
    assert len(health) == len("last run failed: ") + report.JOB_ERROR_CLIP


def failing_jobs(home):
    (home / "cron").mkdir(exist_ok=True)
    jobs = [{"id": "a1", "name": "Weekly summary", "prompt": "Send the user the weekly summary",
             "schedule_display": "every Friday at 17:00", "next_run_at": "2026-10-30T17:00:00+00:00",
             "last_status": "error", "last_error": "Provider returned 503", "failure_streak": 3},
            {"id": "b2", "name": "Morning news", "prompt": "News", "schedule_display": "every day at 07:00",
             "next_run_at": "2026-10-28T07:00:00+00:00", "last_status": "ok"}]
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def test_the_jobs_list_marks_a_failing_job(tmp_path):
    failing_jobs(tmp_path)
    assert report.cron_jobs(tmp_path).splitlines() == [
        "## Hermes's scheduled jobs",
        "- Morning news (every day at 07:00, next 2026-10-28T07:00:00+00:00): News",
        "- Weekly summary (every Friday at 17:00, next 2026-10-30T17:00:00+00:00) "
        "[failing: failed its last 3 runs: Provider returned 503]: Send the user the weekly summary"]


def test_every_model_that_weighs_a_job_hears_a_failing_one_covers_nothing():
    for instructions in (followup.INSTRUCTIONS, followup.COMMITMENT_INSTRUCTIONS, followup.WATCH_INSTRUCTIONS):
        assert followup.JOBS_RULE in instructions
    for instructions in (followup.HELD_INSTRUCTIONS, followup.RESTED_INSTRUCTIONS, followup.FOUND_INSTRUCTIONS):
        assert "a scheduled job already covers it (one marked failing covers nothing)" in instructions
    assert "(a job marked failing covers nothing)" in report.REPORT_TEXT
    assert "a job marked failing covers nothing)" in candidates.INSTRUCTIONS
    assert "A scheduled job marked failing covers nothing: weigh what rests on it as if there were no job, and " \
           "say in its \"why\" that the job is failing." in candidates.INSTRUCTIONS


def test_a_promise_resting_on_a_job_that_failed_its_last_3_runs_comes_up(live):
    # Hermes promised the weekly summary and set a job for it, which has failed three times running.
    item_id = promise(live, "Hermes will send the user the weekly summary on Friday.")
    failing_jobs(live.home)
    asked = []

    def llm(instructions, text):
        # A model that does as it's told: a job covers the promise unless it's marked failing.
        asked.append((instructions, text))
        failing = "[failing: failed its last 3 runs: Provider returned 503]" in text
        if failing and followup.JOBS_RULE in instructions:
            return {"decision": "tell", "news": "The weekly summary hasn't gone out: its job failed its last 3 runs.",
                    "why": "The job meant to send it is failing.", "when": "any", "urgent": False}, 42
        return {"decision": "wait", "why": "A scheduled job covers it.", "check_again_hours": 24}, 42

    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [(instructions, text)] = asked
    assert "- Weekly summary (every Friday at 17:00, next 2026-10-30T17:00:00+00:00) [failing: " in text
    [row] = live.store.followups()
    assert (row["item_id"], row["decision"], row["news"]) == (
        item_id, "tell", "The weekly summary hasn't gone out: its job failed its last 3 runs.")


# What got done --------------------------------------------------------------------------

def test_what_got_done_leaves_out_what_pulse_told_and_what_done_only_put_away(live):
    since = time.time() - 1
    passport = commitment(live)
    flights = commitment(live, "The user will book the flights to Lisbon.", words="I need to book flights to Lisbon")
    invoice = commitment(live, "The user will send the invoice.", words="I'll send the invoice")
    deploy = promise(live)
    landlord = said(live, "Tell the user when the landlord answers.", "tell me when the landlord answers", "watch")
    gym = commitment(live, "The user will cancel the gym.", words="I should cancel the gym")
    dan = said(live, "Dan started a new job at Stripe.", "Dan started at Stripe")
    live.store.close_item(passport, status="done", closed_by=learning.DONE_BY)
    live.store.close_item(deploy, status="done", closed_by=learning.TOLD_BY)
    live.store.close_item(landlord, status="done", closed_by=f"{learning.TOLD_BY} (screen)")
    live.store.close_item(invoice, status="done", closed_by=f"{learning.CHECKED_BY}: the chat shows it sent")
    live.store.close_item(gym, status="cancelled", closed_by=learning.DONE_BY)
    live.store.close_item(dan, status="done", closed_by=learning.DONE_BY)
    done_in_chat(live, flights, "Booked the flights for the 12th")
    # Latest first: told news, a call-off and a fact put away aren't something done.
    assert [item["id"] for item in report.done_items(live.store, since)] == [flights, invoice, passport]
    assert report.done_items(live.store, time.time() + 60) == []
    # What it leaves out never takes the room of what it keeps: the store leaves it out before its limit.
    for n in range(3):
        told = promise(live, f"Hermes will tell the user about build {n}.")
        live.store.close_item(told, status="done", closed_by=learning.TOLD_BY)
    assert [item["id"] for item in live.store.done_items(since, 2, skip_kinds=report.UNDONE_KINDS,
                                                         skip_closer=learning.TOLD_BY)] == [flights, invoice]
    assert [item["id"] for item in report.done_items(live.store, since)] == [flights, invoice, passport]
    assert learning.told(learning.TOLD_BY) and learning.told(f"{learning.TOLD_BY} (brief)")
    assert not learning.told(f"{learning.CHECKED_BY}: x") and not learning.told(None)


@pytest.mark.parametrize("closed_by, how", [
    (learning.DONE_BY, "the user marked it done on the Pulse screen"),
    (learning.ACT_DONE_BY, "done in its chat after the user tapped Do it"),
    (learning.HELD_DONE_BY, "a chat showed it done"),
    (learning.RESTED_DONE_BY, "a chat showed it done"),
    ("chat work message 12: Booked the  flights\nfor the 12th", 'a chat showed it done: "Booked the flights for the 12th"'),
    ("inbox dev1/e4: Payment received", 'the phone logged it done: "Payment received"'),
    (f"{learning.CHECKED_BY}: the chat shows the invoice sent", "Pulse's check found it done: the chat shows the invoice sent"),
    ("user", "closed as done"),
    ("", "closed as done"),
    (None, "closed as done"),
])
def test_a_done_item_says_how_it_got_done(closed_by, how):
    assert report.closed_how(closed_by) == how


def test_the_done_section_numbers_each_item_and_says_where_and_how(live):
    since = time.time() - 1
    flights = commitment(live, "The user will book the flights to Lisbon.", words="I need to book flights to Lisbon",
                         said_at=datetime(2026, 10, 26, 9, 15, tzinfo=UTC).timestamp())
    done_in_chat(live, flights, "Booked the flights for the 12th")
    [item] = report.done_items(live.store, since)
    section, listed = report.done_section([item], UTC)
    done_at = report._local(item["closed_at"], UTC)
    assert section == (f"{report.DONE_TITLE}\n- #{flights} [commitment] The user will book the flights to Lisbon. "
                       f"(said Mon 26 Oct 09:15): done {done_at}, a chat showed it done: \"Booked the flights for the 12th\"")
    assert listed == {flights: item}
    assert report.done_section([], UTC) == ("", {})
    # Another profile's says so; a long list stops at its budget, and lists only what it shows.
    assert ", profile nahida): done" in report.done_section([{**item, "profile": "nahida"}], UTC)[0]
    many, listed = report.done_section([{**item, "id": n, "text": "x" * 160} for n in range(30)], UTC)
    assert len(many) <= len(report.DONE_TITLE) + 1 + report.DONE_BUDGET and many.count("\n- #") < 30
    assert sorted(listed) == [n for n in range(30) if f"\n- #{n} [" in many]


# What ended -----------------------------------------------------------------------------------

def ended_day(home):
    """14 October on the phone: a planning meeting with Dan and the dentist that
    morning, and what doesn't count as ended by lunchtime."""
    at = lambda hour, minute=0: datetime(2026, 10, 14, hour, minute, tzinfo=LONDON).timestamp()
    events = [
        event("e_early", "Gym", at(7), at(8)),  # before the report's window
        event("e_planning", "Planning meeting", at(10), at(11),
              attendees=[{"name": "Dan Okafor", "email": "dan@example.com", "status": "accepted"}]),
        event("e_dentist", "Dentist", at(11, 30), at(12), location="12 High St, London"),
        event("e_sync", "Optional sync", at(11), at(11, 30), my_status="declined"),
        event("e_cancelled", "Coffee with Priya", at(12), at(12, 30), status="canceled"),
        event("e_holiday", "Bank holiday", at(0), at(0) + 86400, all_day=True),
        event("e_now", "Design review", at(13), at(14)),  # still on
        event("e_later", "Call with Sam", at(16), at(16, 30)),
    ]
    phone_home(home, events={**_batch()["events"], "upsert": events})
    return at


def test_the_events_that_ended_are_timed_attended_and_over(tmp_path):
    at = ended_day(tmp_path)
    assert [event["id"] for event in report.ended_events(tmp_path, at(9), LUNCHTIME)] == ["e_planning", "e_dentist"]
    assert [event["id"] for event in report.ended_events(tmp_path, at(6), at(17))] == [
        "e_early", "e_planning", "e_dentist", "e_now", "e_later"]
    # At most the last ten, in the order they ended.
    many = [event(f"e{n}", f"Meeting {n}", at(9) + n * 60, at(9) + n * 60 + 30) for n in range(15)]
    phone_home(tmp_path / "busy", events={**_batch()["events"], "upsert": many})
    assert [event["id"] for event in report.ended_events(tmp_path / "busy", at(9), LUNCHTIME)] == [
        f"e{n}" for n in range(5, 15)]
    # All-day, declined and cancelled events never take the room of one that ended, and one
    # that ended just as the window opened counts.
    crowd = [event(f"a{n}", f"Trip day {n}", at(0) - n * 86400, at(0) + 86400, all_day=True) for n in range(120)]
    crowd += [event(f"d{n}", f"Webinar {n}", at(9), at(9, 30), my_status="declined") for n in range(30)]
    phone_home(tmp_path / "crowd", events={**_batch()["events"], "upsert": crowd + [
        event("e_edge", "Stand-up", at(8, 30), at(9)), event("e_last", "Review", at(12), at(12, 30))]})
    assert [event["id"] for event in report.ended_events(tmp_path / "crowd", at(9), LUNCHTIME)] == [
        "e_edge", "e_last"]
    # No calendar shared, or none at all: nothing.
    assert report.ended_events(tmp_path / "none", at(9), LUNCHTIME) == []


def test_each_ended_event_has_a_key_and_what_pulse_remembers_about_it(live):
    at = ended_day(live.home)
    live.store.set("memory", True)
    notes = said(live, "The user will send Dan the notes from the planning meeting.",
                 "I'll send Dan the notes after planning", "commitment", at=at(9))
    cleaning = said(live, "The dentist wants a cleaning every six months.", "my dentist wants a cleaning twice a year",
                    at=at(9))
    events = report.ended_events(live.home, at(9), LUNCHTIME)
    section, keys = report.ended_section(live.store, events, LONDON, LUNCHTIME)
    assert section.splitlines() == [
        report.ENDED_TITLE,
        "- e1 Planning meeting (Wed 14 Oct 10:00–11:00), with Dan Okafor",
        f"  Pulse remembers: #{notes} [commitment] The user will send Dan the notes from the planning meeting.",
        "- e2 Dentist (Wed 14 Oct 11:30–12:00), at 12 High St, London",
        f"  Pulse remembers: #{cleaning} [fact] The dentist wants a cleaning every six months."]
    assert {key: found["id"] for key, found in keys.items()} == {"e1": "e_planning", "e2": "e_dentist"}
    # With memory off, the events alone.
    live.store.set("memory", False)
    section, _ = report.ended_section(live.store, events, LONDON, LUNCHTIME)
    assert "Pulse remembers" not in section and section.count("\n- e") == 2
    assert report.ended_section(live.store, [], LONDON, LUNCHTIME) == ("", {})


# The list reads them ------------------------------------------------------------------------

def test_the_list_reads_what_got_done_and_what_ended_and_nothing_else_does(live):
    now = time.time()
    live.store.set("memory", True)
    flights = commitment(live, "The user will book the flights to Lisbon.", words="I need to book flights to Lisbon",
                         said_at=now - 5 * 3600)
    done_in_chat(live, flights, "Booked the flights for the 12th", at=now - 3600)
    calendar(live.home, event("e_dentist", "Dentist", now - 3 * 3600, now - 2 * 3600), now=now)
    text = lambda **more: report.gather(live.home, live.store, home_chat_id=SID, now=now, keyed=True, **more)
    found = text(next_steps=True)
    assert set(found.done) == {flights} and set(found.ended) == {"e1"}
    assert found.ended["e1"]["id"] == "e_dentist"
    assert f"{report.DONE_TITLE}\n- #{flights} [commitment] The user will book the flights to Lisbon." in found.text
    assert f"{report.ENDED_TITLE}\n- e1 Dentist (" in found.text
    assert found.text.index(report.DONE_TITLE) < found.text.index(report.ENDED_TITLE) < found.text.index(
        "## Calendar, the next")
    # The report as it was, and the brief, read neither.
    plain = text()
    assert report.DONE_TITLE not in plain.text and report.ENDED_TITLE not in plain.text
    assert (plain.done, plain.ended) == ({}, {})
    # With memory off, nothing it remembers got done; events still end.
    live.store.set("memory", False)
    off = text(next_steps=True)
    assert off.done == {} and set(off.ended) == {"e1"}


def test_a_next_step_follows_only_what_the_list_shows_done(live):
    now = time.time()
    live.store.set("memory", True)
    errands = [commitment(live, f"The user will do errand {n}: " + "x" * 140, words=f"errand {n}",
                          said_at=now - 5 * 3600) for n in range(report.MAX_DONE)]
    for n, item_id in enumerate(errands):
        done_in_chat(live, item_id, f"did errand {n} " + "y" * 140, at=now - 3600 + n)
    found = report.gather(live.home, live.store, home_chat_id=SID, now=now, next_steps=True, room=100_000)
    assert "(cut here" not in found.text  # no cut: the section's own budget leaves some out
    shown = [item_id for item_id in errands if f"\n- #{item_id} [" in found.text]
    assert 0 < len(shown) < len(errands) and sorted(found.done) == sorted(shown)


def test_a_cut_list_keeps_only_what_it_still_shows(live):
    now = time.time()
    live.store.set("memory", True)
    items = [commitment(live, f"The user will do errand {n}.", words=f"errand {n}", said_at=now - 5 * 3600)
             for n in range(3)]
    for n, item_id in enumerate(items):
        done_in_chat(live, item_id, f"did errand {n}", at=now - 3600 + n)
    calendar(live.home, *[event(f"e_{n}", f"Meeting {n}", now - 3 * 3600 + n * 60, now - 2 * 3600 + n * 60)
                          for n in range(3)], now=now)
    whole = report.gather(live.home, live.store, home_chat_id=SID, now=now, next_steps=True, room=100_000)
    assert len(whole.done) == 3 and len(whole.ended) == 3
    # Room for the chats as they were, and for what comes before the second event.
    chats = len(whole.text) - whole.text.index("\n\n## Chats since")
    cut_at = whole.text.index("\n- e2 ")
    cut = report.gather(live.home, live.store, home_chat_id=SID, now=now, next_steps=True, room=cut_at + 40 + chats)
    assert "(cut here: no room in this report)" in cut.text
    assert len(cut.done) == 3 and set(cut.ended) == {"e1"}


# Next steps ----------------------------------------------------------------------------------

def follows(done=(), ended=None):
    return report.Gathered("the context", "Mon 26 Oct 08:00", [], [], {"c1": ("", CHAT)}, UTC,
                           {item["id"]: item for item in done}, ended or {})


DONE = {"id": 12, "kind": "commitment", "profile": "nahida", "chat_id": "trip", "text": "The user will book flights."}
DENTIST = {"id": "e_dentist", "title": "Dentist"}
NEXT = {"kind": "next", "bring_up": True, "value": 4, "skip": "", "actionable": True,
        "line": "Book a hotel in Lisbon for the 12th to the 15th", "why": "You booked the flights; no hotel yet.",
        "source": "chat", "area": "personal"}


@pytest.mark.parametrize("after, ref", [("#12", "after:item:12"), ("12", "after:item:12"), (12, "after:item:12"),
                                        ("e1", "after:event:e_dentist"), (" E1 ", "after:event:e_dentist"),
                                        ("#e1", "after:event:e_dentist")])
def test_a_next_step_follows_something_listed_as_done_or_ended(after, ref):
    row = candidates.found_row({**NEXT, "after": after}, follows().chats, follows=follows([DONE], {"e1": DENTIST}))
    assert (row["kind"], row["ref"]) == ("next", ref)
    if ref.startswith(candidates.AFTER_ITEM):
        # It points at the chat what it follows was said in, so Do it opens that chat.
        assert (row["profile"], row["chat_id"], row["source"]) == ("nahida", "trip", "chat")
    else:
        assert (row["profile"], row["chat_id"], row["source"]) == ("", "", "calendar")


@pytest.mark.parametrize("after", ["", None, "#99", "e2", "the flights", True, "#12.5"])
def test_a_next_step_that_follows_nothing_listed_is_dropped(after):
    entry = {**NEXT, "after": after}
    assert candidates.found_row(entry, {}, follows=follows([DONE], {"e1": DENTIST})) is None
    assert candidates.found_row({**NEXT, "after": "#12"}, {}) is None  # a list with nothing done or ended
    # An event with no id leaves nothing to follow.
    assert candidates.found_row({**NEXT, "after": "e1"}, {}, follows=follows(ended={"e1": {"title": "Dentist"}})) \
        is None


def test_a_next_steps_own_chat_wins_and_other_kinds_ignore_after():
    row = candidates.found_row({**NEXT, "after": "e1", "chat": "c1"}, {"c1": ("", CHAT)},
                               follows=follows([DONE], {"e1": DENTIST}))
    assert (row["chat_id"], row["source"], row["ref"]) == (CHAT, "chat", "after:event:e_dentist")
    row = candidates.found_row({**NEXT, "kind": "follow-up", "after": "#12"}, {}, follows=follows([DONE]))
    assert row["kind"] == "follow-up" and "ref" not in row


def test_the_list_parses_next_steps_against_what_it_showed():
    reply = {"remembered": [], "found": [{**NEXT, "after": "#12"}, {**NEXT, "line": "Send a thank-you note"},
                                         {**NEXT, "after": "e1", "line": "Book the cleaning"}]}
    rows = candidates.parse(reply, follows([DONE], {"e1": DENTIST}))
    assert [(row["text"], row["ref"]) for row in rows] == [
        ("Book a hotel in Lisbon for the 12th to the 15th", "after:item:12"),
        ("Book the cleaning", "after:event:e_dentist")]


def test_the_list_asks_for_next_steps_and_what_each_follows():
    seen = {}

    def llm(instructions, text):
        seen.update(instructions=instructions)
        return {"remembered": [], "found": []}, 1

    candidates.ask(llm, gathered(), nudge="balanced")
    assert f'- "kind" (found only): one of {", ".join(report.LIST_KINDS)}.' in seen["instructions"]
    assert '"after" (found, kind "next" only)' in seen["instructions"]
    assert f"At most {candidates.MAX_NEXT} next steps." in seen["instructions"]
    # Only the list finds one: never the report as it was, nor a deadline.
    assert report.NEXT in report.KINDS and report.NEXT not in report.FOUND_KINDS
    assert report.LIST_KINDS == report.FOUND_KINDS + (report.NEXT,) and "deadline" not in report.LIST_KINDS


def candidate(kind="next", value=5, **more):
    return {"origin": candidates.MODEL, "item_id": None, "kind": kind, "profile": "", "chat_id": "", "source": "",
            "area": "", "due_at": None, "actionable": True, "value": value, "bring_up": True, "skip": "",
            "text": "x", **more}


def test_at_most_two_next_steps_come_up_in_a_report():
    rows = [candidate(value=5) for _ in range(3)] + [candidate(kind="follow-up", value=4) for _ in range(3)]
    candidates.route(rows)
    assert [row["decision"] for row in rows] == [candidates.SURFACED] * 2 + [candidates.OVER_LIMIT] + \
        [candidates.SURFACED] * 3
    assert [row["rank"] for row in rows if row["rank"]] == [1, 2, 3, 4, 5]
    # Not even ones the model says were asked for, due within the hour: a next step is never a floor.
    now = time.time()
    rows = [candidate(asked=True, due_at=now + 600) for _ in range(3)]
    candidates.route(rows, now=now)
    assert [row["decision"] for row in rows] == [candidates.SURFACED] * 2 + [candidates.OVER_LIMIT]
    assert not any(row["floor"] for row in rows)


def test_next_steps_start_held_for_the_morning_brief(live):
    assert learning.DEFAULT_LEVELS["next"] == "brief" and learning.KIND_NAMES["next"] == "Next steps"
    assert learning.level(live.store, "next") == "brief"
    rows = [candidate(), candidate(kind="follow-up")]
    candidates.decide(live.store, rows, now=time.time())
    assert [(row["decision"], row["level"]) for row in rows] == [(candidates.SURFACED, "brief"),
                                                                 (candidates.SURFACED, "push")]
    # The user turns them up, or off.
    learning.set_level(live.store, "next", "screen", time.time())
    rows = [candidate()]
    candidates.decide(live.store, rows, now=time.time())
    assert rows[0]["level"] == "screen"
    learning.set_level(live.store, "next", "off", time.time())
    rows = [candidate()]
    candidates.decide(live.store, rows, now=time.time())
    assert rows[0]["decision"] == candidates.OFF


def test_a_next_step_after_something_private_is_private(live):
    gift = said(live, "The user will order Ana's birthday gift.", "I need to order Ana's birthday gift", "commitment")
    flights = said(live, "The user will book the flights to Lisbon.", "book the flights", "commitment")
    row = lambda ref, text="Wrap it before Friday": {"origin": "model", "item_id": None, "text": text, "ref": ref}
    assert push.private_row(live.store, row(f"{candidates.AFTER_ITEM}{gift}"))
    assert not push.private_row(live.store, row(f"{candidates.AFTER_ITEM}{flights}"))
    assert not push.private_row(live.store, row(f"{candidates.AFTER_EVENT}e_planning"))
    assert push.private_row(live.store, row(f"{candidates.AFTER_EVENT}e_dentist", "Book the dentist's cleaning"))
    assert not push.private_row(live.store, row(f"{candidates.AFTER_ITEM}x"))
    # One after an event goes by its why, which names the event.
    assert push.private_row(live.store, {**row(f"{candidates.AFTER_EVENT}e_9"), "why": "Your therapy session ended."})
    assert not push.private_row(live.store, {**row(f"{candidates.AFTER_EVENT}e_9"), "why": "The planning meeting ended."})
    # With the store unreadable, one after any remembered item is private; one after an event isn't.
    assert push.private_row(None, row(f"{candidates.AFTER_ITEM}{flights}"))
    assert not push.private_row(None, row(f"{candidates.AFTER_EVENT}e_planning"))


def test_pulse_candidates_says_what_a_next_step_follows():
    row = {**candidate(), "decision": candidates.SURFACED, "rank": 1, "card_id": None, "why": "", "level": "brief",
           "text": "Book a hotel", "source": "chat"}
    assert "[next] v5 (after #12) (chat) [brief] Book a hotel" in candidates.describe(
        {**row, "ref": f"{candidates.AFTER_ITEM}12"})
    assert "[next] v5 (after an event that ended) (chat) [brief]" in candidates.describe(
        {**row, "ref": f"{candidates.AFTER_EVENT}e_dentist"})


# The whole way ---------------------------------------------------------------------------

HOTEL = {**NEXT, "line": "Book a hotel in Lisbon for the 12th to the 15th",
         "why": "You booked the flights in Deploy this afternoon; nothing shows a hotel for the 12th to the 15th."}


def booked_flights(live, now):
    live.store.set("memory", True)
    flights = commitment(live, "The user will book the flights to Lisbon for the 12th.",
                         words="I need to book flights to Lisbon for the 12th", said_at=now - 5 * 3600)
    done_in_chat(live, flights, "Booked the flights! Out on the 12th, back on the 15th.", at=now - 3600)
    return flights


def listed_at(live, now):
    pulse.tick(now)
    pulse._report_thread.join(5)
    run = live.store.get(pulse.REPORT_RUN)
    [call] = live.llm.calls
    return run, call["input"][0]["text"]


def test_flights_booked_in_a_chat_bring_up_the_hotel_held_for_the_brief(reporting):
    live = reporting
    now = time.time()
    flights = booked_flights(live, now)
    live.llm.replies.append({"remembered": [], "found": [{**HOTEL, "after": f"#{flights}"}]})
    run, asked = listed_at(live, now)
    assert f"{report.DONE_TITLE}\n- #{flights} [commitment] The user will book the flights to Lisbon for the 12th." \
        in asked and 'a chat showed it done: "Booked the flights! Out on the 12th, back on the 15th."' in asked
    [row] = live.store.run_candidates(run["id"])
    assert (row["kind"], row["decision"], row["level"], row["ref"], row["chat_id"], row["source"]) == (
        "next", candidates.SURFACED, "brief", f"after:item:{flights}", CHAT, "chat")
    pulse.tick(now + 1)
    [sent] = live.sent()
    assert sent.splitlines()[4] == "1. [next] Book a hotel in Lisbon for the 12th to the 15th"
    assert pulse.pre_llm_call(SID, "t1", sent).startswith(report.REPORT_LOOK_NOTE)
    reply = "1. [next] Book a hotel in Lisbon for the 12th to the 15th"
    assert not pulse.pushes(SID, "t1", reply)  # held for the brief: no push
    deliver(live, sent, reply)
    pulse.tick(now + 2)
    [card] = live.store.cards()
    assert (card["kind"], card["topic"], card["level"], card["chat_id"], card["chat_title"], card["news"],
            card["why"]) == (store.NOTICED, "next", "brief", CHAT, "Deploy", HOTEL["line"], HOTEL["why"])
    assert live.store.held_count() == 1  # the morning brief brings it


def test_a_dentist_visit_that_ended_brings_up_booking_the_cleaning(reporting):
    live = reporting
    now = time.time()
    live.store.set("memory", True)
    cleaning = said(live, "The dentist wants a cleaning every six months.",
                    "my dentist wants a cleaning twice a year", at=now - 5 * 86400)
    calendar(live.home, event("e_dentist", "Dentist", now - 3 * 3600, now - 2 * 3600, location="12 High St, London"),
             now=now)
    book = {**NEXT, "after": "e1", "line": "Book the cleaning your dentist wants", "source": "calendar",
            "area": "health", "why": "Your dentist visit ended this afternoon, and your dentist wants a cleaning every "
                                     "six months; nothing in your calendar shows one."}
    live.llm.replies.append({"remembered": [], "found": [book]})
    run, asked = listed_at(live, now)
    assert f"{report.ENDED_TITLE}\n- e1 Dentist (" in asked and ", at 12 High St, London\n  Pulse remembers: " \
        f"#{cleaning} [fact] The dentist wants a cleaning every six months." in asked
    # What Pulse remembers about the dentist is listed too; the model said nothing about it.
    rows = {row["kind"]: row for row in live.store.run_candidates(run["id"])}
    assert (rows["fact"]["item_id"], rows["fact"]["decision"]) == (cleaning, candidates.UNANSWERED)
    row = rows["next"]
    assert (row["kind"], row["decision"], row["level"], row["ref"], row["chat_id"], row["source"]) == (
        "next", candidates.SURFACED, "brief", "after:event:e_dentist", "", "calendar")
    pulse.tick(now + 1)
    [sent] = live.sent()
    pulse.pre_llm_call(SID, "t1", sent)
    deliver(live, sent, "1. [next] Book the cleaning your dentist wants")
    pulse.tick(now + 2)
    [card] = live.store.cards()
    # From no chat: it points at the Pulse chat, as anything found in the calendar does.
    assert (card["topic"], card["level"], card["chat_id"], card["why"]) == ("next", "brief", SID, book["why"])


def test_a_next_step_already_in_the_calendar_brings_up_nothing(reporting):
    live = reporting
    now = time.time()
    flights = booked_flights(live, now)
    tomorrow = now + 86400
    calendar(live.home, event("e_hotel", "Hotel Lisboa check-in", tomorrow, tomorrow + 3600), now=now)
    live.llm.replies.append({"remembered": [], "found": [
        {**HOTEL, "after": f"#{flights}", "bring_up": False, "value": 1, "skip": "covered",
         "why": "The hotel is in your calendar for tomorrow."},
        {**HOTEL, "line": "Pack for Lisbon", "why": "A trip is coming up."}]})  # a tip that follows nothing
    run, asked = listed_at(live, now)
    # What the list read shows both: the flights booked, and the hotel already in the calendar.
    assert f"- #{flights} [commitment]" in asked and re.search(r"\n- v\d+ Hotel Lisboa check-in \(", asked)
    assert [(row["kind"], row["decision"]) for row in live.store.run_candidates(run["id"])] == [
        ("next", candidates.SKIPPED)]
    pulse.tick(now + 1)
    [sent] = live.sent()
    assert sent.endswith("Reply with exactly: Nothing worth bringing up right now.")
