"""Ask me first (designs/cross-owner-agents, N7c): in a room with other people's agents, an agent whose owner chose
Ask me first asks its owner's phone before it shares something personal, waits a little for the answer, and gets a
later answer privately before its next turn in that room.

The ask is a confirm prompt in the profile's present store, shown only by a Conduit that says it can (room-asks).
"""
import json

import pytest

from test_present import IPAD, PHONE, Clock, PresentStore, store_module, _confirm, _load, ROOT
from test_room_rules import plugin, room_choices, rules

asks = _load("conduit_push.room_asks", ROOT / "room_asks.py")

ROOM_SESSION = "room_" + "0123456789abcdef" * 2
TURN = rules.RoomTurn(room_id="zoo-trip", side="member", with_others=True, session_id=ROOM_SESSION)
QUESTION = "Laifu asks whether you're free on the 22nd"
SHARE = "You're free all day on the 22nd."
ASK = {"question": QUESTION, "share": SHARE}


class Sleeper:
    """time.sleep and time.monotonic, with the phone answering while the tool waits."""

    def __init__(self):
        self.now = 0.0
        self.during = None   # called once the tool has slept this long, with the prompt's id

    def sleep(self, seconds):
        self.now += seconds
        if self.during is not None and self.now >= self.during[0]:
            action, self.during = self.during[1], None
            action()

    def monotonic(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return PresentStore(tmp_path, clock=clock)


@pytest.fixture
def sleeper():
    return Sleeper()


@pytest.fixture
def pushes():
    return []


def exchange(store, installation=PHONE, features=("room-asks",), results=()):
    return store.exchange({"installation_id": installation, "features": list(features), "results": list(results)})


def call(store, sleeper, pushes, args=ASK, *, session_id=ROOM_SESSION, turn=TURN, share="ask", tools="talk",
         is_child=lambda _: False):
    choices = room_choices.RoomChoices(share=share, tools=tools)

    def room_turn(session):
        return turn if session == ROOM_SESSION else None

    return json.loads(asks.handle(args, session_id=session_id, is_child=is_child, room_turn=room_turn,
                                  choices=lambda _turn: choices, room_name=lambda _room: "Zoo trip", store=store,
                                  push=pushes.append, sleep=sleeper.sleep, monotonic=sleeper.monotonic))


def answer_while_waiting(store, sleeper, outcome, after=5.0, **result):
    def answer():
        prompt = [p for p in exchange(store)["prompts"] if p.get("room")][-1]  # the newest
        exchange(store, results=[{"id": prompt["id"], "outcome": outcome, "via": "tap", **result}])

    sleeper.during = (after, answer)


def heard(store_for, session_id, did):
    """A turn that ended having replied (did) or not, its hooks in Hermes' order."""
    asks.replied(session_id, did)
    asks.ended(store_for, session_id, did)


def answered(store, room_id, session_id="any-session"):
    """What a turn in the room is framed with, heard as a turn that replies hears it (the plugin's path)."""
    lines = asks.frame(store, room_id, session_id)
    heard(lambda: store, session_id, True)
    return lines


# --- The ask ---------------------------------------------------------------------------------


def test_an_ask_shows_a_room_prompt_to_a_conduit_that_can(store, sleeper, pushes):
    exchange(store)
    answer = call(store, sleeper, pushes)
    assert answer["ok"] is True and answer["status"] == "waiting"
    [shown] = exchange(store)["prompts"]
    assert shown["title"] == QUESTION and shown["preview"] == SHARE and shown["action_label"] == "Share"
    assert shown["room"] == {"id": "zoo-trip", "name": "Zoo trip"} and shown["session_id"] == ROOM_SESSION
    assert shown["expires_at"] - shown["created_at"] == 24 * 3600
    [event] = pushes
    assert event["type"] == "phone.changes" and event["title"] == QUESTION
    assert event["body"] == "Zoo trip: " + SHARE and event["mode"] == "alert"


def test_an_older_conduit_neither_gets_nor_answers_a_room_ask(store, sleeper, pushes):
    exchange(store)
    exchange(store, IPAD, features=())
    answer = call(store, sleeper, pushes)
    assert exchange(store, IPAD, features=())["prompts"] == []
    exchange(store, IPAD, features=(), results=[{"id": answer["id"], "outcome": "confirmed", "via": "tap"}])
    assert store.get(answer["id"])["status"] == "waiting"


def test_no_conduit_that_can_is_refused_with_what_to_say(store, sleeper, pushes):
    exchange(store, features=())
    answer = call(store, sleeper, pushes)
    assert answer["ok"] is False and answer["error"] == "no_device"
    # No ask was kept, so no answer will come: never a promise to check.
    assert "check with your owner" not in answer["note"] and "can't share that" in answer["note"]
    assert pushes == [] and store.status() == []


def test_the_answer_within_the_wait_comes_back(store, sleeper, pushes):
    exchange(store)
    answer_while_waiting(store, sleeper, "confirmed")
    answer = call(store, sleeper, pushes)
    assert answer["status"] == "confirmed" and "share exactly" in answer["note"].lower()
    assert sleeper.now < asks.WAIT_S
    # Heard once its turn replies: the next turn isn't told again.
    heard(lambda: store, ROOM_SESSION, True)
    assert answered(store, "zoo-trip") == []


def test_say_instead_returns_what_the_owner_wrote(store, sleeper, pushes):
    exchange(store)
    answer_while_waiting(store, sleeper, "edit", edit_text="Only the afternoon of the 22nd.")
    answer = call(store, sleeper, pushes)
    assert answer["status"] == "edit" and answer["edit_text"] == "Only the afternoon of the 22nd."
    assert "only what your owner wrote" in answer["note"].lower()


def test_say_instead_with_no_text_reads_as_a_no(store, sleeper, pushes):
    exchange(store)
    answer_while_waiting(store, sleeper, "edit", edit_text="  ")
    answer = call(store, sleeper, pushes)
    assert answer["status"] == "declined" and "don't share" in answer["note"].lower()
    heard(lambda: store, ROOM_SESSION, True)
    later = call(store, sleeper, pushes, {"question": "Mona asks about Sunday", "share": "Sunday works."})
    exchange(store, results=[{"id": later["id"], "outcome": "edit", "via": "tap"}])
    assert answered(store, "zoo-trip") == ['Your owner said no to "Mona asks about Sunday": don\'t share it.']


def test_a_no_says_dont_share(store, sleeper, pushes):
    exchange(store)
    answer_while_waiting(store, sleeper, "declined")
    answer = call(store, sleeper, pushes)
    assert answer["status"] == "declined" and "don't share" in answer["note"].lower()


def test_an_unanswered_ask_says_to_check_later(store, sleeper, pushes):
    exchange(store)
    answer = call(store, sleeper, pushes)
    assert answer["status"] == "waiting" and sleeper.now == pytest.approx(asks.WAIT_S)
    assert "you'll check with your owner" in answer["note"] and "before your next turn" in answer["note"]


def test_a_later_answer_frames_the_next_turn_once(store, sleeper, pushes, clock):
    exchange(store)
    first = call(store, sleeper, pushes)
    second = call(store, sleeper, pushes, {"question": "Mona asks for your address", "share": "12 Elm Street"})
    third = call(store, sleeper, pushes, {"question": "Laifu asks about Sunday", "share": "Sunday works."})
    assert answered(store, "zoo-trip") == []
    exchange(store, results=[
        {"id": first["id"], "outcome": "confirmed", "via": "tap"},
        {"id": second["id"], "outcome": "declined", "via": "tap"},
        {"id": third["id"], "outcome": "edit", "via": "tap", "edit_text": "Say I'll know by Friday."}])
    lines = answered(store, "zoo-trip")
    assert lines == [
        f'Your owner said yes to "{QUESTION}": you may share this, and nothing more: {SHARE}',
        'Your owner said no to "Mona asks for your address": don\'t share it.',
        'Your owner answered "Laifu asks about Sunday" with: Say I\'ll know by Friday. Share only that.']
    assert answered(store, "zoo-trip") == []
    # Another room never gets them.
    assert answered(store, "other-room") == []


def test_a_compacted_room_session_still_hears_the_answer_and_asks_once(store, sleeper, pushes):
    # Hermes continues a compacted room session under a new id; the room is the same (room_rules follows it).
    exchange(store)
    first = call(store, sleeper, pushes)
    later = "room_" + "f" * 32
    continued = rules.RoomTurn(room_id="zoo-trip", side="member", with_others=True, session_id=later)
    choices = room_choices.RoomChoices()
    again = json.loads(asks.handle(ASK, session_id=later, room_turn=lambda _: continued, choices=lambda _: choices,
                                   room_name=lambda _: "Zoo trip", store=store, push=pushes.append,
                                   sleep=sleeper.sleep, monotonic=sleeper.monotonic))
    assert again["id"] == first["id"] and len(pushes) == 1
    exchange(store, results=[{"id": first["id"], "outcome": "declined", "via": "tap"}])
    assert answered(store, continued.room_id) == [f'Your owner said no to "{QUESTION}": don\'t share it.']


def test_a_rooms_asks_have_their_own_budget(store, sleeper, pushes, clock):
    # Other people steer a room's agent: its asks never use up the owner's own confirm prompts.
    exchange(store)
    for n in range(store_module.ROOM_ASKS_PER_HOUR):
        asked = call(store, sleeper, pushes, {"question": f"Question {n}", "share": "x"})
        exchange(store, results=[{"id": asked["id"], "outcome": "declined", "via": "tap"}])
    refused = call(store, sleeper, pushes, {"question": "One more", "share": "x"})
    assert refused["error"] == "rate_limited" and "don't share" in refused["note"].lower()
    assert len(pushes) == store_module.ROOM_ASKS_PER_HOUR
    for _ in range(store_module.PER_HOUR):
        _confirm(store)
    clock.now += 3600
    assert call(store, sleeper, pushes, {"question": "Next hour", "share": "x"})["ok"] is True


def test_an_expired_ask_frames_as_not_answered(store, sleeper, pushes, clock):
    exchange(store)
    call(store, sleeper, pushes)
    clock.now += 24 * 3600
    assert answered(store, "zoo-trip") == [
        f'Your owner didn\'t answer "{QUESTION}" in time: don\'t share it.']


def test_the_plugin_frames_a_room_turn_with_later_answers(store, sleeper, pushes, tmp_path, monkeypatch, clock):
    exchange(store)
    first = call(store, sleeper, pushes)
    exchange(store, results=[{"id": first["id"], "outcome": "declined", "via": "tap"}])
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "PresentStore", lambda home: PresentStore(home, clock=clock))
    monkeypatch.setattr(plugin.room_rules, "room_turn", lambda session, home=None: TURN)
    monkeypatch.setattr(plugin.room_rules, "choices_for", lambda turn, home: room_choices.RoomChoices())
    monkeypatch.setattr(plugin.room_invites, "people", lambda *a, **k: None)
    line = f'- Your owner said no to "{QUESTION}": don\'t share it.'
    assert line in plugin._room_framing(ROOM_SESSION).splitlines()
    # Heard only once a turn that read it answers: a failed turn (no reply) frames it again.
    plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="", turn_id="t1")
    assert line in plugin._room_framing(ROOM_SESSION).splitlines()
    plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="I can't share that.", turn_id="t2")
    assert 'said no' not in (plugin._room_framing(ROOM_SESSION) or "")


