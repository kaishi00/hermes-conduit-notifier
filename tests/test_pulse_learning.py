"""Ratings that change what Pulse does (pulse_learning)."""

import sqlite3
import time

import pytest

from test_pulse import ROOT, SID, _load, cli, control, pulse, store
from test_pulse_followup import CHAT, check_now, followup, live, llm_saying, promise, tell  # noqa: F401

learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")


def brief_later(live):
    """The brief's time ten hours from now: its last slot is always too old to send."""
    from datetime import datetime, timezone
    live.store.set("time_zone", "UTC")
    live.store.set("brief_time", datetime.fromtimestamp(time.time() + 10 * 3600, timezone.utc).strftime("%H:%M"))


def delivered(live):
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()


def told_card(live, news="The deploy finished and everything is green."):
    tell(live, news)
    delivered(live)
    return live.store.cards()[0]


def test_a_delivered_follow_up_becomes_a_card_linked_to_its_chat(live):
    item_id = tell(live)
    delivered(live)
    [card] = live.store.cards()
    item = live.store.item(item_id)
    assert card["kind"] == "followup" and card["level"] == "push" and card["item_id"] == item_id
    assert card["chat_id"] == CHAT and card["message_id"] == item["message_id"] and card["chat_title"] == "Deploy"
    assert card["news"] == "The deploy finished and everything is green." and card["why"]
    assert card["quote"] == "I'll let you know when the deploy finishes." and card["seen_at"] is None
    feed = control.feed(live.store)
    assert feed["unseen"] == 1 and feed["levels"] == {"followup": "push"}
    assert feed["cards"][0]["id"] == card["id"]
    assert control.see(live.store, [card["id"], 999]) == 1 and live.store.unseen_cards() == 0
    with pytest.raises(ValueError):
        live.store.add_card({"kind": "followup", "level": "push", "created_at": 1, "rating) VALUES(1); --": 1})


def test_two_thumbs_down_move_follow_ups_to_the_screen_and_two_up_bring_them_back(live):
    first, second = told_card(live), told_card(live, "The build failed.")
    assert control.rate(live.store, first["id"], "down") == []
    [lesson] = control.rate(live.store, second["id"], "down")
    assert lesson["what"] == "level" and lesson["value"] == "screen" and "2 thumbs down" in lesson["text"]
    assert learning.level(live.store, "followup") == "screen"
    # Changing a rating counts only the change: down to up on the same card is +2.
    [back] = control.rate(live.store, second["id"], "up")
    assert back["value"] == "push"
    assert learning.level(live.store, "followup") == "push"


def test_rating_the_same_way_again_counts_once(live):
    card = told_card(live)
    control.rate(live.store, card["id"], "down")
    assert control.rate(live.store, card["id"], "down") == []
    assert learning.level(live.store, "followup") == "push"


def test_at_screen_level_news_becomes_a_card_without_a_turn(live):
    control.set_level(live.store, "followup", "screen")
    item_id = tell(live)
    pulse.tick()
    assert live.sent() == []
    [card] = live.store.cards()
    assert card["level"] == "screen"
    assert live.store.item(item_id)["status"] == "done"
    assert live.store.followups()[0]["decision"] == "shown"


def test_at_brief_level_news_is_held(live):
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    assert live.sent() == [] and live.store.cards()[0]["level"] == "brief"
    assert live.store.followups()[0]["decision"] == "held"


def test_off_means_no_checks_and_no_new_schedules(live):
    control.set_level(live.store, "followup", "off")
    promise(live)
    llm = llm_saying(decision="tell", news="x", why="y")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert llm.calls == [] and live.store.followups() == []


def test_too_often_moves_follow_ups_to_the_brief_at_once(live):
    card = told_card(live)
    [lesson] = control.rate(live.store, card["id"], "down", "too_often")
    assert lesson["value"] == "brief" and learning.level(live.store, "followup") == "brief"


def test_not_relevant_mutes_that_chat_and_tells_every_check(live):
    card = told_card(live)
    [rule] = control.rate(live.store, card["id"], "down", "not_relevant")
    assert rule["what"] == "rule" and rule["chat_id"] == CHAT
    assert rule["value"] == "Hermes will tell the user when the deploy finishes."
    # A new promise in that chat is closed before Pulse checks or speaks.
    item_id = promise(live, "Hermes will report the deploy time.")
    llm = llm_saying(decision="tell", news="x", why="y")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert llm.calls == []
    assert live.store.item(item_id)["status"] == "cancelled"
    assert {row["item_id"]: row["decision"] for row in live.store.followups()}[item_id] == "muted"
    # Undone, the chat's promises are checked again, and the topic no longer goes to checks.
    assert control.undo(live.store, rule["id"]) is True
    assert learning.rules(live.store, "followup") == []


