"""Ratings that change what Pulse does (pulse_learning)."""

import json
import sqlite3
import threading
import time

import pytest

from test_pulse import ROOT, SID, _load, cli, control, pulse, store
from test_pulse_followup import CHAT, _iso, check_now, followup, live, llm_saying, logged, promise, tell  # noqa: F401

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


def test_cli_sets_shows_and_clears_the_note_to_pulse(live, monkeypatch, capsys):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    run = lambda *argv: cli._pulse(types.SimpleNamespace(pulse_action=argv[0], value=argv[1] if len(argv) > 1 else "",
                                                        extra=argv[2] if len(argv) > 2 else ""))
    assert run("note") == 0 and "No note to Pulse" in capsys.readouterr().out
    assert run("note", " Never bring up the gym.\nAlways tell me when a build finishes. ") == 0
    assert "Note to Pulse: Never bring up the gym. Always tell me when a build finishes." in capsys.readouterr().out
    assert live.store.get("note") == "Never bring up the gym.\nAlways tell me when a build finishes."
    assert run("status") == 0 and "Note to Pulse: Never bring up the gym." in capsys.readouterr().out
    assert run("note", "x" * (control.NOTE_LIMIT + 1)) == 2 and "at most" in capsys.readouterr().out
    assert live.store.get("note").startswith("Never bring up the gym.")
    assert run("note", "clear") == 0 and "No note to Pulse" in capsys.readouterr().out
    assert live.store.get("note") == ""
    # Two words left unquoted are still the whole note.
    assert run("note", "Never", "nag.") == 0 and "Note to Pulse: Never nag." in capsys.readouterr().out
    assert live.store.get("note") == "Never nag."


def test_cli_on_a_host_that_never_turned_pulse_on_leaves_no_file(tmp_path, monkeypatch, capsys):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: tmp_path))
    run = lambda *argv: cli._pulse(types.SimpleNamespace(pulse_action=argv[0], value=argv[1] if len(argv) > 1 else "",
                                                        extra=argv[2] if len(argv) > 2 else ""))
    assert run("cards") == 0 and run("learned") == 0 and run("rate", "1", "up") == 1 and run("note") == 0
    assert run("note", "clear") == 0
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


def test_the_brief_comes_every_morning_with_nothing_held_but_not_before_its_time(live):
    from datetime import datetime, timezone
    live.store.set("time_zone", "UTC")
    live.store.set("brief_time", datetime.fromtimestamp(time.time() + 3600, timezone.utc).strftime("%H:%M"))
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    assert live.sent() == [] and len(live.store.held_cards(10)) == 1
    live.store.set("followups", False)
    control.done(live.store, live.store.held_cards(10)[0]["id"], True)
    brief_due_now(live)
    pulse.tick()
    [text] = live.sent()  # nothing held, and the brief still comes
    assert text.startswith("Pulse morning brief ") and "Pulse holds no news for this brief" in text
    assert pulse.brief.MEMORY_OFF in text  # memory is off here: no claim that nothing is open
    assert followup_or_brief_kinds(live) == ["brief"]
    [attempt] = live.store.in_flight()
    live.store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse.tick()
    assert len(live.sent()) == 1  # once a day


def test_a_brief_with_nothing_kept_still_reads_the_calendar(live, monkeypatch):
    live.store.set("memory", True)
    monkeypatch.setattr(pulse.report, "phone_context",
                        lambda home, now: ("## Calendar, the next 2 days (phone synced 5 min ago)\n- Dentist (09:30)",
                                           None))
    brief_due_now(live)
    pulse.tick()
    [text] = live.sent()
    # What Pulse keeps is empty, and the calendar is still what's on.
    assert "Pulse holds no news for this brief and remembers nothing still open." in text
    assert text.index(pulse.brief.CALENDAR_COUNTS) < text.index("- Dentist (09:30)")
    assert "Nothing open" not in text and pulse.brief.MEMORY_OFF not in text


def test_the_empty_brief_names_only_what_pulse_read():
    nothing = pulse.brief._nothing
    assert nothing(True, True, "Sat 24 Oct 18:00") == (
        "Pulse holds no news for this brief, remembers nothing still open, and the phone's automations logged "
        "nothing since Sat 24 Oct 18:00. The calendar and reminders below, if any, still count.")
    # Memory off, no Shortcuts inbox: it claims nothing about either.
    off = nothing(False, False, "Sat 24 Oct 18:00")
    assert off == ("Pulse holds no news for this brief. Pulse memory is off, so it doesn't track open promises or "
                   "to-dos. The calendar and reminders below, if any, still count.")


def followup_or_brief_kinds(live):
    return [row["kind"] for row in live.store.in_flight()]


def test_the_brief_sums_up_what_is_open_and_what_happened_since(live):
    live.store.set("memory", True)
    promise(live, "Hermes will send the release notes once the deploy is out.")
    live.db.session(sid="other", title="Release", pinned=0)
    live.db.message("assistant", "Sent the release notes for the deploy.", sid="other", at=time.time() - 60)
    live.store.set("note", "Never bring up the gym.")
    brief_due_now(live)
    pulse.tick()
    [text] = live.sent()
    assert "send the release notes" in text and "## Later on the same subject" in text
    assert "Sent the release notes" in text and pulse.brief.HELD_TITLE not in text
    # The user's note to Pulse goes with the brief, before its context.
    assert text.index(f"{pulse.report.NOTE_TITLE}\nNever bring up the gym.") < text.index("<<<context ")


