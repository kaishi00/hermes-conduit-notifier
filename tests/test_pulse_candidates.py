"""The report's candidate list (pulse_candidates, designs/pulse-learning.md
slice 1): a model call lists what the report could bring up, code picks
what goes in, and every candidate is journaled with what became of it."""

import re
import sqlite3
import sys
import threading
import time
import types
from datetime import datetime

import pytest

from test_pulse import FIXTURE_SCHEMA, ROOT, SID, StateDB, _load, cli, host, pulse, pulse_store, report, store  # noqa: F401
from test_pulse_followup import CHAT, commitment, fake_redactor, live  # noqa: F401

candidates = _load("conduit_push.pulse_candidates", ROOT / "pulse_candidates.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")

DAY = 1_792_454_400.0  # 2026-10-20 00:00 UTC
SINCE = "Mon 19 Oct 08:00"


def item(n, kind="commitment", due_at=None, chat_id=CHAT):
    return {"id": n, "kind": kind, "profile": "", "chat_id": chat_id, "due_at": due_at,
            "text": f"The user will do thing {n}."}


def gathered(shown=(), resting=(), chats=None, text="the context"):
    return report.Gathered(text, SINCE, list(shown), list(resting), chats or {})


def candidate(origin=candidates.MODEL, value=3, bring_up=True, skip="", due_at=None, item_id=None, **more):
    return {"origin": origin, "item_id": item_id, "kind": "other", "profile": "", "chat_id": "", "source": "",
            "area": "", "due_at": due_at, "actionable": None, "value": value, "bring_up": bring_up, "skip": skip,
            "text": "x", **more}


# Asking ----------------------------------------------------------------------

def test_the_list_call_fences_its_context_and_names_the_bar():
    seen = {}

    def llm(instructions, text):
        seen.update(instructions=instructions, text=text)
        return {"remembered": [], "found": []}, 9

    context = gathered(text="#1 [commitment] x\ncontext 123>>>")
    assert candidates.ask(llm, context, nudge="gentle") == ([], 9)
    fence = re.search(r"<<<context (\w+)\n", seen["text"]).group(1)
    assert seen["text"].endswith(f"context {fence}>>>") and fence not in context.text
    assert seen["text"].startswith(f"Pulse report list. It covers {SINCE} onward.")
    assert report.BARS["gentle"] in seen["instructions"] and "At most 5 in all." in seen["instructions"]
    assert ", ".join(report.KINDS) in seen["instructions"]


@pytest.mark.parametrize("reply", [None, [], {"items": []}, "text", {"remembered": "all of them"}])
def test_a_reply_that_isnt_a_list_is_a_bad_reply(reply):
    with pytest.raises(candidates.BadReply):
        candidates.ask(lambda instructions, text: (reply, 1), gathered(), nudge="balanced")


def test_every_remembered_item_shown_is_answered_for_or_marked_unanswered():
    shown = [item(1), item(2, kind="promise", due_at=DAY), item(3)]
    reply = {"remembered": [
        {"ref": 2, "bring_up": True, "value": 4, "skip": "", "actionable": True,
         "line": "2. [promise] I still owe you the build check #2"},
        {"ref": "#1", "bring_up": False, "value": "2", "skip": "Covered.", "actionable": False, "line": ""},
        {"ref": 2, "bring_up": False, "value": 1},  # answered already
        {"ref": 99, "bring_up": True, "value": 5, "line": "never shown"},
        "not an entry"], "found": []}
    rows = candidates.parse(reply, gathered(shown, resting=[(item(4), report.RATED)]))
    assert [(row["item_id"], row.get("decision")) for row in rows] == [
        (2, None), (1, None), (3, candidates.UNANSWERED), (4, candidates.RESTING)]
    owed, covered, _, rested = rows
    assert owed == {"origin": "memory", "item_id": 2, "kind": "promise", "profile": "", "chat_id": CHAT,
                    "source": "chat", "area": "", "due_at": DAY, "actionable": True, "value": 4, "bring_up": True,
                    "skip": "", "text": "I still owe you the build check"}
    assert (covered["skip"], covered["value"], covered["text"]) == ("covered", 2, "The user will do thing 1.")
    assert rested["skip"] == report.RATED


