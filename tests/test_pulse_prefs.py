"""What Pulse learns from what the user does with its cards (pulse_prefs)."""

import random

import pytest

from test_pulse import ROOT, _load

prefs = _load("conduit_push.pulse_prefs", ROOT / "pulse_prefs.py")

DAY = 86400.0
NOW = 500 * DAY


def signal(card, at, useful=0.0, *, what="thumb", weight=1.0, kind="interest", place="chat::x", band="undated",
           pushed=None):
    return {"card_id": card, "what": what, "at": at, "useful": useful, "weight": weight, "kind": kind,
            "place": place, "band": band, "pushed": pushed}


def history():
    """Someone who has used Pulse a while: four weeks of thumbs on other
    chats, four in five of them up."""
    kinds = ("promise", "commitment", "interest", "date", "follow-up")
    return [signal(1000 + n, NOW - (28 - n) * DAY, 0.0 if n % 5 == 0 else 1.0, kind=kinds[n % 5],
                   place=f"chat::other{n % 3}") for n in range(28)]


def liked():
    """A week of thumbs up on chat x, a while back."""
    return [signal(n, NOW - (20 - n) * DAY, 1.0) for n in range(1, 8)]


def downs(count, start=100):
    """Thumbs down on chat x, one a day up to now."""
    return [signal(start + n, NOW - (count - 1 - n) * DAY) for n in range(count)]


def surfaces(learned, value=3, *, kind="interest", place="chat::x", band="undated"):
    """Whether a pick with this value guess comes up."""
    return prefs.score(learned, prefs.attributes(kind, place, band), value=value) >= prefs.SURFACE_AT


def learn(rows):
    return prefs.learn(rows, NOW)


def test_with_nothing_learned_the_models_picks_come_up_as_before():
    attrs = prefs.attributes("interest", "chat::x", "undated")
    assert prefs.learn([], NOW) is prefs.EMPTY and prefs.EMPTY.score(attrs) == 0
    assert prefs.score(prefs.EMPTY, attrs) == prefs.PICK >= prefs.SURFACE_AT
    assert prefs.score(prefs.EMPTY, attrs, picked=False) < prefs.SURFACE_AT
    # A pick is pushed at the balanced nudge, as every report item was; a gentle one waits for what's due soon.
    assert prefs.level(prefs.PICK, "undated") == "push"
    assert prefs.level(prefs.PICK, "undated", nudge="gentle") == "screen"
    assert prefs.level(prefs.PICK, "soon", nudge="gentle") == "push"


def test_a_card_is_a_thumb_and_the_stronger_of_do_it_and_done():
    rows = [signal(1, NOW, 0.0), signal(1, NOW, 1.0, what="act", weight=prefs.ACT_DONE),
            signal(1, NOW, 1.0, what="done", weight=prefs.DONE_WEIGHT),
            signal(2, NOW, 1.0, what="act", weight=prefs.ACT_OPENED),
            signal(3, NOW, 1.0, what="act", weight=0.0)]
    assert sorted((vote.useful, vote.weight) for vote in prefs.votes(rows)) == [
        (0.0, 1.0), (prefs.ACT_OPENED, prefs.ACT_OPENED), (prefs.ACT_DONE, prefs.ACT_DONE)]


def test_one_thumbs_down_on_a_chat_is_mild_and_three_in_a_row_stop_its_routine_picks():
    for count in (1, 2, 3):
        assert surfaces(learn(history() + downs(count))) == (count < 3), count
    assert surfaces(learn(history() + downs(3)), 4)  # what the model rates higher still comes up
    # What's learned about one chat hardly touches another.
    assert surfaces(learn(history() + downs(3)), place="chat::y")


def test_a_change_of_heart_about_a_chat_shows_within_three_votes_and_a_thumbs_up_undoes_it():
    assert surfaces(learn(history() + liked() + downs(1)))  # one down on a chat they like changes little
    assert not surfaces(learn(history() + liked() + downs(3)))
    assert surfaces(learn(history() + liked() + downs(3) + [signal(200, NOW + 1, 1.0)]))


def test_the_last_votes_count_again_only_once_a_chat_has_older_ones():
    first = learn([signal(1, NOW - DAY), signal(2, NOW)])
    assert first.short.weights == first.long.weights  # nothing older to outweigh
    key = ("place", "chat::x")
    turned = learn([signal(3, NOW - 3 * DAY, 1.0), signal(4, NOW - 2 * DAY, 1.0), signal(1, NOW - DAY),
                    signal(2, NOW)])
    assert turned.short.weights[key] < turned.long.weights[key] < 0


def test_a_change_of_heart_fades_over_weeks_and_old_votes_go():
    weeks = 90 * DAY
    later = prefs.learn(history() + liked() + downs(3), NOW + weeks)
    assert surfaces(later)
    attrs = prefs.attributes("interest", "chat::x", "undated")
    assert learn([signal(1, NOW - DAY)]).score(attrs) < learn([signal(1, NOW - 120 * DAY)]).score(attrs) < 0
    assert learn([signal(1, NOW - prefs.KEEP_S - 1)]) is prefs.EMPTY


