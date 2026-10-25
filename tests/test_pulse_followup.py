"""Pulse follow-ups: Hermes keeps what it promised (pulse_followup)."""

import json
import re
import sqlite3
import sys
import time
import types
from datetime import datetime

import pytest

from test_pulse import SID, StateDB, brief_off_the_clock, FIXTURE_SCHEMA, FakeHost, ROOT, _load, cli, host, plugin, pulse, store

followup = _load("conduit_push.pulse_followup", ROOT / "pulse_followup.py")
CHAT = "work"


@pytest.fixture
def live(tmp_path, monkeypatch):
    fake = FakeHost().install(monkeypatch)
    ctx = types.SimpleNamespace(plugin_id="conduit_push", profile_name="default",
                                _manager=types.SimpleNamespace(has_tui_message_injector=True))
    monkeypatch.setattr(pulse, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(pulse, "_ctx", ctx)
    monkeypatch.setattr(pulse, "turns_marked", True)
    monkeypatch.setattr(pulse, "_started_at", time.time() - 10)
    monkeypatch.setattr(pulse, "_turns", {})
    monkeypatch.setattr(pulse, "_last_problem", "")
    monkeypatch.setattr(pulse, "_last_ratings", 0.0)
    monkeypatch.setattr(pulse, "_zone_cache", (0.0, None))
    monkeypatch.setattr(pulse, "_probe_due", lambda *_: False)
    pulse_store = store.open_store(tmp_path)
    pulse_store.set("daily_report", False)
    pulse_store.set("memory", False)
    pulse_store.set("enabled", True)
    brief_off_the_clock(pulse_store, tmp_path)
    db = StateDB(tmp_path, FIXTURE_SCHEMA)
    db.session()
    db.session(sid=CHAT, title="Deploy", pinned=0)
    sent = lambda: [params["text"] for name, params in fake.calls if name == host.SUBMIT]
    return types.SimpleNamespace(store=pulse_store, db=db, sent=sent, home=tmp_path, fake=fake)


def promise(live, text="Hermes will tell the user when the deploy finishes.", *, said_at=None, due_at=None):
    said_at = time.time() - 3600 if said_at is None else said_at
    message_id = live.db.message("assistant", "I'll let you know when the deploy finishes.", sid=CHAT, at=said_at)
    mark = live.store.watermark("", CHAT)
    [item_id] = live.store.advance("", CHAT, message_id=message_id, at=said_at,
                                   after_id=mark["message_id"] if mark else 0, items=[{
        "kind": "promise", "text": text, "evidence": "I'll let you know when the deploy finishes.", "explicit": 1,
        "due": "2026-10-22" if due_at else "", "due_at": due_at, "message_id": message_id, "mentioned_at": said_at}])
    return item_id


def commitment(live, text="The user will renew their passport.", *, said_at=None, due="", due_at=None,
               words="I need to renew my passport"):
    said_at = time.time() - 3600 if said_at is None else said_at
    message_id = live.db.message("user", words, sid=CHAT, at=said_at)
    mark = live.store.watermark("", CHAT)
    [item_id] = live.store.advance("", CHAT, message_id=message_id, at=said_at,
                                   after_id=mark["message_id"] if mark else 0, items=[{
        "kind": "commitment", "text": text, "evidence": words, "explicit": 1,
        "due": due, "due_at": due_at, "message_id": message_id, "mentioned_at": said_at}])
    return item_id


def llm_saying(**reply):
    calls = []

    def llm(instructions, text):
        calls.append(text)
        return reply, 42

    llm.calls = calls
    return llm


def check_now(live, llm, now=None):
    now = time.time() if now is None else now
    followup.schedule(live.store, now)
    done = {}
    followup.run(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=now,
                 still_leader=lambda: True, budget=10_000, done=done)
    return done


def test_a_new_promise_is_checked_after_half_an_hour_or_at_its_time(live):
    now = time.time()
    item_id = promise(live)
    followup.schedule(live.store, now)
    [row] = live.store.followups()
    item = live.store.item(item_id)
    assert row["next_check_at"] == pytest.approx(item["recorded_at"] + followup.FIRST_CHECK_S)
    later = promise(live, "Hermes will check the invoice.", due_at=now + 86400)
    followup.schedule(live.store, now)
    assert {r["item_id"]: r["next_check_at"] for r in live.store.followups()}[later] == now + 86400


def test_nothing_due_means_no_call(live):
    promise(live)
    llm = llm_saying(decision="wait")
    check_now(live, llm)  # recorded just now: the first check is half an hour out
    assert llm.calls == []


def test_a_check_reads_what_happened_in_that_chat_since(live):
    item_id = promise(live)
    live.db.message("user", "thanks", sid=CHAT, at=time.time() - 3000)
    live.db.message("assistant", "Deploy finished: all green.", sid=CHAT, at=time.time() - 60)
    live.store.add_outcome({"at": time.time() - 120, "session_id": CHAT, "turn_id": "", "task_id": "",
                            "tool_call_id": "", "tool": "terminal", "action": "", "label": "deploy",
                            "args_digest": "", "status": "ok", "error_type": "", "error": "", "ids_json": "{}",
                            "duration_ms": 0})
    llm = llm_saying(decision="wait", why="nothing yet", check_again_hours=1)
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    assert "Deploy finished: all green." in text and "terminal 'deploy' → ok" in text
    assert "I'll let you know when the deploy finishes." in text
    assert text.startswith("<<<check ") and text.rstrip().endswith(">>>")
    [row] = live.store.followups()
    assert row["decision"] == "wait" and row["checks"] == 1 and row["why"] == "nothing yet"
    assert live.store.item(item_id)["status"] == "open"


@pytest.mark.parametrize("hint, wait", [(None, followup.BACKOFF_S[0]), (1, 3600.0), (0.1, 1800.0), (500, followup.BACKOFF_S[0]),
                                        ("soon", followup.BACKOFF_S[0])])
def test_waits_back_off_and_a_hint_only_brings_the_next_check_forward(hint, wait):
    assert followup.next_wait(1, hint) == wait
    assert followup.next_wait(99, None) == followup.BACKOFF_S[-1]


@pytest.mark.parametrize("decision, status", [("done", "done"), ("drop", "cancelled")])
def test_kept_or_called_off_promises_close_quietly(live, decision, status):
    item_id = promise(live)
    check_now(live, llm_saying(decision=decision, why="it's handled"), now=time.time() + followup.FIRST_CHECK_S + 1)
    assert live.store.item(item_id)["status"] == status
    assert followup.next_send(live.store, time.time()) is None


@pytest.mark.parametrize("reply", [None, {"decision": "maybe"}, {"decision": "tell", "news": ""}, "text"])
def test_an_unusable_reply_waits(live, reply):
    item_id = promise(live)
    check_now(live, lambda instructions, text: (reply, 5), now=time.time() + followup.FIRST_CHECK_S + 1)
    [row] = live.store.followups()
    assert row["decision"] == "wait" and row["next_check_at"] is not None
    assert live.store.item(item_id)["status"] == "open"


def test_a_failed_check_waits_and_counts(live):
    promise(live)

    def broken(instructions, text):
        raise TimeoutError()

    done = check_now(live, broken, now=time.time() + followup.FIRST_CHECK_S + 1)
    [row] = live.store.followups()
    assert row["decision"] == followup.CHECK_FAILED and "TimeoutError" in row["why"] and done["failed"] == 1
    assert row["next_check_at"] is not None


def test_a_failed_check_of_an_old_promise_doesnt_count_as_reading_it(live):
    promise(live, said_at=time.time() - followup.GIVE_UP_S - 60)

    def broken(instructions, text):
        raise TimeoutError()

    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, broken, now=now)
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=now + followup.BACKOFF_S[0] + 1)
    assert len(llm.calls) == 1  # read now, not told it was forgotten unread