def test_found_candidates_need_a_line_and_say_where_they_came_from():
    def entry(**more):
        return {"kind": "date", "bring_up": True, "value": 4, "skip": "", "actionable": True,
                "line": "Ana lands at 18:00 (calendar)", "source": "calendar", "area": "Social", **more}

    reply = {"remembered": [], "found": [
        entry(), entry(kind="weird", chat="C1", source="notes", line="- Your deploy is flaky (Deploy)"),
        entry(line="  "), entry(chat="c9", source="radio", area="space", value=9, bring_up="yes", actionable=1)]
        + [entry(line=f"extra {n}") for n in range(20)]}
    rows = candidates.parse(reply, gathered(chats={"c1": ("nahida", CHAT)}))
    assert len(rows) == candidates.MAX_FOUND
    first, second, third = rows[:3]
    assert (first["origin"], first["kind"], first["source"], first["area"], first["chat_id"], first["item_id"]) == (
        "model", "date", "calendar", "social", "", None)
    assert (second["kind"], second["source"], second["profile"], second["chat_id"], second["text"]) == (
        "other", "chat", "nahida", CHAT, "Your deploy is flaky (Deploy)")
    # An unknown chat key, source and area name nothing; a wild value, a pick or a flag that isn't one count as none.
    assert (third["source"], third["area"], third["chat_id"], third["value"], third["bring_up"],
            third["actionable"]) == ("", "", "", None, False, None)


@pytest.mark.parametrize("value, expected", [(3, 3), (3.6, 4), ("5", 5), (0, None), (6, None), (True, None),
                                             ("lots", None), (float("inf"), None), (None, None)])
def test_a_value_guess_is_one_to_five(value, expected):
    assert candidates._value(value) == expected


@pytest.mark.parametrize("line, ref, expected", [
    ("1. [date] Ana lands at 18:00", None, "Ana lands at 18:00"),
    ("- [follow-up] Chase the invoice #12", 12, "Chase the invoice"),
    ("Check PR #12 today", 12, "Check PR #12 today"),
    ("3.5 hours of meetings tomorrow", None, "3.5 hours of meetings tomorrow"),
    ("-5°C tonight: take the coat", None, "-5°C tonight: take the coat"),
    ("[Work] the offsite moved to Friday", None, "[Work] the offsite moved to Friday"),
])
def test_a_line_loses_only_what_the_report_line_adds(line, ref, expected):
    assert candidates._line(line, ref=ref) == expected


# Deciding ----------------------------------------------------------------------

def test_the_router_keeps_the_models_picks_most_useful_first_up_to_five():
    rows = [candidate(value=3), candidate(value=5, skip="covered"), candidate(value=4, bring_up=False),
            candidate(value=4), candidate(origin="memory", value=4, item_id=7),
            candidate(origin="memory", value=4, item_id=8, due_at=DAY + 60), candidate(value=None),
            candidate(value=2), candidate(value=1),
            candidate(origin="memory", value=5, item_id=9, decision=candidates.RESTING)]
    assert candidates.route(rows) is rows
    assert [(row["decision"], row["rank"]) for row in rows] == [
        ("surfaced", 4), ("skipped", None), ("not_picked", None), ("surfaced", 3), ("surfaced", 2),
        ("surfaced", 1), ("over_limit", None), ("surfaced", 5), ("over_limit", None), ("resting", None)]
    assert [row["value"] for row in candidates.picks(rows)] == [4, 4, 4, 3, 2]


def test_a_remembered_item_rated_no_meanwhile_rests_whatever_the_model_said(live):
    now = time.time()
    item_id = commitment(live)
    [card] = live.store.add_cards([{"kind": store.NOTICED, "item_id": item_id, "level": "screen", "title": "x",
                                    "news": "x", "why": "x", "created_at": now - 60}])
    learning.rate(live.store, card, "down", now=now - 30)
    rows = [candidates._remembered(live.store.item(item_id), {"bring_up": True, "value": 5, "line": "Renew it"})]
    candidates.decide(live.store, rows, now=now)
    assert (rows[0]["decision"], rows[0]["skip"]) == (candidates.RESTING, report.RATED)
    learning.rate(live.store, card, "up", now=now - 20)  # changed back: back to what the model said
    rows = [candidates._remembered(live.store.item(item_id), {"bring_up": True, "value": 5, "line": "Renew it"})]
    candidates.decide(live.store, rows, now=now)
    assert (rows[0]["decision"], rows[0]["skip"]) == (candidates.SURFACED, "")