# --- Who may ask -----------------------------------------------------------------------------


def test_only_ask_me_first_lets_the_tool_through_even_in_talk_only():
    ask_first = room_choices.RoomChoices(share="ask", tools="talk")
    assert rules.tool_block(asks.TOOL_NAME, ask_first) is None
    assert rules.tool_block("web_search", ask_first) == rules.TALK_ONLY_BLOCK
    for share in ("helps", "nothing"):
        block = rules.tool_block(asks.TOOL_NAME, room_choices.RoomChoices(share=share, tools="look"))
        assert block == rules.ASK_BLOCK
    custom = room_choices.RoomChoices(share="helps", tools="custom", custom_tools=(asks.TOOL_NAME, "terminal"))
    assert rules.tool_block(asks.TOOL_NAME, custom) == rules.ASK_BLOCK
    assert asks.TOOL_NAME not in rules.allowed_tools(custom)
    # Tool Search reaches it when it's deferred, in a Talk only room too.
    assert rules.tool_block("tool_search", ask_first) is None
    assert rules.tool_block("tool_call", ask_first, {"name": asks.TOOL_NAME}) is None
    assert rules.tool_block("tool_call", ask_first, {"name": "web_search"}) == rules.TALK_ONLY_BLOCK
    # One whose tool can't be read stays blocked where no tool is allowed.
    for args in ({}, {"calls": "not json"}, {"calls": [{"tool": "web_search"}]}, None):
        assert rules.tool_block("tool_call", ask_first, args) == rules.TALK_ONLY_BLOCK
    assert rules.tool_block("tool_call", room_choices.RoomChoices(share="ask", tools="look"), {}) is None


