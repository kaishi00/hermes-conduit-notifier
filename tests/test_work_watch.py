"""The watch (work_watch): a helper model reads what the workstreams report
as they work and tells the Bot Chat when one may affect another. It only
points: the Bot Chat decides, and no workstream hears from the watch."""
import json
import time

import pytest

from test_work import (BOT_CHAT, WORKER, _chat_db, batched, call, home, hermes, on, start,  # noqa: F401
                       store_mod, work)

watch = work.work_watch
WORKER_2 = "20261031_work_2"


class Helper:
    """The helper model: answers what it's set to, and keeps what it read."""

    def __init__(self, answer=None, tokens=120):
        self.answer, self.tokens, self.read = answer or {"flag": False}, tokens, []

    def __call__(self, instructions, text):
        self.read.append(text)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, self.tokens


@pytest.fixture
def told(monkeypatch):
    """The watch's lines for the Bot Chat, as work_tool queues them."""
    monkeypatch.setattr(work, "WAKE_BATCH_S", 60)
    monkeypatch.setattr(watch, "_tell", work._watch_tell)
    monkeypatch.setattr(watch, "_ago", work._ago)
    yield
    batched()


def two(on):
    start()
    start("Billing export", "Move the billing export to the v1 users API.")
    assert on.get("W2").session_id == WORKER_2


FLAG = {"flag": True, "between": ["W1", "W2"], "why": "W1 found the v1 users API is gone, and W2 is moving to it"}


def test_a_finding_that_touches_other_work_reaches_the_bot_chat_while_it_runs(on, hermes, home, told):
    two(on)
    call("work_progress", {"found": "GET /v1/users now returns 410 Gone"}, session_id=WORKER)
    call("work_progress", {"assumes": "the v1 users API stays"}, session_id=WORKER_2)
    batched()
    helper = Helper(FLAG)
    flag = watch.check(home, ask=helper)
    assert flag == {"ref": "W1", "with": ["W2"], "why": FLAG["why"], "woke": True}
    page, = helper.read
    assert "W1 “Login bug” found, just now: «GET /v1/users now returns 410 Gone»" in page
    assert "W2 “Billing export” (" in page and "Its task: «Move the billing export to the v1 users API.»" in page
    line, = batched()
    assert "thinks W1 “Login bug” and W2 “Billing export” may affect each other" in line and FLAG["why"] in line
    assert "not a finding" in line and "W1 found, just now: «GET /v1/users now returns 410 Gone»" in line
    assert "W2 relies on, just now: «the v1 users API stays»" in line
    assert on.get("W1").status in ("starting", "running")  # still at work: nothing was sent to it
    assert on.outbox_rows("W1", "send") == [] and on.outbox_rows("W2", "send") == []
    note = work.digest(BOT_CHAT)
    assert "· the helper reading reports thought, just now, that this and W2 may affect each other" in note
    # Read once: nothing new, no second call.
    assert watch.check(home, ask=helper) is None and len(helper.read) == 1


def test_one_workstream_alone_is_not_read(on, hermes, home, told):
    start()
    call("work_progress", {"found": "the login page loads 4 MB of scripts"}, session_id=WORKER)
    helper = Helper(FLAG)
    assert watch.check(home, ask=helper) is None and helper.read == []
    assert batched() == []


def test_nothing_to_flag_starts_no_turn_and_is_counted(on, hermes, home, told):
    two(on)
    call("work_progress", {"found": "the login page loads 4 MB of scripts"}, session_id=WORKER)
    helper = Helper({"flag": False}, tokens=90)
    assert watch.check(home, ask=helper) is None
    assert batched() == [] and on.events("W1", kinds=("flag",)) == []
    seen = watch.metrics(on, time.time() - 60)
    assert (seen["watch_reads"], seen["watch_tokens"], seen["flags"]) == (1, 90, 0)


@pytest.mark.parametrize("answer", [
    {"flag": True, "between": ["W1"], "why": "it matters"},  # to nothing else
    {"flag": True, "between": ["W2"], "why": "it matters"},  # not what reported
    {"flag": True, "between": ["W1", "W9"], "why": "it matters"},  # no such work
    {"flag": True, "between": ["W1", "W2"], "why": ""},
    {"flag": "yes", "between": ["W1", "W2"], "why": "it matters"},
    "W1 and W2, obviously",
])
def test_a_flag_names_another_workstream_and_says_why(on, hermes, home, told, answer):
    two(on)
    call("work_progress", {"found": "the v1 users API is gone"}, session_id=WORKER)
    assert watch.check(home, ask=Helper(answer)) is None
    assert batched() == []