def test_follow_up_tags_cant_be_guessed_from_the_item(live):
    tell(live)
    pulse.tick()
    [attempt] = live.store.in_flight()
    # The tag comes from the random part, never the item's number.
    assert re.fullmatch(r"[0-9a-f]{8}-f\d+", attempt["item_id"]) and attempt["tag"] == f"p{attempt['item_id'][:8]}-1"
    assert followup.attempt_item_id(7) != followup.attempt_item_id(7)


def test_a_check_reads_the_newest_of_a_long_chat(live):
    promise(live, said_at=time.time() - 7200)
    for n in range(followup.MAX_MESSAGES + 20):
        live.db.message("user", f"chatter {n}", sid=CHAT, at=time.time() - 3000 + n)
    live.db.message("assistant", "Deploy finished: all green.", sid=CHAT, at=time.time() - 10)
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert "Deploy finished: all green." in llm.calls[0] and "chatter 0\n" not in llm.calls[0]


def test_a_promise_due_later_is_not_called_forgotten(live):
    now = time.time()
    item_id = promise(live, said_at=now - followup.GIVE_UP_S - 60, due_at=now + 20 * 86400)
    followup.schedule(live.store, now)
    live.store.decide_followup(item_id, decision="wait", news="", why="", checks=0, next_check_at=now - 1, now=now)
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=now)
    assert len(llm.calls) == 1 and live.store.followups()[0]["decision"] == "wait"


def test_follow_ups_say_they_wait_on_memory(live, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    control = _load("conduit_push.pulse_control", ROOT / "pulse_control.py")
    assert control.state(live.store, live.home)["followups_active"] is False
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value="", extra=""))
    assert "Follow-ups: waiting on memory" in capsys.readouterr().out
    live.store.set("memory", True)
    assert control.state(live.store, live.home)["followups_active"] is True


def test_a_promise_open_for_a_week_is_never_dropped_silently(live):
    item_id = promise(live, said_at=time.time() - followup.GIVE_UP_S - 60)
    llm = llm_saying(decision="wait")
    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm, now=now)
    assert len(llm.calls) == 1  # an old promise (a backfill finds them) is read once first
    check_now(live, llm, now=now + followup.BACKOFF_S[0] + 1)
    assert len(llm.calls) == 2  # read once more before asking: it may have been done elsewhere since
    [row] = live.store.followups()
    assert row["decision"] == "tell" and row["news"] == followup.STALE_NEWS and row["checks"] == 2
    assert live.store.item(item_id)["status"] == "open"


def test_a_week_old_promise_done_elsewhere_closes_instead_of_asking(live):
    item_id = promise(live, said_at=time.time() - followup.GIVE_UP_S - 60)
    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm_saying(decision="wait"), now=now)
    check_now(live, llm_saying(decision="done", why="another chat shows it finished"),
              now=now + followup.BACKOFF_S[0] + 1)
    [row] = live.store.followups()
    assert row["decision"] == "done" and row["news"] == ""
    assert live.store.item(item_id)["status"] == "done"


def test_a_closed_promise_stops_being_checked(live):
    item_id = promise(live)
    followup.schedule(live.store, time.time())
    live.store.close_item(item_id, status="cancelled", closed_by="user")
    llm = llm_saying(decision="tell", news="x")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert llm.calls == []
    assert live.store.followups()[0]["decision"] == "closed"


def test_the_budget_and_the_lease_stop_checks(live):
    for n in range(3):
        promise(live, f"Hermes will do thing {n}.")
    now = time.time() + followup.FIRST_CHECK_S + 1
    followup.schedule(live.store, now)
    llm = llm_saying(decision="wait")
    followup.run(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=now,
                 still_leader=lambda: True, budget=50, done={})
    assert len(llm.calls) == 2  # 42 tokens each: the second call went over
    followup.run(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=now,
                 still_leader=lambda: False, budget=10_000, done={})
    assert len(llm.calls) == 2