def test_the_tool_refuses_outside_a_room_and_in_a_subagent(store, sleeper, pushes):
    exchange(store)
    assert call(store, sleeper, pushes, session_id="chat-1")["error"] == "not_room"
    assert call(store, sleeper, pushes, session_id="")["error"] == "not_room"
    alone = rules.RoomTurn(room_id="zoo-trip", side="home", with_others=False, session_id=ROOM_SESSION)
    assert call(store, sleeper, pushes, turn=alone)["error"] == "not_room"
    # A member's room session not titled with its room yet is still a room: never told to ask "the user" there.
    untitled = rules.RoomTurn(room_id="", side="member", with_others=True, session_id=ROOM_SESSION)
    refused = call(store, sleeper, pushes, turn=untitled)
    assert refused["error"] == "unavailable" and "can't share that" in refused["note"]
    assert "check with your owner" not in refused["note"]
    assert "ask the user" not in refused["note"]
    assert call(store, sleeper, pushes, is_child=lambda _: True)["error"] == "subagent"
    assert call(store, sleeper, pushes, share="helps")["error"] == "not_ask_first"
    assert pushes == [] and exchange(store)["prompts"] == []


def test_the_question_and_share_are_checked(store, sleeper, pushes):
    exchange(store)
    assert call(store, sleeper, pushes, {"share": SHARE})["error"] == "no_question"
    assert call(store, sleeper, pushes, {"question": QUESTION})["error"] == "no_share"
    assert call(store, sleeper, pushes, {"question": "x" * 81, "share": SHARE})["error"] == "too_long"
    assert call(store, sleeper, pushes, {"question": QUESTION, "share": "x" * 501})["error"] == "too_long"