def test_a_read_that_failed_is_read_again_with_the_next_line(on, hermes, home, told):
    two(on)
    call("work_progress", {"found": "the v1 users API is gone"}, session_id=WORKER)
    with pytest.raises(RuntimeError):
        watch.check(home, ask=Helper(RuntimeError("no model")))
    call("work_progress", {"changed": "the client now calls v2"}, session_id=WORKER)
    helper = Helper(FLAG)
    assert watch.check(home, ask=helper)["woke"]
    assert "the v1 users API is gone" in helper.read[0] and "the client now calls v2" in helper.read[0]


def test_what_the_user_wrote_in_a_workstreams_chat_is_read_too(on, hermes, home, told):
    two(on)
    _chat_db(home, [(WORKER, "user", on.outbox_rows("W1", "start")[0]["text"], None),
                    (WORKER, "user", "Drop v1 altogether, we're on v2 from Monday", None)])
    helper = Helper({"flag": True, "between": ["W1", "W2"], "why": "the user is dropping v1, which W2 moves to"})
    assert watch.check(home, words={"W1"}, ask=helper)["ref"] == "W1"
    assert "W1 “Login bug” was told by the user in its chat" in helper.read[0]
    assert "«Drop v1 altogether, we're on v2 from Monday»" in helper.read[0]


def test_past_the_hourly_limit_a_flag_is_kept_and_the_bot_chat_told_once(on, hermes, home, told, monkeypatch):
    monkeypatch.setattr(watch, "FLAGS_PER_HOUR", 2)
    two(on)
    for number in range(3):
        call("work_progress", {"found": f"finding number {number}"}, session_id=WORKER)
        flag = watch.check(home, ask=Helper(FLAG))
        assert flag["woke"] == (number < 2)
    assert len(batched()) == 2
    note = work.digest(BOT_CHAT)
    assert "flagged 1 more thing in the last hour than it may start a turn for (2 an hour)" in note
    assert "flagged 1 more thing" not in work.digest(BOT_CHAT)  # said once
    seen = watch.metrics(on, time.time() - 60)
    assert (seen["flags"], seen["flags_held"]) == (3, 1)
    # The helper is shown what was flagged already.
    call("work_progress", {"found": "another"}, session_id=WORKER)
    helper = Helper()
    watch.check(home, ask=helper)
    assert "Already flagged in the last hour:" in helper.read[0] and "- W1 with W2: «" in helper.read[0]


def test_with_coordination_off_nothing_is_read(on, hermes, home, told):
    two(on)
    on.set_coordination("off")
    call("work_progress", {"found": "the v1 users API is gone"}, session_id=WORKER)
    helper = Helper(FLAG)
    assert watch.check(home, ask=helper) is None and helper.read == []


def test_a_report_and_a_turns_start_ask_for_a_read(on, hermes, home, monkeypatch):
    asked = []
    monkeypatch.setattr(watch, "noticed", lambda where, ref, words=False: asked.append((ref, words)))
    two(on)
    call("work_progress", {"steps": [{"step": "Read", "state": "doing"}]}, session_id=WORKER)
    assert asked == []  # a checklist alone is nothing to read
    call("work_progress", {"found": "the v1 users API is gone"}, session_id=WORKER)
    work.turn_started(WORKER_2)
    assert asked == [("W1", False), ("W2", True)]


def test_reads_wait_a_moment_and_run_one_at_a_time(on, hermes, home, monkeypatch):
    ran = []

    class Ctx:
        llm = object()

    monkeypatch.setattr(watch, "_ctx", Ctx())
    monkeypatch.setattr(watch, "_tell", lambda *args: None)
    monkeypatch.setattr(watch, "BATCH_S", 0.01)
    monkeypatch.setattr(watch, "check", lambda where, words=(): ran.append(set(words)))
    watch.noticed(home, "W1")
    watch.noticed(home, "W2", words=True)
    deadline = time.time() + 5
    while not ran and time.time() < deadline:
        time.sleep(0.01)
    watch.stop()
    assert ran == [{"W2"}]