def test_a_rule_made_while_news_waits_stops_it_going_out(live):
    first = told_card(live)
    item_id = tell(live, "Another deploy finished.")
    control.rate(live.store, first["id"], "down", "not_relevant")
    pulse.tick()
    assert len(live.sent()) == 1  # only the first one, from before the rule
    assert live.store.item(item_id)["status"] == "cancelled"


def test_rule_topics_go_to_checks_of_other_chats(live):
    card = told_card(live)
    control.rate(live.store, card["id"], "down", "not_relevant")
    live.db.session(sid="other", title="Other", pinned=0)
    message_id = live.db.message("assistant", "I'll check the invoice.", sid="other", at=time.time() - 3600)
    live.store.advance("", "other", message_id=message_id, at=time.time() - 3600, items=[{
        "kind": "promise", "text": "Hermes will check the invoice.", "evidence": "I'll check the invoice.",
        "explicit": 1, "due": "", "due_at": None, "message_id": message_id, "mentioned_at": time.time() - 3600}])
    llm = llm_saying(decision="wait", why="nothing")
    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    [text] = llm.calls
    assert "Topics the user wants no updates about" in text and "deploy finishes" in text


def test_too_late_halves_the_waits_twice_at_most(live):
    cards = [told_card(live, f"news {n}") for n in range(3)]
    for card in cards:
        control.rate(live.store, card["id"], "down", "too_late")
    assert learning.pace(live.store, "followup") == 0.25
    assert len(learning.rules(live.store, "followup")) == 0
    assert len(live.store.lessons("followup", "sooner")) == 2
    item_id = promise(live, "Hermes will look at the logs.")
    now = time.time()
    followup.schedule(live.store, now)
    row = {r["item_id"]: r for r in live.store.followups()}[item_id]
    assert row["next_check_at"] == pytest.approx(live.store.item(item_id)["recorded_at"] + followup.FIRST_CHECK_S / 4)
    assert followup.next_wait(1, None, 0.25) == followup.BACKOFF_S[0] / 4
    assert followup.next_wait(1, None, 0.1) == 1800.0


def test_already_knew_asks_checks_to_skip_what_the_user_saw(live):
    card = told_card(live)
    control.rate(live.store, card["id"], "down", "already_knew")
    assert learning.skips_seen(live.store, "followup")
    seen = []
    promise(live, "Hermes will watch the queue.")

    def llm(instructions, text):
        seen.append(instructions)
        return {"decision": "wait", "why": "nothing"}, 1

    check_now(live, llm, now=time.time() + followup.FIRST_CHECK_S + 1)
    assert seen and followup.SEEN_INSTRUCTION in seen[0]


def test_learned_lists_lessons_and_undoing_a_level_goes_back_a_step(live):
    card = told_card(live)
    control.set_level(live.store, "followup", "screen")
    control.rate(live.store, card["id"], "down", "too_often")
    lessons = control.learned(live.store)["lessons"]
    assert [lesson["value"] for lesson in lessons] == ["brief"]  # the replaced level is left out
    control.undo(live.store, lessons[0]["id"])
    assert learning.level(live.store, "followup") == "screen"
    assert control.undo(live.store, lessons[0]["id"]) is False


@pytest.mark.parametrize("card, rating, reason, field", [
    (999, "down", "", "card"), ("1", "down", "", "card"), (None, "up", "", "card"),
    (1, "meh", "", "rating"), (1, "up", "too_late", "reason"), (1, "down", "boring", "reason")])
def test_bad_ratings_are_refused(live, card, rating, reason, field):
    told_card(live)
    with pytest.raises(control.Refused) as refused:
        control.rate(live.store, card, rating, reason)
    assert refused.value.code == "invalid" and refused.value.data == {"field": field}


def test_levels_refuse_unknown_kinds_and_levels(live):
    for kind, level, field in (("dates", "push", "kind"), ("followup", "loud", "level")):
        with pytest.raises(control.Refused) as refused:
            control.set_level(live.store, kind, level)
        assert refused.value.data == {"field": field}
    assert control.set_level(live.store, "followup", "push") is None  # already there


def test_cli_rates_and_lists_what_was_learned(live, monkeypatch, capsys):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    card = told_card(live)
    run = lambda *argv: cli._pulse(types.SimpleNamespace(pulse_action=argv[0], value=argv[1] if len(argv) > 1 else "",
                                                        extra=argv[2] if len(argv) > 2 else ""))
    assert run("cards") == 0 and f"#{card['id']} [push]" in capsys.readouterr().out
    assert run("rate", str(card["id"]), "too_often") == 0
    assert "held for the morning brief" in capsys.readouterr().out
    assert run("learned") == 0
    out = capsys.readouterr().out
    assert "Follow-ups: held for the morning brief" in out and "(too often)" in out
    lesson = control.learned(live.store)["lessons"][0]
    assert run("learned", "undo", str(lesson["id"])) == 0 and "Undone." in capsys.readouterr().out
    assert run("learned", "followup", "screen") == 0 and "shown in Pulse" in capsys.readouterr().out
    assert run("rate", str(card["id"]), "meh") == 2
    assert run("learned", "followup") == 2


