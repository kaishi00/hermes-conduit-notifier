"""Walking past (pulse_spots): designs/pulse-walking-past.md."""

import json
import time
from datetime import datetime, timezone

import pytest

from test_location import BASE, DEVICE, Phone, client, location  # noqa: F401
from test_location import api as location_api
from test_pulse import ROOT, _load, cli, control, store
from test_pulse_followup import followup, live  # noqa: F401
from test_pulse_dates import Model, ahead, at, look
from test_pulse_held import LEFT, LINE, LOOKED, UTC, holding, places, waits  # noqa: F401
from test_pulse_places import placed, shared

spots = _load("conduit_push.pulse_spots", ROOT / "pulse_spots.py")


def held_for(live, spot="a florist", now=LOOKED, **moment):
    """The look ahead's run, its model holding Sam's birthday for a kind of
    shop; the model and the card."""
    model = Model({"dates": [{"ref": "D1", "bring_up": True, "value": 5, "line": LINE,
                              "moment": {"spot": spot, **moment}}], "ahead": []})
    look(live, now, model)
    [card] = [card for card in live.store.moment_cards() or live.store.cards() if card["news"] == LINE]
    return model, card


def entry(watch, name="Bloom & Co", at_=None, **changes):
    value = {"id": f"s_{name[:4].lower()}", "spot_id": watch, "name": name, "at": at_, "fix_at": None,
             "distance_m": 40.0, "accuracy_m": 8.0, "speed_mps": 1.0, "radius_m": 100.0}
    value.update(changes)
    return value


# Held by the look ahead -------------------------------------------------------------

def test_a_pick_held_for_a_kind_of_shop_waits_off_the_screen(holding):
    model, card = held_for(holding)
    [(instructions, _)] = model.asked
    assert ahead.SPOT_RULE.strip() in instructions
    assert card["level"] == places.MOMENT
    assert waits(card) == {"place_id": "spot:florist", "place": "a florist", "kind": "spot", "spot": "florist",
                           "intent": "opportunity", "on": "arrive", "until": at(28), "private": True}
    assert ("(held: Pulse brings it up when the user next passes near a florist)"
            in ahead.candidates.recent_section(holding.store, UTC, LOOKED + 86400))
    [card] = holding.store.moment_cards()
    assert places.held_line(card, UTC).endswith("(near a florist, until Wed 28 Oct 00:00)")


def test_the_look_ahead_offers_shops_only_while_the_phone_may_watch_them(holding):
    holding.store.set(spots.SETTING, False)
    model, card = held_for(holding)
    [(instructions, _)] = model.asked
    assert ahead.SPOT_RULE.strip() not in instructions
    assert card["level"] == "brief" and card["moment"] == ""


@pytest.mark.parametrize("said", ["Work", "", "a shop that sells very particular flowers in town", "1 2 3", 7])
def test_words_that_arent_a_kind_of_shop_hold_nothing(holding, said):
    _, card = held_for(holding, spot=said)
    assert card["level"] == "brief" and card["moment"] == ""


def test_search_words_lose_a_leading_article():
    assert spots.query("a florist") == "florist"
    assert spots.query("The Card Shop") == "Card Shop"
    assert spots.query("Costco") == "Costco"
    assert spots.words("  a  pharmacy ") == "a pharmacy"


@pytest.mark.parametrize("said, kept", [("errand", "errand"), ("arrival", "arrival"), ("soon", "opportunity")])
def test_the_look_ahead_may_give_a_pick_another_intent(holding, said, kept):
    model, card = held_for(holding, intent=said)
    [(instructions, _)] = model.asked
    assert '"errand" when it only makes sense at that shop' in instructions
    assert waits(card)["intent"] == kept and "radius_m" not in waits(card)
    [watch] = spots.reply([holding.home], LOOKED + 60)["spots"]
    assert (watch["intent"], watch["radius"]) == (kept, spots.INTENTS[kept])


