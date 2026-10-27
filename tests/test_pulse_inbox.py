"""Deadlines Pulse memory keeps from the Shortcuts inbox (pulse_memory.read_inbox,
forget_inbox): designs/pulse-inbox-deadlines.md."""

import json
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from test_pulse import _load, ROOT, cli, control, memory, report, store
from test_pulse_followup import followup, live, promise  # noqa: F401
from test_pulse_dates import Model, at, candidates, dates, last_run, learning, look, looking

inbox_store = _load("conduit_push.inbox_store", ROOT / "inbox_store.py")
UTC = report.zone_for("UTC")
DEVICE = "phone-installation-1"


class Phone:
    """The phone's Shortcuts automations logging to the inbox: one device,
    keyed once, its batches stored ``ago`` seconds back, since Pulse reads
    only what settled (inbox_store.SETTLE_S)."""

    def __init__(self, home, device=DEVICE):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

        self.home, self.device, self.batches = home, device, 0
        private = X25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        answer = inbox_store.InboxStore(home).register_key({"device_id": device, "public_key": inbox_store.b64u(public)})
        host_public = inbox_store.unb64u(answer["host_public_key"])
        self.kid = answer["kid"]
        self.key = inbox_store.derive_key(private.exchange(X25519PublicKey.from_public_bytes(host_public)), device,
                                          public, host_public)

    def log(self, *events, ago=120.0):
        self.batches += 1
        batch_id = f"batch-{self.batches:04d}"
        batch = {"batch_id": batch_id, "device": {"name": "iPhone", "time_zone": "UTC"}, "events": list(events)}
        nonce, ct = inbox_store.seal(self.key, json.dumps(batch).encode(),
                                     inbox_store.ingest_aad(self.kid, self.device, batch_id))
        return inbox_store.InboxStore(self.home, clock=lambda: time.time() - ago).ingest(
            {"v": 1, "device_id": self.device, "kid": self.kid, "batch_id": batch_id, "nonce": nonce, "ct": ct})

    def delete(self, *ids):
        return inbox_store.InboxStore(self.home).delete_events({"device_id": self.device, "kid": self.kid,
                                                                "ids": list(ids)})


def event(event_id, text, *, logged=None, kind="email", source="Netflix", title=""):
    when = datetime.fromtimestamp(time.time() - 3600 if logged is None else logged, timezone.utc)
    return {"id": event_id, "kind": kind, "at": when.isoformat(timespec="seconds"), "source": source,
            "title": title, "text": text}


def words(day):
    return f"{day.day} {day:%B}"