# Saying ----------------------------------------------------------------------

def test_the_report_turn_says_the_picks_as_report_lines():
    picked = [{"kind": "date", "text": "Ana lands at 18:00", "item_id": None},
              {"kind": "promise", "text": "I still owe you the build check", "item_id": 12}]
    text = candidates.report_text("pab-1", picked, SINCE)
    assert text.startswith("Pulse report pab-1.") and f"since {SINCE}" in text
    lines = "1. [date] Ana lands at 18:00\n2. [promise] I still owe you the build check #12"
    assert lines in text
    # Said back as asked, the reply splits into the same items the cards are made from.
    assert learning.report_lines(lines) == candidates.lines(picked) == [
        ("date", "Ana lands at 18:00", None), ("promise", "I still owe you the build check", 12)]
    nothing = candidates.report_text("pab-1", [], SINCE)
    assert nothing.endswith("Reply with exactly: Nothing worth bringing up right now.")
    assert learning.empty_turn("report", "Nothing worth bringing up right now.")


# The journal ----------------------------------------------------------------------

def test_the_journal_takes_a_run_only_while_it_is_current_and_keeps_thirty_days(pulse_store):
    run = {"id": "r1", "state": "working"}
    row = {**candidate(), "decision": candidates.NOT_PICKED, "rank": None}
    pulse_store.set("report_run", run)
    assert pulse_store.finish_run("report_run", run, [row], {**run, "state": "ready"}, now=DAY)
    assert pulse_store.get("report_run") == {"id": "r1", "state": "ready"}
    # Given up on meanwhile (sent without it, or another run): nothing more is journaled.
    assert not pulse_store.finish_run("report_run", run, [row], {**run, "state": "ready"}, now=DAY)
    [kept] = pulse_store.run_candidates("r1")
    assert (kept["run_id"], kept["created_at"], kept["decision"], kept["bring_up"]) == ("r1", DAY, "not_picked", 1)
    later = {"id": "r2", "state": "working"}
    pulse_store.set("report_run", later)
    picked = {**candidate(value=5), "decision": candidates.SURFACED, "rank": 1}
    assert pulse_store.finish_run("report_run", later, [row, picked], {**later, "state": "ready"},
                                  now=DAY + 31 * 86400)
    assert pulse_store.prune_candidates(DAY + 86400) == 1
    assert pulse_store.run_candidates("r1") == []  # past thirty days
    rows = pulse_store.run_candidates("r2")
    assert [r["rank"] for r in rows] == [1, None]  # the picks first
    [card] = pulse_store.add_cards([{"kind": store.NOTICED, "level": "screen", "title": "t", "created_at": DAY}],
                                   links=[rows[0]["id"]])
    assert [r["card_id"] for r in pulse_store.run_candidates("r2")] == [card, None]  # noted with the card
    assert pulse_store.candidate_runs(5) == ["r2"]
    with pytest.raises(ValueError):
        pulse_store.add_candidates("r3", [{**row, "decision) VALUES(1); --": 1}], now=DAY)


def test_the_list_reads_the_report_context_with_chat_keys(live):
    now = time.time()
    first = commitment(live, due="2026-10-30", due_at=now + 86400)
    live.store.set("memory", True)
    live.db.message("user", "The deploy is flaky again", sid=CHAT, at=now - 600)
    context = report.gather(live.home, live.store, home_chat_id=SID, now=now, room=candidates.CONTEXT_BUDGET,
                            extra="## Extra\nsomething", keyed=True)
    assert "### Deploy (c1)\n" in context.text and context.chats == {"c1": ("", CHAT)}
    assert [shown["id"] for shown in context.shown] == [first] and context.resting == []
    text = context.text
    assert text.index(report.REMEMBERED_TITLE) < text.index("## Extra") < text.index("### Deploy")
    assert "(c1)" not in report.build(live.home, live.store, home_chat_id=SID, now=now)[0]
    cut = report.gather(live.home, live.store, home_chat_id=SID, now=now, room=100, keyed=True)
    assert "(cut here: no room in this report)" in cut.text and cut.shown == []  # cut off: not shown to the model