@pytest.mark.parametrize("wanted, said, kept", [
    ("opportunity", 1500, 1500.0),
    ("opportunity", 20000, 5000.0),  # kept within the intent's bounds
    ("opportunity", 100, 500.0),
    ("errand", 250, 250.0),
    ("errand", 1000, 300.0),
])
def test_a_pick_may_size_its_own_circle(holding, wanted, said, kept):
    _, card = held_for(holding, intent=wanted, radius_m=said)
    assert waits(card)["radius_m"] == kept
    [watch] = spots.reply([holding.home], LOOKED + 60)["spots"]
    assert watch["radius"] == {"walk": kept, "drive": kept}


@pytest.mark.parametrize("said", ["big", True, None, float("nan")])
def test_a_circle_that_isnt_a_number_is_the_intents(holding, said):
    _, card = held_for(holding, radius_m=said)
    assert "radius_m" not in waits(card)


# What the phone watches -------------------------------------------------------------

def test_watches_list_held_picks_and_places_said_that_arent_saved(holding):
    _, card = held_for(holding)
    now = LOOKED + 60
    costco = placed(holding, "the Costco", said_at=now - 60)
    placed(holding, "Work", said_at=now - 60)  # a saved place: it waits for that
    placed(holding, "home", said_at=now - 60)  # only a saved place can be home
    placed(holding, "the Costco", on="leave", said_at=now - 60)  # leaving a shop isn't watched
    watching = spots.watches(holding.store, holding.home, now)
    assert watching == [{"id": f"c{card['id']}", "query": "florist", "until": at(28), "intent": "opportunity",
                         "radius": {"walk": 750.0, "drive": 3000.0}},
                        {"id": f"i{costco}", "query": "Costco", "until": now + spots.ITEM_WATCH_S,
                         "intent": "errand", "radius": {"walk": 100.0, "drive": 100.0}}]


def test_nothing_is_watched_without_where_you_are_walking_past_or_pulse(holding):
    held_for(holding)
    now = LOOKED + 60
    assert spots.watches(holding.store, holding.home, now)
    holding.store.set(spots.SETTING, False)
    assert spots.watches(holding.store, holding.home, now) == []
    holding.store.set(spots.SETTING, True)
    holding.store.set(store.WHERE_SETTING, False)
    assert spots.watches(holding.store, holding.home, now) == []
    holding.store.set(store.WHERE_SETTING, True)
    holding.store.set("enabled", False)
    assert spots.watches(holding.store, holding.home, now) == []
    holding.store.set("enabled", True)
    shared(holding.home, background=False)
    assert spots.watches(holding.store, holding.home, now) == []


def test_the_reply_prefixes_each_watch_with_its_profile(holding):
    _, card = held_for(holding)
    other = holding.home / "profiles" / "elsewhere"  # no Pulse there: skipped
    body = spots.reply([other, holding.home], LOOKED + 60)
    assert body == {"spots": [{"id": f"1.c{card['id']}", "query": "florist",
                               "until": datetime.fromtimestamp(at(28), timezone.utc).isoformat(),
                               "intent": "opportunity", "radius": {"walk": 750.0, "drive": 3000.0}}]}


# Walking into one ---------------------------------------------------------------------