class Reader:
    """The inbox's model: the queued replies in turn (an exception is
    raised), keeping what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked = []

    def __call__(self, instructions, text):
        self.asked.append((instructions, text))
        reply = self.replies.pop(0) if self.replies else {"deadlines": [], "closed": []}
        if isinstance(reply, Exception):
            raise reply
        return reply, 40


def read(live, model, *, now=None, budget=float("inf")):
    done = {}
    memory.read_inbox(live.store, live.home, model, zone=UTC, now=time.time() if now is None else now,
                      budget=budget, done=done)
    return done


def kept(live, *items, mark=None):
    """``items`` (deadlines) kept as a read of the inbox keeps them; their ids."""
    mark = mark or [int(time.time()), DEVICE, f"m{time.time_ns()}"]
    rows = [{"kind": "deadline", "explicit": 1, "message_id": 0, "same_as": None, "replaces": None, "late": False,
             "sender": "Netflix", **item} for item in items]
    return live.store.keep_inbox(mark=mark, after=live.store.get(store.INBOX_MARK), items=rows)


def trial(due, *, logged, event_id="e1", text="Netflix free trial ends"):
    return {"text": text, "evidence": f"Your free trial ends on {words(due)}", "due": due.isoformat(),
            "due_at": datetime(due.year, due.month, due.day, tzinfo=timezone.utc).timestamp(),
            "mentioned_at": logged, "from_inbox": f"{DEVICE}/{event_id}"}


# Reading the inbox ---------------------------------------------------------------

def test_a_deadline_is_kept_only_when_its_words_and_its_day_are_in_the_event():
    logged = datetime(2026, 10, 27, 9, 15, tzinfo=timezone.utc).timestamp()
    events = [{"device": DEVICE, "id": "e1", "kind": "email", "at_utc": logged, "source": "Netflix",
               "title": "Your trial", "text": "Your free trial ends on Tuesday 3 November. Cancel any time.",
               "amount": ""}]
    good = {"text": "Netflix free trial ends", "evidence": "Your free trial ends on Tuesday 3 November",
            "event": "E1", "due": "2026-11-03", "due_words": "Tuesday 3 November"}
    found = memory._deadline(good, events, UTC, [])
    assert (found["kind"], found["due"], found["from_inbox"], found["sender"], found["mentioned_at"],
            found["message_id"]) == ("deadline", "2026-11-03", f"{DEVICE}/e1", "Netflix", logged, 0)
    assert memory._deadline({**good, "event": 1}, events, UTC, [])["due"] == "2026-11-03"
    for bad in ({"evidence": "Your trial ended"},  # not what the event says
                {"due_words": "3 December"},  # the day's words aren't in it
                {"due": "2026-10-26", "due_words": "3 November"},  # before the day it was logged
                {"due": "2026-11-04"},  # Tuesday is the 3rd
                {"due": "2027-12-03", "due_words": "3 November"},  # over DEADLINE_WITHIN_S out
                {"event": "E2"}, {"event": True}, {"event": "E" + "1" * 5000}, {"text": ""}, {"due": None}):
        assert memory._deadline({**good, **bad}, events, UTC, []) is None, bad
    # A plain repeat the model missed: the same day, in the same words.
    remembered = [{"id": 7, "text": "Netflix free trial ends", "evidence": "x", "due": "2026-11-03"}]
    assert memory._deadline(good, events, UTC, remembered)["same_as"] == 7
    assert memory._deadline({**good, "replaces": 7}, events, UTC, remembered)["replaces"] == 7
    # Read once its day is over: kept as expired.
    day = datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp()
    assert memory._deadline(good, events, UTC, [], now=day + 3600)["late"] is False
    assert memory._deadline(good, events, UTC, [], now=day + 86400)["late"] is True


def test_memory_reads_what_the_phone_logged_once_and_keeps_its_deadlines(live):
    live.store.set("time_zone", "UTC")
    phone = Phone(live.home)
    due = datetime.now(timezone.utc).date() + timedelta(days=10)
    phone.log(event("e1", f"Your free trial ends on {words(due)}. Cancel any time."),
              event("e2", "Tap to pay at the door", kind="nfc", source="Office"),
              event("e3", "20% off ends tonight", source="Shop"))
    model = Reader({"deadlines": [
        {"text": "Netflix free trial ends", "evidence": f"Your free trial ends on {words(due)}", "event": "E1",
         "due": due.isoformat(), "due_words": words(due)}], "closed": []})
    done = read(live, model)
    [(instructions, text)] = model.asked
    assert "other people's words" in instructions and "deadlines" in instructions
    assert "[E1] email, logged " in text and ", from Netflix: Your free trial ends on" in text
    assert "[E2] email" in text and "[E3]" not in text and "20% off" in text and "Tap to pay" not in text  # NFC skipped
    [item] = live.store.deadlines()
    assert (item["kind"], item["text"], item["due"], item["from_inbox"], item["sender"], item["chat_id"],
            item["profile"]) == ("deadline", "Netflix free trial ends", due.isoformat(), f"{DEVICE}/e1", "Netflix",
                                 "", "")
    assert done["inbox_batches"] == 1 and done["deadlines"] == 1 and done["tokens"] == 40
    mark = live.store.get(store.INBOX_MARK)
    assert mark[1:] == [DEVICE, "e3"]
    last = live.store.get(memory.INBOX_LAST)
    assert (last["events"], last["batches"], last["deadlines"], last["error"]) == (2, 1, 1, "")
    # Nothing new: no call. Then only what arrived since.
    read(live, model)
    assert len(model.asked) == 1
    phone.log(event("e4", "Your order has shipped", source="Shop"))
    read(live, model)
    assert len(model.asked) == 2 and "[E1] email" in model.asked[1][1] and "shipped" in model.asked[1][1] \
        and "[E2]" not in model.asked[1][1]
    # Stored in the last minute: it waits for the next read, so nothing can sort in behind the mark.
    phone.log(event("e5", "Your bill is ready", source="Hydro"), ago=10)
    read(live, model)
    assert len(model.asked) == 2


def test_a_first_read_goes_back_a_week_and_off_it_reads_nothing(live):
    phone = Phone(live.home)
    phone.log(event("old", "Your trial ends soon", logged=time.time() - 9 * 86400), ago=9 * 86400)
    phone.log(event("new", "Your bill is due on Friday", source="Hydro"))
    model = Reader()
    live.store.set(store.INBOX_SETTING, False)
    read(live, model)
    assert model.asked == []
    live.store.set(store.INBOX_SETTING, True)
    read(live, model)
    [(_, text)] = model.asked
    assert "Your bill is due" in text and "Your trial ends soon" not in text
    assert memory.inbox_on(live.store) and store.DEFAULTS[store.INBOX_SETTING] is True


def test_a_deadline_already_over_when_read_is_kept_as_expired(live):
    live.store.set("time_zone", "UTC")
    phone = Phone(live.home)
    logged = time.time() - 5 * 86400
    over = datetime.fromtimestamp(logged, timezone.utc).date() + timedelta(days=2)
    phone.log(event("e1", f"Your bill is due on {words(over)}.", source="Hydro", logged=logged))
    done = read(live, Reader({"deadlines": [{"text": "Bill due", "evidence": f"Your bill is due on {words(over)}",
                                             "event": "E1", "due": over.isoformat(), "due_words": words(over)}]}))
    assert live.store.deadlines() == [] and done["deadlines"] == 0
    [item_id] = live.store.items_from_inbox()
    assert live.store.item(item_id)["status"] == "expired"


def test_a_repeat_moves_a_deadline_a_new_date_replaces_it_and_a_payment_closes_it(live):
    live.store.set("time_zone", "UTC")
    phone = Phone(live.home)
    today = datetime.now(timezone.utc).date()
    due, later = today + timedelta(days=10), today + timedelta(days=12)
    phone.log(event("e1", f"Your electricity bill is due on {words(due)}.", source="Hydro", logged=time.time() - 7200))
    bill = {"text": "Electricity bill due", "event": "E1"}
    read(live, Reader({"deadlines": [{**bill, "evidence": f"due on {words(due)}", "due": due.isoformat(),
                                      "due_words": words(due)}]}))
    [first] = live.store.deadlines()
    phone.log(event("e2", f"Reminder: your electricity bill is due on {words(due)}.", source="Hydro"))
    model = Reader({"deadlines": [{**bill, "evidence": "Reminder: your electricity bill", "due": due.isoformat(),
                                   "due_words": words(due), "same_as": first["id"]}]})
    read(live, model)
    assert f"#{first['id']} Electricity bill due (due {due.isoformat()}) (logged on the phone from Hydro " \
        in model.asked[0][1]
    [again] = live.store.deadlines()
    assert again["id"] == first["id"] and again["mentioned_at"] > first["mentioned_at"]
    phone.log(event("e3", f"Your bill's due date moved to {words(later)}.", source="Hydro"))
    read(live, Reader({"deadlines": [{**bill, "evidence": "Your bill's due date moved", "due": later.isoformat(),
                                      "due_words": words(later), "replaces": first["id"]}]}))
    [moved] = live.store.deadlines()
    assert moved["id"] != first["id"] and moved["due"] == later.isoformat()
    assert live.store.item(first["id"])["superseded_by"] == moved["id"]
    [card] = live.store.add_cards([{"kind": learning.NOTICED, "level": "brief", "topic": "deadline",
                                    "item_id": moved["id"], "news": "Electricity bill due", "created_at": time.time()}])
    phone.log(event("e4", "Payment received. Thank you!", source="Hydro"))
    read(live, Reader({"deadlines": [], "closed": [{"number": moved["id"], "status": "done",
                                                    "evidence": "Payment received", "event": "E1"}]}))
    assert live.store.deadlines() == []
    closed = live.store.item(moved["id"])
    assert closed["status"] == "done" and closed["closed_by"] == f"inbox {DEVICE}/e4: Payment received"
    assert live.store.done_marks([card])  # its card gets Pulse's done mark (Done elsewhere)


def test_a_chat_can_close_a_deadline(live):
    logged = time.time() - 3600
    [item_id] = kept(live, trial(date.today() + timedelta(days=5), logged=logged))
    remembered = live.store.remembered_items(profile="", limit=50, reminded_since=0)
    assert [row["id"] for row in remembered] == [item_id]
    assert "(logged on the phone from Netflix " in memory.render_remembered(remembered, UTC)
    chunk = [{"id": 3, "role": "user", "content": "I cancelled the Netflix trial this morning", "timestamp": logged}]
    entry = memory._closed({"number": item_id, "status": "cancelled", "evidence": "I cancelled the Netflix trial",
                            "line": 1}, chunk, remembered)
    assert entry == {"id": item_id, "status": "cancelled", "evidence": "I cancelled the Netflix trial", "message_id": 3}
    assert "deadline" in memory.CLOSABLE and "deadlines" in memory.INSTRUCTIONS
    live.store.advance("", "work", message_id=3, at=logged, closed=[entry])
    assert live.store.item(item_id)["status"] == "cancelled"


def test_a_failed_read_is_tried_again_later_and_skipped_after_too_many(live):
    phone = Phone(live.home)
    phone.log(event("e1", "Your bill is due on Friday", source="Hydro"))
    now = time.time()
    done = read(live, Reader(RuntimeError("down")), now=now)
    assert done["failed"] == 1 and live.store.get(store.INBOX_MARK) is None
    assert live.store.get(memory.INBOX_FAILED)["count"] == 1
    model = Reader()
    read(live, model, now=now + 60)  # waits for its retry
    assert model.asked == []
    read(live, Reader(RuntimeError("down")), now=now + memory.RETRY_S + 1)
    read(live, Reader({"oops": True}), now=now + 2 * memory.RETRY_S + 2)
    assert live.store.get(memory.INBOX_FAILED)["count"] == memory.MAX_FAILURES
    done = read(live, model, now=now + 3 * memory.RETRY_S + 3)
    assert model.asked == [] and done["skipped"] == 1
    assert live.store.get(store.INBOX_MARK)[1:] == [DEVICE, "e1"] and live.store.get(memory.INBOX_FAILED) is None
    # `pulse inbox` says what was skipped.
    assert live.store.get(memory.INBOX_LAST)["skipped"] == 1
    live.store.set("memory", True)
    assert "0 events, 0 new deadlines; 1 skipped after failed reads" in cli._inbox_line(live.store, UTC)


def test_another_reader_or_turning_it_off_meanwhile_keeps_nothing(live):
    assert kept(live, trial(date.today() + timedelta(days=5), logged=time.time()), mark=[1, DEVICE, "a"]) is not None
    stale = live.store.keep_inbox(mark=[2, DEVICE, "b"], after=None, items=[])
    assert stale is None and live.store.get(store.INBOX_MARK) == [1, DEVICE, "a"]
    live.store.set(store.INBOX_SETTING, False)
    assert live.store.keep_inbox(mark=[2, DEVICE, "b"], after=[1, DEVICE, "a"], items=[]) is None


# Coming up -----------------------------------------------------------------------

def test_a_deadline_comes_up_a_week_ahead_and_the_day_before_held_for_the_brief(live):
    looking(live)
    [item_id] = kept(live, trial(date(2026, 10, 21), logged=at(13, 9)))
    deadline = dates.deadlines(live.store)[0]
    assert (deadline.key, deadline.on, deadline.occasion) == (f"deadline:{item_id}", date(2026, 10, 21), "deadline")
    due = lambda day: [(entry.days, entry.stage) for entry in dates.coming([deadline], date(2026, 10, day))]
    assert [due(day) for day in (6, 14, 19, 20, 21, 22)] == [
        [], [(7, "soon")], [(2, "soon")], [(1, "last")], [(0, "last")], []]
    look(live, at(14, 6, 30))
    [card] = live.store.cards()
    assert (card["kind"], card["level"], card["topic"], card["item_id"], card["chat_id"]) == (
        learning.NOTICED, "brief", "deadline", item_id, "")
    assert card["news"] == "Netflix free trial ends on Wednesday 21 October."
    assert card["quote"] == "Your free trial ends on 21 October"
    assert card["why"] == ("From Netflix, logged on your phone on 13 October 2026. Pulse brings up deadlines from "
                           "your Shortcuts inbox a week ahead and the day before.")
    [row] = last_run(live)
    assert (row["origin"], row["kind"], row["item_id"], row["source"], row["area"], row["floor"], row["ref"]) == (
        candidates.DATE, "deadline", item_id, "inbox", "", 1, f"date:deadline:{item_id}:2026-10-21:soon")
    look(live, at(15, 6, 30))
    assert len(live.store.cards()) == 1  # once a week ahead
    look(live, at(20, 6, 30))
    assert live.store.cards()[0]["news"] == "Netflix free trial ends tomorrow, Wednesday 21 October."
    look(live, at(21, 6, 30))
    assert len(live.store.cards()) == 2  # the day before covers the day


def test_done_on_a_deadlines_card_closes_it_so_the_day_before_doesnt_come(live):
    looking(live)
    [item_id] = kept(live, trial(date(2026, 10, 21), logged=at(13, 9)))
    look(live, at(14, 6, 30))
    [card] = live.store.cards()
    assert live.store.mark_done(card["id"], card["item_id"], closed_by=learning.DONE_BY, now=at(14, 8))
    assert live.store.item(item_id)["status"] == "done"
    look(live, at(20, 6, 30))
    assert len(live.store.cards()) == 1


def test_the_model_words_a_deadline_and_sees_it_apart_from_the_rest(live):
    looking(live)
    [item_id] = kept(live, trial(date(2026, 10, 21), logged=at(13, 9)))
    model = Model({"dates": [{"ref": "D1", "bring_up": True, "value": 5,
                              "line": "Your Netflix trial ends next Wednesday: cancel before then if you don't want it."}],
                   "ahead": []})
    look(live, at(14, 6, 30), model)
    [(instructions, text)] = model.asked
    assert "deadlines in them that the phone logged" in instructions
    assert ("## Deadlines in the next two weeks, from what the phone logged\nD1 [deadline] Netflix free trial ends "
            "(logged from Netflix, 13 Oct 2026: \"Your free trial ends on 21 October\"): Wednesday 21 October, in 7 "
            "days. Bring it up today: a week ahead.") in text
    assert "## What Pulse remembers" not in text  # the deadline is the dates' own
    [card] = live.store.cards()
    assert card["news"].startswith("Your Netflix trial ends next Wednesday") and card["item_id"] == item_id


def test_deadlines_start_held_for_the_brief_and_turned_off_never_come_up(live):
    looking(live)
    assert learning.levels(live.store) == {"followup": "push", "deadline": "brief", "next": "brief"}
    assert learning.KIND_NAMES["deadline"] == "Deadlines"
    kept(live, trial(date(2026, 10, 21), logged=at(13, 9)))
    control.set_level(live.store, "deadline", "off")
    look(live, at(14, 6, 30))
    assert live.store.cards() == []
    [row] = last_run(live)
    assert row["decision"] == candidates.OFF
    assert learning.preference_words("place", "source:inbox") == "things from the Shortcuts inbox"


def test_a_report_sees_a_deadline_from_the_inbox_and_never_finds_one(live):
    logged = time.time() - 3600
    [item_id] = kept(live, trial(date.today() + timedelta(days=5), logged=logged))
    item = live.store.item(item_id)
    listed, shown = report.remembered_lines([item], UTC)
    assert "(open items it kept from the chats it has read and what the phone logged)" in listed
    assert f"#{item_id} [deadline] Netflix free trial ends (due " in listed and "(logged on the phone from Netflix " \
        in listed
    row = candidates.remembered_row(item, {"area": "money", "bring_up": True})
    assert (row["kind"], row["source"], row["area"]) == ("deadline", "inbox", "")
    assert "deadline" in report.KINDS and "deadline" not in report.FOUND_KINDS
    assert candidates.found_row({"kind": "deadline", "line": "Bill due Friday"}, {})["kind"] == "other"
    assert followup.chat_title(live.home, item) == ""  # no chat to point at


def test_a_reports_line_is_a_deadline_only_when_it_names_one_pulse_remembers(live, monkeypatch):
    """A report turn without a candidate list writes its own lines, so its
    model could tag one it found "[deadline]": that line's card and a
    rating typed for it count as "other", as does a line naming something
    else Pulse remembers or something the report didn't show, on a card and
    in a rating alike. Both go by what the report showed, even once the
    item it names is closed."""
    [deadline] = kept(live, trial(date.today() + timedelta(days=5), logged=time.time() - 3600))
    other = promise(live)
    lines = [("deadline", "Electricity bill due Friday", None), ("deadline", "Netflix free trial ends", deadline),
             ("deadline", "Hermes will say when the deploy finishes", other), ("deadline", "Rent due", other + 100)]
    cards = learning.add_noticed_cards(
        live.store, turn=types.SimpleNamespace(session_id="pulse", reply_id=7), lines=lines, chat_id="pulse",
        chat_title="Pulse", shown={deadline: "deadline", other: "promise"}, kinds=report.KINDS,
        item_chat_title=lambda item: "", now=time.time())
    assert [live.store.card(card)["topic"] for card in cards] == ["other", "deadline", "other", "other"]
    assert live.store.card(cards[1])["item_id"] == deadline
    reply = (f"1. [deadline] Electricity bill due Friday\n2. [Deadline] Netflix free trial ends #{deadline}\n"
             f"3. [deadline] Review PR #12\n4. [deadline] Hermes will say when the deploy finishes #{other}")
    assert report.report_items(reply) == {1: "other", 2: "other", 3: "other", 4: "other"}  # naming no deadline
    turn = types.SimpleNamespace(reply=reply, session_id="pulse", prompt=(
        f"{report.REMEMBERED_TITLE}\n#{deadline} [deadline] Netflix free trial ends\n"
        f"#{other} [promise] Hermes will tell the user when the deploy finishes."))
    assert report.shown_kinds(turn.prompt) == {deadline: "deadline", other: "promise"}
    monkeypatch.setattr(report.hermes, "find_turn", lambda home, **kwargs: turn)
    monkeypatch.setattr(report.hermes, "user_text_after", lambda home, **kwargs: ["1 yes, 2 yes, 3 no, 4 no"])
    items, ratings = report.ratings_after(live.home, chat_id="pulse", tag="p1", since=0.0)
    assert items == {1: "other", 2: "deadline", 3: "other", 4: "other"}
    assert ratings == [report.Rating(1, "other", True), report.Rating(2, "deadline", True, deadline),
                       report.Rating(3, "other", False), report.Rating(4, "other", False, other)]
    # A report made from a candidate list: its picks say what each remembered item is.
    items, _ = report.ratings_after(live.home, chat_id="pulse", tag="p1", since=0.0, shown={other: "promise"})
    assert items == {1: "other", 2: "other", 3: "other", 4: "other"}
    items, _ = report.ratings_after(live.home, chat_id="pulse", tag="p1", since=0.0, shown={deadline: "deadline"})
    assert items == {1: "other", 2: "deadline", 3: "other", 4: "other"}
    # Both closed before the cards are written: each card goes by what the report showed, as its rating does.
    for item_id in (deadline, other):
        assert live.store.close_item(item_id, status="done", closed_by="test")
    late = learning.add_noticed_cards(
        live.store, turn=types.SimpleNamespace(session_id="pulse", reply_id=8), lines=lines[1:3], chat_id="pulse",
        chat_title="Pulse", shown={deadline: "deadline", other: "promise"}, kinds=report.KINDS,
        item_chat_title=lambda item: "", now=time.time())
    assert [(live.store.card(card)["topic"], live.store.card(card)["item_id"]) for card in late] == [
        ("deadline", None), ("other", None)]


# Deleting ------------------------------------------------------------------------

def test_deleting_an_event_deletes_its_deadline_and_an_aged_out_one_stays(live, monkeypatch):
    phone = Phone(live.home)
    now = time.time()
    phone.log(event("e0", "z", logged=now - 10800), event("e1", "a", logged=now - 7200),
              event("e2", "b", logged=now - 3600))
    ids = kept(live, trial(date.today() + timedelta(days=5), logged=now - 7200, event_id="e1"),
               trial(date.today() + timedelta(days=6), logged=now - 3600, event_id="e2"),
               # Older than every event kept: aged out of the inbox (or past its cap), not deleted.
               trial(date.today() + timedelta(days=7), logged=now - 3 * 86400, event_id="old"))
    assert memory.forget_inbox(live.store, live.home, now) == 0
    phone.delete("e2")
    assert memory.forget_inbox(live.store, live.home, now) == 1
    assert [item["id"] for item in live.store.deadlines()] == [ids[0], ids[2]]
    # While the inbox can't be read, nothing is taken for deleted.
    monkeypatch.setattr(inbox_store.InboxStore, "present", lambda self, refs: None)
    phone.delete("e1")
    assert memory.forget_inbox(live.store, live.home, now) == 0
    monkeypatch.undo()
    assert memory.forget_inbox(live.store, live.home, now) == 1
    # A phone wiped, or the whole inbox: gone with it.
    inbox_store.InboxStore(live.home).wipe(DEVICE)
    assert memory.forget_inbox(live.store, live.home, now) == 1 and live.store.deadlines() == []


def test_pairing_the_phone_again_keeps_the_deadlines_from_what_it_sent(live):
    now = time.time()
    phone = Phone(live.home)
    phone.log(event("e1", "a", logged=now - 7200))
    kept(live, trial(date.today() + timedelta(days=5), logged=now - 7200, event_id="e1"))
    again = Phone(live.home)  # a new key: the inbox drops what the earlier one sent
    assert inbox_store.InboxStore(live.home).present([(DEVICE, "e1")]) == (set(), {DEVICE: (30, None)})
    assert memory.forget_inbox(live.store, live.home, now) == 0
    again.log(event("e2", "b", logged=now - 60))
    assert memory.forget_inbox(live.store, live.home, now) == 0 and len(live.store.deadlines()) == 1


def test_a_deadline_is_deleted_a_day_after_it_stops_standing(live):
    now = time.time()
    first, second = kept(live, trial(date.today() + timedelta(days=5), logged=now - 7200, event_id="x1"),
                         trial(date.today() + timedelta(days=6), logged=now - 7200, event_id="x2"))
    third = kept(live, {**trial(date.today() + timedelta(days=7), logged=now - 7200, event_id="x3"),
                        "replaces": second})[0]
    # The phone's newer event is still there, so x1 to x3 aged out of the inbox: not deleted for that.
    Phone(live.home).log(event("e9", "z", logged=now - 60))
    live.store.close_item(first, status="cancelled", closed_by="user")  # `pulse memory forget`
    assert memory.forget_inbox(live.store, live.home, time.time() + 3600) == 0  # Undo still works
    assert memory.forget_inbox(live.store, live.home, time.time() + store.INBOX_FORGET_S + 1) == 1
    assert live.store.item(first) is None
    # Replaced: it goes with the one that replaced it, so the chain holds while that stands.
    assert live.store.item(second)["superseded_by"] == third
    live.store.close_item(third, status="done", closed_by="user")
    assert memory.forget_inbox(live.store, live.home, time.time() + store.INBOX_FORGET_S + 1) == 2
    assert live.store.item(second) is None and live.store.item(third) is None


def test_the_inbox_routes_delete_the_deadlines_of_what_they_delete(monkeypatch, tmp_path):
    from test_inbox import Phone as ApiPhone, _batch, _client, _event

    client = _client(monkeypatch, tmp_path)
    phone = ApiPhone()
    phone.accept(client.post("/api/plugins/conduit_push/inbox/key", json=phone.key_request()).json())
    at_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client.post("/api/plugins/conduit_push/inbox/ingest",
                json=phone.seal(_batch(_event("evt-1", at=at_now), _event("evt-2", at=at_now))))
    pulse_store = store.open_store(tmp_path)
    rows = [{"kind": "deadline", "text": f"t{n}", "evidence": "e", "explicit": 1, "due": "2026-11-03",
             "due_at": None, "message_id": 0, "mentioned_at": time.time(), "from_inbox": f"{phone.device_id}/evt-{n}",
             "sender": "Desjardins"} for n in (1, 2)]
    pulse_store.keep_inbox(mark=[1, phone.device_id, "evt-2"], after=None, items=rows)
    deleted = client.post("/api/plugins/conduit_push/inbox/delete",
                          json={"device_id": phone.device_id, "kid": phone.kid, "ids": ["evt-1"]})
    assert deleted.status_code == 200 and [item["text"] for item in pulse_store.deadlines()] == ["t2"]
    client.post("/api/plugins/conduit_push/inbox/wipe", json={"all": True})
    assert pulse_store.deadlines() == [] and pulse_store.items_from_inbox() == []
    # Where Pulse never ran, it leaves no file behind.
    assert memory.forget_deleted(tmp_path / "elsewhere") == 0 and not (tmp_path / "elsewhere").exists()


# Settings ------------------------------------------------------------------------

def test_turning_it_off_forgets_the_deadlines_and_where_it_read_to(live):
    kept(live, trial(date.today() + timedelta(days=5), logged=time.time()))
    live.store.set(memory.INBOX_LAST, {"at": time.time(), "events": 3, "deadlines": 1})
    assert control.state(live.store, live.home)["inbox_deadlines"] is True
    control.update(live.store, {"inbox_deadlines": False}, live.home)
    assert live.store.deadlines() == [] and live.store.get(store.INBOX_MARK) is None
    # Its last read went with what it found, so on again it says nothing was read yet.
    assert live.store.get(memory.INBOX_LAST) is None
    assert control.state(live.store, live.home)["inbox_deadlines"] is False
    with pytest.raises(control.Refused):
        control.update(live.store, {"inbox_deadlines": "no"}, live.home)
    control.update(live.store, {"inbox_deadlines": True}, live.home)
    assert memory.inbox_on(live.store)
    # Should turning it off fail to forget them, the loop's next tick does, its event still logged.
    now = time.time()
    Phone(live.home).log(event("e1", "a", logged=now - 3600))
    [item_id] = kept(live, trial(date.today() + timedelta(days=5), logged=now - 3600))
    assert memory.forget_inbox(live.store, live.home, now) == 0
    live.store.set(store.INBOX_SETTING, False)
    assert memory.forget_inbox(live.store, live.home, time.time()) == 1
    assert live.store.item(item_id) is None and live.store.get(store.INBOX_MARK) is None
    assert memory.forget_inbox(live.store, live.home, time.time()) == 0


def test_what_pulse_remembers_lists_a_deadline_with_who_its_from(live):
    logged = time.time() - 3600
    [item_id] = kept(live, trial(date.today() + timedelta(days=5), logged=logged))
    [listed] = control.items(live.store, "deadline")
    assert (listed["kind"], listed["from_inbox"], listed["sender"], listed["logged_at"], listed["chat_id"]) == (
        "deadline", True, "Netflix", logged, "")
    assert control.items(live.store, "fact") == []
    described = memory.describe(live.store.item(item_id), UTC)
    assert described.startswith(f"#{item_id} Netflix free trial ends (due ")
    assert "(from Netflix, logged " in described and "inferred" not in described


def test_the_commands_show_switch_and_list_the_deadlines(live, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    live.store.set("time_zone", "UTC")
    live.store.set("memory", True)
    run = lambda action, value="": cli._pulse(types.SimpleNamespace(pulse_action=action, value=value, extra=""))
    today = datetime.fromtimestamp(time.time(), UTC).date()
    due = today + timedelta(days=10)
    [item_id] = kept(live, trial(due, logged=time.time() - 3600))
    assert run("inbox") == 0
    out = capsys.readouterr().out
    assert "Deadlines from the Shortcuts inbox: on; 1 kept; nothing read yet" in out
    assert (f"- #{item_id} Netflix free trial ends: {dates.day_words(due)} {due.year}, from Netflix; comes up "
            f"{dates.day_words(due - timedelta(days=7))}, a week ahead") in out
    assert run("memory", "deadline") == 0 and "Deadline (1)" in capsys.readouterr().out
    run("status")
    assert "Deadlines from the Shortcuts inbox: on; 1 kept" in capsys.readouterr().out
    assert run("inbox", "soon") == 2
    assert run("inbox", "off") == 0
    assert "and forgot the 1 it kept" in capsys.readouterr().out and live.store.deadlines() == []
    assert run("inbox") == 0 and "Deadlines from the Shortcuts inbox: off" in capsys.readouterr().out
    assert run("inbox", "on") == 0 and memory.inbox_on(live.store)
    kept(live, trial(due, logged=time.time() - 3600))
    assert run("inbox") == 0 and "Read what the phone logged up to " in capsys.readouterr().out
    live.store.set(store.INBOX_MARK, [1, DEVICE])  # not a mark
    assert run("inbox") == 0 and "Read what the phone logged" not in capsys.readouterr().out