def test_cli_on_a_host_that_never_turned_pulse_on_leaves_no_file(tmp_path, monkeypatch, capsys):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: tmp_path))
    run = lambda *argv: cli._pulse(types.SimpleNamespace(pulse_action=argv[0], value=argv[1] if len(argv) > 1 else "",
                                                        extra=argv[2] if len(argv) > 2 else ""))
    assert run("cards") == 0 and run("learned") == 0 and run("rate", "1", "up") == 1
    assert not (tmp_path / store.DB_NAME).exists()


def brief_due_now(live):
    from datetime import datetime, timezone
    live.store.set("time_zone", "UTC")
    live.store.set("brief_time", datetime.fromtimestamp(time.time() - 60, timezone.utc).strftime("%H:%M"))


def test_held_cards_go_out_in_the_morning_brief_once(live):
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()  # the follow-up becomes a held card
    assert live.sent() == []
    brief_due_now(live)
    pulse.tick()
    [text] = live.sent()
    assert text.startswith("Pulse morning brief ") and "everything is green" in text and 'the chat "Deploy"' in text
    [attempt] = live.store.in_flight()
    assert attempt["kind"] == "brief"
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    assert live.store.cards()[0]["briefed_at"] is not None and live.store.held_cards(10) == []
    pulse.tick()
    assert len(live.sent()) == 1


def test_no_brief_without_held_cards_or_before_its_time(live):
    brief_due_now(live)
    pulse.tick()
    assert live.sent() == []
    from datetime import datetime, timezone
    live.store.set("brief_time", datetime.fromtimestamp(time.time() + 3600, timezone.utc).strftime("%H:%M"))
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    assert live.sent() == [] and len(live.store.held_cards(10)) == 1


def test_a_brief_turn_pushes(live):
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    brief_due_now(live)
    pulse.tick()
    [attempt] = live.store.in_flight()
    pulse._mark_turn(SID, "turn-b", attempt["tag"])
    assert pulse.pushes(SID, "turn-b") is True


def test_a_failed_push_becomes_a_card_once_follow_ups_go_quiet(live):
    item_id = tell(live)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.FAILED, detail="boom")
    control.set_level(live.store, "followup", "screen")
    pulse.tick()
    assert len(live.sent()) == 1 and live.store.cards()[0]["level"] == "screen"
    assert live.store.item(item_id)["status"] == "done"


def test_too_late_brings_booked_checks_sooner(live):
    card = told_card(live)
    item_id = promise(live, "Hermes will check the logs.")
    now = time.time()
    followup.schedule(live.store, now)
    before = {r["item_id"]: r["next_check_at"] for r in live.store.followups()}[item_id]
    control.rate(live.store, card["id"], "down", "too_late")
    after = {r["item_id"]: r["next_check_at"] for r in live.store.followups()}[item_id]
    assert after < before and after >= now


def test_old_cards_are_pruned_but_held_ones_stay(live):
    for n in range(5):
        live.store.add_card({"kind": "followup", "level": "brief" if n == 0 else "push", "created_at": n})
    assert live.store.prune_cards(2) == 2
    assert sorted(card["created_at"] for card in live.store.cards()) == [0, 3, 4]


def test_brief_time_setting(live):
    control.update(live.store, {"brief_time": "7:30"})
    assert live.store.get("brief_time") == "07:30"
    for bad in ("7:30,9:00", "noon", 7):
        with pytest.raises(control.Refused):
            control.update(live.store, {"brief_time": bad})


def test_a_card_is_made_once_per_item_and_news(live):
    item = {"id": 7, "text": "t", "evidence": "e", "profile": "", "chat_id": CHAT, "message_id": 1}
    first = learning.add_card(live.store, kind="followup", item=item, level_name="screen", news="n", why="w",
                              chat_title="", now=1)
    again = learning.add_card(live.store, kind="followup", item=item, level_name="screen", news="n", why="w",
                              chat_title="", now=2)
    assert first == again and len(live.store.cards()) == 1


def test_held_cards_older_than_a_month_can_be_pruned(live):
    for n in range(3):
        live.store.add_card({"kind": "followup", "level": "brief", "created_at": n})
    assert live.store.prune_cards(1) == 0
    assert live.store.prune_cards(1, held_before=1.5) == 2
    assert live.store.held_count() == 1