def test_the_same_question_waiting_is_not_asked_twice(store, sleeper, pushes):
    exchange(store)
    first = call(store, sleeper, pushes)
    again = call(store, sleeper, pushes, {"question": QUESTION.upper(), "share": SHARE})
    assert again["id"] == first["id"] and again["status"] == "waiting"
    assert len(pushes) == 1 and len(exchange(store)["prompts"]) == 1


def test_a_yes_licenses_only_the_share_the_owner_saw(store, sleeper, pushes):
    # The same question with another share is another ask: a yes to the first never covers the second.
    exchange(store)
    first = call(store, sleeper, pushes)
    other = call(store, sleeper, pushes, {"question": QUESTION, "share": "You're free on the 22nd and 23rd."})
    assert other["id"] != first["id"] and len(pushes) == 2
    exchange(store, results=[{"id": first["id"], "outcome": "confirmed", "via": "tap"}])
    again = call(store, sleeper, pushes, {"question": QUESTION, "share": "You're free on the 22nd and 23rd."})
    assert again["id"] == other["id"] and again["status"] == "waiting"
    answer_while_waiting(store, sleeper, "confirmed")
    confirmed = call(store, sleeper, pushes, {"question": "Mona asks about lunch", "share": "Lunch works."})
    assert confirmed["status"] == "confirmed" and confirmed["share"] == "Lunch works."
    assert "share exactly the share you gave" in confirmed["note"].lower()


def test_room_asks_never_fill_the_owners_own_waiting_prompts(store, sleeper, pushes, clock):
    exchange(store)
    rooms = [rules.RoomTurn(room_id=f"room-{n}", side="member", with_others=True, session_id=f"room_{n:032x}")
             for n in range(8)]
    choices = room_choices.RoomChoices()
    for turn in rooms:
        for q in range(3):
            asks.handle({"question": f"Q{q}", "share": "x"}, session_id=turn.session_id,
                        room_turn=lambda _, turn=turn: turn, choices=lambda _: choices, room_name=lambda _: "R",
                        store=store, push=pushes.append, sleep=sleeper.sleep, monotonic=sleeper.monotonic)
        clock.now += 3600  # past every hourly cap
    mine = []
    for _ in range(store_module.MAX_WAITING):
        mine.append(_confirm(store, ttl_minutes=24 * 60)["id"])
        clock.now += 3600 / store_module.PER_HOUR
    waiting = [prompt for prompt in store._load()["prompts"] if prompt["status"] == "waiting"]
    assert sum(bool(prompt.get("room")) for prompt in waiting) == 24
    assert {prompt["id"] for prompt in waiting if not prompt.get("room")} == set(mine)


def test_room_asks_have_a_cap_across_rooms(store, sleeper, pushes):
    exchange(store)
    choices = room_choices.RoomChoices()
    sent = 0
    for n in range(store_module.ROOM_ASKS_ALL_PER_HOUR + 2):
        turn = rules.RoomTurn(room_id=f"room-{n}", side="member", with_others=True, session_id=f"room_{n:032x}")
        answer = json.loads(asks.handle({"question": "Q", "share": "x"}, session_id=turn.session_id,
                                        room_turn=lambda _, turn=turn: turn, choices=lambda _: choices,
                                        room_name=lambda _: "R", store=store, push=pushes.append,
                                        sleep=sleeper.sleep, monotonic=sleeper.monotonic))
        sent += answer["ok"]
    assert sent == store_module.ROOM_ASKS_ALL_PER_HOUR and answer["error"] == "rate_limited"


def test_room_budgets_keep_the_newest_rooms(store, clock, tmp_path):
    exchange(store)
    path = tmp_path / store_module.FILE_NAME
    state = json.loads(path.read_text())
    state["room_asked"] = {f"room-{n}": [clock.now - 3000 + n] for n in range(store_module.MAX_ROOM_BUDGETS + 5)}
    path.write_text(json.dumps(state))
    kept = store._load()["room_asked"]
    assert len(kept) == store_module.MAX_ROOM_BUDGETS and "room-0" not in kept
    assert f"room-{store_module.MAX_ROOM_BUDGETS + 4}" in kept