def test_walking_into_a_watched_shop_brings_up_the_pick(holding):
    _, card = held_for(holding)
    now = LEFT
    told = spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", at_=now - 30, fix_at=now - 20)], now)
    assert told
    places.take_moments(holding.store, holding.home, UTC, now)
    [row] = holding.store.moments()
    assert (row["place"], row["kind"], row["event"]) == ("Bloom & Co", "spot", "arrive")
    assert json.loads(row["outcome"])["pick"] == card["id"]
    wait = waits(holding.store.card(card["id"]))
    assert wait["at_place"] == "Bloom & Co" and "reached_at" in wait
    text = places.pick_text(holding.store.card(card["id"]), zone=UTC)("t")
    # The fix was 40 m from the shop, close enough for an arrival: "got to", never "walked into".
    assert "The user just got to Bloom & Co or right by it (a florist), at 17:59" in text
    assert places._held_why(card, wait, places.FIRED).endswith(
        "Pulse held this for when you passed near a florist, and you just got to Bloom & Co or right by it.")
    # Kept with its timing: 60 m inside at 1 m/s, and the fix came 10 s after iOS's time.
    [kept] = holding.store.spot_entries()
    assert (kept["name"], kept["query"], kept["depth_m"], kept["late_s"]) == ("Bloom & Co", "florist", 60.0, 70.0)
    assert spots.describe(kept, UTC) == ("- Thu 15 Oct 17:59: Bloom & Co (florist, opportunity), 60 m inside, "
                                         "about 70 s late (fix ±8 m)")
    # The same entry sent again adds nothing, and the pick isn't waiting any more.
    assert not spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", at_=now - 30)], now)
    assert spots.watches(holding.store, holding.home, now) == []


def test_walking_into_a_shop_said_for_something_makes_it_due(holding):
    holding.store.set("memory", True)
    now = time.time()
    costco = placed(holding, "Costco", said_at=now - 3600)
    [watch] = spots.watches(holding.store, holding.home, now)
    assert watch["id"] == f"i{costco}"
    assert spots.entered([holding.home], DEVICE, [entry(f"0.i{costco}", name="Costco Wholesale",
                                                        at_=now - 30)], now)
    places.take_moments(holding.store, holding.home, UTC, now)
    [row] = holding.store.moments()
    assert json.loads(row["outcome"])["due"] == [costco]


@pytest.mark.parametrize("wanted, fix, counts", [
    ("opportunity", {"distance_m": 280.0, "radius_m": 300.0}, True),
    ("opportunity", {"distance_m": None}, True),  # nearby is enough, a fix or not
    ("errand", {"distance_m": 90.0, "accuracy_m": 40.0}, True),
    ("errand", {"distance_m": 140.0}, False),  # iOS said inside, the fix didn't
    ("errand", {"distance_m": None}, False),
    ("errand", {"distance_m": 150.0, "radius_m": 300.0}, False),  # an errand's circle is its own
    ("arrival", {"distance_m": 60.0, "accuracy_m": 30.0}, True),
    ("arrival", {"distance_m": 80.0, "accuracy_m": 30.0}, False),  # the fix might be outside
    ("arrival", {"distance_m": 20.0, "accuracy_m": None}, False),
])
def test_how_close_each_intent_needs_the_user(wanted, fix, counts):
    assert spots.close_enough(entry("0.c1", at_=0, **fix), wanted) is counts


def test_an_errand_with_its_own_circle_counts_within_it():
    wider = entry("0.c1", at_=0, distance_m=250.0, radius_m=300.0)
    assert not spots.close_enough(wider, "errand")
    assert spots.close_enough(wider, "errand", own=300.0)
    assert not spots.close_enough({**wider, "radius_m": 200.0}, "errand", own=300.0)  # the phone watched less


def test_a_shop_only_nearby_isnt_enough_for_an_errand(holding):
    holding.store.set("memory", True)
    now = time.time()
    costco = placed(holding, "Costco", said_at=now - 3600)
    outside = entry(f"0.i{costco}", name="Costco Wholesale", at_=now - 30, distance_m=160.0)
    assert not spots.entered([holding.home], DEVICE, [outside], now)
    assert holding.store.new_moments() == []
    [kept] = holding.store.spot_entries()
    assert (kept["intent"], kept["confirmed"], kept["moment"]) == ("errand", 0, "")
    assert spots.describe(kept, UTC).endswith(", not close enough for an errand")
    # Still watched: the next time they're there counts.
    assert [watch["id"] for watch in spots.watches(holding.store, holding.home, now)] == [f"i{costco}"]