def test_two_not_relevant_ratings_from_one_chat_make_one_rule(live):
    first, second = told_card(live), told_card(live, "Another one.")
    control.rate(live.store, first["id"], "down", "not_relevant")
    made = control.rate(live.store, second["id"], "down", "not_relevant")
    assert [lesson["what"] for lesson in made] == ["level"]  # two downs step the ladder; no second rule
    assert len(learning.rules(live.store, "followup")) == 1


def test_not_relevant_on_another_topic_in_the_same_chat_adds_its_rule(live):
    first = told_card(live)
    with live.store._connect() as db:
        db.execute("UPDATE cards SET title = 'Another topic' WHERE id = ?", (first["id"],))
    second = told_card(live, "Another one.")
    control.rate(live.store, first["id"], "down", "not_relevant")
    control.rate(live.store, second["id"], "down", "not_relevant")
    assert len(learning.rules(live.store, "followup")) == 2


def test_a_level_already_in_force_adds_no_second_lesson_even_past_the_early_check(live):
    assert learning.set_level(live.store, "followup", "screen", 1.0)
    row = {"kind": "followup", "what": learning.LEVEL, "value": "screen", "profile": "", "chat_id": "",
           "card_id": None, "cause": "set", "created_at": 2.0}
    assert live.store.add_lesson(row, changes_from="push") is None  # a racing set lands here
    assert live.store.add_lesson(dict(row, value="push"), changes_from="push")
    assert live.store.add_lesson(dict(row, value="push"), changes_from="push") is None
    assert [lesson["value"] for lesson in live.store.lessons("followup", learning.LEVEL)] == ["screen", "push"]
    # A step worked out from "push" isn't laid over a move to "brief" made meanwhile.
    assert live.store.add_lesson(dict(row, value="brief"), changes_from="push")
    assert live.store.add_lesson(dict(row, value="screen"), changes_from="push", expect="push") is None
    assert learning.level(live.store, "followup") == "brief"


def test_a_step_refused_over_a_move_made_meanwhile_steps_from_the_new_level(live, monkeypatch):
    first, second = told_card(live), told_card(live, "Another one.")
    control.rate(live.store, first["id"], "down")
    real = live.store.swap
    row = {"kind": "followup", "what": learning.LEVEL, "value": "screen", "profile": "", "chat_id": "",
           "card_id": None, "cause": "set", "created_at": 2.0}

    def swap(key, old, new):  # another writer moves the level just as the thumbs are spent
        if key == learning.SCORES:
            monkeypatch.setattr(live.store, "swap", real)
            assert live.store.add_lesson(row)
        return real(key, old, new)

    monkeypatch.setattr(live.store, "swap", swap)
    made = control.rate(live.store, second["id"], "down")
    assert learning.level(live.store, "followup") == "brief"
    assert [lesson["what"] for lesson in made] == ["level"]


def test_ratings_are_refused_while_pulse_is_off(live):
    card = told_card(live)
    live.store.set("enabled", False)
    with pytest.raises(control.Refused) as refused:
        control.rate(live.store, card["id"], "down", "too_often")
    assert refused.value.code == "pulse_off"
    assert live.store.card(card["id"])["rating"] == ""


def test_cli_refuses_ids_too_long_for_a_number(live, monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    assert cli._pulse(types.SimpleNamespace(pulse_action="rate", value="9" * 5000, extra="up")) == 2
    assert cli._pulse(types.SimpleNamespace(pulse_action="learned", value="undo", extra="9" * 5000)) == 2


def test_levels_are_refused_while_pulse_is_off(live, monkeypatch, capsys):
    import sys
    import types
    live.store.set("enabled", False)
    with pytest.raises(control.Refused) as refused:
        control.set_level(live.store, "followup", "screen")
    assert refused.value.code == "pulse_off"
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    assert cli._pulse(types.SimpleNamespace(pulse_action="learned", value="followup", extra="screen")) == 1
    assert cli._pulse(types.SimpleNamespace(pulse_action="rate", value="²", extra="up")) == 2


def test_cli_learned_set_on_a_host_that_never_turned_pulse_on_leaves_no_file(tmp_path, monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: tmp_path))
    assert cli._pulse(types.SimpleNamespace(pulse_action="learned", value="followup", extra="screen")) == 1
    assert not (tmp_path / store.DB_NAME).exists()