def test_at_most_three_asks_wait_per_room(store, sleeper, pushes):
    exchange(store)
    for n in range(3):
        assert call(store, sleeper, pushes, {"question": f"Question {n}", "share": "x"})["ok"] is True
    refused = call(store, sleeper, pushes, {"question": "Question 3", "share": "x"})
    assert refused["error"] == "too_many" and "don't share" in refused["note"].lower()
    assert len(exchange(store)["prompts"]) == 3


def test_private_status_leaves_room_asks_out(store, sleeper, pushes):
    exchange(store)
    asked = call(store, sleeper, pushes)
    plain = _confirm(store)
    exchange(store, results=[{"id": asked["id"], "outcome": "confirmed", "via": "tap"}])
    assert [prompt["id"] for prompt in store.status()] == [plain["id"]]
    assert store.status(asked["id"]) == [] and store.cancel(asked["id"]) is None
    assert store.unreported_outcomes() == []
    # Still the room's to hear.
    assert len(answered(store, "zoo-trip")) == 1


def test_a_room_prompt_with_a_bad_room_on_disk_is_dropped(store, sleeper, pushes, tmp_path):
    exchange(store)
    call(store, sleeper, pushes)
    path = tmp_path / store_module.FILE_NAME
    state = json.loads(path.read_text())
    state["prompts"][0]["room"] = {"id": 3}
    path.write_text(json.dumps(state))
    assert exchange(store)["prompts"] == [] and store.status() == []


# --- The framing -------------------------------------------------------------------


def test_the_framing_names_the_tool_for_ask_me_first():
    framing = rules.framing(TURN, room_choices.RoomChoices(share="ask"))
    assert asks.TOOL_NAME in framing
    assert asks.TOOL_NAME not in rules.framing(TURN, room_choices.RoomChoices(share="helps"))
    assert asks.TOOL_NAME == rules.ASK_TOOL and asks.TOOLSET == "conduit_rooms"


def test_the_tool_description_says_when_to_use_it():
    description = asks.SCHEMA["description"]
    assert "Ask me first" in description and "share" in description


def test_room_asks_never_push_the_owners_own_prompt_off_the_phone(store, sleeper, pushes, clock):
    exchange(store)
    plain = _confirm(store, ttl_minutes=24 * 60)
    for n in range(8):
        turn = rules.RoomTurn(room_id=f"room-{n}", side="member", with_others=True, session_id=f"room_{n:032x}")
        for q in range(3):
            asks.handle({"question": f"Q{q}", "share": "x"}, session_id=turn.session_id,
                        room_turn=lambda _, turn=turn: turn, choices=lambda _: room_choices.RoomChoices(),
                        room_name=lambda _: "R", store=store, push=pushes.append, sleep=sleeper.sleep,
                        monotonic=sleeper.monotonic)
        clock.now += 3600
    shown = [prompt["id"] for prompt in exchange(store)["prompts"]]
    assert plain["id"] in shown and len(shown) == 24 + 1


def test_a_rooms_unheard_answer_outlives_later_prompts(store, sleeper, pushes, clock):
    exchange(store)
    asked = call(store, sleeper, pushes)
    exchange(store, results=[{"id": asked["id"], "outcome": "confirmed", "via": "tap"}])
    for n in range(store_module.MAX_PROMPTS + 5):
        prompt = _confirm(store)
        exchange(store, results=[{"id": prompt["id"], "outcome": "declined", "via": "tap"}])
        clock.now += 3600 / store_module.PER_HOUR
    assert len(answered(store, "zoo-trip")) == 1


def test_waiting_room_asks_are_capped_across_rooms_so_each_one_shows(store, sleeper, pushes, clock):
    exchange(store)
    accepted = []
    for n in range(20):
        turn = rules.RoomTurn(room_id=f"room-{n}", side="member", with_others=True, session_id=f"room_{n:032x}")
        for q in range(3):
            answer = json.loads(asks.handle(
                {"question": f"Q{q}", "share": "x"}, session_id=turn.session_id, room_turn=lambda _, turn=turn: turn,
                choices=lambda _: room_choices.RoomChoices(), room_name=lambda _: "R", store=store,
                push=pushes.append, sleep=sleeper.sleep, monotonic=sleeper.monotonic))
            if answer["ok"]:
                accepted.append(answer["id"])
            last = answer
        clock.now += 3600
    assert len(accepted) == store_module.MAX_ROOM_ASKS_SHOWN and last["error"] == "too_many"
    assert set(accepted) <= {prompt["id"] for prompt in exchange(store)["prompts"]}


