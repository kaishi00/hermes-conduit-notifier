"""Use my data (designs/cross-owner-agents, N8): in a room with other people's agents whose owner chose Use my data,
the agent can say when its owner is busy, from the calendar the phone shares with the profile, and nothing else from
it: no titles, places, calendars or people.
"""
import json

import pytest

from test_phone import NOW, Phone, _batch, _sharing, phone
from test_room_rules import ROOT, _load, plugin, room_choices, rules

free_busy = _load("conduit_push.room_free_busy", ROOT / "room_free_busy.py")

ROOM_SESSION = "room_" + "0123456789abcdef" * 2
TURN = rules.RoomTurn(room_id="zoo-trip", side="member", with_others=True, session_id=ROOM_SESSION)
DATA = room_choices.RoomChoices(tools="data")
KEYS = {"ok", "from", "to", "time_zone", "as_of", "busy", "window", "note"}


@pytest.fixture
def store(tmp_path):
    return phone.PhoneStore(tmp_path, clock=lambda: NOW)


def _share(store, **sharing):
    handset = Phone()
    handset.accept(store.register_key(handset.key_request()))
    store.ingest(handset.seal(_batch(sharing=_sharing(**sharing))))


def call(store, args, *, turn=TURN, choices=DATA, child=False, session=ROOM_SESSION):
    return json.loads(free_busy.handle(args, session_id=session, is_child=lambda _: child,
                                       room_turn=lambda _: turn, choices=lambda _: choices, store=store,
                                       now=lambda: NOW))


def test_busy_blocks_only_never_titles_or_places(store):
    _share(store)
    answer = call(store, {"from": "2026-10-14", "to": "2026-10-16"})
    assert answer["ok"] is True and set(answer) <= KEYS
    # Standup and review merge; lunch is free and the bank holiday all-day and free; the dentist is busy.
    assert answer["busy"] == [{"start": "2026-10-14T09:30:00+01:00", "end": "2026-10-14T11:00:00+01:00"},
                              {"start": "2026-10-16T15:00:00+01:00", "end": "2026-10-16T16:00:00+01:00"}]
    assert answer["time_zone"] == "Europe/London"
    text = json.dumps(answer)
    for private in ("Dentist", "Standup", "Design review", "High St", "Personal", "Family", "Sam", "iPhone",
                    "e_dentist", "c_personal"):
        assert private not in text, private
    assert "nothing else" in answer["note"]


def test_the_tool_refuses_outside_a_room_and_in_a_subagent(store):
    _share(store)
    assert call(store, {"from": "2026-10-14"}, turn=None)["error"] == "not_room"
    alone = rules.RoomTurn(room_id="ours", side="home", with_others=False, session_id=ROOM_SESSION)
    assert call(store, {"from": "2026-10-14"}, turn=alone)["error"] == "not_room"
    assert call(store, {"from": "2026-10-14"}, child=True)["error"] == "subagent"
    assert "busy" not in call(store, {"from": "2026-10-14"}, child=True)


@pytest.mark.parametrize("tools", ["talk", "look"])
def test_a_room_not_set_to_use_my_data_refuses(store, tools):
    _share(store)
    answer = call(store, {"from": "2026-10-14"}, choices=room_choices.RoomChoices(tools=tools))
    assert answer["error"] == "not_allowed" and "busy" not in answer
    assert answer["note"] == rules.TOOL_BLOCK.format(tool=free_busy.TOOL_NAME)
    picked = room_choices.RoomChoices(tools="custom", custom_tools=(free_busy.TOOL_NAME,))
    assert call(store, {"from": "2026-10-14"}, choices=picked)["ok"] is True


def test_use_my_data_allows_free_busy_without_a_pick():
    assert free_busy.TOOL_NAME in rules.allowed_tools(DATA)
    assert rules.tool_block(free_busy.TOOL_NAME, DATA) is None
    assert rules.tool_block("web_search", DATA) is None
    # Only room-safe data tools: picking another doesn't let it in.
    picked = room_choices.RoomChoices(tools="data", custom_tools=("conduit_calendar",))
    assert rules.tool_block("conduit_calendar", picked) and rules.tool_block(free_busy.TOOL_NAME, picked) is None
    for tools in ("talk", "look"):
        assert rules.tool_block(free_busy.TOOL_NAME, room_choices.RoomChoices(tools=tools))


def test_more_than_31_days_is_refused(store):
    _share(store)
    assert call(store, {"from": "2026-10-14", "to": "2026-11-13"})["ok"] is True
    answer = call(store, {"from": "2026-10-14", "to": "2026-11-14"})
    assert answer["error"] == "too_long" and "31" in answer["note"]
    assert call(store, {"from": "2026-10-16", "to": "2026-10-14"})["error"] == "malformed"


@pytest.mark.parametrize("calendar", [None, "off", "write_only"])
def test_no_shared_calendar_says_what_to_say(store, calendar):
    if calendar:
        _share(store, calendar=calendar)
    answer = call(store, {"from": "2026-10-14"})
    assert answer["error"] == "nothing_shared" and "busy" not in answer
    assert "can't check" in answer["note"]


def test_the_framing_says_busy_times_may_be_shared():
    framing = rules.framing(TURN, DATA)
    assert free_busy.TOOL_NAME in framing and rules.FREE_BUSY_LINE in framing
    assert rules.FREE_BUSY_LINE not in rules.framing(TURN, room_choices.RoomChoices(tools="look"))
    # Under Ask me first, busy times don't need an ask: the owner chose Use my data for them.
    asked = rules.framing(TURN, room_choices.RoomChoices(tools="data", share="ask"))
    assert rules.FREE_BUSY_LINE in asked and "without asking" in rules.FREE_BUSY_LINE


def test_the_tool_is_registered_in_conduit_rooms():
    registered = []

    class Context:
        def register_tool(self, **tool):
            registered.append(tool)

    free_busy.register(Context(), is_child=lambda _: False)
    assert [(tool["name"], tool["toolset"]) for tool in registered] == [(free_busy.TOOL_NAME, "conduit_rooms")]
    assert "busy" in free_busy.DESCRIPTION and "31 days" in free_busy.DESCRIPTION
    assert any(name == "room_free_busy" for name in dir(plugin))