def test_a_delivered_brief_lands_on_the_pulse_screen_as_hermes_wrote_it(live):
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    brief_due_now(live)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"])
    reply_id = live.db.message("assistant", "Good morning!\n\n- The deploy finished and everything is green.")
    pulse.tick()
    brief_card = live.store.cards()[0]
    assert brief_card["kind"] == "brief" and brief_card["level"] == "push" and brief_card["chat_id"] == SID
    assert brief_card["news"] == "Good morning!\n\n- The deploy finished and everything is green."
    assert brief_card["message_id"] == reply_id and brief_card["why"] and brief_card["item_id"] is None
    pulse.tick()
    assert [card["kind"] for card in live.store.cards()].count("brief") == 1
    page = control.feed(live.store, live.home)
    assert page["home_chat_id"] == SID and page["cards"][0]["kind"] == "brief"
    with pytest.raises(control.Refused):  # no loudness ladder for briefs (yet)
        control.rate(live.store, brief_card["id"], "up")


def test_a_requested_report_lands_on_the_pulse_screen_and_pushes_its_items(live):
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    assert attempt["kind"] == "report"
    pulse._mark_turn(SID, "turn-r", attempt["tag"])
    assert pulse.pushes(SID, "turn-r") is False
    assert pulse.pushes(SID, "turn-r", "Nothing worth bringing up right now.") is False
    # A report that brought something up tells the phone, in plain lines.
    reply = "1. [date] Dentist Friday at 9\n2. [promise] Send the deck #4"
    assert pulse.pushes(SID, "turn-r", reply) is True
    assert pulse.push_body(SID, "turn-r", reply) == "Dentist Friday at 9\nSend the deck"
    assert pulse.push_body(SID, "other-turn", reply) == reply
    live.db.message("user", attempt["text"])
    reply_id = live.db.message("assistant", "Here's your report.\n1. Deploy: green")
    pulse.tick()
    [card] = live.store.cards()
    assert card["kind"] == "report" and card["level"] == "screen" and card["message_id"] == reply_id
    assert live.store.unseen_cards() == 1


def test_a_failed_report_makes_no_card_and_feed_has_no_chat_without_home(live, tmp_path):
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.FAILED, detail="boom")
    pulse.tick()
    assert live.store.cards() == []
    assert control.feed(live.store)["home_chat_id"] == ""


def test_a_report_card_a_busy_state_db_kept_out_lands_on_a_later_tick(live, monkeypatch):
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"])
    live.db.message("assistant", "Here's your report.")
    learning = pulse.learning
    real = learning.add_turn_card

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(learning, "add_turn_card", locked)
    pulse.tick()  # delivered, but the card couldn't be written; the tick goes on
    assert live.store.cards() == [] and live.store.get("last_tick_at")
    monkeypatch.setattr(learning, "add_turn_card", real)
    pulse.tick()
    assert [card["kind"] for card in live.store.cards()] == ["report"]
    assert live.store.get(pulse.TURN_CARDS_DONE) == [attempt["attempt_id"]]
    looked = []
    monkeypatch.setattr(pulse.hermes, "find_turn", lambda *a, **kw: looked.append(a))
    pulse.tick()  # its card landed: not looked up again
    assert looked == [] and len(live.store.cards()) == 1


def test_a_report_becomes_one_card_per_item(live, monkeypatch):
    item_id = promise(live)
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"] + f"\n\n## What Pulse remembers\n#{item_id} [promise] Tell the deploy")
    reply_id = live.db.message("assistant", f"""1. [date] The dentist is Friday at 9 (calendar).
2. [promise] Hermes still owes the deploy note (from Monday) #{item_id}
3. [Weird] Something odd #999999""")
    pulse.tick()
    cards = sorted(live.store.cards(), key=lambda card: card["id"])
    assert [(c["kind"], c["topic"]) for c in cards] == [("noticed", "date"), ("noticed", "promise"),
                                                         ("noticed", "other")]
    date, owed, odd = cards
    assert date["news"] == "The dentist is Friday at 9 (calendar)." and date["item_id"] is None
    assert date["chat_id"] == SID and date["message_id"] == reply_id and date["level"] == "screen"
    # Drawn from a remembered thing: points at its chat, so Do it and Done work.
    assert owed["item_id"] == item_id and owed["chat_id"] == CHAT and owed["news"].endswith("(from Monday)")
    assert odd["item_id"] is None  # #999999 wasn't shown in the report
    assert live.store.get(pulse.TURN_CARDS_DONE) == [attempt["attempt_id"]]
    pulse.tick()
    assert len(live.store.cards()) == 3  # once
    entry = next(c for c in control.feed(live.store)["cards"] if c["id"] == owed["id"])
    assert entry["topic"] == "promise"
    # Thumbs on an item: no ladder, but the report counts it, and a remembered
    # thing rated down stays out of later reports.
    assert control.rate(live.store, date["id"], "up") == []
    assert control.rate(live.store, owed["id"], "down") == []
    assert pulse.report.rating_counts(live.store) == {"date": (1, 0), "promise": (0, 1)}
    assert item_id in pulse.report.rated_not_useful(live.store)
    assert control.done(live.store, owed["id"]) is True
    assert live.store.item(item_id)["status"] == "done"
    with pytest.raises(control.Refused):
        control.done(live.store, date["id"])  # nothing remembered behind it
    # A reason has nothing to move on an item's card.
    with pytest.raises(control.Refused):
        control.rate(live.store, odd["id"], "down", "too_late")
    # Typed ratings of a split report aren't read: an item counts once.
    assert live.store.get(store.SPLIT_REPORTS) == [attempt["attempt_id"]]
    live.db.message("user", "2 yes")
    monkeypatch.setattr(pulse, "_last_ratings", 0.0)
    pulse._collect_ratings(live.store, live.home, time.time())
    assert pulse.report.rating_counts(live.store) == {"date": (1, 0), "promise": (0, 1)}
    # Even a rating typed before the cards landed doesn't count again.
    live.store.set_ratings(attempt["attempt_id"], "[]", '[[2, "promise", true]]')
    assert pulse.report.rating_counts(live.store) == {"date": (1, 0), "promise": (0, 1)}