def test_an_ask_from_a_session_key_the_store_cant_keep_is_refused(store):
    exchange(store)
    with pytest.raises(store_module.PresentError) as refused:
        store.ask_room(QUESTION, SHARE, room_id="zoo-trip", room_name="Zoo trip", session_id="room\n" + "x" * 600)
    assert refused.value.code == "invalid" and exchange(store)["prompts"] == []


def test_an_answer_the_tool_returned_is_framed_again_when_its_turn_never_replied(store, sleeper, pushes):
    exchange(store)
    answer_while_waiting(store, sleeper, "confirmed")
    assert call(store, sleeper, pushes)["status"] == "confirmed"
    heard(lambda: store, ROOM_SESSION, False)  # the turn failed before it replied
    assert answered(store, "zoo-trip", ROOM_SESSION) == [
        f'Your owner said yes to "{QUESTION}": you may share this, and nothing more: {SHARE}']
    assert answered(store, "zoo-trip", ROOM_SESSION) == []


def test_an_answer_the_tool_returned_is_heard_once_its_turn_replies(store, sleeper, pushes):
    exchange(store)
    answer_while_waiting(store, sleeper, "declined")
    assert call(store, sleeper, pushes)["status"] == "declined"
    heard(lambda: store, ROOM_SESSION, True)
    assert answered(store, "zoo-trip") == []


def test_unheard_answers_keep_only_the_newest_per_room_and_across_rooms(store, sleeper, pushes, clock):
    exchange(store)
    per_room, total = store_module.MAX_ROOM_ANSWERS_KEPT, store_module.MAX_ROOM_ANSWERS_KEPT_ALL
    rooms = total // per_room + 2
    for n in range(rooms):
        turn = rules.RoomTurn(room_id=f"room-{n}", side="member", with_others=True, session_id=f"room_{n:032x}")
        for q in range(per_room + 2):
            asked = json.loads(asks.handle(
                {"question": f"Q{q}", "share": "x"}, session_id=turn.session_id, room_turn=lambda _, turn=turn: turn,
                choices=lambda _: room_choices.RoomChoices(), room_name=lambda _: "R", store=store,
                push=pushes.append, sleep=sleeper.sleep, monotonic=sleeper.monotonic))
            exchange(store, results=[{"id": asked["id"], "outcome": "declined", "via": "tap"}])
            clock.now += 3600 / store_module.ROOM_ASKS_PER_HOUR
    kept = {f"room-{n}": len(store.room_answers(f"room-{n}")) for n in range(rooms)}
    assert all(count <= per_room for count in kept.values()) and sum(kept.values()) == total
    # The newest are kept: the last room keeps its newest answers, the first rooms lose theirs.
    newest = [prompt["title"] for prompt in store.room_answers(f"room-{rooms - 1}")]
    assert newest == [f"Q{q}" for q in range(2, per_room + 2)] and kept["room-0"] == 0


def test_an_ask_over_the_limits_never_promises_a_check(store, sleeper, pushes):
    exchange(store)
    for n in range(store_module.MAX_ROOM_ASKS_WAITING):
        call(store, sleeper, pushes, {"question": f"Question {n}", "share": "x"})
    refused = call(store, sleeper, pushes, {"question": "One more", "share": "x"})
    assert refused["error"] == "too_many"
    assert "check with your owner" not in refused["note"] and "don't share" in refused["note"].lower()


def test_a_local_member_session_hermes_hasnt_titled_is_answered_quietly(store, sleeper, pushes, caplog):
    def untitled(_session):
        raise rules.RoomStoreError("not titled yet")

    with caplog.at_level("WARNING", logger="hermes.plugins.conduit_push"):
        answer = json.loads(asks.handle(ASK, session_id=ROOM_SESSION, room_turn=untitled,
                                        choices=lambda _: room_choices.RoomChoices(), store=store,
                                        push=pushes.append, sleep=sleeper.sleep, monotonic=sleeper.monotonic))
    assert answer["error"] == "unavailable" and "can't share that" in answer["note"]
    assert caplog.records == []


def test_a_turn_with_nothing_to_frame_never_hears_an_earlier_turns_list(store, sleeper, pushes):
    exchange(store)
    asked = call(store, sleeper, pushes)
    exchange(store, results=[{"id": asked["id"], "outcome": "declined", "via": "tap"}])
    assert asks.frame(store, "zoo-trip", ROOM_SESSION)  # that turn failed: never heard
    assert asks.frame(store, "other-room", ROOM_SESSION) == []
    heard(lambda: store, ROOM_SESSION, True)
    assert len(answered(store, "zoo-trip")) == 1