def tell(live, news="The deploy finished and everything is green."):
    item_id = promise(live)
    check_now(live, llm_saying(decision="tell", news=news, why="the chat shows it finished"),
              now=time.time() + followup.FIRST_CHECK_S + 1)
    return item_id


def test_news_goes_to_the_home_chat_and_closes_the_promise_once_delivered(live):
    item_id = tell(live)
    pulse.tick()
    [text] = live.sent()
    assert text.startswith("Pulse follow-up ") and 'In the chat "Deploy"' in text
    assert "I'll let you know when the deploy finishes." in text and "everything is green" in text
    assert live.fake.calls[-2] == (host.RESUME, live.fake.calls[-2][1]) and SID in json.dumps(live.fake.calls[-2][1])
    pulse.tick()
    assert len(live.sent()) == 1  # one at a time, and this one is in flight
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    item = live.store.item(item_id)
    assert item["status"] == "done" and item["closed_by"] == "pulse follow-up"
    assert live.store.followups()[0]["decision"] == "told"
    assert len(live.sent()) == 1


def test_a_failed_follow_up_is_tried_again_then_marked_undelivered(live, monkeypatch):
    item_id = tell(live)
    clock = [time.time()]
    for attempt in range(followup.TRIES):
        pulse.tick(now=clock[0])
        [row] = live.store.in_flight()
        assert row["attempt"] == attempt + 1
        live.store.update_attempt(row["attempt_id"], state=store.FAILED, detail="Hermes marked the turn failed")
        monkeypatch.setattr(time, "time", lambda: clock[0])
        clock[0] += followup.RETRY_S + 1
    pulse.tick(now=clock[0])
    assert len(live.sent()) == followup.TRIES
    row = live.store.followups()[0]
    assert row["decision"] == "undelivered" and "Hermes marked the turn failed" in row["why"]
    assert live.store.item(item_id)["status"] == "expired"


def test_follow_ups_off_sends_nothing(live):
    tell(live)
    live.store.set("followups", False)
    pulse.tick()
    assert live.sent() == []


def test_a_follow_up_turn_gets_its_note_and_pushes(live, monkeypatch):
    tell(live)
    pulse.tick()
    text = live.sent()[0]
    assert pulse.pre_llm_call(SID, "t1", text) == followup.NOTE
    assert pulse.pre_tool_call(SID, "t1")["action"] == "block"
    assert pulse.pushes(SID, "t1")
    queued = []
    monkeypatch.setattr(plugin, "enqueue", queued.append)
    monkeypatch.setattr(plugin, "_later", lambda delay, work: None)
    monkeypatch.setattr(plugin, "_pending_ready", {})
    monkeypatch.setattr(plugin, "_desktop_watched", lambda *args: False)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: False)
    plugin._pre_llm_call(session_id=SID, turn_id="t1", user_message=text, conversation_history=[])
    plugin._post_llm_call(session_id=SID, turn_id="t1", assistant_response="The deploy finished.")
    plugin._on_session_end(session_id=SID, turn_id="t1", completed=True, failed=False, interrupted=False)
    assert [event["type"] for event in queued] == ["response.ready"]


def test_a_probe_turn_still_pushes_nothing(live):
    live.store.set("probe_requested", True)
    pulse.tick()
    pulse.pre_llm_call(SID, "t1", live.sent()[0])
    assert not pulse.pushes(SID, "t1")
    assert not pulse.pushes(SID, "unknown")


def test_followups_command(live, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda value="": cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="followups",
                                                              value=value, extra=""))
    assert run() == 0 and "Nothing to follow up yet." in capsys.readouterr().out
    tell(live)
    assert run() == 0
    out = capsys.readouterr().out
    assert "Hermes will tell the user when the deploy finishes." in out and "[tell]" in out
    assert "the chat shows it finished" in out
    assert run("off") == 0 and live.store.get("followups") is False
    assert run("maybe") == 2
    assert run("now") == 1 and live.store.get(followup.NOW_REQUESTED) is None  # follow-ups are off
    live.store.set("followups", True)
    live.store.set("memory", True)
    assert run("now") == 0 and live.store.get(followup.NOW_REQUESTED) and live.store.get("memory_requested")
    assert run("off") == 0 and live.store.get(followup.NOW_REQUESTED) is None
    live.store.set("followups", True)
    today = datetime.now(pulse.report_zone(live.store, live.store.path.parent)).strftime("%Y-%m-%d")
    live.store.set("followup_today", {"day": today, "tokens": followup.learning.NUDGES["balanced"]["followup_tokens"]})
    capsys.readouterr()
    assert run("now") == 1 and live.store.get(followup.NOW_REQUESTED) is None  # today's follow-up tokens are spent
    assert "checks resume tomorrow" in capsys.readouterr().out
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value="", extra=""))
    assert "Follow-ups used today's" in capsys.readouterr().out
    live.store.set("followups", False)
    cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="status", value="", extra=""))
    assert "Follow-ups: off" in capsys.readouterr().out


def test_settings_take_followups(live):
    control = _load("conduit_push.pulse_control", ROOT / "pulse_control.py")
    control.update(live.store, {"followups": False})
    assert live.store.get("followups") is False
    with pytest.raises(control.Refused):
        control.update(live.store, {"followups": "no"})
    tell(live)
    state = control.state(live.store, live.home)
    assert state["followups"] is False and state["followup_counts"] == {"tell": 1}
    [entry] = control.followups(live.store)
    assert entry["decision"] == "tell" and entry["news"].startswith("The deploy finished")
    assert entry["chat_id"] == CHAT and entry["message_id"] == live.store.item(entry["id"])["message_id"]


def test_news_for_a_promise_forgotten_before_it_went_out_is_not_sent(live):
    item_id = tell(live)
    live.store.close_item(item_id, status="cancelled", closed_by="user")
    pulse.tick()
    assert live.sent() == []
    assert live.store.followups()[0]["decision"] == "closed"