def test_the_work_panel_lists_what_coordination_did_and_why(on, hermes, home, told):
    two(on)
    call("work_progress", {"found": "GET /v1/users now returns 410 Gone"}, session_id=WORKER)
    watch.check(home, ask=Helper(FLAG))
    item = on.outbox_add("W2", "send", WORKER_2, "Furina passes on: v1 is gone, check before you move to it", own=True)
    on.outbox_done(item, "accepted")
    work.work_coord.sent(on, on.get("W2"), "v1 is gone, check before you move to it", item, "steered", True)
    theirs = on.outbox_add("W2", "send", WORKER_2, "Also add a CSV option")
    work.work_coord.sent(on, on.get("W2"), "Also add a CSV option", theirs, "steered", False)
    made = on.proposal_add("W2", "Move W2 to the v2 users API")
    listed = work.app_list(home)["coordination"]
    assert listed["mode"] == "ask"
    proposal, sent, flag = listed["items"]
    assert (proposal["id"], proposal["kind"], proposal["status"], proposal["title"]) == (made, "proposal", "open",
                                                                                         "Billing export")
    assert (sent["kind"], sent["ref"], sent["state"], sent["how"]) == ("sent", "W2", "waiting",
                                                                       "added to the turn it was working on")
    assert [(line["ref"], line["kind"]) for line in sent["why"]] == [("W1", "found"), ("W1", "flag")]
    assert (flag["kind"], flag["ref"], flag["with"], flag["woke"], flag["text"]) == ("flag", "W1", ["W2"], True,
                                                                                    FLAG["why"])
    assert flag["why"] == [{"ref": "W1", "title": "Login bug", "kind": "found",
                            "text": "GET /v1/users now returns 410 Gone", "at": flag["why"][0]["at"]}]
    on.outbox_delivered(item)
    assert work.app_list(home)["coordination"]["items"][1]["state"] == "delivered"
    on.set_coordination("off")
    assert work.app_list(home)["coordination"] == {"mode": "off", "items": []}
    assert json.dumps(listed)  # plain data for the route


def test_more_lines_than_one_read_takes_are_read_oldest_first_and_the_rest_next(on, hermes, home, told, monkeypatch):
    two(on)
    monkeypatch.setattr(watch, "NEW_EVENTS", 2)
    again = []
    monkeypatch.setattr(watch, "noticed", lambda home, ref, **_: again.append(ref))
    for number in (1, 2, 3):
        call("work_progress", {"found": f"finding {number}"}, session_id=WORKER)
    again.clear()
    with pytest.raises(RuntimeError):  # a read that failed asks for no other: the next report brings one
        watch.check(home, ask=Helper(RuntimeError("no model")))
    assert again == []
    helper = Helper()
    assert watch.check(home, ask=helper) is None and again == [""]
    assert "«finding 1»" in helper.read[0].split("The open workstreams:")[0]
    assert "«finding 3»" not in helper.read[0].split("The open workstreams:")[0]
    assert watch.check(home, ask=helper) is None and again == [""]
    assert "«finding 3»" in helper.read[1].split("The open workstreams:")[0]


def test_work_no_bot_chat_coordinates_is_neither_read_nor_shown(on, hermes, home, told, monkeypatch):
    two(on)
    monkeypatch.setattr(watch, "_watched", lambda work: work.ref != "W2")
    call("work_progress", {"found": "the v1 users API is gone"}, session_id=WORKER)
    call("work_progress", {"assumes": "the v1 users API stays"}, session_id=WORKER_2)
    helper = Helper(FLAG)
    assert watch.check(home, ask=helper) is None and helper.read == []


def test_the_flag_keeps_the_other_sides_earlier_line(on, hermes, home, told):
    two(on)
    call("work_progress", {"assumes": "the v1 users API stays"}, session_id=WORKER_2)
    assert watch.check(home, ask=Helper()) is None
    call("work_progress", {"found": "GET /v1/users now returns 410 Gone"}, session_id=WORKER)
    assert watch.check(home, ask=Helper(FLAG))["woke"]
    line = batched()[-1]
    assert "W2 relies on, just now: «the v1 users API stays»" in line and "W1 found" in line


def test_flags_kept_without_a_turn_dont_reopen_the_hours_allowance(on, hermes, home, told, monkeypatch):
    two(on)
    monkeypatch.setattr(watch, "FLAGS_PER_HOUR", 1)
    for number in range(45):
        call("work_progress", {"found": f"finding {number}"}, session_id=WORKER)
        flag = watch.check(home, ask=Helper(FLAG))
        assert flag["woke"] is (number == 0)


def test_a_long_report_keeps_the_line_the_flag_is_about(on, hermes, home, told):
    two(on)
    call("work_progress", {"found": ["GET /v1/users now returns 410 Gone", "the docs say v2", "v2 needs a token",
                                     "the token is in the vault"]}, session_id=WORKER)
    assert watch.check(home, ask=Helper(FLAG))["woke"]
    assert "«GET /v1/users now returns 410 Gone»" in batched()[-1]


def test_what_the_user_wrote_while_it_ran_is_read_when_the_turn_ends(on, hermes, home, told, monkeypatch):
    two(on)
    seen = []
    monkeypatch.setattr(watch, "noticed", lambda home, ref, **_: seen.append(ref))
    monkeypatch.setattr(work.work_coord, "read_user_words", lambda home, store, item: 1)
    work._ledger_turn_end(home, on, on.get("W1"), "done", failed=False, began=time.time())
    assert seen == ["W1"]