def test_report_item_cards_and_their_mark_land_together(live, monkeypatch):
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"])
    live.db.message("assistant", "1. [date] Dentist Friday\n2. [fact] The repo moved")
    # The mark fails to land: the cards don't either, and the next tick adds them once.
    real = store.PulseStore.add_cards

    def busy(self, rows, **marks):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.PulseStore, "add_cards", busy)
    pulse.tick()
    assert live.store.cards() == [] and not live.store.get(pulse.TURN_CARDS_DONE)
    monkeypatch.setattr(store.PulseStore, "add_cards", real)
    pulse.tick()
    pulse.tick()
    assert len(live.store.cards()) == 2
    # Another process that split it too, from a stale read, adds nothing.
    row = {"kind": "noticed", "level": "screen", "news": "again", "created_at": time.time()}
    assert live.store.add_cards([row], attempt=attempt["attempt_id"], marks=(pulse.TURN_CARDS_DONE,)) == []
    assert len(live.store.cards()) == 2


def test_a_rating_typed_before_the_split_goes_with_it(live):
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"])
    live.db.message("assistant", "1. [date] Dentist Friday\n2. [fact] The repo moved")
    live.store.set_ratings(attempt["attempt_id"], "[]", '[[1, "date", true]]')
    pulse.tick()
    assert len(live.store.cards()) == 2
    # Long after, with the split mark trimmed away, it still doesn't count.
    live.store.set(store.SPLIT_REPORTS, [])
    assert pulse.report.rating_counts(live.store) == {}
    # Nor is it read back from the chat and stored again.
    assert live.store.set_ratings(attempt["attempt_id"], "[]", '[[1, "date", true]]') is False
    assert pulse.report.rating_counts(live.store) == {}


def test_a_split_report_takes_no_typed_rating(live):
    live.store.set(store.SPLIT_REPORTS, ["a1"])
    assert live.store.set_ratings("a1", "[]", '[[1, "date", true]]') is False
    assert live.store.all_ratings() == []


def test_a_settled_card_mark_is_appended_once(live):
    pulse._mark_turn_card(live.store, "a1")
    pulse._mark_turn_card(live.store, "a2")
    pulse._mark_turn_card(live.store, "a1")
    assert live.store.get(pulse.TURN_CARDS_DONE) == ["a1", "a2"]


def test_a_line_naming_more_than_one_number_links_nothing():
    assert learning.report_lines("1. [interest] PR #12 is waiting on #4") == [
        ("interest", "PR #12 is waiting on #4", None)]
    assert learning.report_lines("1. [promise] Send the deck #4") == [("promise", "Send the deck", 4)]


def test_report_text_asks_for_cards_not_a_shadow():
    text = pulse.report.report_text("pAAAA-1", "context", "Mon 09:00")
    assert "would have" not in text and "Rate them" not in text and "dry run" not in text


def test_a_report_that_brought_nothing_up_makes_no_card(live, monkeypatch):
    live.store.set("report_requested", True)
    pulse.tick()
    [attempt] = live.store.in_flight()
    live.db.message("user", attempt["text"])
    live.db.message("assistant", "Here's what I would have brought up:\n\nNothing worth bringing up right now.")
    pulse.tick()
    assert live.store.cards() == [] and live.store.get(pulse.TURN_CARDS_DONE) == [attempt["attempt_id"]]
    looked = []
    monkeypatch.setattr(pulse.hermes, "find_turn", lambda *a, **kw: looked.append(a))
    pulse.tick()
    assert looked == []