def test_a_turn_that_couldnt_be_framed_never_hears_a_failed_turns_answers(store, sleeper, pushes, tmp_path,
                                                                          monkeypatch, clock):
    exchange(store)
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "PresentStore", lambda home: PresentStore(home, clock=clock))
    monkeypatch.setattr(plugin.room_rules, "choices_for", lambda turn, home: room_choices.RoomChoices())
    monkeypatch.setattr(plugin.room_invites, "people", lambda *a, **k: None)
    monkeypatch.setattr(plugin.room_rules, "room_turn", lambda session, home=None: TURN)
    answer_while_waiting(store, sleeper, "declined")
    assert call(store, sleeper, pushes)["status"] == "declined"
    plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="", turn_id="t1")  # that turn failed

    def unreadable(session, home=None):
        raise rules.RoomStoreError("room store unreadable")

    monkeypatch.setattr(plugin.room_rules, "room_turn", unreadable)
    plugin._room_framing(ROOM_SESSION)
    plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="Not sure.", turn_id="t2")
    monkeypatch.setattr(plugin.room_rules, "room_turn", lambda session, home=None: TURN)
    assert f'- Your owner said no to "{QUESTION}": don\'t share it.' in plugin._room_framing(ROOM_SESSION).splitlines()


def test_a_default_room_is_told_the_ask_tool_is_there():
    # Talk only and Ask me first (the defaults): the tool goes through, so the framing never says there are none.
    framing = rules.framing(TURN, room_choices.RoomChoices())
    assert "without tools" not in framing and "you'll ask them" not in framing
    assert framing.count(asks.TOOL_NAME) >= 2  # the share line and the tool line
    with_search = rules.framing(TURN, room_choices.RoomChoices(search_chats=True))
    assert asks.TOOL_NAME in with_search.split("past chats", 1)[1]
    for tools in ("look", "custom"):
        tiered = rules.framing(TURN, room_choices.RoomChoices(tools=tools, custom_tools=("web_search",)))
        assert asks.TOOL_NAME in tiered.split("- Here", 1)[1]


def test_a_room_not_set_to_ask_first_never_mentions_the_tool():
    for share in ("helps", "nothing"):
        framing = rules.framing(TURN, room_choices.RoomChoices(share=share))
        assert asks.TOOL_NAME not in framing and "without tools" in framing


def test_every_result_gives_back_the_share(store, sleeper, pushes):
    exchange(store)
    waited = call(store, sleeper, pushes)
    again = call(store, sleeper, pushes)
    assert waited["status"] == again["status"] == "waiting" and waited["share"] == again["share"] == SHARE


# --- Follow-ups (review of #460) -------------------------------------------------------------


@pytest.fixture
def room_plugin(store, tmp_path, monkeypatch, clock):
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "PresentStore", lambda home: PresentStore(home, clock=clock))
    monkeypatch.setattr(plugin.room_rules, "room_turn", lambda session, home=None: TURN)
    monkeypatch.setattr(plugin.room_rules, "choices_for", lambda turn, home: room_choices.RoomChoices())
    monkeypatch.setattr(plugin.room_invites, "people", lambda *a, **k: None)
    exchange(store)
    return plugin


def _declined(store, sleeper, pushes):
    asked = call(store, sleeper, pushes)
    exchange(store, results=[{"id": asked["id"], "outcome": "declined", "via": "tap"}])
    return f'- Your owner said no to "{QUESTION}": don\'t share it.'


def test_a_turn_that_failed_with_error_text_frames_its_answers_again(room_plugin, store, sleeper, pushes):
    line = _declined(store, sleeper, pushes)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()
    # Hermes runs post_llm_call for a failed turn with its error explanation; on_session_end says it failed.
    room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="Error: the model timed out.",
                               turn_id="t1")
    room_plugin._on_session_end(session_id=ROOM_SESSION, turn_id="t1", completed=False, failed=True,
                                interrupted=False)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()
    room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="I can't share that.", turn_id="t2")
    room_plugin._on_session_end(session_id=ROOM_SESSION, turn_id="t2", completed=True, interrupted=False)
    assert "said no" not in (room_plugin._room_framing(ROOM_SESSION) or "")


def test_a_hermes_that_never_says_how_the_turn_ended_hears_a_turn_that_replied(room_plugin, store, sleeper, pushes):
    line = _declined(store, sleeper, pushes)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()
    room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="I can't share that.", turn_id="t1")
    # No on_session_end: the next turn's start settles it.
    assert "said no" not in (room_plugin._room_framing(ROOM_SESSION) or "")