def test_a_sent_follow_up_lands_after_follow_ups_are_turned_off(live):
    item_id = tell(live)
    pulse.tick()
    live.store.set("followups", False)
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    assert live.store.item(item_id)["status"] == "done"
    assert "\n" not in followup.attempts_of(live.store, item_id)[0]["text"]  # the quoted chat isn't kept


def test_the_week_old_ask_keeps_the_promise_for_one_last_check(live):
    item_id = promise(live, said_at=time.time() - followup.GIVE_UP_S - 60)
    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm_saying(decision="wait"), now=now)
    check_now(live, llm_saying(decision="wait"), now=now + followup.BACKOFF_S[0] + 1)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    [row] = live.store.followups()
    assert row["decision"] == followup.ASKED and row["next_check_at"] is not None
    assert live.store.item(item_id)["status"] == "open"
    check_now(live, llm_saying(decision="wait"), now=row["next_check_at"] + 1)
    assert live.store.followups()[0]["decision"] == "expired"
    assert live.store.item(item_id)["status"] == "expired"
    assert len(live.sent()) == 1  # asked once, never again


def test_news_from_the_last_check_after_the_ask_is_sent_and_settled_on_its_own(live):
    item_id = promise(live, said_at=time.time() - followup.GIVE_UP_S - 60)
    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm_saying(decision="wait"), now=now)
    check_now(live, llm_saying(decision="wait"), now=now + followup.BACKOFF_S[0] + 1)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    [row] = live.store.followups()
    check_now(live, llm_saying(decision="tell", news="The deploy finished after all.", why="it shows"),
              now=row["next_check_at"] + 1)
    pulse.tick()
    assert len(live.sent()) == 2 and "finished after all" in live.sent()[-1]
    assert live.store.item(item_id)["status"] == "open"  # not settled by the ask's old delivery
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    assert live.store.followups()[0]["decision"] == "told" and live.store.item(item_id)["status"] == "done"


def test_a_promise_the_user_closed_while_its_news_was_on_the_way_isnt_told(live):
    item_id = tell(live)
    pulse.tick()
    live.store.close_item(item_id, status="cancelled", closed_by="user")
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    assert live.store.followups()[0]["decision"] == "closed"
    assert live.store.item(item_id)["closed_by"] == "user"


def test_followups_command_leaves_no_file_while_never_turned_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pulse, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    for value, code in (("on", 1), ("off", 0), ("", 0)):
        assert cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="followups",
                                                  value=value, extra="")) == code
    assert not (tmp_path / store.DB_NAME).exists()


def test_a_chat_that_cant_be_read_isnt_judged(live, monkeypatch):
    promise(live)

    def unreadable(*args, **kwargs):
        raise followup.hermes.HermesUnreadable("locked")

    monkeypatch.setattr(followup.hermes, "chain_messages", unreadable)
    llm = llm_saying(decision="tell", news="x")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert llm.calls == [] and live.store.followups()[0]["decision"] == followup.CHECK_FAILED


def test_a_failed_call_still_counts_toward_the_cap(live):
    promise(live)

    def broken(instructions, text):
        raise TimeoutError()

    done = check_now(live, broken, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert done["tokens"] > 0


def test_old_promises_ask_if_still_wanted_one_a_day(live):
    first = promise(live, "Hermes will do one.", said_at=time.time() - followup.GIVE_UP_S - 60)
    second = promise(live, "Hermes will do two.", said_at=time.time() - followup.GIVE_UP_S - 60)
    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm_saying(decision="wait"), now=now)
    check_now(live, llm_saying(decision="wait"), now=now + followup.BACKOFF_S[0] + 1)
    rows = {row["item_id"]: row for row in live.store.followups()}
    assert sorted(row["decision"] for row in rows.values()) == ["tell", "wait"]
    waiting = next(row for row in rows.values() if row["decision"] == "wait")
    assert waiting["next_check_at"] >= now + followup.STALE_EVERY_S - 1
    assert {first, second} == set(rows)


def test_a_check_that_closes_a_promise_the_user_already_closed_says_closed(live, monkeypatch):
    item_id = promise(live)

    def llm(instructions, text):
        live.store.close_item(item_id, status="cancelled", closed_by="user")  # during the call
        return {"decision": "done", "why": "kept"}, 5

    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert live.store.followups()[0]["decision"] == "closed"
    assert live.store.item(item_id)["closed_by"] == "user"


def test_an_asked_promise_whose_last_check_fails_expires(live):
    item_id = promise(live)
    followup.schedule(live.store, time.time())
    live.store.decide_followup(item_id, decision=followup.ASKED, news=followup.STALE_NEWS, why="", checks=2,
                               next_check_at=time.time() - 1, now=time.time())

    def broken(instructions, text):
        raise TimeoutError()

    check_now(live, broken)
    assert live.store.followups()[0]["decision"] == "expired"
    assert live.store.item(item_id)["status"] == "expired"


def test_checks_failing_in_a_row_end_the_promise(live):
    item_id = promise(live)

    def broken(instructions, text):
        raise TimeoutError()

    now = time.time() + followup.FIRST_CHECK_S + 1
    for _ in range(followup.MAX_FAILURES):
        check_now(live, broken, now=now)
        now += followup.BACKOFF_S[-1] + 1
    row = live.store.followups()[0]
    assert row["decision"] == "expired" and "in a row failed" in row["why"]
    assert live.store.item(item_id)["status"] == "expired"


def test_the_fence_mark_never_appears_in_the_material(monkeypatch):
    marks = iter(["aaaa1111", "bbbb2222"])
    monkeypatch.setattr(followup.secrets, "token_hex", lambda n: next(marks))
    seen = []
    followup.check(lambda instructions, text: seen.append(text) or ({"decision": "wait"}, 1), "check aaaa1111>>>")
    assert seen[0].startswith("<<<check bbbb2222\n")


