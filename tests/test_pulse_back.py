"""A Do it that stalled comes back once (designs/pulse-what-comes-next.md,
"A Do it that stalled comes back once"): left unsent, or found not done by
its checks, the card comes back at the next steps' level, never twice."""

import sqlite3
import sys
import time
import types
from datetime import datetime, timezone

import pytest

from test_pulse import cli, control, pulse
from test_pulse_followup import CHAT, followup, live, llm_saying, promise  # noqa: F401
from test_pulse_learning import learning, run_acts, told_card

UTC = pulse.report.zone_for("UTC")


def left_unsent(live, card, acted_at=None):
    """Do it on ``card``, then nothing sent for a day: when the check gave up."""
    acted_at = time.time() if acted_at is None else acted_at
    control.acted(live.store, card["id"], now=acted_at)
    now = acted_at + followup.ACT_GIVE_UP_S + 1
    run_acts(live, llm_saying(done=True, why=""), now)
    return now


def not_done(live, card_id, acted_at):
    """Do it on a card, the chat answers, and every check finds it not done:
    when the last one ran."""
    control.acted(live.store, card_id, now=acted_at)
    live.db.message("assistant", "That failed, the server is down.", sid=CHAT, at=acted_at + 30)
    said = llm_saying(done=False, why="it failed")
    now = acted_at + followup.ACT_FIRST_S + followup.ACT_QUIET_S
    for again in followup.ACT_AGAIN_S:
        run_acts(live, said, now)
        assert live.store.card(card_id)["back"] == ""  # not while it's still being checked
        now += again
    run_acts(live, said, now)
    return now


def test_a_do_it_left_unsent_comes_back_once_held_for_the_brief(live, monkeypatch, capsys):
    card = told_card(live)
    # Its promise closed when Pulse told it, before Do it: that doesn't keep it away.
    assert live.store.current_item(card["item_id"]) is None
    control.see(live.store, [card["id"]])
    acted_at = time.time() + 1
    now = left_unsent(live, card, acted_at)
    back = live.store.card(card["id"])
    assert (back["back"], back["back_acted_at"], back["level"], back["briefed_at"], back["seen_at"],
            back["created_at"]) == ("unsent", acted_at, "brief", None, None, now)
    assert back["why"] == "You tapped Do it yesterday but didn't send it."
    assert (back["title"], back["news"]) == (card["title"], card["news"])  # with its words
    assert [held["id"] for held in live.store.held_cards(10)] == [card["id"]]
    assert live.store.act(card["id"])["outcome"] == "no answer"
    # Its chat was just read: the held check reads it again only HELD_EVERY_S on.
    assert live.store.get(followup.HELD_CHECKED) == {str(card["id"]): now}
    # The app says why in its own language.
    [entry] = [shown for shown in control.feed(live.store)["cards"] if shown["id"] == card["id"]]
    assert (entry["back"], entry["back_acted_at"], entry["why"]) == ("unsent", acted_at, back["why"])
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: live.home))
    assert cli._pulse(types.SimpleNamespace(pulse_action="cards", value="", extra="")) == 0
    assert "(back: Do it left unsent)" in capsys.readouterr().out
    # The brief says so.
    assert pulse.brief.line(live.store.card(card["id"])) == (
        '- The deploy finished and everything is green. (from the chat "Deploy") '
        "[came back: You tapped Do it yesterday but didn't send it.]")
    # Out in the brief, tapped again and left again: it stays where it is.
    live.store.brief_cards([card["id"]], now + 600, "brief-1")
    briefed = live.store.card(card["id"])
    left_unsent(live, briefed, now + 3600)
    assert live.store.card(card["id"]) == briefed and live.store.act(card["id"])["outcome"] == "no answer"


def test_a_do_it_found_not_done_comes_back_with_its_why(live):
    card = told_card(live)
    acted_at = time.time() + 1
    now = not_done(live, card["id"], acted_at)
    back = live.store.card(card["id"])
    assert live.store.act(card["id"])["outcome"] == "not done"
    assert (back["back"], back["back_acted_at"], back["level"], back["seen_at"]) == ("not_done", acted_at, "brief",
                                                                                     None)
    assert back["why"] == f"You tapped Do it {followup.tapped_words(acted_at, now, UTC)}, and it isn't done yet."
    assert live.store.done_marks([card["id"]]) == {}


@pytest.mark.parametrize("level", ["screen", "push"])
def test_with_next_steps_on_the_screen_it_comes_back_there_and_never_pushes(live, level):
    learning.set_level(live.store, "next", level, time.time())
    card = told_card(live)
    sent = len(live.sent())
    now = left_unsent(live, card)
    back = live.store.card(card["id"])
    assert (back["back"], back["level"], back["seen_at"], back["created_at"]) == ("unsent", "screen", None, now)
    assert control.feed(live.store)["cards"][0]["id"] == card["id"]  # at the top, as new
    assert live.store.held_cards(10) == [] and live.store.get(followup.HELD_CHECKED) is None
    pulse.tick(now)
    assert len(live.sent()) == sent  # no turn, so no push