def test_a_delegated_childs_turn_never_hears_the_rooms_answers(room_plugin, store, sleeper, pushes, monkeypatch):
    line = _declined(store, sleeper, pushes)
    child = "child-of-room"
    monkeypatch.setattr(room_plugin, "_is_child", lambda session: session == child)
    assert line not in (room_plugin._room_framing(child) or "").splitlines()
    room_plugin._post_llm_call(session_id=child, assistant_response="Done.", turn_id="c1")
    room_plugin._on_session_end(session_id=child, turn_id="c1", completed=True, interrupted=False)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()


def test_two_answers_given_at_once_in_one_turn_are_both_heard(store, monkeypatch):
    import threading
    import time
    start = threading.Barrier(8)
    keep = asks._keep_locked

    def slow_keep(session_id, prompt_ids):
        time.sleep(0.02)  # every other call reads the list meanwhile, unless the read waits for the write
        keep(session_id, prompt_ids)

    monkeypatch.setattr(asks, "_keep_locked", slow_keep)

    def give(n):
        start.wait()
        asks._given("racing-session", f"prompt-{n}")

    threads = [threading.Thread(target=give, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(asks._framed.pop("racing-session")) == sorted(f"prompt-{n}" for n in range(8))


def test_the_tool_text_says_a_re_ask_must_match_and_names_the_share():
    assert "same question and share" in asks.DESCRIPTION
    assert "share exactly share" not in asks._NOTES["confirmed"]


def test_a_store_read_failing_mid_wait_still_says_the_ask_waits(store, sleeper, pushes, monkeypatch):
    exchange(store)

    def unreadable(_prompt_id):
        raise OSError("present file busy")

    sleeper.during = (1.0, lambda: monkeypatch.setattr(store, "room_ask", unreadable))
    answer = call(store, sleeper, pushes)
    assert answer["ok"] is True and answer["status"] == "waiting" and answer["share"] == SHARE
    assert "check with your owner" in answer["note"]


def test_a_room_ask_is_never_answered_automatically(store, sleeper, pushes):
    exchange(store)
    asked = call(store, sleeper, pushes)
    exchange(store, results=[{"id": asked["id"], "outcome": "confirmed", "via": "auto"}])
    assert store.get(asked["id"])["status"] == "waiting"


def test_a_store_read_failing_on_a_re_ask_still_says_the_ask_waits(store, sleeper, pushes, monkeypatch):
    exchange(store)
    first = call(store, sleeper, pushes)

    def unreadable(_prompt_id):
        raise OSError("present file busy")

    monkeypatch.setattr(store, "room_ask", unreadable)
    again = call(store, sleeper, pushes)
    assert again["ok"] is True and again["id"] == first["id"] and again["status"] == "waiting"
    assert again["share"] == SHARE and "check with your owner" in again["note"]


def test_a_stopped_turn_never_hears_its_answers_whatever_hermes_reports(room_plugin, store, sleeper, pushes):
    line = _declined(store, sleeper, pushes)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()
    room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="Partial answer", turn_id="t1")
    room_plugin._on_session_end(session_id=ROOM_SESSION, turn_id="t1", interrupted=True)  # no completed or failed
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()


def test_an_end_that_doesnt_say_how_the_turn_ended_frames_its_answers_again(room_plugin, store, sleeper, pushes):
    # The push path counts such a turn failed and drops its held reply, so its answers never reached the room.
    line = _declined(store, sleeper, pushes)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()
    room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="Error: the model timed out.",
                               turn_id="t1")
    room_plugin._on_session_end(session_id=ROOM_SESSION, turn_id="t1")
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()


def test_a_fork_of_a_room_turn_leaves_the_turn_its_answers(room_plugin, store, sleeper, pushes):
    # The self-improvement review or a /btw of the room's chat runs under its id (forks.py), like a child.
    line = _declined(store, sleeper, pushes)
    assert line in room_plugin._room_framing(ROOM_SESSION).splitlines()  # the room's turn
    token = room_plugin.forks._in_fork.set(True)
    try:
        assert "said no" not in (room_plugin._room_framing(ROOM_SESSION) or ""), "the fork isn't framed with it"
        room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="Noted.", turn_id="fork")
        room_plugin._on_session_end(session_id=ROOM_SESSION, turn_id="fork", completed=True, interrupted=False)
    finally:
        room_plugin.forks._in_fork.reset(token)
    room_plugin._post_llm_call(session_id=ROOM_SESSION, assistant_response="I can't share that.", turn_id="t1")
    room_plugin._on_session_end(session_id=ROOM_SESSION, turn_id="t1", completed=True, interrupted=False)
    assert "said no" not in (room_plugin._room_framing(ROOM_SESSION) or ""), "the turn heard what it was framed with"