def test_a_kind_learns_across_chats():
    rows = history() + [signal(n, NOW - n * 3600, kind="watch", place=f"chat::c{n}") for n in range(1, 7)]
    learned = learn(rows)
    assert learned.score(prefs.attributes("watch", "chat::new", "undated")) < \
        learned.score(prefs.attributes("date", "chat::new", "undated"))


def test_the_model_is_a_function_of_the_signals_alone():
    rows = history() + liked() + downs(3)
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    attrs = prefs.attributes("interest", "chat::x", "undated")
    assert learn(shuffled).score(attrs) == pytest.approx(learn(rows).score(attrs))


def test_a_push_acted_on_within_the_hour_makes_its_kind_louder():
    pushed = NOW - 2 * 3600
    rows = [signal(1, pushed + 600, 1.0, what="act", weight=prefs.ACT_OPENED, pushed=pushed),
            signal(2, pushed + 2 * 3600, 1.0, what="done", weight=prefs.DONE_WEIGHT, pushed=pushed),  # too late
            signal(3, pushed + 60, 1.0, what="act", weight=prefs.ACT_OPENED, kind="date"),  # never pushed
            signal(4, pushed - 60, 1.0, what="act", weight=prefs.ACT_OPENED, kind="date", pushed=pushed)]  # before it
    learned = learn(rows)
    assert learned.loudness("interest") == pytest.approx(prefs.QUICK_STEP * 0.5 ** ((NOW - pushed - 600) / prefs.LONG_S))
    assert learned.loudness("date") == 0
    many = [signal(n, pushed + 60, 1.0, what="act", weight=prefs.ACT_OPENED, pushed=pushed) for n in range(10)]
    assert learn(many).loudness("interest") == prefs.QUICK_MAX


def test_what_scores_under_the_bar_stays_out_however_soon_and_a_floor_comes_up_anyway():
    assert prefs.level(0.0, "undated") == "off" and prefs.level(0.0, "undated", lowest="brief") == "brief"
    assert prefs.level(prefs.SURFACE_AT - 0.01, "overdue", loud=prefs.QUICK_MAX) == "off"
    assert prefs.level(0.0, "undated", floor=True) == "screen"
    assert prefs.level(1.0, "soon", floor=True) == "push"
    # A ceiling: what the user set is the loudest it gets.
    assert prefs.quieter("push", "screen") == "screen" and prefs.quieter("screen", "push") == "screen"
    assert prefs.quieter("brief", "off") == "off" and prefs.quieter("push", "unknown") == "push"


def test_bands_and_places():
    assert [prefs.band(at, NOW) for at in (None, NOW - 1, NOW + 3600, NOW + 12 * 3600, NOW + 2 * DAY,
                                          NOW + 5 * DAY)] == ["undated", "overdue", "soon", "today", "days", "later"]
    assert prefs.band(NOW + 12 * 3600, NOW, pace=0.25) == "soon"  # "too late": counted as due sooner
    assert [prefs.attributes("date", "", band)["timely"] for band in ("today", "days", "")] == ["yes", "no", ""]
    assert prefs.place("p", "c1") == "chat:p:c1" and prefs.place("", "", "work") == "area:work"
    assert prefs.place("", "", "", "calendar") == "source:calendar" and prefs.place("", "") == ""


def test_why_names_the_biggest_part_and_the_votes_behind_it():
    attrs = prefs.attributes("interest", "chat::x", "undated")
    factor = learn(history() + downs(3)).factor(attrs)
    assert (factor["scope"], factor["value"], factor["useful"], factor["votes"]) == ("place", "chat::x", 0.0, 3.0)
    assert factor["effect"] < 0
    assert prefs.factor_words(factor) == "less of things from that chat: useful 0 of 3 times"
    # A chat that follows its last votes says those, not the older ones it liked.
    changed = learn(history() + liked() + downs(3)).factor(attrs)
    assert (changed["scope"], changed["useful"], changed["votes"]) == ("place", 0.0, 3.0) and changed["effect"] < 0
    assert prefs.EMPTY.factor(attrs) is None
    assert prefs.factor_words({"scope": "kind", "value": "promise", "effect": 0.4, "useful": 5, "votes": 6}) == \
        "more of promise items: useful 5 of 6 times"
    # Timing too: thumbs down on what's due today, from all over, is about its timing.
    today = [signal(200 + n, NOW - n * DAY, band="today", kind=("promise", "date", "interest")[n % 3],
                    place=f"chat::t{n}") for n in range(6)]
    timely = learn(history() + today).factor(prefs.attributes("commitment", "chat::new", "today"))
    assert (timely["scope"], timely["value"], timely["votes"]) == ("timely", "yes", 6.0)
    assert prefs.factor_words(timely).startswith("less of things due within the day or overdue: useful")
