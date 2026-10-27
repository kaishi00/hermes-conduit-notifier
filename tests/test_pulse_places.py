"""Pulse knows where you are (pulse_places, designs/pulse-where-you-are.md):
the switch, arrivals and departures from the location store, places in
what memory keeps, and a reminder that waits for its place."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from test_location import BASE, DEVICE, NOW, PLACES, Phone, client, location, sharing  # noqa: F401
from test_location import _paired, api as location_api
from test_pulse import ROOT, SID, DAY, FakeModel, _load, _row, cli, control, memory, pulse, store
from test_pulse_followup import CHAT, check_now, followup, live, llm_saying, promise  # noqa: F401

places = _load("conduit_push.pulse_places", ROOT / "pulse_places.py")
hours = _load("conduit_push.pulse_hours", ROOT / "pulse_hours.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")

UTC = timezone.utc
COSTCO = {"id": "p_costco", "name": "Costco Wholesale", "kind": "other", "private": False}


def placed(live, words="Costco", on="arrive", *, text="The user will buy batteries.", line=None, said_at=None,
           kind="commitment", due="", due_at=None):
    """An item memory kept for a place, from the user's line (Hermes's for a promise); its id."""
    said_at = time.time() - 3600 if said_at is None else said_at
    line = line or f"Next time I'm at {words}, remind me to get batteries"
    message_id = live.db.message("user" if kind == "commitment" else "assistant", line, sid=CHAT, at=said_at)
    mark = live.store.watermark("", CHAT)
    [item_id] = live.store.advance("", CHAT, message_id=message_id, at=said_at,
                                   after_id=mark["message_id"] if mark else 0, items=[{
        "kind": kind, "text": text, "evidence": line, "explicit": 1, "due": due, "due_at": due_at,
        "message_id": message_id, "mentioned_at": said_at, "place_words": words, "place_on": on}])
    return item_id


_moments = iter(range(1, 1_000_000))


def moment(place=COSTCO, on="arrive", at=None, keep=True):
    """An arrival or departure as the location store passes it on."""
    return {"id": f"{DEVICE}/e{next(_moments)}", "place_id": place["id"], "place": place["name"],
            "kind": place["kind"], "on": on, "at": time.time() - 60 if at is None else at, "keep": keep}


@pytest.fixture
def where(live):
    """Pulse with Where you are, memory and follow-ups on."""
    live.store.set(store.WHERE_SETTING, True)
    live.store.set("memory", True)
    live.store.set("time_zone", "UTC")
    return live


def arrive(live, *moments, now=None):
    """The moments in, and taken as a tick takes them: the items made due."""
    now = time.time() if now is None else now
    places.arrived(live.store, list(moments), now)
    return places.take_moments(live.store, live.home, UTC, now)


def outcome(live, moment_id):
    [row] = [row for row in live.store.moments() if row["id"] == moment_id]
    return json.loads(row["outcome"])


def shared(home, **changes):
    """A phone sharing its location with this home (test_location's places)."""
    phone = Phone()
    found = location.LocationStore(home)
    phone.accept(found.register_key(phone.key_request()))
    found.ingest(phone.seal(phone.batch(sharing=sharing(**changes))))
    return found


# Words to saved places ----------------------------------------------------------

@pytest.mark.parametrize("words, place, matched", [
    ("Costco", COSTCO, True),
    ("the Costco on Main Street", {"name": "Costco", "kind": "other"}, True),
    ("my mum’s", {"name": "Mum's", "kind": "other"}, True),
    ("home", {"name": "Casa", "kind": "home"}, True),
    ("my place", {"name": "Casa", "kind": "home"}, True),
    ("the office", {"name": "Acme HQ", "kind": "work"}, True),
    ("work", {"name": "Work", "kind": "work"}, True),
    ("the gym", {"name": "Gym", "kind": "other"}, True),  # named just that
    ("home", {"name": "Home Depot", "kind": "other"}, False),  # a kind's words name that kind
    ("the office", {"name": "Office Depot", "kind": "other"}, False),
    ("her place", {"name": "Casa", "kind": "home"}, False),  # only the user's own
    ("work", {"name": "PureGym", "kind": "gym"}, False),
    ("Costco", {"name": "Trader Joe's", "kind": "other"}, False),
    ("", COSTCO, False),
    ("the", {"name": "The", "kind": "other"}, False),
])
def test_words_name_a_saved_place_by_its_name_or_kind(words, place, matched):
    assert places.matches(words, place) is matched


def test_a_private_place_is_never_named():
    found = {"places": {place["id"]: place for place in [*PLACES, COSTCO]}}
    assert [place["name"] for place in places.saved(found)] == ["Home", "Work", "Costco Wholesale"]
    assert places.place_for("the clinic", places.saved(found)) is None
    assert places.place_for("Costco", places.saved(found)) == "Costco Wholesale"
    assert places.saved(None) == []


# What the location store passes on ----------------------------------------------

def test_the_location_store_passes_on_fresh_moves_at_saved_places(tmp_path):
    found = location.LocationStore(tmp_path, clock=lambda: NOW)
    phone = Phone()
    phone.accept(found.register_key(phone.key_request()))
    home = {"id": "e1", "kind": "arrive", "place_id": "p_home", "at": "2026-10-13T06:55:00+01:00"}
    clinic = {"id": "e2", "kind": "leave", "place_id": "p_clinic", "at": "2026-10-13T06:56:00+01:00"}
    old = {"id": "e3", "kind": "leave", "place_id": "p_work", "at": "2026-10-13T06:30:00+01:00"}
    result = found.ingest(phone.seal(phone.batch(events=[home, clinic, old])))
    # Never a private place, nor a move too old to act on (the place rules' limit).
    assert result["moments"] == [{"id": f"{DEVICE}/e1", "place_id": "p_home", "place": "Home", "kind": "home",
                                  "on": "arrive", "at": NOW - 300, "keep": True}]
    # A batch sent again passes on nothing new.
    assert found.ingest(phone.seal(phone.batch(events=[home])))["moments"] == []
    # Paused: nothing.
    paused = sharing(paused_until="2026-10-13T12:00:00+01:00")
    left = {"id": "e4", "kind": "leave", "place_id": "p_home", "at": "2026-10-13T06:58:00+01:00"}
    assert found.ingest(phone.seal(phone.batch(sharing=paused, events=[left])))["moments"] == []
    # A phone that keeps no history: passed on, marked to be kept by nothing but its id.
    [kept] = found.ingest(phone.seal(phone.batch(sharing=sharing(retention_days=0),
                                                 events=[{**left, "id": "e5"}])))["moments"]
    assert kept["keep"] is False and kept["place"] == "Home"


def test_moments_are_kept_only_while_pulse_and_where_you_are_are_on(live):
    one = moment()
    assert not places.arrived(live.store, [one], time.time())  # Where you are is off by default
    assert live.store.new_moments() == []
    live.store.set(store.WHERE_SETTING, True)
    live.store.set("enabled", False)
    assert not places.arrived(live.store, [one], time.time())
    live.store.set("enabled", True)
    assert places.arrived(live.store, [one, {"on": "teleport"}, "x"], time.time())
    assert not places.arrived(live.store, [one], time.time())  # once
    [row] = live.store.new_moments()
    assert (row["id"], row["place"], row["event"]) == (one["id"], "Costco Wholesale", "arrive")


def test_a_moment_from_a_phone_that_keeps_no_history_keeps_only_its_id(where):
    one = moment(keep=False)
    arrive(where, one)
    assert where.store.moments() == []  # nothing to list
    assert not places.arrived(where.store, [one], time.time())  # still known, so not taken twice
    where.store.prune_moments(time.time() + 1)
    assert places.arrived(where.store, [one], time.time())  # a day on, gone


# Waiting for a place ------------------------------------------------------------

def test_an_item_for_a_place_waits_for_it_a_month_instead_of_a_schedule(where):
    now = time.time()
    undated = placed(where)
    dated = placed(where, "home", text="The user will call the plumber.", line="When I get home on Friday I'll "
                   "call the plumber", due="2026-11-06", due_at=now + 5 * 86400)
    plain = promise(where)
    followup.schedule(where.store, now)
    rows = {row["item_id"]: row for row in where.store.followups()}
    assert rows[undated]["decision"] == followup.PLACE
    assert rows[undated]["next_check_at"] == pytest.approx(where.store.item(undated)["valid_from"] + 30 * 86400)
    assert rows[dated]["next_check_at"] == pytest.approx(now + 5 * 86400 + followup.GIVE_UP_S)
    assert rows[plain]["decision"] == ""
    # `followups now` doesn't bring a place's check forward: its time is when the user gets there.
    where.store.set(followup.NOW_REQUESTED, now)
    followup.check_all_now(where.store, now)
    assert where.store.followup(undated)["next_check_at"] == rows[undated]["next_check_at"]


def test_a_repeat_that_names_a_place_makes_the_item_wait_for_it(where):
    now = time.time()
    item_id = placed(where, "", line="I need to buy batteries")
    followup.schedule(where.store, now)
    assert where.store.followup(item_id)["decision"] == ""  # on a schedule

    def again(line, words, on):
        message_id = where.db.message("user", line, sid=CHAT, at=now - 60)
        mark = where.store.watermark("", CHAT)
        assert where.store.advance("", CHAT, message_id=message_id, at=now - 60, after_id=mark["message_id"], items=[{
            "kind": "commitment", "text": "The user will buy batteries.", "evidence": line, "explicit": 1, "due": "",
            "due_at": None, "message_id": message_id, "mentioned_at": now - 60, "same_as": item_id,
            "place_words": words, "place_on": on}]) == []

    again("Next time I'm at Costco, remind me about the batteries", "Costco", "arrive")
    item = where.store.item(item_id)
    assert (item["place_words"], item["place_on"]) == ("Costco", "arrive")
    followup.schedule(where.store, now)
    row = where.store.followup(item_id)
    assert row["decision"] == followup.PLACE and row["next_check_at"] == pytest.approx(item["valid_from"] + 30 * 86400)
    # Said again with no place, it keeps its place; a moment makes it due.
    again("Don't forget the batteries", "", "")
    assert where.store.item(item_id)["place_words"] == "Costco"
    assert arrive(where, moment()) == [item_id]


def test_arriving_makes_what_waits_for_the_place_due_and_its_check_knows_it(where):
    item_id = placed(where)
    other = placed(where, "the pharmacy", text="The user will pick up the prescription.",
                   line="When I'm at the pharmacy I'll pick up the prescription")
    leaving = placed(where, "Costco", "leave", text="The user will check the receipt.",
                     line="When I leave Costco I'll check the receipt")
    followup.schedule(where.store, time.time())
    one = moment()
    assert arrive(where, one) == [item_id]
    assert outcome(where, one["id"]) == {"due": [item_id]}
    assert where.store.get("memory_requested") is True  # memory's next run checks it first
    now = time.time()
    assert followup.moment_due(where.store, now) == [item_id]
    seen = []

    def llm(instructions, text):
        seen.append((instructions, text))
        return {"decision": "tell", "news": "You're at Costco: batteries.", "why": "nothing shows it's done"}, 9

    done = {}
    followup.run(where.store, where.home, llm, zone=UTC, now=now, still_leader=lambda: True, budget=10_000, done=done,
                 only=followup.moment_due(where.store, now))
    [(instructions, text)] = seen
    assert followup.PLACE_INSTRUCTION in instructions
    assert "## Where the user is\nJust arrived at Costco Wholesale" in text and '("Costco")' in text
    row = where.store.followup(item_id)
    assert (row["decision"], row["news"]) == ("tell", "You're at Costco: batteries.")
    assert followup.followup_timing(row)["moment"] == {"place": "Costco Wholesale", "words": "Costco",
                                                       "on": "arrive", "at": one["at"]}
    assert done["checks"] == 1
    for waiting in (other, leaving):
        assert where.store.followup(waiting)["decision"] == followup.PLACE


def test_a_place_reminder_goes_at_once_whatever_the_days_count_or_the_hour(where):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    one = moment()
    arrive(where, one)
    now = time.time()
    per_day = learning.NUDGES["balanced"]["reminders"]
    where.store.set(followup.REMINDERS_TOLD, [now - 60] * per_day)
    check_now(where, llm_saying(decision="tell", news="You're at Costco: batteries.", why="not done"), now=now)
    row = where.store.followup(item_id)
    assert row["decision"] == "tell"  # not held for tomorrow's count
    assert len(where.store.get(followup.REMINDERS_TOLD)) == per_day + 1
    # The night set over this very hour: a place's reminder doesn't wait for the morning.
    clock = datetime.fromtimestamp(now, UTC)
    where.store.set("night", [(clock - timedelta(hours=1)).strftime("%H:%M"),
                              (clock + timedelta(hours=1)).strftime("%H:%M")])
    item, sent, attempt = followup.next_send(where.store, now + 60, where.home, zone=UTC)
    assert item["id"] == item_id and attempt == 1
    assert followup.held_note(followup.followup_timing(sent), "push") == \
        "You said this for Costco, and you just arrived."


def test_a_place_reminder_that_couldnt_go_while_there_shows_without_a_push(where):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    one = moment(at=time.time() - 120)
    arrive(where, one)
    check_now(where, llm_saying(decision="tell", news="You're at Costco: batteries.", why="not done"))
    assert followup.next_send(where.store, one["at"] + followup.MOMENT_FRESH_S + 1, where.home, zone=UTC) is None
    [card] = where.store.cards()
    assert card["level"] == "screen"
    assert card["why"].endswith("You said this for Costco, and you just arrived. Shown without a notification: it "
                                "couldn't go out while you were there.")
    assert where.store.item(item_id)["status"] == followup.REMINDED


def test_leaving_makes_due_only_what_was_said_for_leaving(where):
    item_id = placed(where, "work", "leave", text="The user will buy milk.", line="When I leave work I'll buy milk")
    followup.schedule(where.store, time.time())
    office = {"id": "p_work", "name": "Acme", "kind": "work"}
    assert arrive(where, moment(office)) == []
    assert arrive(where, moment(office, "leave")) == [item_id]
    timing = {"moment": {"place": "Acme", "words": "work", "on": "leave", "at": time.time()}}
    assert followup.held_note(timing, "push") == "You said this for leaving work, and you just left."


@pytest.mark.parametrize("why", ["night", "quiet", "event", "part", "followups_off", "brief", "level_off"])
def test_a_moment_makes_nothing_due_when_pulse_may_not_speak_up(where, monkeypatch, why):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    now = time.time()
    clock = datetime.fromtimestamp(now, UTC)
    if why == "night":
        where.store.set("night", [(clock - timedelta(hours=1)).strftime("%H:%M"),
                                  (clock + timedelta(hours=1)).strftime("%H:%M")])
    elif why == "quiet":
        where.store.set("quiet_days", [clock.weekday()])
    elif why == "event":
        monkeypatch.setattr(places.hours, "busy", lambda home, at: (hours.EVENT, at + 600))
    elif why == "part":
        monkeypatch.setattr(places.learning, "closed_parts",
                            lambda pulse_store, kind: {hours.part_of(pulse_store, UTC, now)})
    elif why == "followups_off":
        where.store.set("followups", False)
    else:
        control.set_level(where.store, "followup", "brief" if why == "brief" else "off")
    one = moment()
    assert arrive(where, one, now=now) == []
    expected = {"event": hours.EVENT, "part": f"part:{hours.part_of(where.store, UTC, now)}",
                "level_off": "followups_off"}.get(why, why)
    assert outcome(where, one["id"]) == {"skipped": expected, "items": [item_id]}
    # It waits for the next time the user gets there.
    assert where.store.followup(item_id)["decision"] == followup.PLACE
    assert not where.store.get("memory_requested")
    texts = {item_id: "The user will buy batteries."}
    line = places.describe(where.store.moments()[0], UTC, texts)
    assert line.endswith(f"(waiting: The user will buy batteries.)") and "Arrived at Costco Wholesale" in line


def test_driving_doesnt_hold_a_moment(where, monkeypatch):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    monkeypatch.setattr(places.hours, "busy", lambda home, at: (hours.DRIVING, None))
    assert arrive(where, moment()) == [item_id]


def test_a_moment_that_reaches_pulse_late_or_with_where_you_are_off_does_nothing(where):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    late = moment(at=time.time() - places.FRESH_S - 60)
    assert arrive(where, late) == [] and outcome(where, late["id"]) == {"skipped": "late"}
    one = moment()
    places.arrived(where.store, [one], time.time())
    where.store.set(store.WHERE_SETTING, False)
    assert places.take_moments(where.store, where.home, UTC, time.time()) == []
    assert where.store.followup(item_id)["decision"] == followup.PLACE


def test_a_place_that_never_comes_ends_the_wait_without_a_check(where):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    llm = llm_saying(decision="tell", news="x", why="y")
    check_now(where, llm, now=where.store.item(item_id)["valid_from"] + 30 * 86400 + 1)
    assert llm.calls == []
    item = where.store.item(item_id)
    assert item["status"] == "expired" and item["closed_by"] == "pulse follow-up: never at Costco"


@pytest.mark.parametrize("reply", [RuntimeError("model down"), "wait"])
def test_a_failed_or_waiting_check_waits_for_the_next_time_there(where, reply):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    arrive(where, moment())

    def llm(instructions, text):
        if isinstance(reply, Exception):
            raise reply
        return {"decision": "wait", "news": "", "why": "they said not this week"}, 5

    check_now(where, llm)
    row = where.store.followup(item_id)
    assert row["decision"] == followup.PLACE and "moment" not in followup.followup_timing(row)
    assert row["next_check_at"] == pytest.approx(where.store.item(item_id)["valid_from"] + 30 * 86400)
    assert followup.moment_due(where.store, time.time()) == []
    assert where.store.item(item_id)["status"] == "open"


def test_a_follow_up_already_on_its_way_isnt_made_due_again(where):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    arrive(where, moment())
    check_now(where, llm_saying(decision="tell", news="You're at Costco: batteries.", why="not done"))
    again = moment()
    assert arrive(where, again) == [] and outcome(where, again["id"]) == {"due": []}
    assert where.store.followup(item_id)["decision"] == "tell"


# The tick and the memory run ----------------------------------------------------

def test_a_tick_takes_moments_before_memory_starts(where, monkeypatch):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    order = []
    taken = places.take_moments
    monkeypatch.setattr(pulse.places, "take_moments", lambda *args: order.append("moments") or taken(*args))
    monkeypatch.setattr(pulse, "_maybe_memory", lambda *args: order.append("memory"))
    places.arrived(where.store, [moment()], time.time())
    pulse.tick()
    assert order == ["moments", "memory"]
    assert followup.moment_due(where.store, time.time()) == [item_id]


def test_the_memory_run_checks_moments_first_whatever_the_budget(where, monkeypatch):
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    arrive(where, moment())
    now = time.time()
    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    where.store.set("followup_today", {"day": day, "tokens": 10_000_000})  # the day's budget spent
    where.store.set("memory_today", {"day": day, "tokens": 10_000_000})
    asked, woken = [], []
    monkeypatch.setattr(pulse.memory, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(pulse, "_memory_llm", lambda instructions, text, purpose="", route=None: (
        asked.append(purpose),
        ({"decision": "tell", "news": "You're at Costco: batteries.", "why": "not done"}, 30))[1])
    monkeypatch.setattr(pulse, "_leads", lambda pulse_store: True)
    monkeypatch.setattr(pulse, "wake", lambda: woken.append(True))
    pulse._memory_run(where.home, SID, now, threading.Event())
    assert asked == ["pulse follow-up"] and woken == [True]
    assert where.store.followup(item_id)["decision"] == "tell"
    assert where.store.get("followup_today") == {"day": day, "tokens": 10_000_030}
    assert where.store.get("followup_usage")["checks"] == 1


# Memory --------------------------------------------------------------------------

def test_memory_keeps_a_place_only_when_its_words_are_on_the_items_line():
    chunk = [_row(1, "user", "Next time I'm at Costco I need to get batteries", DAY + 60),
             _row(2, "user", "When I leave work I'll buy milk", DAY + 70),
             _row(3, "user", "I'll call Sam", DAY + 80),
             _row(4, "user", "Sam lives near the station", DAY + 90),
             _row(5, "user", f"At {'x' * 61} I'll buy bread", DAY + 100)]
    item = lambda evidence, kind="commitment", **raw: {"kind": kind, "text": evidence, "evidence": evidence, **raw}
    items, _, _ = memory.extract(FakeModel({"items": [
        item("I need to get batteries", place="Costco"),
        item("I'll buy milk", place="work", place_on="leave"),
        item("I'll call Sam", place="the station"),  # said on another line: never guessed
        item("Sam lives near the station", "fact", place="the station"),  # only what can wait for a place
        item("I'll buy bread", place="x" * 61),  # too long to be a place's name
    ]}), chunk, UTC)
    assert [(i["place_words"], i["place_on"]) for i in items] == [
        ("Costco", "arrive"), ("work", "leave"), ("", ""), ("", ""), ("", "")]
    assert '"place"' in memory.INSTRUCTIONS and '"place_on"' in memory.INSTRUCTIONS
    rendered = memory.render_remembered([{"id": 7, "kind": "commitment", "text": "The user will buy milk.", "due": "",
                                          "place_words": "work", "place_on": "leave", "mentioned_at": DAY,
                                          "evidence": "e", "status": "open", "closed_at": None}], UTC)
    assert rendered.startswith("#7 commitment: The user will buy milk. (when leaving work) (said 2026-10-20")


# Settings, What Pulse remembers, and the command -----------------------------------

def test_where_you_are_is_a_setting_off_by_default(live):
    assert control.state(live.store, live.home)["where"] is False
    assert control.state(live.store, live.home)["where_ready"] is False  # nothing shared
    control.update(live.store, {"where": True})
    assert places.on(live.store) and control.state(live.store, live.home)["where"] is True
    shared(live.home)
    assert control.state(live.store, live.home)["where_ready"] is True
    places.arrived(live.store, [moment()], time.time())
    control.update(live.store, {"where": False})
    assert live.store.new_moments() == [] and not places.on(live.store)  # off keeps nothing of where they were
    with pytest.raises(control.Refused):
        control.update(live.store, {"where": "yes"})


def test_pulse_reads_location_only_with_where_you_are_and_background_sharing(live):
    now = time.time()
    assert not places.reads(live.store, now, live.home)
    live.store.set(store.WHERE_SETTING, True)
    assert not places.reads(live.store, now, live.home)  # nothing shared
    shared(live.home, background=False)
    assert not places.reads(live.store, now, live.home)
    shared(live.home)
    assert places.reads(live.store, now, live.home)
    shared(live.home, paused_until=(datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds"))
    assert not places.reads(live.store, now, live.home)


def test_what_pulse_remembers_shows_the_place_an_item_waits_for(where):
    work = placed(where, "the office", "leave", text="The user will buy milk.",
                  line="When I leave the office I'll buy milk")
    costco = placed(where)
    plain = promise(where)
    listed = {item["id"]: item for item in control.items(where.store, home=where.home)}
    assert listed[work]["place"] == {"words": "the office", "on": "leave", "saved": None}
    assert listed[plain]["place"] is None
    shared(where.home)
    listed = {item["id"]: item for item in control.items(where.store, home=where.home)}
    assert listed[work]["place"]["saved"] == "Work"
    assert listed[costco]["place"] == {"words": "Costco", "on": "arrive", "saved": None}


def test_the_where_command(where, capsys):
    run = lambda value="": cli._pulse_where(where.store, where.home, value)
    item_id = placed(where)
    followup.schedule(where.store, time.time())
    assert run() == 0
    out = capsys.readouterr().out
    assert "Where you are: on, but Conduit isn't sharing your location in the background" in out
    assert f"#{item_id} The user will buy batteries. (Costco: no saved place has that name, until " in out
    shared(where.home)
    arrive(where, moment({"id": "p_home", "name": "Home", "kind": "home"}))
    assert run() == 0
    out = capsys.readouterr().out
    assert "Where you are: on\n" in out and "Arrivals and departures in the last day:" in out
    assert "Arrived at Home: nothing waited for it" in out
    assert run("off") == 0 and not places.on(where.store) and where.store.moments() == []
    assert "no longer reads where you are" in capsys.readouterr().out
    assert run() == 0 and "Where you are: off" in capsys.readouterr().out
    assert run("on") == 0 and places.on(where.store)
    assert run("sometimes") == 2


def test_status_says_whether_pulse_reads_where_you_are(live):
    assert cli._where_line(live.store, live.home) == "Where you are: off (`hermes conduit-push pulse where on`)"
    live.store.set(store.WHERE_SETTING, True)
    shared(live.home)
    assert cli._where_line(live.store, live.home) == "Where you are: on"


# The location routes ----------------------------------------------------------------

def test_the_ingest_route_tells_pulse_and_a_wipe_forgets_what_it_kept(client, monkeypatch):
    woken = []
    monkeypatch.setattr(location_api, "_wake_running_pulse", lambda: woken.append(True) or 1)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/location/key", json=phone.key_request()).json())
    at = datetime.now(timezone.utc).replace(microsecond=0)
    batch = lambda event_id: phone.batch(sent_at=at.isoformat(), events=[
        {"id": event_id, "kind": "arrive", "place_id": "p_work", "at": at.isoformat()}])
    # Where Pulse never ran, it leaves no file behind.
    assert client.post(f"{BASE}/location/ingest", json=phone.seal(batch("e1"))).status_code == 200
    assert not (client.home / store.DB_NAME).exists() and woken == []
    pulse_store = store.open_store(client.home)
    pulse_store.set("enabled", True)
    pulse_store.set(store.WHERE_SETTING, True)
    response = client.post(f"{BASE}/location/ingest", json=phone.seal(batch("e2")))
    assert response.status_code == 200 and "moments" not in response.json()
    [row] = pulse_store.new_moments()
    assert (row["id"], row["place"], row["kind"], row["event"]) == (f"{DEVICE}/e2", "Work", "work", "arrive")
    assert woken == [True]
    assert client.post(f"{BASE}/location/wipe", json={"all": True}).status_code == 200
    assert pulse_store.new_moments() == []


def test_a_profile_that_reads_this_location_hears_its_arrivals_and_a_wipe_reaches_it(client, monkeypatch):
    # Location is shared with `work`, which shares it with this host's other
    # profiles: the default profile (client.home) reads it, so its Pulse hears it too.
    work = client.home / "profiles" / "work"
    _paired(client.home)
    _paired(work)
    _paired(client.home / "profiles" / "personal", "installation-2")  # another phone: not this location
    monkeypatch.setattr(location_api, "_calls_home", lambda: work)
    monkeypatch.setattr(location_api, "_wake_running_pulse", lambda: 1)
    stores = []
    for home in (work, client.home, client.home / "profiles" / "personal"):
        stores.append(store.open_store(home))
        stores[-1].set("enabled", True)
        stores[-1].set(store.WHERE_SETTING, True)
    phone = Phone()
    phone.accept(client.post(f"{BASE}/location/key", json=phone.key_request()).json())
    at = datetime.now(timezone.utc).replace(microsecond=0)
    arrive = {"id": "e1", "kind": "arrive", "place_id": "p_home", "at": at.isoformat()}
    batch = phone.batch(sent_at=at.isoformat(), sharing=sharing(other_profiles=True), events=[arrive])
    assert client.post(f"{BASE}/location/ingest", json=phone.seal(batch)).status_code == 200
    assert places.homes(work) == [work, client.home]
    assert [[row["place"] for row in found.new_moments()] for found in stores] == [["Home"], ["Home"], []]
    # Wiping another device leaves them; wiping this one deletes them wherever they reached.
    assert client.post(f"{BASE}/location/wipe", json={"device_id": "phone-installation-2"}).status_code == 200
    assert [len(found.new_moments()) for found in stores] == [1, 1, 0]
    assert client.post(f"{BASE}/location/wipe", json={"device_id": DEVICE}).status_code == 200
    assert [found.new_moments() for found in stores] == [[], [], []]