def test_a_chat_that_kept_going_brings_nothing_back(live):
    card = told_card(live)
    acted_at = time.time()
    control.acted(live.store, card["id"], now=acted_at)
    now = acted_at + followup.ACT_GIVE_UP_S + 1
    live.db.message("user", "Let's do it.", sid=CHAT, at=acted_at + 5)
    live.db.message("assistant", "Still on step one.", sid=CHAT, at=now - 10)  # still going a moment ago
    run_acts(live, llm_saying(done=True, why=""), now)
    assert live.store.act(card["id"])["why"] == "the chat kept going"
    assert live.store.card(card["id"])["back"] == ""


@pytest.mark.parametrize("meanwhile", ["put off", "put off until tomorrow", "not useful", "marked done",
                                       "next steps off"])
def test_a_stalled_do_it_stays_away_once_the_user_moved_on(live, meanwhile):
    card = told_card(live)
    acted_at = time.time()
    control.acted(live.store, card["id"], now=acted_at)
    if meanwhile == "put off":
        control.dismiss(live.store, card["id"], True, now=acted_at + 60)
    elif meanwhile == "put off until tomorrow":
        control.dismiss(live.store, card["id"], True, now=acted_at + 60, until=followup.TOMORROW)
    elif meanwhile == "not useful":
        control.rate(live.store, card["id"], "down", "not_relevant", now=acted_at + 60)
    elif meanwhile == "marked done":
        control.done(live.store, card["id"])
    else:
        control.set_level(live.store, "next", "off", now=acted_at + 60)
    before = live.store.card(card["id"])
    run_acts(live, llm_saying(done=True, why=""), acted_at + followup.ACT_GIVE_UP_S + 1)
    assert live.store.card(card["id"]) == before


def test_a_stalled_do_it_stays_away_once_its_item_closed_since(live):
    item_id = promise(live)
    card_id = learning.add_card(live.store, kind="followup", item=live.store.item(item_id), level_name="screen",
                                news="Still waiting on the deploy.", why="", chat_title="Deploy", now=time.time())
    acted_at = time.time() - 60
    control.acted(live.store, card_id, now=acted_at)
    live.store.close_item(item_id, status="cancelled", closed_by="a chat called it off")
    run_acts(live, llm_saying(done=True, why=""), acted_at + followup.ACT_GIVE_UP_S + 1)
    assert live.store.card(card_id)["back"] == ""


def test_an_item_closed_since_counts_through_what_replaced_it(live):
    first, second = promise(live), promise(live, "Hermes will tell the user when the release ships.")
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE items SET superseded_at=50, superseded_by=? WHERE id=?", (second, first))
    assert not live.store.closed_since(first, 100.0)  # what replaced it is still open
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("UPDATE items SET status='done', closed_at=150 WHERE id=?", (second,))
    assert live.store.closed_since(first, 100.0) and live.store.closed_since(second, 150.0)
    assert not live.store.closed_since(first, 200.0)  # closed before then
    assert not live.store.closed_since(999, 0.0)


def test_when_do_it_was_tapped_in_words():
    tuesday = datetime(2026, 10, 27, 9, tzinfo=timezone.utc).timestamp()  # Tuesday 27 October 2026
    assert followup.tapped_words(tuesday, tuesday + 3600, UTC) == "today"
    assert followup.tapped_words(tuesday, tuesday + 86400, UTC) == "yesterday"
    assert followup.tapped_words(tuesday, tuesday + 3 * 86400, UTC) == "on Tuesday"
    assert followup.tapped_words(tuesday, tuesday + 9 * 86400, UTC) == "on 27 October"
    # In the user's time zone: 23:30 on Tuesday in UTC is already Wednesday in Tokyo.
    late = datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc).timestamp()
    assert followup.tapped_words(late, late + 3600, pulse.report.zone_for("Asia/Tokyo")) == "today"
    assert followup.tapped_words(late, late + 3600, UTC) == "yesterday"


def test_a_store_from_before_come_backs_gets_the_columns(live):
    with sqlite3.connect(live.store.path) as conn:
        conn.execute("ALTER TABLE cards DROP COLUMN back_acted_at")
        conn.execute("ALTER TABLE cards DROP COLUMN back")
    reopened = pulse.store.PulseStore(live.store.path)
    card_id = reopened.add_card({"kind": "noticed", "level": "screen", "news": "x", "created_at": time.time()})
    assert (reopened.card(card_id)["back"], reopened.card(card_id)["back_acted_at"]) == ("", None)


def test_a_message_hermes_never_answered_was_sent_and_brings_nothing_back(live):
    card = told_card(live)
    acted_at = time.time() + 1
    control.acted(live.store, card["id"], now=acted_at)
    live.db.message("user", "Can you check the deploy?", sid=CHAT, at=acted_at + 30)
    run_acts(live, llm_saying(done=True, why=""), acted_at + followup.ACT_GIVE_UP_S + 1)
    assert live.store.card(card["id"])["back"] == ""
    assert (live.store.act(card["id"])["outcome"], live.store.act(card["id"])["why"]) == (
        "no answer", "no answer after Do it")


def test_the_brief_keeps_why_a_long_card_came_back():
    why = "You tapped Do it yesterday but didn't send it."
    line = pulse.brief.line({"news": "word " * 200, "chat_title": "", "back": "unsent", "why": why})
    assert line.endswith(f"[came back: {why}]") and len(line) <= pulse.brief.LINE_LIMIT + 2
