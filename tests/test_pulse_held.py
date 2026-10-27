"""The look ahead's picks held for a place (pulse_ahead, pulse_places):
designs/pulse-where-you-are.md, "Opportunities wait for their moment"."""

import json
import sys
import types

import pytest

from test_pulse import SID, ROOT, _load, cli, control, pulse, store
from test_pulse_followup import followup, live  # noqa: F401
from test_pulse_dates import SAM, Model, ahead, at, look, looking, picked
from test_pulse_places import moment, shared

places = _load("conduit_push.pulse_places", ROOT / "pulse_places.py")
candidates = _load("conduit_push.pulse_candidates", ROOT / "pulse_candidates.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")
tools = _load("conduit_push.pulse_tools", ROOT / "pulse_tools.py")
proposals = _load("conduit_push.pulse_proposals", ROOT / "pulse_proposals.py")
UTC = ahead.report.zone_for("UTC")

HOME = {"id": "p_home", "name": "Home", "kind": "home"}
WORK = {"id": "p_work", "name": "Work", "kind": "work"}
# Wednesday 14 October 2026, 06:30: Sam turns 40 in two weeks.
LOOKED = at(14, 6, 30)
# Thursday 15 October, 18:00: leaving work.
LEFT = at(15, 18)
LINE = "Sam turns 40 on Wednesday 28 October."


@pytest.fixture
def holding(live, monkeypatch):
    """Pulse with the look ahead, Where you are on and the phone sharing its
    location in the background, and Sam picked in Conduit; the phone's
    Focus and calendar never busy."""
    looking(live)
    live.store.set(store.WHERE_SETTING, True)
    picked(live.home, SAM)
    shared(live.home)
    monkeypatch.setattr(places.hours, "busy", lambda home, at: None)
    return live


def held(live, on="leave", place="Work", now=LOOKED):
    """The look ahead's run, its model holding Sam's birthday for a place;
    the model and the card."""
    model = Model({"dates": [{"ref": "D1", "bring_up": True, "value": 5, "line": LINE,
                              "moment": {"place": place, "on": on}}], "ahead": []})
    look(live, now, model)
    [card] = [card for card in live.store.moment_cards() or live.store.cards() if card["news"] == LINE]
    return model, card


def reach(live, place=WORK, on="leave", now=LEFT):
    """The user gets to (or leaves) ``place`` a minute before ``now``; what
    the moment did."""
    one = moment(place, on, at=now - 60)
    places.arrived(live.store, [one], now)
    places.take_moments(live.store, live.home, UTC, now)
    [row] = [row for row in live.store.moments() if row["id"] == one["id"]]
    return json.loads(row["outcome"])


def waits(card):
    return store.card_moment(card)


# Held by the look ahead ---------------------------------------------------------------

def test_the_look_ahead_offers_saved_places_only_with_where_you_are(live):
    looking(live)
    picked(live.home, SAM)
    assert ahead.held_places(live.store, live.home, LOOKED) == []  # Where you are is off
    live.store.set(store.WHERE_SETTING, True)
    assert ahead.held_places(live.store, live.home, LOOKED) == []  # nothing shared in the background
    shared(live.home, background=False)
    assert ahead.held_places(live.store, live.home, LOOKED) == []
    model, card = held(live)
    [(instructions, text)] = model.asked
    assert "may wait for a place" not in instructions and "## Saved places" not in text
    assert card["level"] == "brief" and card["moment"] == ""  # held for the brief, as before