def test_cards_of_reports_that_brought_nothing_up_are_taken_away(live):
    def card(news):
        return live.store.add_card({"kind": "report", "item_id": None, "level": "screen", "news": news, "why": "",
                                    "chat_id": SID, "chat_title": "Pulse", "message_id": 1, "created_at": time.time()})

    empty = card("Here's what I would have brought up:\nNothing worth bringing up right now.\nRate them: reply like 1 yes, 2 no.")
    full = card("Here's what I would have brought up:\n1. [promise] Send the deck #4\nRate them: reply like 1 yes, 2 no.")
    bold = card("Nothing worth bringing up right now, except:\n**1.** [promise] Call Sam")
    assert learning.empty_turn("brief", "Nothing worth bringing up right now.") is False
    # The words the report is told to use when it has nothing.
    assert learning._REPORT_NOTHING.search(pulse.report.REPORT_TEXT)
    assert learning.drop_empty_reports(live.store) == 1
    assert {c["id"] for c in live.store.cards()} == {full, bold} and empty not in {c["id"] for c in live.store.cards()}


def test_feed_names_no_chat_while_pulse_is_off(live):
    assert control.feed(live.store, live.home)["home_chat_id"] == SID
    live.store.set("enabled", False)
    assert control.feed(live.store, live.home)["home_chat_id"] == ""


def test_done_closes_the_promise_without_a_rating_and_undo_puts_it_back(live):
    item_id = promise(live)
    check_now(live, llm_saying(decision="wait", news="", why="not yet"), now=time.time() + followup.FIRST_CHECK_S + 1)
    before = live.store.followup(item_id)
    assert before["decision"] == "wait" and before["next_check_at"] is not None
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="screen",
                                news="Still waiting on the deploy.", why="", chat_title="Deploy", now=time.time())
    assert control.done(live.store, card_id) is True
    assert control.done(live.store, card_id) is False  # already
    item = live.store.item(item_id)
    assert item["status"] == "done" and item["closed_by"] == learning.DONE_BY
    assert live.store.followup(item_id)["next_check_at"] is None
    [entry] = [card for card in control.feed(live.store)["cards"] if card["id"] == card_id]
    assert entry["done_at"] is not None and entry["rating"] == "" and entry["seen_at"] is not None
    assert learning.learned(live.store) == []  # no lesson: Pulse was right to bring it up
    assert control.done(live.store, card_id, False) is True
    item = live.store.item(item_id)
    assert item["status"] == "open" and item["closed_by"] == ""
    after = live.store.followup(item_id)
    assert after["decision"] == "wait" and after["next_check_at"] >= before["next_check_at"] - 1
    assert control.feed(live.store)["cards"][0]["done_at"] is None
    assert control.done(live.store, card_id, False) is False


def test_done_upgrades_a_reminded_or_told_card_and_undo_restores_that(live):
    card = told_card(live)
    item = live.store.item(card["item_id"])
    told_status, told_by = item["status"], item["closed_by"]
    assert control.done(live.store, card["id"]) is True
    assert live.store.item(card["item_id"])["status"] == "done"
    assert control.done(live.store, card["id"], False) is True
    item = live.store.item(card["item_id"])
    assert (item["status"], item["closed_by"]) == (told_status, told_by)


def test_a_done_card_leaves_the_morning_brief(live):
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    [card] = live.store.cards()
    assert live.store.held_count() == 1
    control.done(live.store, card["id"])
    assert live.store.held_count() == 0 and live.store.held_cards(10) == []


def test_done_refuses_what_isnt_a_follow_up_card(live):
    card = told_card(live)
    for card_id, value, field in ((999, True, "card"), ("1", True, "card"), (True, True, "card"),
                                  (card["id"], "yes", "done"), (card["id"], 1, "done"),
                                  (card["id"], None, "done")):
        with pytest.raises(control.Refused) as refused:
            control.done(live.store, card_id, value)
        assert refused.value.data == {"field": field}


def test_cli_marks_a_card_done_and_undoes_it(live, monkeypatch, capsys):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    card = told_card(live)
    run = lambda *argv: cli._pulse(types.SimpleNamespace(pulse_action=argv[0], value=argv[1] if len(argv) > 1 else "",
                                                        extra=argv[2] if len(argv) > 2 else ""))
    assert run("done", str(card["id"])) == 0 and "Marked done" in capsys.readouterr().out
    assert run("cards") == 0 and "(done)" in capsys.readouterr().out
    assert run("done", str(card["id"]), "undo") == 0 and "Back as it was" in capsys.readouterr().out
    assert run("done", "x") == 2 and run("done", str(card["id"]), "maybe") == 2