def test_a_moment_only_nearby_brings_up_and_makes_due_only_what_nearby_is_enough_for(holding):
    holding.store.set("memory", True)
    _, card = held_for(holding, intent="errand")
    now = LEFT
    costco = placed(holding, "florist", said_at=now - 3600)
    near = {"id": f"{DEVICE}/n1", "place_id": "spot:florist", "place": "florist", "kind": "spot", "on": "arrive",
            "at": now - 30, "keep": True, "reach": "opportunity"}
    places.arrived(holding.store, [near], now)
    places.take_moments(holding.store, holding.home, UTC, now)
    [row] = holding.store.moments()
    outcome = json.loads(row["outcome"])
    assert "pick" not in outcome and outcome.get("due", []) == []
    assert "reached_at" not in waits(holding.store.card(card["id"]))
    assert costco not in outcome.get("items", [])


def test_a_saved_place_and_a_shop_never_stand_for_each_other(holding):
    _, card = held_for(holding)
    now = LEFT
    # A shop that happens to be called "a florist" isn't the pick's wait matched by name...
    shop = {"id": f"{DEVICE}/x1", "place_id": "p_x", "place": "a florist", "kind": "other", "on": "arrive",
            "at": now - 60, "keep": True}
    places.arrived(holding.store, [shop], now)
    places.take_moments(holding.store, holding.home, UTC, now)
    assert "reached_at" not in waits(holding.store.card(card["id"]))


def test_an_entry_for_a_watch_thats_gone_or_another_profile_counts_for_nothing(holding):
    _, card = held_for(holding)
    now = LEFT
    assert not spots.entered([holding.home], DEVICE, [entry("0.c999", at_=now - 30)], now)
    assert not spots.entered([holding.home], DEVICE, [entry(f"3.c{card['id']}", at_=now - 30)], now)
    assert not spots.entered([holding.home], DEVICE, [entry("0.x1", at_=now - 30)], now)
    holding.store.set(store.WHERE_SETTING, False)
    assert not spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", at_=now - 30)], now)
    assert holding.store.spot_entries() == []


@pytest.mark.parametrize("found, expected", [
    ({"distance_m": 40, "radius_m": 100, "speed_mps": 2.0, "at": 0, "fix_at": 5}, (60.0, 35.0)),
    ({"distance_m": 40, "radius_m": 100, "speed_mps": 0.1, "at": 0}, (60.0, 42.9)),  # slower than walking: walking
    ({"distance_m": 140, "radius_m": 100, "speed_mps": None, "at": 0}, (0.0, 0.0)),  # the fix is outside
    ({"distance_m": None, "radius_m": 100, "at": 0}, (None, None)),
])
def test_how_late_ios_told_the_app(found, expected):
    assert spots.lateness(found) == expected


def test_turning_where_you_are_off_forgets_the_shops_walked_into(holding):
    _, card = held_for(holding)
    spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", at_=LEFT - 30)], LEFT)
    assert holding.store.spot_entries()
    control.update(holding.store, {"where": False})
    assert holding.store.spot_entries() == []


def test_shops_walked_into_are_kept_a_month(holding, monkeypatch):
    _, card = held_for(holding)
    spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", at_=LEFT - 30)], LEFT)
    later = LEFT + spots.KEEP_S + 1
    monkeypatch.setattr(spots, "_watched", lambda *_: ("florist", "opportunity", None))  # still waiting a month on
    spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", name="Petals", at_=later - 30)], later)
    assert [kept["name"] for kept in holding.store.spot_entries()] == ["Petals"]


# The location route -------------------------------------------------------------------

def _held_card(pulse_store, until):
    return pulse_store.add_card({
        "kind": "noticed", "level": store.MOMENT, "news": LINE, "title": LINE, "created_at": time.time(),
        "moment": json.dumps({"place_id": "spot:florist", "place": "a florist", "kind": "spot", "spot": "florist",
                              "on": "arrive", "until": until})})