def test_a_pick_held_for_a_place_waits_off_the_screen(holding):
    model, card = held(holding)
    [(instructions, text)] = model.asked
    assert ahead.MOMENT_RULE.strip() in instructions
    assert f"{ahead.PLACES_TITLE}\n- Home (home)\n- Work (work)" in text and "Clinic" not in text  # never a private one
    assert card["level"] == places.MOMENT and card["news"] == LINE
    assert waits(card) == {"place_id": "p_work", "place": "Work", "kind": "work", "on": "leave", "until": at(28)}
    # Off the screen: not listed, not counted, not seen, and kept.
    assert holding.store.cards() == [] and holding.store.unseen_cards() == 0
    assert control.feed(holding.store, holding.home)["cards"] == []
    holding.store.see_cards(None, LOOKED)
    assert holding.store.card(card["id"])["seen_at"] is None
    assert holding.store.found_cards(0) == []
    holding.store.prune_cards(0)
    assert holding.store.card(card["id"]) is not None
    assert followup.waiting_for_moment(holding.store) == [{**holding.store.card(card["id"]), "done": False}]
    last = holding.store.get(ahead.LAST)
    assert (last["picked"], last["held"], last["moments"]) == (1, False, 1)
    holding.store.set("memory", True)
    assert "1 brought up, 1 held for a place" in cli._ahead_line(holding.store, UTC)
    holding.store.set("memory", False)
    # The next run sees it held, so it isn't raised twice.
    assert ("Sam turns 40 on Wednesday 28 October. (held: Pulse brings it up when the user next leaves Work)"
            in candidates.recent_section(holding.store, UTC, LOOKED + 86400))


@pytest.mark.parametrize("place, on", [("Costco", "leave"), ("Clinic", "arrive"), ("Work", "pass")])
def test_a_place_pulse_didnt_offer_isnt_waited_for(holding, place, on):
    _, card = held(holding, on=on, place=place)
    assert card["level"] == "brief" and card["moment"] == ""  # the brief's, as before


def test_a_pick_waits_until_the_start_of_its_day_and_two_weeks_at_most():
    assert ahead.held_until({"due_at": at(28, 23, 59)}, UTC, LOOKED) == at(28)
    assert ahead.held_until({"due_at": None}, UTC, LOOKED) == LOOKED + ahead.DAYS * 86400
    assert ahead.held_until({"due_at": at(15, 23, 59)}, UTC, LOOKED) == at(15)
    assert ahead.held_until({"due_at": at(14, 23, 59)}, UTC, LOOKED) is None  # today: no time to wait


def test_at_most_max_held_wait_at_once(holding, monkeypatch):
    monkeypatch.setattr(places, "MAX_HELD", 0)
    _, card = held(holding)
    assert card["level"] == "brief" and holding.store.moment_cards() == []


def quieter(live, now, card=None):
    """"Too often" on a date card: dates come up on the screen at most."""
    card = card or live.store.card(live.store.add_cards([{"kind": learning.NOTICED, "topic": "date",
                                                          "level": "push", "news": "x", "created_at": now}])[0])
    learning.set_level(live.store, "date", "screen", now, cause=learning.TOO_OFTEN, card=card)


def test_a_kind_quieter_than_a_push_isnt_waited_for(holding):
    quieter(holding, LOOKED)
    _, card = held(holding)
    assert card["level"] == "screen" and card["moment"] == ""


# Brought up at its moment --------------------------------------------------------------

def test_leaving_the_place_brings_the_pick_up_in_a_turn_of_its_own(holding, monkeypatch):
    _, card = held(holding)
    assert reach(holding, HOME, "arrive") == {"due": []}  # another place: nothing
    assert reach(holding, WORK, "arrive") == {"due": []}  # getting there, not leaving
    assert reach(holding) == {"due": [], "pick": card["id"]}
    wait = waits(holding.store.card(card["id"]))
    assert (wait["reached_at"], wait["at"]) == (LEFT, LEFT - 60)
    assert places.next_pick(holding.store)["id"] == card["id"]
    pulse.tick(now=LEFT)
    [text] = holding.sent()
    assert text.startswith("Pulse noticed something ")
    assert "The user just left Work, at 17:59 on Thursday 15 October." in text
    assert "Pulse's daily look ahead held this for that moment on Wednesday 14 October:" in text
    assert LINE in text and "Why it came up: Sam (wife) is one of the people you picked" in text
    assert places.next_pick(holding.store) is None  # one on its way
    # Its turn may look things up, never the web, and it pushes as "Pulse noticed something".
    note = pulse.pre_llm_call(SID, "t1", text)
    assert note.startswith(places.PICK_LOOK_NOTE) and "web_search" not in note
    assert pulse.pre_tool_call(SID, "t1", "web_search", {"query": "florists"})["action"] == "block"
    assert pulse.pushes(SID, "t1")
    assert pulse.push_copy(SID, "t1", "Leaving work: Sam turns 40 on Wednesday.")["title_label"] == "pulse.noticed"
    # Its words are the card's once it's in.
    reply = "Leaving work: Sam turns 40 on Wednesday 28 October, and Bloom & Co on your way is open until 7."
    holding.db.message("user", text, display_kind="hidden")
    holding.db.message("assistant", reply)
    pulse.tick(now=LEFT + 60)
    shown = holding.store.card(card["id"])
    assert (shown["level"], shown["news"], shown["moment"], shown["seen_at"]) == ("push", reply, "", None)
    assert shown["created_at"] == LEFT + 60  # new, at the top
    assert shown["why"].endswith("Pulse held this for when you left Work, and you just did.")
    assert [row["id"] for row in holding.store.cards()] == [card["id"]]
    assert holding.store.moment_cards() == []