def test_the_report_shows_what_later_chats_said_about_an_item_and_what_runs(live, monkeypatch):
    fake_redactor(monkeypatch)
    now = time.time()
    first = commitment(live, "The user will add iPad and Duo support in the patch after the 2.0 release.",
                       words="duo/ipad support didn't make it into the 2.0 release, it'll be in the patch after that",
                       said_at=now - 7 * 86400)
    live.store.set("memory", True)
    live.db.session(sid="ship", title="Conduit Build", pinned=0)
    live.db.message("assistant", "Version 2.2.1 is uploaded, with iPad and Duo support in this patch.", sid="ship",
                    at=now - 3600)
    live.db.message("assistant", "The 2.0 release and its iPad and Duo patch are still open.", at=now - 60)
    (live.home / "processes.json").write_text(
        '[{"session_id": "proc_1", "command": "watch-bridge.sh", "started_at": %f}]' % (now - 300))
    text = report.gather(live.home, live.store, home_chat_id=SID, now=now, room=candidates.CONTEXT_BUDGET,
                         keyed=True).text
    later = text.split("## Later on the same subject", 1)[1].split("\n\n", 1)[0]
    assert f"#{first}: " in later and "2.2.1 is uploaded" in later and "still open" not in later
    assert "## Running in the background now\n- watch-bridge.sh, since " in text
    assert text.index(report.REMEMBERED_TITLE) < text.index("## Later on the same subject")


def test_the_report_picks_the_closest_later_row_and_skips_the_items_own_continuation(live):
    now = time.time()
    first = commitment(live, "The user will add iPad and Duo support in the patch after the 2.0 release.",
                       words="duo/ipad support didn't make it into the 2.0 release, it'll be in the patch after that",
                       said_at=now - 7 * 86400)
    live.store.set("memory", True)
    live.db.session(sid="own-2", title="Continued", pinned=0, parent=CHAT)
    live.db.message("assistant", "iPad and Duo support for the patch after 2.0 release is still open.", sid="own-2",
                    at=now - 30)
    live.db.session(sid="ship", title="Conduit Build", pinned=0)
    live.db.message("assistant", "Uploaded 2.2.1 with iPad and Duo support in this patch release.", sid="ship",
                    at=now - 3600)
    live.db.message("assistant", "The iPad mockups look fine.", sid="ship", at=now - 60)
    text = report.gather(live.home, live.store, home_chat_id=SID, now=now, room=candidates.CONTEXT_BUDGET,
                         keyed=True).text
    later = text.split("## Later on the same subject", 1)[1].split("\n\n", 1)[0]
    assert f"#{first}: " in later and "Uploaded 2.2.1" in later
    assert "still open" not in later and "mockups" not in later


def test_later_lines_cover_watches_intentions_and_other_profiles(live):
    now = time.time()
    bot_home = live.home / "profiles" / "nahida"
    bot_home.mkdir(parents=True)
    bot = StateDB(bot_home, FIXTURE_SCHEMA)
    bot.session("b1", title="Bot Chat", pinned=0)
    bot.session("b2", title="Later", pinned=0)
    bot.message("assistant", "The landlord called back about the lease renewal, it's signed.", sid="b2", at=now - 60)
    live.db.session(sid="gym", title="Gym", pinned=0)
    live.db.message("user", "Started the morning running routine today, three kilometres.", sid="gym", at=now - 60)
    def item(n, kind, text, profile="", chat_id=CHAT):
        return {"id": n, "kind": kind, "text": text, "evidence": "", "profile": profile, "chat_id": chat_id,
                "valid_from": now - 86400}
    items = [item(1, "intention", "The user wants to start a morning running routine."),
             item(2, "watch", "Tell the user when the landlord answers about the lease renewal.", "nahida", "b1"),
             item(3, "fact", "The user likes morning running.")]
    text = report.later_lines(live.home, items, home_chat_id=SID, zone=datetime.now().astimezone().tzinfo)
    assert "#1: " in text and "running routine today" in text
    assert "#2: " in text and "it's signed" in text
    assert "#3: " not in text  # facts aren't followed


# The report, made from the list ------------------------------------------------------

class Llm:
    """Hermes's model access: each call gets the next reply queued (an
    exception raises), and is kept."""

    def __init__(self):
        self.replies = []
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else {"remembered": [], "found": []}
        if isinstance(reply, Exception):
            raise reply
        return types.SimpleNamespace(parsed=reply, usage=types.SimpleNamespace(total_tokens=321))