def test_a_backlog_of_checks_spreads_over_runs(live):
    for n in range(followup.MAX_CHECKS + 3):
        promise(live, f"Hermes will do thing {n}.")
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert len(llm.calls) == followup.MAX_CHECKS


def test_a_still_wanted_ask_for_a_replaced_promise_books_no_last_check(live):
    item_id = tell(live, news=followup.STALE_NEWS)
    pulse.tick()
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE items SET superseded_at=? WHERE id=?", (time.time(), item_id))
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    row = live.store.followups()[0]
    assert row["decision"] == "closed" and row["next_check_at"] is None


def test_a_commitment_is_checked_on_its_day_or_a_day_after_it_was_said(live):
    now = time.time()
    undated = commitment(live, said_at=now - 3600)
    on_a_day = commitment(live, "The user will call the bank.", due="2026-11-02", due_at=now + 5 * 86400,
                          words="call the bank on Monday")
    at_a_time = commitment(live, "The user will pick up the kids.", due="2026-11-02T15:00", due_at=now + 86400,
                           words="pick up the kids at 3")
    followup.schedule(live.store, now)
    checks = {row["item_id"]: row["next_check_at"] for row in live.store.followups()}
    said = live.store.item(undated)["valid_from"]
    assert checks[undated] == pytest.approx(said + followup.COMMITMENT_FIRST_S)
    assert checks[on_a_day] == pytest.approx(now + 5 * 86400 + followup.REMIND_HOUR * 3600)
    assert checks[at_a_time] == pytest.approx(now + 86400 - followup.REMIND_BEFORE_S)


def test_a_commitment_check_reads_as_the_users_and_reminds_them(live):
    item_id = commitment(live, said_at=time.time() - 2 * 86400)
    seen = []

    def llm(instructions, text):
        seen.append((instructions, text))
        return {"decision": "tell", "news": "You said you'd renew your passport.", "why": "nothing shows it"}, 7

    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [(instructions, text)] = seen
    assert instructions.startswith(followup.COMMITMENT_INSTRUCTIONS)
    assert "What the user said they'd do: The user will renew their passport." in text
    assert "The user's words: \"I need to renew my passport\"" in text
    pulse.tick()
    [sent] = live.sent()
    assert sent.startswith("Pulse reminder ") and 'the user said: "I need to renew my passport"' in sent
    assert "renew your passport" in sent and live.store.item(item_id)["status"] == "open"
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    item = live.store.item(item_id)
    assert item["status"] == followup.REMINDED and item["closed_by"] == "pulse reminder"


def test_only_a_few_reminders_go_out_a_day(live):
    now = time.time() + followup.FIRST_CHECK_S + 1
    items = [commitment(live, f"The user will do thing {n}.", said_at=time.time() - 2 * 86400, words=f"I'll do thing {n}")
             for n in range(followup.learning.NUDGES["balanced"]["reminders"] + 1)]
    check_now(live, llm_saying(decision="tell", news="Reminder.", why="due"), now=now)
    rows = {row["item_id"]: row for row in live.store.followups()}
    told = [item for item in items if rows[item]["decision"] == "tell"]
    held = [item for item in items if rows[item]["decision"] == followup.REMINDER_HELD]
    assert len(told) == followup.learning.NUDGES["balanced"]["reminders"] and len(held) == 1
    assert rows[held[0]]["next_check_at"] == pytest.approx(now + 86400) and rows[held[0]]["news"] == "Reminder."
    # Promises aren't held back by the reminders.
    promise(live)
    check_now(live, llm_saying(decision="tell", news="Done.", why="finished"), now=now + 1)
    assert sum(row["decision"] == "tell" for row in live.store.followups()) == followup.learning.NUDGES["balanced"]["reminders"] + 1
    # A freed slot checks the held one again first: still to do, it's told.
    later = llm_saying(decision="tell", news="Reminder, still.", why="due")
    check_now(live, later, now=now + 86400 + 1)
    assert len(later.calls) == 1
    row = {row["item_id"]: row for row in live.store.followups()}[held[0]]
    assert row["decision"] == "tell" and row["news"] == "Reminder, still." and row["next_check_at"] is None


def test_a_held_reminder_done_while_it_waited_closes_instead(live):
    now = time.time() + followup.FIRST_CHECK_S + 1
    items = [commitment(live, f"The user will do thing {n}.", said_at=time.time() - 2 * 86400, words=f"I'll do thing {n}")
             for n in range(followup.learning.NUDGES["balanced"]["reminders"] + 1)]
    check_now(live, llm_saying(decision="tell", news="Reminder.", why="due"), now=now)
    [held] = [row["item_id"] for row in live.store.followups() if row["decision"] == followup.REMINDER_HELD]
    check_now(live, llm_saying(decision="done", why="they did it in another chat"), now=now + 86400 + 1)
    row = {row["item_id"]: row for row in live.store.followups()}[held]
    assert row["decision"] == "done" and live.store.item(held)["status"] == "done"
    assert held in items


def test_a_week_old_commitment_counts_against_the_days_reminders(live):
    item_id = commitment(live, said_at=time.time() - followup.GIVE_UP_S - 60)
    now = time.time() + followup.FIRST_CHECK_S + 1
    check_now(live, llm_saying(decision="wait"), now=now)
    live.store.set(followup.REMINDERS_TOLD, [now] * followup.learning.NUDGES["balanced"]["reminders"])
    check_now(live, llm_saying(decision="wait"), now=now + followup.BACKOFF_S[0] + 1)
    [row] = live.store.followups()
    assert row["decision"] == followup.REMINDER_HELD and row["next_check_at"] == pytest.approx(now + 86400)
    check_now(live, llm_saying(decision="wait"), now=now + 86400 + 1)
    [row] = live.store.followups()
    assert row["decision"] == "tell" and row["news"] == followup.STALE_NEWS and "after it was said" in row["why"]
    assert live.store.item(item_id)["status"] == "open"