def test_the_brief_reads_what_the_phone_logged_since_the_last_report_fenced(live):
    logged(live.home,
           {"id": "old", "kind": "notification", "at": _iso(time.time() - 3 * 86400), "source": "Desjardins",
            "text": "Rent paid"},
           {"id": "new", "kind": "notification", "at": _iso(time.time() - 3600), "source": "UPS",
            "text": "Your parcel is out for delivery"})
    brief_due_now(live)
    pulse.tick()
    [text] = live.sent()
    assert "Your parcel is out for delivery" in text and "Rent paid" not in text  # since the last report only
    assert "## Logged on the phone since " in text and "other people's words" in text
    assert "Pulse holds no news" not in text  # it logged something
    fence = text.split("<<<context ", 1)[1].split("\n", 1)[0]
    assert text.rstrip().endswith(f"context {fence}>>>")


def test_a_long_brief_is_cut_to_the_reports_budget(live, monkeypatch):
    monkeypatch.setenv(pulse.report.LCM_THRESHOLD, "6000")
    text = pulse.brief.text_for("x" * 20_000)("t1")
    assert len(text) <= pulse.report.report_budget() and text.rstrip().endswith(">>>")
    text = pulse.brief.text_for("x" * 20_000, "n" * 1_500)("t1")
    assert len(text) <= pulse.report.report_budget("n" * 1_500) and "n" * 1_500 in text


def test_a_busy_brief_keeps_the_calendar_and_lets_the_held_news_go(live, monkeypatch):
    from datetime import timezone
    monkeypatch.delenv(pulse.report.LCM_THRESHOLD, raising=False)
    live.store.set("memory", True)
    phone = ("## Calendar, the next 2 days (phone synced 5 min ago)\n"
             + "\n".join(f"- Meeting {n} {'m' * 95} (2026-10-25 {n % 24:02d}:00)" for n in range(25))
             + "\n\n## Open reminders\n" + "\n".join(f"- Reminder {n} {'r' * 95} (due 2026-10-26)" for n in range(25)))
    monkeypatch.setattr(pulse.report, "phone_context", lambda home, now: (phone, None))
    monkeypatch.setattr(pulse.report, "remembered_lines",
                        lambda items, zone: (pulse.report.REMEMBERED_TITLE + "\n" + "o" * 3_400, []))
    monkeypatch.setattr(pulse.report, "inbox_context", lambda home, since, title="": title + "\n" + "i" * 2_000)
    cards = [{"news": f"Held {n} " + "h" * 390, "chat_title": "Deploy"} for n in range(pulse.brief.MAX_CARDS)]
    context = pulse.brief.context(live.home, live.store, cards, home_chat_id="home", now=time.time(),
                                  zone=timezone.utc)
    text = pulse.brief.text_for(context)("t1")
    # Over the budget, the cut takes the held news (each shows as its own card), never the day's calendar.
    assert len(context) > pulse.report.report_budget() and len(text) <= pulse.report.report_budget()
    assert phone in text and f"- Held {pulse.brief.MAX_CARDS - 1} " not in text


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
    assert pulse.push_copy(SID, "turn-b", "Two things waited.") == {
        "title_label": "pulse.brief", "title": "Your morning brief", "body": "Two things waited."}
    # The summary line is the push; the lines under it are for the card.
    assert pulse.push_copy(SID, "turn-b", "\nA calm day, two things open.\n- Send the notes\n- Call Sam")["body"] \
        == "A calm day, two things open."
    # A first line that runs on is cut to what a push should carry.
    body = pulse.push_copy(SID, "turn-b", "A long day. " * 40 + "\n- Send the notes")["body"]
    assert len(body) == pulse.brief.SUMMARY_LIMIT and body.endswith("…")


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


def test_a_held_card_put_away_is_pruned_like_any_other(live):
    for n in range(4):
        live.store.add_card({"kind": "followup", "level": "brief" if n < 2 else "push", "created_at": n})
    held = min(live.store.cards(), key=lambda card: card["created_at"])
    assert live.store.dismiss_card(held["id"], True, 10)
    assert live.store.prune_cards(2) == 1
    assert sorted(card["created_at"] for card in live.store.cards()) == [1, 2, 3]


def test_a_card_put_away_while_its_brief_is_out_waits_for_the_next(live):
    first = live.store.add_card({"kind": "followup", "level": "brief", "created_at": 1})
    second = live.store.add_card({"kind": "followup", "level": "brief", "created_at": 2})
    assert live.store.dismiss_card(second, True, 5)
    live.store.brief_cards([first, second], 6, "b1")
    assert live.store.dismiss_card(second, False, 7)
    assert [card["id"] for card in live.store.held_cards(10)] == [second]


def test_brief_time_setting(live):
    # By default the brief comes an hour before the first report, never with it.
    assert pulse.brief.DEFAULT_TIME == store.DEFAULTS["brief_time"] == "07:00"
    assert store.DEFAULTS["brief_time"] not in store.DEFAULTS["report_time"].split(",")
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
    # The held card it brought carries the brief's tag, so Conduit shows it under the brief.
    held = next(card for card in live.store.cards() if card["kind"] == "followup")
    assert held["briefed_at"] is not None and held["brief"] == brief_card["brief"] == attempt["item_id"]
    entry = next(c for c in control.feed(live.store, live.home)["cards"] if c["id"] == held["id"])
    assert entry["brief"] == attempt["item_id"] and entry["dismissed_at"] is None
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
    assert pulse.push_copy(SID, "turn-r", reply) == {
        "title_label": "pulse.noticed", "title": "Pulse noticed 2 things", "count": 2,
        "body": "Dentist Friday at 9 · Send the deck"}
    one = pulse.push_copy(SID, "turn-r", "1. [date] Dentist\n   Friday")
    assert one["title"] == "Pulse noticed something" and "count" not in one
    assert pulse.push_copy(SID, "other-turn", reply)["body"] == reply
    live.db.message("user", attempt["text"])
    reply_id = live.db.message("assistant", "Here's your report.\n1. Deploy: green")
    pulse.tick()
    [card] = live.store.cards()
    assert card["kind"] == "report" and card["level"] == "screen" and card["message_id"] == reply_id
    assert live.store.unseen_cards() == 1
    # A whole-report card gives no stock reason for Why?.
    assert card["why"] == ""
    # A report written without a list asks for its lines without the reason in brackets.
    assert "(why now" not in attempt["text"] and "1. [kind] what to tell the user #12" in attempt["text"]


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
    # Nothing remembered behind it: Done marks the card alone.
    assert control.done(live.store, date["id"]) is True and live.store.done_marks([date["id"]])
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


