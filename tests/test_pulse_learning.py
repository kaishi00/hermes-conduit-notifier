"""Ratings that change what Pulse does (pulse_learning)."""

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