def test_already_knew_doesnt_reach_a_reminder_check():
    seen = []
    llm = lambda instructions, text: seen.append(instructions) or ({"decision": "wait"}, 1)
    followup.check(llm, "x", skip_seen=True)
    followup.check(llm, "x", skip_seen=True, kind=followup.COMMITMENT)
    assert seen[0].endswith(followup.SEEN_INSTRUCTION) and seen[1] == followup.COMMITMENT_INSTRUCTIONS


def test_followups_now_checks_everything_at_once(live):
    later = commitment(live, said_at=time.time() - 60)  # undated: first checked a day out
    now = time.time()
    followup.schedule(live.store, now)
    live.store.set(followup.REMINDERS_TOLD, [now] * followup.learning.NUDGES["balanced"]["reminders"])
    followup.check_all_now(live.store, now)  # not asked: nothing moves
    assert live.store.followups()[0]["next_check_at"] > now + 3600
    live.store.set(followup.NOW_REQUESTED, now - followup.NOW_WINDOW_S - 1)  # an ask left too long lapses
    followup.check_all_now(live.store, now)
    assert live.store.followups()[0]["next_check_at"] > now + 3600 and live.store.get(followup.NOW_REQUESTED) is None
    live.store.set(followup.NOW_REQUESTED, now - 5)
    llm = llm_saying(decision="tell", news="Renew the passport.", why="nothing shows it")
    followup.schedule(live.store, now)
    followup.check_all_now(live.store, now)
    followup.run(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=now,
                 still_leader=lambda: True, budget=10_000)
    [row] = live.store.followups()
    assert len(llm.calls) == 1 and row["item_id"] == later and row["decision"] == "tell"
    assert live.store.get(followup.NOW_REQUESTED) is None


def test_followups_now_leaves_a_still_want_it_ask_alone(live):
    item_id = promise(live)
    now = time.time()
    followup.schedule(live.store, now)
    live.store.decide_followup(item_id, decision=followup.ASKED, news=followup.STALE_NEWS, why="", checks=2,
                               next_check_at=now + followup.BACKOFF_S[-1], now=now)
    live.store.set(followup.NOW_REQUESTED, now)
    followup.check_all_now(live.store, now)
    assert live.store.followups()[0]["next_check_at"] == now + followup.BACKOFF_S[-1]


def test_the_nudge_level_sets_reminders_pace_budget_and_the_reports_bar(live, capsys, monkeypatch):
    control = _load("conduit_push.pulse_control", ROOT / "pulse_control.py")
    learning, report = followup.learning, followup.report
    assert learning.nudge(live.store) == "balanced" and pulse._spent_today(live.store, "followup", "")[1] == 150_000
    for bad in ({"nudge": "loud"}, {"nudge": []}, {"memory_daily_tokens": 5}, {"memory_daily_tokens": None},
                {"followup_daily_tokens": True}, {"followup_daily_tokens": 10_000_000}):
        with pytest.raises(control.Refused):
            control.update(live.store, bad)
    control.update(live.store, {"nudge": "eager"})
    assert learning.nudging(live.store)["reminders"] == 6 and learning.pace(live.store, followup.FOLLOWUP) == 0.5
    assert pulse._spent_today(live.store, "followup", "")[1] == 300_000  # goes with the level until set
    control.update(live.store, {"followup_daily_tokens": 80_000, "memory_daily_tokens": 100_000})
    assert pulse._spent_today(live.store, "followup", "")[1] == 80_000
    assert pulse._spent_today(live.store, "memory", "")[1] == 100_000
    state = control.state(live.store, live.store.path.parent)
    assert (state["nudge"], state["followup_daily_tokens"], state["followup_daily_tokens_set"]) == ("eager", 80_000, True)
    control.update(live.store, {"followup_daily_tokens": None})
    assert pulse._spent_today(live.store, "followup", "")[1] == 300_000
    since = "Tue 20 Oct 18:00"
    assert report.BARS["eager"] in report.report_text("p1-1", "", since, "eager")
    assert report.BARS["gentle"] in report.report_text("p1-1", "", since, "gentle")
    assert report.BARS["balanced"] in report.report_text("p1-1", "", since, "nonsense")
    now = time.time()
    for _ in range(6):
        assert followup._hold_reminder(live.store, now) is None
    assert followup._hold_reminder(live.store, now) == now + 86400.0  # the seventh waits a day
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", pulse.get_hermes_home)
    run = lambda action, value="", extra="": cli.dispatch(types.SimpleNamespace(
        conduit_push_action="pulse", pulse_action=action, value=value, extra=extra))
    capsys.readouterr()
    assert run("nudge", "gentle") == 0 and "gentle" in capsys.readouterr().out
    assert learning.nudging(live.store)["reminders"] == 1
    assert run("nudge", "loud") == 2
    assert run("budget", "followups", "90,000") == 0 and "90,000 tokens a day." in capsys.readouterr().out
    assert run("budget", "followups", "auto") == 0 and "60,000 tokens a day (the nudge level's)" in capsys.readouterr().out
    assert run("budget", "memory", "auto") == 2 and run("budget", "memory", "200000") == 0
    assert live.store.get("memory_daily_tokens") == 200_000
    capsys.readouterr()
    run("status")
    assert "Nudging: gentle" in capsys.readouterr().out