def test_done_refuses_what_isnt_a_follow_up_or_report_item_card(live):
    card = told_card(live)
    report_card = live.store.add_card({"kind": "report", "level": "screen", "news": "Here's your report.",
                                       "created_at": time.time()})
    for card_id, value, field in ((999, True, "card"), ("1", True, "card"), (True, True, "card"),
                                  (report_card, True, "card"),
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
    live.store.dismiss_card(card["id"], True, 5)
    assert run("cards") == 0 and "(put away)" in capsys.readouterr().out


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


def test_a_do_it_check_reads_where_a_long_chat_got_it_done(live):
    card = told_card(live, news="The deploy note for the release still needs sending.")
    acted_at = time.time() - 600
    control.acted(live.store, card["id"], now=acted_at)
    live.db.message("assistant", "Sent the deploy note for the release.", sid=CHAT, at=acted_at + 10)
    for n in range(followup.MAX_MESSAGES):  # then the chat moved on
        live.db.message("user" if n % 2 else "assistant", f"Step {n} of the migration is green.", sid=CHAT,
                        at=acted_at + 20 + n)
    said = llm_saying(done=True, why="the note was sent")
    run_acts(live, said, time.time() + followup.ACT_FIRST_S)
    [text] = said.calls
    earlier = text.split("## Earlier lines from that chat on the same subject\n", 1)[1]
    assert "Sent the deploy note for the release." in earlier and "migration" not in earlier
    assert live.store.done_marks([card["id"]])


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


def test_do_it_done_in_another_chat_marks_the_card_done(live):
    card = told_card(live, news="The deploy note for the release still needs sending.")
    acted_at = time.time()
    control.acted(live.store, card["id"], now=acted_at)
    live.db.session(sid="other", title="Release", pinned=0)
    live.db.message("assistant", "Sent the release deploy note to the team.", sid="other", at=acted_at + 60)
    said = llm_saying(done=True, why="sent in the Release chat")
    run_acts(live, said, acted_at + followup.ACT_FIRST_S)
    [text] = said.calls  # the Do it chat never answered, but another chat did it
    assert "Sent the release deploy note" in text
    assert live.store.act(card["id"])["outcome"] == "done"


def found_card(live, *, chat_id=SID, news="The dentist is Friday at 9 (calendar).", topic="date"):
    """A report item Pulse found on its own, nothing remembered behind it:
    by default from no chat, so it points at the report's turn in Pulse's
    chat (pulse_learning.add_noticed_cards)."""
    return live.store.add_card({"kind": "noticed", "level": "screen", "topic": topic, "title": news, "news": news,
                                "chat_id": chat_id, "message_id": 1 if chat_id == SID else None,
                                "created_at": time.time()})


def test_done_marks_a_report_item_found_on_its_own_and_the_next_report_reads_it(live):
    card_id = found_card(live)
    assert control.done(live.store, card_id) is True
    assert control.done(live.store, card_id) is False  # already
    [entry] = [card for card in control.feed(live.store)["cards"] if card["id"] == card_id]
    assert entry["done_at"] is not None and entry["done_by"] == "user" and entry["seen_at"] is not None
    assert learning.learned(live.store) == []
    recent = pulse.candidates.recent_section(live.store, pulse.report.zone_for("UTC"), time.time())
    assert "The dentist is Friday at 9 (calendar). (done)" in recent
    assert "marked (done) in what Pulse brought up lately" in pulse.candidates.INSTRUCTIONS  # so it stays out
    assert control.done(live.store, card_id, False) is True
    assert live.store.done_marks([card_id]) == {}


def test_do_it_on_a_report_item_found_in_a_chat_reads_that_chat(live):
    card_id = found_card(live, chat_id=CHAT, news="Look at the flaky deploy (you asked in Deploy)", topic="follow-up")
    acted_at = time.time()
    assert control.acted(live.store, card_id, now=acted_at) is True
    live.db.message("user", "Pulse reminded me: the flaky deploy. Let's take care of it now.", sid=CHAT, at=acted_at + 5)
    live.db.message("assistant", "Fixed the flaky deploy test.", sid=CHAT, at=acted_at + 30)
    said = llm_saying(done=True, why="fixed")
    run_acts(live, said, acted_at + followup.ACT_FIRST_S)
    [text] = said.calls
    assert "Fixed the flaky deploy test." in text
    assert live.store.done_marks([card_id])[card_id]["done_by"] == "pulse"
    assert control.done(live.store, card_id, False) is True


def test_do_it_on_a_report_item_from_no_chat_reads_the_new_chat_not_pulses_own(live):
    card_id = found_card(live)
    acted_at = time.time()
    assert control.acted(live.store, card_id, now=acted_at) is True
    live.db.message("assistant", "Your Pulse report: the dentist is Friday at 9.", sid=SID, at=acted_at + 10)
    said = llm_saying(done=True, why="the reminder is set")
    # Pulse's own chat moving is no answer, and nothing else was said yet.
    run_acts(live, said, acted_at + followup.ACT_FIRST_S)
    assert said.calls == [] and live.store.act(card_id)["outcome"] == ""
    live.db.session(sid="new", title="New conversation", pinned=0)
    live.db.message("user", "Pulse reminded me: The dentist is Friday at 9 (calendar).\n\nLet's take care of it now.",
                    sid="new", at=acted_at + 700)
    live.db.message("assistant", "Set a reminder for the dentist on Friday, from your calendar.", sid="new",
                    at=acted_at + 720)
    run_acts(live, said, acted_at + 2 * followup.ACT_FIRST_S)
    [text] = said.calls
    assert "Set a reminder for the dentist" in text and "came from no chat" in text
    assert "Your Pulse report" not in text
    assert live.store.done_marks([card_id])[card_id]["done_by"] == "pulse"


def test_do_it_on_a_report_item_from_no_chat_leaves_pulses_chat_out_after_pulse_moves(live):
    card_id = found_card(live)
    live.store.set("home_session_key", "another")  # Pulse reports in another chat now
    acted_at = time.time()
    assert control.acted(live.store, card_id, now=acted_at) is True
    live.db.message("assistant", "Your Pulse report: the dentist is Friday at 9.", sid=SID, at=acted_at + 10)
    said = llm_saying(done=True, why="the reminder is set")
    run_acts(live, said, acted_at + followup.ACT_FIRST_S)
    # The chat the card points at was Pulse's, not one Do it opened.
    assert said.calls == [] and live.store.act(card_id)["outcome"] == ""


def run_held(live, llm, now):
    done = {}
    followup.check_held(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=now,
                        still_leader=lambda: True, budget=10_000, done=done)
    return done


def test_a_card_held_for_the_brief_done_meanwhile_is_left_out_of_it(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="The deploy finished and the release notes still need sending.", why="",
                                chat_title="Deploy", now=time.time() - 3600)
    live.db.session(sid="other", title="Release", pinned=0)
    live.db.message("assistant", "Sent the release notes for the deploy.", sid="other", at=time.time() - 60)
    said = llm_saying(stale=True, why="the notes were sent in Release")
    now = time.time()
    run_held(live, said, now)
    [text] = said.calls
    assert "Sent the release notes" in text and "release notes still need sending" in text
    assert live.store.held_cards(12) == [] and live.store.done_marks([card_id])
    # Read again at most every few hours; Undo puts it back in the brief.
    run_held(live, said, now + 60)
    assert len(said.calls) == 1
    assert control.done(live.store, card_id, False) is True
    assert [card["id"] for card in live.store.held_cards(12)] == [card_id]
    run_held(live, llm_saying(stale=False, why="still stands"), now + followup.HELD_EVERY_S)
    assert [card["id"] for card in live.store.held_cards(12)] == [card_id]


def test_the_held_card_check_reads_the_note_to_pulse(live):
    item_id = promise(live)
    learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                      news="The deploy finished and the release notes still need sending.", why="",
                      chat_title="Deploy", now=time.time() - 3600)
    live.store.set("note", "Never bring up the gym.")
    seen = []

    def llm(instructions, text):
        seen.append(instructions)
        return {"stale": False, "why": "still stands"}, 7

    run_held(live, llm, time.time())
    [instructions] = seen
    assert instructions.startswith(followup.HELD_INSTRUCTIONS)
    assert instructions.endswith(f"\n\n{followup.report.NOTE_TITLE}\nNever bring up the gym.")