def test_a_held_pick_waits_for_the_next_time_while_pulse_may_not_speak(holding, monkeypatch):
    _, card = held(holding)
    holding.store.set("night", ["17:00", "19:00"])
    assert reach(holding) == {"due": [], "picks": [card["id"]], "picks_skipped": "night"}
    assert "reached_at" not in waits(holding.store.card(card["id"]))
    line = places.describe(holding.store.moments()[0], UTC, {}, {card["id"]: card["news"]})
    assert line.endswith(f"what the look ahead held for it waits for the next time, as it was night: {LINE}")
    holding.store.set("night", [])
    # "Wrong time" for its kind (a date) closes that part of the day to it.
    with monkeypatch.context() as patched:
        patched.setattr(places.learning, "closed_parts",
                        lambda pulse_store, kind: {"evening"} if kind == "date" else set())
        assert reach(holding, now=LEFT + 600)["picks_skipped"] == "part:evening"
    assert reach(holding, now=LEFT + 1200)["pick"] == card["id"]


def test_a_turn_that_doesnt_go_out_leaves_the_pick_waiting(holding):
    _, card = held(holding)
    reach(holding)
    pulse.tick(now=LEFT)
    [attempt] = holding.store.in_flight()
    assert attempt["kind"] == places.MOMENT
    holding.store.update_attempt(attempt["attempt_id"], state=store.REFUSED, detail="busy")
    pulse.tick(now=LEFT + 60)
    assert waits(holding.store.card(card["id"])) == {"place_id": "p_work", "place": "Work", "kind": "work",
                                                     "on": "leave", "until": at(28)}
    assert holding.store.card(card["id"])["level"] == places.MOMENT
    # Reached but never sent while the moment was fresh: it waits too.
    reach(holding, now=LEFT + 3600)
    assert places.settle_picks(holding.store, holding.home, LEFT + 3600 + places.FRESH_S + 1) == []
    assert "reached_at" not in waits(holding.store.card(card["id"]))


def test_a_pick_marked_done_while_its_turn_is_on_its_way_waits_for_the_turn(holding):
    _, card = held(holding)
    reach(holding)
    pulse.tick(now=LEFT)
    [attempt] = holding.store.in_flight()
    holding.store.mark_done(card["id"], card["item_id"], closed_by=learning.HELD_DONE_BY, now=LEFT, by="pulse")
    assert places.release_picks(holding.store, LEFT + 30) == []
    holding.store.update_attempt(attempt["attempt_id"], state=store.REFUSED, detail="busy")
    assert places.settle_picks(holding.store, holding.home, LEFT + 60) == []
    assert places.release_picks(holding.store, LEFT + 60) == [card["id"]]  # then where it was made, as seen
    assert holding.store.card(card["id"])["seen_at"] is not None