@pytest.fixture
def reporting(live, monkeypatch):
    """The loop with model access, and a report asked for. Only the test's
    own ticks run: a loop another test's plugin.register left running wakes
    when a list is done, and its tick would race the test's."""
    live.llm = Llm()
    monkeypatch.setattr(pulse._ctx, "llm", live.llm, raising=False)
    monkeypatch.setattr(pulse, "_report_thread", None)
    monkeypatch.setattr(pulse, "_maybe_memory", lambda *args: None)  # memory would call the model too
    tick = pulse.tick
    monkeypatch.setattr(pulse, "tick",
                        lambda now=None: tick(now) if threading.current_thread() is threading.main_thread() else None)
    live.store.set("report_requested", True)
    return live


def listed(live, now=None):
    """A tick that starts the list, once its thread is done."""
    pulse.tick(now)
    pulse._report_thread.join(5)
    return live.store.get(pulse.REPORT_RUN)


def deliver(live, text, reply):
    """The report turn as Hermes keeps it, and its reply."""
    live.db.message("user", text, display_kind="hidden")
    live.db.message("assistant", reply)


FOUND = {"kind": "follow-up", "bring_up": True, "value": 4, "skip": "", "actionable": True,
         "line": "Look at the flaky deploy (you asked in Deploy)", "source": "chat", "chat": "c1", "area": "work"}


def test_a_report_lists_its_candidates_then_says_what_code_picked(reporting):
    live = reporting
    now = time.time()
    item_id = commitment(live, due="2026-10-30", due_at=now + 3 * 86400)
    live.store.set("memory", True)
    live.db.message("user", "Can you look at the flaky deploy tomorrow?", sid=CHAT, at=now - 600)
    live.llm.replies.append({
        "remembered": [{"ref": item_id, "bring_up": True, "value": 3, "skip": "", "actionable": True,
                        "line": "Renew your passport before the trip (you said so in Deploy)"}],
        "found": [FOUND, {**FOUND, "kind": "fact", "bring_up": False, "value": 1, "skip": "minor",
                          "line": "Your sister is Ana"}]})
    pulse.tick(now)
    assert live.sent() == []  # the list comes first, on a thread of its own
    pulse._report_thread.join(5)
    run = live.store.get(pulse.REPORT_RUN)
    assert (run["state"], run["tokens"], run["since"]) == (pulse.RUN_READY, 321, report._local(
        now - report.WINDOW_S, pulse.report_zone(live.store, live.home)))
    [call] = live.llm.calls
    assert (call["purpose"], call["max_tokens"], call["json_mode"]) == ("pulse report", candidates.REPLY_TOKENS, True)
    asked = call["input"][0]["text"]
    assert f"#{item_id} [commitment]" in asked and "### Deploy (c1)" in asked and "flaky deploy" in asked
    pulse.tick(now + 1)
    [sent] = live.sent()
    assert sent.splitlines()[4:6] == [
        "1. [follow-up] Look at the flaky deploy (you asked in Deploy)",
        f"2. [commitment] Renew your passport before the trip (you said so in Deploy) #{item_id}"]
    [attempt] = live.store.in_flight()
    assert attempt["item_id"] == run["id"] and live.store.get(pulse.REPORT_RUN)["state"] == pulse.RUN_SENT
    assert pulse.pre_llm_call(SID, "t1", sent) == report.REPORT_NOTE
    reply = f"1. [follow-up] The deploy, reworded\n2. [commitment] The passport #{item_id}"
    # The push and the cards say the list's lines, whatever the turn made of them.
    assert pulse.pushes(SID, "t1", reply)
    assert pulse.push_copy(SID, "t1", reply) == {
        "title_label": "pulse.noticed", "title": "Pulse noticed 2 things", "count": 2,
        "body": "Look at the flaky deploy (you asked in Deploy) · "
                "Renew your passport before the trip (you said so in Deploy)"}
    deliver(live, sent, reply)
    pulse.tick(now + 2)
    found, remembered = sorted(live.store.cards(), key=lambda card: card["id"])
    assert (found["kind"], found["topic"], found["news"], found["item_id"], found["chat_id"]) == (
        store.NOTICED, "follow-up", "Look at the flaky deploy (you asked in Deploy)", None, SID)
    assert (remembered["item_id"], remembered["chat_id"], remembered["chat_title"], remembered["topic"]) == (
        item_id, CHAT, "Deploy", "commitment")
    assert [(row["decision"], row["card_id"]) for row in live.store.run_candidates(run["id"])] == [
        (candidates.SURFACED, found["id"]), (candidates.SURFACED, remembered["id"]), (candidates.SKIPPED, None)]
    pulse.tick(now + 3)
    assert len(live.store.cards()) == 2  # landed once
    pulse.tick(now + candidates.JOURNAL_DAYS * 86400 + 60)
    assert live.store.candidate_runs(5) == []  # kept thirty days