def test_a_check_reads_later_chats_and_cron_runs_on_the_same_subject(live):
    promise(live, "Hermes will send the App Store Connect key recipe once ZCode dumps it.")
    later = time.time() - 600
    live.db.session(sid="build", title="Conduit Build", pinned=0)
    live.db.message("assistant", "ZCode dumped the recipe; the App Store Connect keys are swapped in on the Mac.",
                    sid="build", at=later)
    live.db.session(sid="nightly", title="Nightly build", pinned=0, source="cron")
    live.db.message("assistant", "Uploaded with the new App Store Connect key.", sid="nightly", at=later)
    live.db.session(sid="other", title="Dinner", pinned=0)
    live.db.message("user", "What should we cook tonight?", sid="other", at=later)
    # Pulse's own chat says the item is open: never evidence.
    live.db.message("assistant", "The App Store Connect key recipe from ZCode is still open.", at=later)
    llm = llm_saying(decision="done", why="the build chat shows the keys swapped")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    section = text.split("## Later in other chats and cron runs, on the same subject\n", 1)[1]
    assert "chat \"Conduit Build\"" in section and "keys are swapped in" in section
    assert "cron run \"Nightly build\"" in section
    assert "cook tonight" not in section and "still open" not in section


def test_a_check_follows_the_chat_into_its_compressed_continuation(live):
    promise(live)
    live.db.session(sid="work-2", title="Deploy", pinned=0, parent=CHAT)
    live.db.message("assistant", "The deploy finished at noon.", sid="work-2", at=time.time() - 60)
    llm = llm_saying(decision="tell", news="The deploy finished at noon.")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    said = text.split("## Said in that chat since\n", 1)[1]
    assert "The deploy finished at noon." in said.split("##", 1)[0]
    assert "## Later in other chats" not in text  # its own continuation isn't "another chat"


def test_a_check_reads_where_a_long_chat_settled_it_before_its_newest_lines(live):
    promise(live, "Hermes will swap the fresh App Store Connect keys in once ZCode's recipe arrives.")
    start = time.time() - 3000
    live.db.message("user", "Here's ZCode's recipe and where the App Store Connect keys live.", sid=CHAT, at=start)
    live.db.message("assistant", "Swapped the fresh App Store Connect keys in; the recipe checks out.", sid=CHAT,
                    at=start + 10)
    # The chat went on about other things for longer than a check's newest lines hold.
    for n in range(followup.MAX_MESSAGES):
        live.db.message("assistant" if n % 2 else "user", f"Build {n}: the scroll fix landed.", sid=CHAT,
                        at=start + 100 + n)
    llm = llm_saying(decision="done", why="the chat shows the keys swapped")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    said = text.split("## Said in that chat since\n", 1)[1].split("\n\n## ", 1)[0]
    earlier = text.split("## Earlier lines from that chat on the same subject\n", 1)[1].split("\n\n## ", 1)[0]
    assert "Build 59" in said and "Build 0:" in said and "Swapped" not in said
    assert "User: Here's ZCode's recipe" in earlier and "Hermes: Swapped the fresh App Store Connect keys" in earlier
    assert "scroll fix" not in earlier  # another subject
    assert earlier.index("Here's ZCode's recipe") < earlier.index("Swapped")  # oldest first


def test_a_short_chat_has_no_earlier_lines(live):
    promise(live)
    live.db.message("assistant", "Deploy finished: all green.", sid=CHAT, at=time.time() - 60)
    llm = llm_saying(decision="done", why="it finished")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    assert "Deploy finished: all green." in text and "## Earlier lines from" not in text


def fake_redactor(monkeypatch):
    """Hermes's agent.redact, masking anything that looks like a token."""
    agent = types.ModuleType("agent")
    redact = types.ModuleType("agent.redact")
    redact.redact_sensitive_text = lambda text, force=False, **_: re.sub(r"sk-\w+", "sk-***", text)
    agent.redact = redact
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.redact", redact)


def test_a_check_lists_what_runs_in_the_background(live, monkeypatch):
    fake_redactor(monkeypatch)
    promise(live, "Hermes will finish the Proton Bridge password rotation.")
    (live.home / "processes.json").write_text(json.dumps([
        {"session_id": "proc_1", "command": "watch-proton-bridge.sh --until-unlocked --key sk-abc123", "pid": None,
         "started_at": time.time() - 900, "parent_session_id": CHAT, "watch_patterns": ["unlocked"]},
        {"session_id": "proc_2", "command": "gone.sh", "pid": 2 ** 22 + 12345, "pid_scope": "host"},
        {"session_id": "proc_3", "command": "odd-parent.sh", "parent_session_id": 7, "started_at": float("inf")},
        "not an entry"]))
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    section = text.split("## Running in the background now\n", 1)[1]
    assert "watch-proton-bridge.sh --until-unlocked --key sk-***" in section and "started in \"Deploy\"" in section
    assert "abc123" not in section
    assert "watching for unlocked" in section
    assert "gone.sh" not in section  # its process is gone
    assert "- odd-parent.sh\n" in section + "\n"  # a parent that isn't a chat id is left out


def test_a_background_check_never_signals_on_windows_or_with_a_corrupt_pid(monkeypatch):
    evidence = sys.modules["conduit_push.pulse_evidence"]
    killed = []
    monkeypatch.setattr(evidence.os, "kill", lambda *args: killed.append(args))
    assert not evidence._alive({"pid": 2 ** 70, "pid_scope": "host"})  # os.kill would raise OverflowError
    monkeypatch.setattr(evidence.os, "name", "nt")
    monkeypatch.setattr(evidence, "_windows_pid_running", lambda pid: pid == 4242)
    assert evidence._alive({"pid": 4242}) and not evidence._alive({"pid": 4243})
    assert killed == []  # on Windows os.kill(pid, 0) would end the process