@pytest.mark.parametrize("how", ["ended", "off", "quieter"])
def test_a_held_pick_comes_on_the_screen_without_a_push_when_it_cant_wait(holding, how):
    _, card = held(holding)
    now = LOOKED + 3600
    if how == "ended":
        now = at(28)
    elif how == "off":
        control.update(holding.store, {"where": False})
    else:
        quieter(holding, now)
    assert places.release_picks(holding.store, now) == [card["id"]]
    shown = holding.store.card(card["id"])
    assert (shown["level"], shown["created_at"], shown["seen_at"], shown["moment"]) == ("screen", now, None, "")
    assert shown["why"].endswith({
        "ended": "Pulse held this for when you next left Work. That didn't happen in time, so it's here without a "
                 "notification.",
        "off": "Pulse held this for when you next left Work. Where you are was turned off, so it's here without a "
               "notification.",
        "quieter": "Pulse held this for when you next left Work. You've since asked for these more quietly, so it's "
                   "here without a notification."}[how])
    assert places.release_picks(holding.store, now) == []  # once


def test_a_held_pick_is_read_again_and_one_done_goes_where_it_was_made(holding):
    _, card = held(holding)
    asked, stale = [], [False]

    def llm(instructions, text):
        asked.append((instructions, text))
        return {"stale": stale[0], "why": "the chat says the gift is bought"}, 40

    def check(now):
        followup.check_held(holding.store, holding.home, llm, zone=UTC, now=now, still_leader=lambda: True,
                            budget=1e9, done={})

    check(LOOKED + 60)
    assert asked == []  # just made: read hours on
    now = LOOKED + followup.HELD_EVERY_S + 1
    check(now)
    [(instructions, text)] = asked
    assert "the next time they get to or leave a place" in instructions and f"The news: {LINE}" in text
    assert followup._times(holding.store.get(followup.HELD_CHECKED)) == {str(card["id"]): now}
    check(now + 60)
    assert len(asked) == 1  # not again for HELD_EVERY_S
    stale[0] = True
    now += followup.HELD_EVERY_S + 1
    check(now)
    assert followup.waiting_for_moment(holding.store) == []
    assert places.release_picks(holding.store, now) == [card["id"]]
    shown = holding.store.card(card["id"])
    assert (shown["level"], shown["created_at"], shown["seen_at"]) == ("screen", LOOKED, now)
    assert shown["why"] == card["why"]


# Commands and look-ups -----------------------------------------------------------------

def test_pulse_where_lists_what_the_look_ahead_held(holding, capsys, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: holding.home)
    _, card = held(holding)
    run = lambda value="": cli.dispatch(types.SimpleNamespace(conduit_push_action="pulse", pulse_action="where",
                                                              value=value))
    assert run() == 0
    out = capsys.readouterr().out
    assert f"Held by the look ahead for a place:\n- {LINE} (leaving Work, until Wed 28 Oct 00:00)" in out
    monkeypatch.setattr(cli.time, "time", lambda: LEFT)
    reach(holding)
    assert run() == 0
    out = capsys.readouterr().out
    assert "(leaving Work, until Wed 28 Oct 00:00, coming up now)" in out
    assert "Left Work: nothing waited for it" not in out
    assert f"Left Work: brought up what the look ahead held for it: {LINE}" in out
    assert run("off") == 0 and "comes to the Pulse screen without a notification" in capsys.readouterr().out


def test_pulse_where_reads_only_whole_ids_from_a_moments_outcome():
    moment = {"at": LEFT, "event": "leave", "place": "Work", "handled_at": LEFT,
              "outcome": json.dumps({"due": True, "items": "12", "related": [True, 7], "pick": False, "picks": 3})}
    assert places.describe(moment, UTC, {7: "Sam's party"}) == \
        "- Thu 15 Oct 18:00 Left Work: nothing waited for it; it would have brought up: Sam's party"
    assert cli._moment_items(moment) == [7] and cli._moment_picks(moment) == []


def test_a_held_picks_turn_looks_things_up_without_the_web():
    turn = tools.Turn(places.MOMENT)
    assert tools.check("conduit_calendar", {"action": "events"}, turn).allowed
    assert tools.check("web_search", {"query": "florists near work"}, turn).message == tools.SEARCH_BLOCK
    assert tools.check("conduit_calendar", {"action": "events"}, tools.Turn(pulse.PROBE)).message == tools.NO_TOOLS
    assert proposals.WHY[places.MOMENT] == "Pulse suggested this with something it held for a place."