def test_a_list_that_picks_nothing_says_so_and_makes_no_card(reporting):
    live = reporting
    live.llm.replies.append({"remembered": [], "found": [{**FOUND, "bring_up": False}]})
    run = listed(live)
    pulse.tick()
    [sent] = live.sent()
    assert sent.endswith("Reply with exactly: Nothing worth bringing up right now.")
    assert pulse.pre_llm_call(SID, "t1", sent) == report.REPORT_NOTE
    assert not pulse.pushes(SID, "t1", "Nothing worth bringing up right now.")
    deliver(live, sent, "Nothing worth bringing up right now.")
    pulse.tick(time.time() + 1)
    assert live.store.cards() == []
    assert [row["decision"] for row in live.store.run_candidates(run["id"])] == [candidates.NOT_PICKED]


@pytest.mark.parametrize("reply", [RuntimeError("the model is down"), None, {"items": []}])
def test_a_list_that_fails_leaves_the_report_as_it_was(reporting, reply):
    live = reporting
    live.llm.replies.append(reply)
    assert listed(live)["state"] == pulse.RUN_FAILED
    pulse.tick()
    [sent] = live.sent()
    assert "write at most 5 numbered items" in sent  # the turn picks its own items, as it used to
    assert live.store.get(pulse.REPORT_RUN)["state"] == pulse.RUN_SENT
    assert live.store.candidate_runs(5) == []
    deliver(live, sent, "1. [follow-up] Look at the flaky deploy\n2. [date] Ana lands at 18:00")
    assert pulse.pre_llm_call(SID, "t1", sent) == report.REPORT_NOTE
    assert pulse.push_copy(SID, "t1", "1. [follow-up] Look at the flaky deploy\n2. [date] Ana lands at 18:00")[
        "body"] == "Look at the flaky deploy · Ana lands at 18:00"
    pulse.tick(time.time() + 1)
    assert sorted(card["news"] for card in live.store.cards()) == ["Ana lands at 18:00", "Look at the flaky deploy"]


def test_a_list_left_working_by_a_process_that_stopped_gives_way(reporting, monkeypatch):
    live = reporting
    monkeypatch.setattr(pulse, "_report_run", lambda *args: None)  # the process making it stopped
    now = time.time()
    working = listed(live, now)
    assert working["state"] == pulse.RUN_WORKING
    live.store.set("report_requested", True)
    pulse.tick(now + 60)
    assert live.sent() == [] and not live.store.get("report_requested")  # asked again: it's on its way
    pulse.tick(now + pulse.RUN_STALE_S + 1)
    [sent] = live.sent()
    assert "write at most 5 numbered items" in sent
    assert live.store.get(pulse.REPORT_RUN)["error"] == "left working"
    # The list, coming now, is too late: nothing journaled, nothing sent twice.
    row = {**candidate(), "decision": candidates.SURFACED, "rank": 1}
    assert not live.store.finish_run(pulse.REPORT_RUN, working, [row], {**working, "state": pulse.RUN_READY},
                                     now=now)
    pulse.tick(now + pulse.RUN_STALE_S + 60)
    assert len(live.sent()) == 1 and live.store.candidate_runs(5) == []


def test_a_list_is_sent_once_and_not_once_too_late(reporting):
    live = reporting
    run = listed(live)
    assert pulse._send_report(live.store, live.home, run, SID, time.time())
    assert not pulse._send_report(live.store, live.home, run, SID, time.time())  # another process read it ready too
    assert len(live.sent()) == 1
    old = {"id": "abcd1234abcd1234", "state": pulse.RUN_READY, "owner": "x",
           "started_at": time.time() - pulse.REPORT_WINDOW_S - 60}
    live.store.set(pulse.REPORT_RUN, old)
    pulse.tick()
    assert len(live.sent()) == 1 and live.store.get(pulse.REPORT_RUN)["state"] == pulse.RUN_DROPPED