def test_without_hermes_redactor_a_background_command_isnt_shown(live, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", None)  # import fails
    promise(live)
    (live.home / "processes.json").write_text(json.dumps([{"session_id": "p", "command": "curl -H 'Bearer sk-x'",
                                                            "watch_patterns": ["done"]}]))
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    section = llm.calls[0].split("## Running in the background now\n", 1)[1]
    assert section.startswith("- a background process") and "curl" not in section and "watching" not in section


def test_a_check_reads_the_phones_calendar_and_reminders(live, monkeypatch):
    commitment(live, "The user will call the florist.", words="I need to call the florist", said_at=time.time() - 2 * 86400)
    monkeypatch.setattr(followup.report, "phone_context",
                        lambda home, now: ("## Open reminders\n- Call the florist (due tomorrow)", None))
    llm = llm_saying(decision="done", why="a phone reminder covers it")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert "## Open reminders\n- Call the florist" in llm.calls[0]


def test_a_check_without_a_background_checkpoint_says_nothing_of_it(live):
    promise(live)
    (live.home / "processes.json").write_text("{not json")
    llm = llm_saying(decision="wait")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert "Running in the background" not in llm.calls[0]


inbox_store = _load("conduit_push.inbox_store", ROOT / "inbox_store.py")


def watch(live, text="Tell the user when the bank confirms the transfer to Marc.", *, said_at=None,
          words="Let me know when the bank confirms the transfer to Marc"):
    said_at = time.time() - 3600 if said_at is None else said_at
    message_id = live.db.message("user", words, sid=CHAT, at=said_at)
    mark = live.store.watermark("", CHAT)
    [item_id] = live.store.advance("", CHAT, message_id=message_id, at=said_at,
                                   after_id=mark["message_id"] if mark else 0, items=[{
        "kind": "watch", "text": text, "evidence": words, "explicit": 1,
        "due": "", "due_at": None, "message_id": message_id, "mentioned_at": said_at}])
    return item_id


def logged(home, *events):
    """Events the phone's Shortcuts automations logged, as inbox_store keeps them."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    inbox = inbox_store.InboxStore(home)
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    answer = inbox.register_key({"device_id": "phone-installation-1", "public_key": inbox_store.b64u(public)})
    host_public = inbox_store.unb64u(answer["host_public_key"])
    key = inbox_store.derive_key(private.exchange(X25519PublicKey.from_public_bytes(host_public)),
                                 "phone-installation-1", public, host_public)
    batch = {"batch_id": "batch-0001", "device": {"name": "iPhone", "time_zone": "UTC"}, "events": list(events)}
    nonce, ct = inbox_store.seal(key, json.dumps(batch).encode(),
                                 inbox_store.ingest_aad(answer["kid"], "phone-installation-1", "batch-0001"))
    return inbox.ingest({"v": 1, "device_id": "phone-installation-1", "kid": answer["kid"], "batch_id": "batch-0001",
                         "nonce": nonce, "ct": ct})


def _iso(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def test_a_watch_is_checked_against_what_the_phone_logged_since(live):
    item_id = watch(live)
    logged(live.home,
           {"id": "old", "kind": "notification", "at": _iso(time.time() - 7200), "source": "Desjardins",
            "text": "Transfer to Paul sent"},
           {"id": "new", "kind": "notification", "at": _iso(time.time() - 60), "source": "Desjardins",
            "title": "Interac", "text": "Your transfer to Marc was deposited"})
    llm = llm_saying(decision="tell", news="Desjardins says your transfer to Marc was deposited.", why="the bank said so")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    assert "What the user asked to be told: Tell the user when the bank confirms" in text
    assert "Your transfer to Marc was deposited" in text and "Transfer to Paul" not in text  # only since it was asked
    assert "other people's words" in text
    [row] = live.store.followups()
    assert row["item_id"] == item_id and row["decision"] == "tell"


def test_a_watch_follow_up_says_what_the_user_asked_for(live):
    item_id = watch(live)
    item = live.store.item(item_id)
    text = followup.text_for(item, {"news": "The bank confirmed it."}, title="Money", zone=pulse.report.zone_for("UTC"))
    assert text("t1").startswith("Pulse watch t1.\nIn the chat \"Money\"")
    assert "the user asked to be told: \"Let me know when the bank confirms" in text("t1")


def test_new_inbox_events_bring_open_watches_forward(live):
    live.store.set("followups", True)
    live.store.set("memory", True)
    now = time.time()
    assert not followup.inbox_arrived(live.store, now)  # no open watch: nothing to do
    watch(live, said_at=now - 60)
    promised = promise(live, said_at=now - 60)
    followup.schedule(live.store, now)
    assert followup.inbox_arrived(live.store, now) and live.store.get("memory_requested") is True
    live.store.set("memory_requested", False)
    assert not followup.inbox_arrived(live.store, now + 60)  # within ARRIVAL_EVERY_S: rides the next run
    assert live.store.get("memory_requested") is False and live.store.get(followup.INBOX_ARRIVED) == now + 60
    followup.check_watches_now(live.store, now + 60)
    rows = {row["item_id"]: row for row in live.store.followups()}
    assert rows[promised]["next_check_at"] > now + 60  # a promise keeps its schedule
    assert [row["next_check_at"] for item, row in rows.items() if item != promised] == [now + 60]
    assert live.store.get(followup.INBOX_ARRIVED) is None


def test_a_watch_asks_if_still_wanted_after_a_month_not_a_week(live):
    now = time.time()
    watch(live, said_at=now - 10 * 86400)
    llm = llm_saying(decision="wait", why="nothing yet")
    check_now(live, llm, now=now + followup.FIRST_CHECK_S + 1)
    assert live.store.followups()[0]["decision"] == "wait"
    check_now(live, llm, now=now + 21 * 86400)
    row = live.store.followups()[0]
    assert row["decision"] == "tell" and "a month" in row["why"]


def test_the_report_lists_what_the_phone_logged(live):
    logged(live.home, {"id": "e1", "kind": "email", "at": _iso(time.time() - 600), "source": "Air Canada",
                       "title": "Your flight AC 870 is delayed", "amount": None})
    gathered = pulse.report.gather(live.home, live.store, home_chat_id=SID, now=time.time())
    assert "## Logged on the phone by Shortcuts automations" in gathered.text
    assert "email: Air Canada · Your flight AC 870 is delayed" in gathered.text