def test_pruned_cards_take_their_done_marks(live):
    card = told_card(live)
    control.done(live.store, card["id"])
    told_card(live, "The build failed.")
    live.store.prune_cards(1)
    assert live.store.done_marks([card["id"]]) == {}


def run_acts(live, llm, now):
    done = {}
    followup.check_acts(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=now,
                        still_leader=lambda: True, budget=10_000, done=done)
    return done


def test_do_it_marks_the_card_done_once_the_chat_shows_it_got_done(live):
    card = told_card(live)
    acted_at = time.time()
    assert control.acted(live.store, card["id"], now=acted_at) is True
    said = llm_saying(done=True, why="Hermes confirmed the deploy note was sent")
    # Nothing answered yet: no model call, look again later.
    assert run_acts(live, said, acted_at + followup.ACT_FIRST_S) == {} and said.calls == []
    assert live.store.act(card["id"])["check_at"] > acted_at + followup.ACT_FIRST_S
    second = acted_at + 2 * followup.ACT_FIRST_S
    live.db.message("user", "Pulse reminded me: the deploy. Let's take care of it now.", sid=CHAT, at=acted_at + 5)
    live.db.message("assistant", "Done, I sent the deploy note.", sid=CHAT, at=second - 30)
    # Answered but still going a moment ago: wait for the chat to go quiet.
    run_acts(live, said, second)
    assert said.calls == []
    run_acts(live, said, second + followup.ACT_FIRST_S)
    [text] = said.calls
    assert "Done, I sent the deploy note." in text and "everything is green" in text
    [entry] = [c for c in control.feed(live.store)["cards"] if c["id"] == card["id"]]
    assert entry["done_at"] is not None and entry["done_by"] == "pulse"
    assert live.store.act(card["id"])["outcome"] == "done"
    # Undo works on what Pulse marked too.
    assert control.done(live.store, card["id"], False) is True
    assert live.store.done_marks([card["id"]]) == {}


def test_an_open_promise_pulse_saw_done_after_do_it_closes_and_undo_reopens_it(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="screen",
                                news="Still waiting on the deploy.", why="", chat_title="Deploy", now=time.time())
    acted_at = time.time()
    control.acted(live.store, card_id, now=acted_at)
    live.db.message("assistant", "Deploy is out.", sid=CHAT, at=acted_at + 30)
    run_acts(live, llm_saying(done=True, why="it's out"), acted_at + followup.ACT_FIRST_S + followup.ACT_QUIET_S)
    item = live.store.item(item_id)
    assert item["status"] == "done" and item["closed_by"] == learning.ACT_DONE_BY
    control.done(live.store, card_id, False)
    assert live.store.item(item_id)["status"] == "open"


def test_do_it_not_done_looks_again_then_stops(live):
    card = told_card(live)
    acted_at = time.time()
    control.acted(live.store, card["id"], now=acted_at)
    live.db.message("assistant", "That failed, the server is down.", sid=CHAT, at=acted_at + 30)
    said = llm_saying(done=False, why="it failed")
    now = acted_at + followup.ACT_FIRST_S + followup.ACT_QUIET_S
    for again in followup.ACT_AGAIN_S:
        run_acts(live, said, now)
        act = live.store.act(card["id"])
        assert act["outcome"] == "" and act["check_at"] == now + again
        now += again
    run_acts(live, said, now)
    assert live.store.act(card["id"])["outcome"] == "not done" and live.store.act(card["id"])["check_at"] is None
    assert len(said.calls) == len(followup.ACT_AGAIN_S) + 1
    assert live.store.done_marks([card["id"]]) == {}


def test_do_it_left_unsent_gives_up_without_a_model_call(live):
    card = told_card(live)
    acted_at = time.time()
    control.acted(live.store, card["id"], now=acted_at)
    said = llm_saying(done=True, why="")
    run_acts(live, said, acted_at + followup.ACT_GIVE_UP_S + 1)
    assert said.calls == [] and live.store.act(card["id"])["outcome"] == "no answer"


def test_do_it_on_a_done_card_books_nothing_and_done_stops_a_booked_check(live):
    card = told_card(live)
    control.acted(live.store, card["id"])
    control.done(live.store, card["id"])
    assert live.store.act(card["id"])["check_at"] is None
    assert control.acted(live.store, card["id"]) is False
    with pytest.raises(control.Refused):
        control.acted(live.store, 999)


def test_a_store_from_before_card_topics_gets_the_column(live):
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("ALTER TABLE cards DROP COLUMN topic")
    reopened = store.PulseStore(live.store.path)
    card_id = reopened.add_card({"kind": "noticed", "level": "screen", "news": "x", "topic": "date",
                                 "created_at": time.time()})
    assert reopened.card(card_id)["topic"] == "date"