def test_the_ingest_reply_lists_the_shops_and_takes_what_was_walked_into(client, monkeypatch):
    woken = []
    monkeypatch.setattr(location_api, "_wake_running_pulse", lambda: woken.append(True) or 1)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/location/key", json=phone.key_request()).json())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pulse_store = store.open_store(client.home)
    pulse_store.set("enabled", True)
    pulse_store.set(store.WHERE_SETTING, True)
    card = _held_card(pulse_store, time.time() + 3600)

    precise = {**phone.batch()["sharing"], "background_precision": "precise"}

    def send(**extra):
        batch = phone.batch(sent_at=now.isoformat(), **{"sharing": precise, **extra})
        response = client.post(f"{BASE}/location/ingest", json=phone.seal(batch))
        assert response.status_code == 200
        body = response.json()
        assert "spot_entries" not in body and "spots_room" not in body
        if "reply" not in body:
            return None
        return json.loads(location.open_sealed(phone.key, body["reply"]["nonce"], body["reply"]["ct"],
                                               location.spots_aad(phone.kid, DEVICE, batch["batch_id"])))

    # An app that doesn't watch shops gets no reply.
    assert send() is None
    first = send(spot_entries=[], spots_room=12)
    assert [(watch["id"], watch["query"]) for watch in first["spots"]] == [(f"0.c{card}", "florist")]
    assert pulse_store.get(spots.PHONE)["room"] == 12
    # An entry that doesn't parse is dropped, never the batch.
    walked = {"id": "s_1", "spot_id": f"0.c{card}", "name": "Bloom & Co", "at": now.isoformat(),
              "fix_at": now.isoformat(), "distance_m": 30, "accuracy_m": 9, "speed_mps": -1, "radius_m": 100}
    after = send(spot_entries=[walked, {"id": "s_2", "spot_id": "nope"}, "junk"])
    [moment] = pulse_store.new_moments()
    assert (moment["id"], moment["place"], moment["kind"]) == (f"{DEVICE}/s_1", "Bloom & Co", "spot")
    assert woken == [True]
    [kept] = pulse_store.spot_entries()
    assert (kept["speed_mps"], kept["depth_m"]) == (None, 70.0)
    assert after["spots"]  # still waiting: the moment brings it up on the next tick
    # Background sharing off, or less than Precise: nothing to watch, and the phone's entries count for nothing.
    off = send(sharing={**precise, "background": False}, spot_entries=[{**walked, "id": "s_3"}])
    rough = send(sharing={**precise, "background_precision": "area"},
                 spot_entries=[{**walked, "id": "s_4"}])
    assert off == rough == {"spots": []}
    assert len(pulse_store.spot_entries()) == 1


# The CLI and the control route --------------------------------------------------------

def test_pulse_spots_lists_what_is_watched_and_walked_into(holding, capsys, monkeypatch):
    _, card = held_for(holding)
    monkeypatch.setattr(spots.places, "reads", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli.time, "time", lambda: LEFT)
    spots.entered([holding.home], DEVICE, [entry(f"0.c{card['id']}", at_=LEFT - 30)], LEFT)
    holding.store.set(spots.PHONE, {"room": 14, "at": LEFT})
    assert cli._pulse_spots(holding.store, holding.home, "") == 0
    out = capsys.readouterr().out
    assert "Walking past: on\n" in out
    assert "The phone said it could watch 14 more places" in out
    assert "- florist (opportunity, 750 m walking, 3000 m driving, until " in out
    assert ("Came within a watched shop's circle (how late is an estimate from the phone's fix):\n"
            "- Thu 15 Oct 17:59: Bloom & Co (florist, opportunity)") in out
    assert cli._pulse_spots(holding.store, holding.home, "off") == 0 and not spots.on(holding.store)
    assert cli._pulse_spots(holding.store, holding.home, "on") == 0 and spots.on(holding.store)
    assert cli._pulse_spots(holding.store, holding.home, "maybe") == 2


def test_the_control_route_reads_and_sets_walking_past(holding):
    assert control.state(holding.store, holding.home)["spots"] is True
    control.update(holding.store, {"spots": False})
    assert control.state(holding.store, holding.home)["spots"] is False
    with pytest.raises(control.Refused):
        control.update(holding.store, {"spots": "yes"})