def test_the_rested_card_check_reads_the_note_to_pulse(live):
    from datetime import timezone
    live.store.set("memory", True)
    live.store.set("note", "Never bring up the gym.")
    item_id = promise(live)
    now = time.time()
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="push",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=now - 3600)
    assert control.dismiss(live.store, card_id, True, now=now - 3600, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    seen = []

    def llm(instructions, text):
        seen.append(instructions)
        return {"stale": False, "why": "still stands"}, 7

    followup.check_rested(live.store, live.home, llm, zone=timezone.utc, now=back_at + 1,
                          still_leader=lambda: True, budget=10_000, done={})
    [instructions] = seen
    assert instructions.startswith(followup.RESTED_INSTRUCTIONS)
    assert instructions.endswith(f"\n\n{followup.report.NOTE_TITLE}\nNever bring up the gym.")
    assert live.store.card(card_id)["dismissed_at"] is None


def test_a_report_card_carries_its_reason_and_one_written_without_a_list_has_none(live):
    from types import SimpleNamespace
    turn = SimpleNamespace(session_id=SID, reply_id=7)
    report = followup.report
    [with_why, without] = learning.add_noticed_cards(
        live.store, turn=turn, lines=[("date", "Dentist Friday at 9", None), ("fact", "Ana is in town", None)],
        chat_id=SID, chat_title="Pulse", shown=set(), kinds=report.KINDS, item_chat_title=lambda item: "",
        now=time.time(), whys=["Your calendar has it for Friday and nothing else that morning."])
    assert live.store.card(with_why)["why"] == "Your calendar has it for Friday and nothing else that morning."
    assert live.store.card(without)["why"] == ""
    # A report the turn wrote itself (no candidate list behind it) gives its cards no reason.
    [plain] = learning.add_noticed_cards(
        live.store, turn=turn, lines=[("date", "Dentist Friday at 9", None)], chat_id=SID, chat_title="Pulse",
        shown=set(), kinds=report.KINDS, item_chat_title=lambda item: "", now=time.time() + 1)
    assert live.store.card(plain)["why"] == ""
    feed = {card["id"]: card for card in control.feed(live.store)["cards"]}
    assert feed[with_why]["why"] and feed[plain]["why"] == ""


@pytest.mark.parametrize("before", ["no why column", "no candidates table", "stopped after the column"])
def test_a_store_from_before_candidates_had_a_reason_gains_it_and_drops_the_stock_why(live, before):
    noticed, kept = found_card(live), promise(live)
    followup_card = learning.add_card(live.store, kind="followup", item=live.store.item(kept), level_name="push",
                                      news="The deploy is still waiting.", why="You said you would check it.",
                                      chat_title="Deploy", now=time.time())
    # A whole report's card and a brief's, as cards from before had them.
    whole = live.store.add_card({"kind": "report", "level": "screen", "news": "1. [date] Dentist Friday",
                                 "why": store._STOCK_REPORT_WHY, "created_at": time.time()})
    brief_why = "Your morning brief: what's still outstanding, and the news you asked Pulse to hold for the morning."
    brief_card = live.store.add_card({"kind": "brief", "level": "push", "news": "Good morning.", "why": brief_why,
                                      "created_at": time.time()})
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE cards SET why=? WHERE id=?", (store._STOCK_NOTICED_WHY, noticed))
        conn.execute("DELETE FROM settings WHERE key=?", (store.STOCK_WHY_CLEARED,))
        if before == "no why column":
            conn.execute("ALTER TABLE candidates DROP COLUMN why")
            assert "why" not in {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
        elif before == "no candidates table":  # a store from before candidate lists (0.66 to 0.68)
            conn.execute("DROP TABLE candidates")
        # "stopped after the column": the column is there, the stock line isn't cleared yet.
    reopened = store.PulseStore(live.store.path)
    with sqlite3.connect(live.store.path) as conn:
        assert "why" in {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert reopened.card(noticed)["why"] == "" and reopened.card(followup_card)["why"] == "You said you would check it."
    assert reopened.card(whole)["why"] == "" and reopened.card(brief_card)["why"] == brief_why
    # Cleared once: a stock line written after it (none ever is) stays, and opening again changes nothing.
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE cards SET why=? WHERE id=?", (store._STOCK_NOTICED_WHY, noticed))
    store.PulseStore(live.store.path)
    assert reopened.card(noticed)["why"] == store._STOCK_NOTICED_WHY
    assert reopened.card(followup_card)["why"] == "You said you would check it."


def test_a_held_card_check_follows_its_chat_into_the_continuation(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=time.time() - 3600)
    live.db.session(sid="deploy-2", title="Deploy", pinned=0, parent=CHAT)
    live.db.message("assistant", "Approval came through, deploy is live.", sid="deploy-2", at=time.time() - 60)
    said = llm_saying(stale=True, why="it went live")
    run_held(live, said, time.time())
    assert "Approval came through" in said.calls[0] and live.store.done_marks([card_id])


def test_a_held_card_check_reads_where_a_long_chat_got_it_done(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="The release notes for the deploy still need sending.", why="",
                                chat_title="Deploy", now=time.time() - 3600)
    live.db.message("assistant", "Sent the release notes for the deploy.", sid=CHAT, at=time.time() - 3000)
    for n in range(followup.MAX_MESSAGES):
        live.db.message("user" if n % 2 else "assistant", f"Step {n} of the migration is green.", sid=CHAT,
                        at=time.time() - 2000 + n)
    said = llm_saying(stale=True, why="the notes were sent")
    run_held(live, said, time.time())
    earlier = said.calls[0].split("## Earlier lines from its chat on the same subject\n", 1)[1]
    assert "Sent the release notes for the deploy." in earlier and live.store.done_marks([card_id])


def test_a_held_card_check_reads_the_scheduled_jobs(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="Remember to check the backup tonight.", why="", chat_title="Deploy",
                                now=time.time() - 3600)
    (live.home / "cron").mkdir(exist_ok=True)
    (live.home / "cron" / "jobs.json").write_text(json.dumps(
        [{"id": "b", "name": "Backup check", "schedule_display": "every day at 22:00", "prompt": "Check the backup"}]))
    said = llm_saying(stale=True, why="a job checks it")
    run_held(live, said, time.time())
    assert "Backup check" in said.calls[0] and live.store.done_marks([card_id])


def test_a_held_check_with_nothing_held_writes_nothing(live):
    run_held(live, llm_saying(stale=True, why=""), time.time())
    assert live.store.get(followup.HELD_CHECKED) in (None, {})


def test_a_failed_held_check_tries_again_next_run(live):
    item_id = promise(live)
    learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                      news="Waiting on the deploy.", why="", chat_title="Deploy", now=time.time() - 3600)

    def broken(instructions, text):
        raise RuntimeError("model down")
    now = time.time()
    assert run_held(live, broken, now)["failed"] == 1
    said = llm_saying(stale=False, why="still waiting")
    run_held(live, said, now + 60)
    assert said.calls == []  # not every run: after a while
    run_held(live, said, now + followup.HELD_RETRY_S)
    assert len(said.calls) == 1


def test_a_held_check_leaves_the_cards_of_a_brief_on_its_way(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="Waiting on the deploy.", why="", chat_title="Deploy", now=time.time() - 3600)
    live.store.set(followup.BRIEF_PENDING, {"item_id": "x-brief", "ids": [card_id]})
    said = llm_saying(stale=True, why="")
    run_held(live, said, time.time())
    assert said.calls == []


def test_a_card_marked_done_while_its_brief_was_out_comes_back_with_undo(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="Waiting on the deploy.", why="", chat_title="Deploy", now=time.time())
    control.done(live.store, card_id, True)
    live.store.brief_cards([card_id], time.time(), "x-brief")
    control.done(live.store, card_id, False)
    assert [card["id"] for card in live.store.held_cards(12)] == [card_id]


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


def test_not_now_takes_a_card_off_the_list_and_learns_nothing(live):
    card = told_card(live)
    assert control.dismiss(live.store, card["id"], True, now=100.0) is True
    assert control.dismiss(live.store, card["id"], True, now=200.0) is False  # once
    gone = live.store.card(card["id"])
    assert gone["dismissed_at"] == 100.0 and gone["seen_at"] is not None and gone["rating"] == ""
    assert live.store.lessons("followup", learning.LEVEL) == [] and learning.level(live.store, "followup") == "push"
    assert control.dismiss(live.store, card["id"], False) is True
    assert live.store.card(card["id"])["dismissed_at"] is None
    for card_id, value, field in ((card["id"], None, "dismissed"), (999999, True, "card"), (True, True, "card")):
        with pytest.raises(control.Refused) as refused:
            control.dismiss(live.store, card_id, value)
        assert refused.value.data == {"field": field}


def test_a_held_card_swiped_away_stays_out_of_the_brief(live):
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    [held] = live.store.held_cards(10)
    control.dismiss(live.store, held["id"], True)
    assert live.store.held_cards(10) == [] and live.store.held_count() == 0
    brief_due_now(live)
    pulse.tick()
    [text] = live.sent()
    assert pulse.brief.HELD_TITLE not in text and "everything is green" not in text


def test_a_remembered_thing_swiped_away_rests_a_week(live):
    item_id = promise(live)
    now = time.time()
    card_id = live.store.add_card({"kind": "noticed", "item_id": item_id, "level": "screen", "news": "x",
                                   "topic": "promise", "created_at": now})
    zone = pulse.report.timezone.utc
    assert f"#{item_id} " in pulse.report.remembered(live.store, zone, now)
    control.dismiss(live.store, card_id, True, now=now)
    assert f"#{item_id} " not in pulse.report.remembered(live.store, zone, now + 60)
    assert f"#{item_id} " in pulse.report.remembered(live.store, zone, now + pulse.report.DISMISS_REST_S + 60)


def test_not_now_until_a_time_is_when_the_card_comes_back(live):
    from datetime import datetime, timezone
    utc = timezone.utc
    live.store.set("time_zone", "UTC")
    live.store.set("brief_time", None)  # the fixture put the brief off the clock; here it's the default 07:00
    tuesday = datetime(2026, 10, 27, 10, 0, tzinfo=utc).timestamp()
    at = lambda day, hour, minute=0: datetime(2026, 10, day, hour, minute, tzinfo=utc).timestamp()
    rest = lambda choice, now: followup.rest_time(choice, now, utc, live.store)
    # Later today: in three hours, by 23:00; past 22:30 it's tomorrow.
    assert rest("later_today", tuesday) == at(27, 13)
    assert rest("later_today", at(27, 21)) == at(27, 23)
    assert rest("later_today", at(27, 22, 45)) == at(28, 6, 50)
    # Tomorrow and next week come back ten minutes before that morning's brief, in time for it.
    assert rest("tomorrow", tuesday) == at(28, 6, 50)
    assert rest("next_week", tuesday) == datetime(2026, 11, 3, 6, 50, tzinfo=utc).timestamp()
    live.store.set("brief_time", "06:30")
    assert rest("tomorrow", at(27, 23, 30)) == at(28, 6, 20)
    # A brief minutes after midnight, asked for just before it: the day after, as later today rolls.
    live.store.set("brief_time", "00:05")
    assert rest("tomorrow", at(27, 23, 20)) == at(27, 23, 55)
    assert rest("tomorrow", at(27, 23, 58)) == at(28, 23, 55)
    assert rest("later_today", at(27, 23, 58)) == at(28, 23, 55)
    assert rest("next_week", at(27, 23, 58)) == datetime(2026, 11, 2, 23, 55, tzinfo=utc).timestamp()
    with pytest.raises(ValueError):
        rest("never", tuesday)


def test_not_now_until_tomorrow_rests_the_thing_until_then_and_brings_the_card_back(live):
    from datetime import timezone
    item_id = promise(live)
    now = time.time()
    card_id = live.store.add_card({"kind": "noticed", "item_id": item_id, "level": "screen", "news": "x",
                                   "topic": "promise", "created_at": now})
    control.see(live.store, [card_id])
    zone = timezone.utc
    live.store.set("time_zone", "UTC")
    assert control.dismiss(live.store, card_id, True, now=now, until="tomorrow") is True
    back_at = followup.rest_time("tomorrow", now, zone, live.store)
    card = live.store.card(card_id)
    assert card["dismissed_at"] == now and card["rest_until"] == back_at
    # The thing rests while the card is away, not a week.
    assert f"#{item_id} " not in pulse.report.remembered(live.store, zone, back_at - 60)
    assert f"#{item_id} " not in pulse.report.remembered(live.store, zone, back_at + 60)
    # Not yet time: the tick leaves it away.
    assert followup.return_rested(live.store, back_at - 60) == []
    assert live.store.card(card_id)["dismissed_at"] == now
    # Its time, memory off (nothing reads chats): back as new, at the top, and the thing with it.
    assert followup.return_rested(live.store, back_at + 1) == [card_id]
    card = live.store.card(card_id)
    assert card["dismissed_at"] is None and card["rest_until"] is None and card["seen_at"] is None
    assert card["created_at"] == back_at + 1 and live.store.unseen_cards() == 1
    assert f"#{item_id} " in pulse.report.remembered(live.store, zone, back_at + 60)
    # Not a time Pulse knows, not a card, or with Put back: refused, refused, or ignored.
    with pytest.raises(control.Refused) as refused:
        control.dismiss(live.store, card_id, True, now=now, until="never")
    assert refused.value.data == {"field": "until"}
    with pytest.raises(control.Refused) as refused:
        control.dismiss(live.store, card_id + 50, True, now=now, until="tomorrow")
    assert refused.value.data == {"field": "card"}
    assert control.dismiss(live.store, card_id, True, now=now, until="next_week") is True
    assert control.dismiss(live.store, card_id, False, until="tomorrow") is True
    assert live.store.card(card_id)["rest_until"] is None


def test_a_card_about_nothing_remembered_comes_back_at_its_time_through_the_tick(live, capsys, monkeypatch):
    import sys
    import types
    from datetime import datetime
    card_id = found_card(live)
    then = time.time() - 8 * 86400  # swiped away a week ago, until the next day
    live.store.set("time_zone", "Asia/Tokyo")
    assert control.dismiss(live.store, card_id, True, now=then, until="tomorrow") is True
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    cli._pulse(types.SimpleNamespace(pulse_action="cards", value="", extra=""))
    back = datetime.fromtimestamp(live.store.card(card_id)["rest_until"], pulse.report.zone_for("Asia/Tokyo"))
    assert f"(put away, back {back.strftime('%a %H:%M')})" in capsys.readouterr().out  # the user's zone
    pulse.tick()
    card = live.store.card(card_id)
    assert card["dismissed_at"] is None and card["seen_at"] is None and card["created_at"] >= then + 86400
    cli._pulse(types.SimpleNamespace(pulse_action="cards", value="", extra=""))
    assert "put away" not in capsys.readouterr().out


def test_a_card_about_something_open_has_its_chat_read_before_it_comes_back(live):
    from datetime import timezone
    live.store.set("memory", True)
    item_id = promise(live)
    now = time.time()
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="push",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=now - 3600)
    assert control.dismiss(live.store, card_id, True, now=now - 3600, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    # With memory on the tick leaves it to the check.
    assert followup.return_rested(live.store, back_at + 1) == [] and live.store.card(card_id)["dismissed_at"] is not None
    live.db.message("assistant", "Approval came through, deploy is live.", sid=CHAT, at=now - 60)
    said = llm_saying(stale=False, why="still waiting")
    done = {}
    followup.check_rested(live.store, live.home, said, zone=timezone.utc, now=back_at + 1,
                          still_leader=lambda: True, budget=10_000, done=done)
    [text] = said.calls
    assert "deploy is live" in text and "waiting on approval" in text and done["checks"] == 1
    card = live.store.card(card_id)
    assert card["dismissed_at"] is None and card["seen_at"] is None and card["created_at"] == back_at + 1
    # Stale by then: done, seen in the chat, and Undo puts it back.
    assert control.dismiss(live.store, card_id, True, now=back_at + 2, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    stale = llm_saying(stale=True, why="the deploy is live")
    followup.check_rested(live.store, live.home, stale, zone=timezone.utc, now=back_at + 1,
                          still_leader=lambda: True, budget=10_000, done={})
    card = live.store.card(card_id)
    assert card["dismissed_at"] is None and card["rest_until"] is None
    assert live.store.done_marks([card_id])[card_id]["done_by"] == "pulse"
    assert live.store.item(item_id)["status"] == "done" and live.store.item(item_id)["closed_by"] == learning.RESTED_DONE_BY
    assert control.done(live.store, card_id, False) is True and live.store.item(item_id)["status"] == "open"
    # A check that fails lets it come back unchecked.
    assert control.dismiss(live.store, card_id, True, now=back_at + 2, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]

    def failing(instructions, text):
        raise RuntimeError("no model")

    done = {}
    followup.check_rested(live.store, live.home, failing, zone=timezone.utc, now=back_at + 1,
                          still_leader=lambda: True, budget=10_000, done=done)
    assert done["failed"] == 1 and live.store.card(card_id)["dismissed_at"] is None


def test_a_held_card_put_off_to_tomorrow_comes_back_for_that_brief(live, monkeypatch):
    from datetime import timezone
    brief_later(live)
    control.set_level(live.store, "followup", "brief")
    tell(live)
    pulse.tick()
    [held] = live.store.held_cards(10)
    now = time.time()
    control.dismiss(live.store, held["id"], True, now=now, until="tomorrow")
    assert live.store.held_cards(10) == []
    back_at = live.store.card(held["id"])["rest_until"]
    brief_at = pulse.report.slot_on(now, timezone.utc, live.store.get("brief_time"), 1)
    assert back_at == brief_at - followup.BRIEF_LEAD_S  # time for its chat to be read first
    # With memory on (the default), the tick leaves it to the memory run, which reads its chat
    # and holds it again for the brief.
    live.store.set("memory", True)
    monkeypatch.setattr(pulse, "_maybe_memory", lambda *args: None)  # the run itself, below, on this thread
    monkeypatch.setattr(pulse, "_maybe_probe", lambda *args: False)
    pulse.tick(back_at)
    assert live.store.card(held["id"])["dismissed_at"] is not None and live.sent() == []
    asked = []

    def memory_llm(instructions, text, purpose="pulse memory", route=None):
        asked.append(purpose)
        return {"stale": False, "why": "still open"}, 42

    monkeypatch.setattr(pulse, "_memory_llm", memory_llm)
    monkeypatch.setattr(pulse.memory, "run", lambda *args, **kwargs: None)  # memory itself read its chats
    monkeypatch.setattr(pulse.followup, "run", lambda *args, **kwargs: None)
    pulse._memory_run(live.home, "home", back_at + 1, threading.Event())
    assert asked == ["pulse follow-up"]  # the rested card's chat, on the follow-up budget
    assert [card["id"] for card in live.store.held_cards(10)] == [held["id"]]
    assert live.store.get("followup_today")["tokens"] == 42
    pulse.tick(brief_at + 1)
    [text] = live.sent()
    assert text.startswith("Pulse morning brief ") and "everything is green" in text


def test_a_rested_card_no_run_got_to_comes_back_unread_in_time_for_the_brief(live):
    live.store.set("memory", True)
    item_id = promise(live)
    now = time.time()
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="brief",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=now - 3600)
    assert control.dismiss(live.store, card_id, True, now=now, until="tomorrow") is True
    back_at = live.store.card(card_id)["rest_until"]
    # Left to the memory run while there's time for it; at the brief's time, back as it is.
    assert followup.return_rested(live.store, back_at + followup.BRIEF_LEAD_S - 1) == []
    assert followup.return_rested(live.store, back_at + followup.BRIEF_LEAD_S) == [card_id]
    assert [card["id"] for card in live.store.held_cards(10)] == [card_id]
    # And with the follow-up budget spent, the run brings it back unread rather than late.
    assert control.dismiss(live.store, card_id, True, now=now, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    said = llm_saying(stale=False, why="x")
    done = {"tokens": 5_000}
    followup.check_rested(live.store, live.home, said, zone=pulse.report.zone_for("UTC"), now=back_at + 1,
                          still_leader=lambda: True, budget=5_000, done=done)
    assert said.calls == [] and live.store.card(card_id)["dismissed_at"] is None


def test_a_rested_card_marked_done_meanwhile_ends_its_rest_without_a_read(live):
    live.store.set("memory", True)
    item_id = promise(live)
    now = time.time()
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="push",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=now - 3600)
    assert control.dismiss(live.store, card_id, True, now=now, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    assert control.done(live.store, card_id) is True
    said = llm_saying(stale=False, why="x")
    followup.check_rested(live.store, live.home, said, zone=pulse.report.zone_for("UTC"), now=back_at + 1,
                          still_leader=lambda: True, budget=10_000, done={})
    assert said.calls == [] and live.store.card(card_id)["dismissed_at"] is not None
    assert followup.return_rested(live.store, back_at + 1) == []  # not back as new: it's done
    card = live.store.card(card_id)
    assert card["dismissed_at"] is None and card["rest_until"] is None and card["seen_at"] is not None


def test_a_rested_thing_stays_out_of_reports_until_its_card_is_back(live):
    live.store.set("memory", True)
    live.store.set("time_zone", "UTC")
    item_id = promise(live)
    now = time.time()
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="push",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=now - 3600)
    zone = pulse.report.zone_for("UTC")
    assert control.dismiss(live.store, card_id, True, now=now, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    # Its time: the tick leaves the card to the memory run, and a report meanwhile leaves the thing
    # out, so a fresh card about it doesn't land beside the one coming back.
    assert followup.return_rested(live.store, back_at + 1) == []
    assert item_id in live.store.resting_items(back_at + 1, pulse.report.DISMISS_REST_S)
    assert f"#{item_id} " not in pulse.report.remembered(live.store, zone, back_at + 1)
    # Read and still open: back as new, and the thing with it.
    followup.check_rested(live.store, live.home, llm_saying(stale=False, why="still open"), zone=zone,
                          now=back_at + 2, still_leader=lambda: True, budget=10_000, done={})
    assert live.store.card(card_id)["dismissed_at"] is None
    assert item_id not in live.store.resting_items(back_at + 2, pulse.report.DISMISS_REST_S)
    assert f"#{item_id} " in pulse.report.remembered(live.store, zone, back_at + 2)


def test_a_rested_card_done_during_its_read_comes_back_as_the_user_left_it(live):
    live.store.set("memory", True)
    item_id = promise(live)
    now = time.time()
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="push",
                                news="The deploy is still waiting on approval.", why="", chat_title="Deploy",
                                now=now - 3600)
    control.see(live.store, [card_id])
    assert control.dismiss(live.store, card_id, True, now=now, until="later_today") is True
    back_at = live.store.card(card_id)["rest_until"]
    calls = []

    def llm(instructions, text):  # the user marks the card done while the model reads its chat
        calls.append(text)
        assert control.done(live.store, card_id) is True
        return {"stale": False, "why": "still open"}, 42

    followup.check_rested(live.store, live.home, llm, zone=pulse.report.zone_for("UTC"), now=back_at + 1,
                          still_leader=lambda: True, budget=10_000, done={})
    card = live.store.card(card_id)
    assert len(calls) == 1 and card["dismissed_at"] is None and card["rest_until"] is None
    # Not as new: done, as they left it, seen and where it was.
    assert card["created_at"] == now - 3600 and card["seen_at"] is not None and live.store.done_marks([card_id])
    assert live.store.unseen_cards() == 0


def test_a_card_put_off_again_meanwhile_keeps_its_new_rest(live):
    card_id = found_card(live)
    now = time.time()
    assert control.dismiss(live.store, card_id, True, now=now, until="later_today") is True
    first = live.store.card(card_id)["rest_until"]
    assert control.dismiss(live.store, card_id, False) is True
    assert control.dismiss(live.store, card_id, True, now=now, until="tomorrow") is True
    second = live.store.card(card_id)["rest_until"]
    # A return or an end of rest read before the new Not now changes nothing.
    assert live.store.return_card(card_id, now, first) is False and live.store.end_rest(card_id, first) is False
    assert live.store.card(card_id)["rest_until"] == second
    assert live.store.return_card(card_id, now, second) is True


def test_a_store_from_before_briefs_and_not_now_gets_their_columns(tmp_path):
    path = tmp_path / store.DB_NAME
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item_id INTEGER, "
                     "level TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL DEFAULT '', "
                     "news TEXT NOT NULL DEFAULT '', why TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT '', "
                     "chat_id TEXT NOT NULL DEFAULT '', chat_title TEXT NOT NULL DEFAULT '', message_id INTEGER, "
                     "created_at REAL NOT NULL, seen_at REAL, briefed_at REAL, rating TEXT NOT NULL DEFAULT '', "
                     "reason TEXT NOT NULL DEFAULT '', rated_at REAL)")
        conn.execute("INSERT INTO cards(kind, level, created_at) VALUES('followup', 'screen', 1)")
    old = store.PulseStore(path)
    [card] = old.cards()
    assert card["topic"] == "" and card["brief"] == "" and card["dismissed_at"] is None
    assert old.dismiss_card(card["id"], True, 5.0) is True