def test_a_report_asked_for_past_a_list_too_old_to_send_starts_a_new_one(reporting):
    live = reporting
    old = {"id": "abcd1234abcd1234", "state": pulse.RUN_READY, "owner": "x",
           "started_at": time.time() - pulse.REPORT_WINDOW_S - 60}
    live.store.set(pulse.REPORT_RUN, old)
    fresh = listed(live)
    assert fresh["id"] != old["id"] and fresh["state"] == pulse.RUN_READY


def test_a_list_the_store_refuses_to_send_stays_ready(reporting):
    live = reporting
    run = listed(live)

    def refuse(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    add_attempt = live.store.add_attempt
    live.store.add_attempt = refuse
    with pytest.raises(sqlite3.OperationalError):
        pulse._send_report(live.store, live.home, run, SID, time.time())
    assert live.store.get(pulse.REPORT_RUN) == run  # the next tick tries again
    live.store.add_attempt = add_attempt
    assert pulse._send_report(live.store, live.home, run, SID, time.time()) and len(live.sent()) == 1


def test_a_list_hermes_refuses_says_so(reporting, monkeypatch):
    live = reporting
    run = listed(live)
    monkeypatch.setattr(pulse, "_sender", lambda pulse_store: lambda chat_id, text: host.Sent(False, "", "chat gone"))
    assert pulse._send_report(live.store, live.home, run, SID, time.time())
    assert live.store.get(pulse.REPORT_RUN)["refused"] == "chat gone"
    failed = {**run, "id": "f" * 16, "state": pulse.RUN_FAILED, "error": "BadReply"}  # the report as it was
    live.store.set(pulse.REPORT_RUN, failed)
    assert pulse._send_report(live.store, live.home, failed, SID, time.time())
    assert live.store.get(pulse.REPORT_RUN)["refused"] == "chat gone"


def test_typed_ratings_of_a_list_report_still_name_its_remembered_picks(reporting, monkeypatch):
    live = reporting
    now = time.time()
    item_id = commitment(live, due="2026-10-30", due_at=now + 3 * 86400)
    live.store.set("memory", True)
    live.llm.replies.append({"remembered": [{"ref": item_id, "bring_up": True, "value": 3, "skip": "",
                                             "line": "Renew your passport"}], "found": []})
    listed(live, now)
    pulse.tick(now + 1)
    [sent] = live.sent()
    assert report.shown_items(sent) == set()  # the turn lists no "What Pulse remembers" of its own
    [attempt] = live.store.attempts_since(pulse.REPORT, now - 60)
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)  # its cards never landed
    asked = []
    monkeypatch.setattr(report, "ratings_after", lambda home, **kwargs: asked.append(kwargs["shown"]) or ({}, []))
    monkeypatch.setattr(pulse, "_last_ratings", 0.0)
    pulse._collect_ratings(live.store, live.home, now + 2)
    assert asked == [{item_id}]  # so "1 no" still names the remembered item


def test_a_ref_written_as_a_whole_float_still_counts():
    assert [candidates._ref(value) for value in (12, 12.0, "#12", 12.5, True, 1e12)] == [12, 12, 12, None, None, None]


def test_without_model_access_the_report_goes_as_it_was(live):
    live.store.set("report_requested", True)
    pulse.tick()
    [sent] = live.sent()
    assert "write at most 5 numbered items" in sent and live.store.get(pulse.REPORT_RUN) is None


def test_candidates_command(reporting, capsys, monkeypatch):
    live = reporting
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="": cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse",  # noqa: E731
                                                              pulse_action="candidates", value=value))
    assert run() == 0
    assert "No candidate lists yet" in capsys.readouterr().out
    live.llm.replies.append({"remembered": [], "found": [FOUND, {**FOUND, "skip": "covered", "line": "The dentist"}]})
    listed(live)
    assert run("3") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Latest list: ready to send, 321 tokens" and out[1].endswith(": 1 picked of 2")
    assert out[2:] == ["  picked     [follow-up] v4 (chat) Look at the flaky deploy (you asked in Deploy)",
                       "  skipped    [follow-up] v4 (chat) The dentist (covered)"]
    assert run("lots") == 2
    capsys.readouterr()
    live.store.set(pulse.REPORT_RUN, {**live.store.get(pulse.REPORT_RUN), "state": "sent", "error": "BadReply"})
    run()
    assert capsys.readouterr().out.startswith("Latest list: failed (BadReply), so the report went out as before")
