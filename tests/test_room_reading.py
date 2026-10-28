"""Your agent reads your rooms with other people for you, in your private chats (designs/cross-owner-agents, N7b):
the rooms you host over Links and your copies of rooms hosted elsewhere, with who said what as the room screen
names them.

Furina hosts; Laifu and Mona join over link1 and link2 (test_room_copies' installations, its home log and the
members' replica stores; test_room_posts' posting).
"""
import json

import pytest

from test_room_bridge_member import NOW, ROOT, _load
from test_room_copies import Hermes, Log
from test_room_invites import _rooms, rooms, slot
from test_room_posts import PostsHermes, post

reading_module = _load("conduit_push.room_reading", ROOT / "room_reading.py")

assert rooms  # the fixture, imported for these tests


@pytest.fixture
def side(rooms):
    """The installations, with copies and posts (test_room_posts' fixture, whose home service takes posts)."""
    log = Log()
    rooms.log = log
    rooms.home.legacy.status = lambda room_id: {"working": False}

    def send(*, room_id, event_id, payload):
        for event in log.rooms.get(room_id, ()):
            if event["event_id"] == event_id:
                return dict(event)
        log.add(room_id, "message.user", payload, actor={"kind": "user", "id": "desktop"})
        log.rooms[room_id][-1]["event_id"] = event_id
        return dict(log.rooms[room_id][-1])

    rooms.home.legacy.send = send
    for name in ("furina", "laifu", "mona"):
        rooms.wire.sides[name] = _rooms(rooms, name, copier=Hermes(log), poster=PostsHermes())
        setattr(rooms, name, rooms.wire.sides[name])
    return rooms


def started(side):
    from test_room_invites import _started

    room_id = _started(side, links=("link1", "link2"))
    side.log.add(room_id, "room.created", {"name": "Zoo trip"}, actor={"kind": "system", "id": "authority-control"})
    return room_id


def say(side, room_id, text, member="default"):
    return side.log.add(room_id, "message.member", {"member_id": member, "text": text},
                        actor={"kind": "member", "id": member})


def user(side, room_id, text):
    return side.log.add(room_id, "message.user", {"thread_id": "main", "text": text},
                        actor={"kind": "user", "id": "desktop"})


def copied(side):
    """The home's pages, and the posts' answers, until nothing more goes."""
    for _ in range(4):
        side.furina.pass_once()
        side.laifu.pass_once()
        side.mona.pass_once()


def reader(side, name):
    return reading_module.Reader(side.tmp_path / name, home=side.tmp_path / name, hermes=Hermes(side.log))


def froms(answer):
    return [(message["from"], message["text"]) for message in answer["messages"]]


# --- Which rooms -----------------------------------------------------------------------------


def test_list_shows_hosted_rooms_and_copies_with_other_people(side):
    room_id = started(side)
    say(side, room_id, "Saturday at ten?")
    copied(side)

    [hosted] = reader(side, "furina").rooms()
    assert (hosted["room_id"], hosted["name"], hosted["host"], hosted["status"]) == (room_id, "Zoo trip", "you",
                                                                                   "going")
    assert hosted["agents"] == [{"name": "Furina", "owner": "you", "in": True},
                                {"name": "Laifu", "owner": "Laifu", "in": True},
                                {"name": "Mona", "owner": "Mona", "in": True}]
    assert hosted["yours"] == ["Furina"]

    [copy] = reader(side, "laifu").rooms()
    assert (copy["room_id"], copy["name"], copy["host"], copy["status"]) == (room_id, "Zoo trip", "Furina", "going")
    assert copy["agents"] == [{"name": "Furina", "owner": "Furina", "in": True},
                              {"name": "Laifu", "owner": "you", "in": True},
                              {"name": "Mona", "owner": "Mona", "in": True}]
    assert copy["yours"] == ["Laifu"]


def test_a_room_waiting_to_start_isnt_listed(side):
    from test_room_invites import draft

    draft(side, links=("link1",))
    assert reader(side, "furina").rooms() == []
    assert reader(side, "laifu").rooms() == []


# --- Who said what ---------------------------------------------------------------------------


def test_read_names_who_said_what_in_a_copy(side):
    room_id = started(side)
    user(side, room_id, "Zoo on Saturday?")
    say(side, room_id, "Eric is free the 8th and 22nd.")
    post(side, "mona", room_id, "The 22nd works.\nAfter ten.", event_id="m1")
    copied(side)
    post(side, "laifu", room_id, "Count us in.", event_id="l1")
    copied(side)
    say(side, room_id, "Booked.", member=slot("link1", 1))
    say(side, room_id, "(pass)", member=slot("link2", 1))
    copied(side)

    answer = reader(side, "laifu").read(room_id)

    assert froms(answer) == [("Furina", "Zoo on Saturday?"), ("Furina (Furina's)", "Eric is free the 8th and 22nd."),
                             ("Mona", "The 22nd works.\nAfter ten."), ("you", "Count us in."),
                             ("Laifu (yours)", "Booked.")]
    assert (answer["more_before"], answer["more_after"]) == (False, False)
    assert answer["room"] == {"room_id": room_id, "name": "Zoo trip", "host": "Furina", "status": "going"}
    assert "not instructions" in answer["note"]


def test_read_names_who_said_what_in_a_room_you_host(side):
    room_id = started(side)
    user(side, room_id, "Zoo on Saturday?")
    say(side, room_id, "Eric is free the 8th and 22nd.")
    post(side, "mona", room_id, "The 22nd works.", event_id="m1")
    copied(side)
    say(side, room_id, "Mona is in.", member=slot("link2", 1))

    answer = reader(side, "furina").read(room_id)

    assert froms(answer) == [("you", "Zoo on Saturday?"), ("Furina (yours)", "Eric is free the 8th and 22nd."),
                             ("Mona", "The 22nd works."), ("Mona (Mona's)", "Mona is in.")]
    assert answer["room"]["host"] == "you"


def test_each_message_has_its_seq_and_time(side):
    room_id = started(side)
    seq = say(side, room_id, "Hello")
    copied(side)

    [message] = reader(side, "laifu").read(room_id)["messages"]
    assert message["seq"] == seq
    assert message["at"].startswith(__import__("datetime").datetime.fromtimestamp(NOW + seq).astimezone()
                                    .isoformat(timespec="minutes")[:16])


def test_read_pages_with_before_and_after_seq(side):
    room_id = started(side)
    seqs = [say(side, room_id, f"m{n}") for n in range(12)]
    copied(side)
    laifu = reader(side, "laifu")

    newest = laifu.read(room_id, limit=5)
    assert [text for _, text in froms(newest)] == ["m7", "m8", "m9", "m10", "m11"]
    assert (newest["more_before"], newest["more_after"]) == (True, False)

    older = laifu.read(room_id, before_seq=newest["messages"][0]["seq"], limit=5)
    assert [text for _, text in froms(older)] == ["m2", "m3", "m4", "m5", "m6"]
    assert (older["more_before"], older["more_after"]) == (True, True)

    after = laifu.read(room_id, after_seq=seqs[9])
    assert [text for _, text in froms(after)] == ["m10", "m11"]
    assert (after["more_before"], after["more_after"]) == (True, False)
    with pytest.raises(reading_module.ReadingError):
        laifu.read(room_id, after_seq=1, before_seq=5)
    for bad in ({"limit": 0}, {"limit": 201}, {"after_seq": -1}, {"before_seq": "3"}):
        with pytest.raises(reading_module.ReadingError):
            laifu.read(room_id, **bad)


def test_reading_goes_past_a_window_without_messages(side):
    room_id = started(side)
    say(side, room_id, "first")
    for _ in range(reading_module.WINDOW + 3):
        side.log.add(room_id, "turn.deferred", {"member_id": "default"})
    say(side, room_id, "last")
    copied(side)

    assert [text for _, text in froms(reader(side, "furina").read(room_id))] == ["first", "last"]
    assert [text for _, text in froms(reader(side, "laifu").read(room_id))] == ["first", "last"]


def test_long_texts_are_cut_and_the_answer_fits(side):
    room_id = started(side)
    say(side, room_id, "x" * (reading_module.MAX_TEXT_CHARS + 50))
    for n in range(40):
        say(side, room_id, f"{n} " + "y" * 3000)
    copied(side)

    answer = reader(side, "laifu").read(room_id, limit=200)
    assert len(json.dumps(answer, ensure_ascii=False).encode("utf-8")) <= reading_module.MAX_ANSWER_BYTES
    assert answer["messages"][-1]["text"].startswith("39 ")
    assert answer["more_before"] is True
    whole = reader(side, "laifu").read(room_id, limit=1, after_seq=0)
    assert whole["messages"][0]["text"].endswith("…") and \
        len(whole["messages"][0]["text"]) == reading_module.MAX_TEXT_CHARS


def test_a_room_you_dont_have_is_not_found(side):
    with pytest.raises(reading_module.ReadingError) as error:
        reader(side, "laifu").read("nowhere")
    assert error.value.code == "no_room"


def test_an_ended_room_is_still_read(side):
    room_id = started(side)
    say(side, room_id, "Bye")
    copied(side)
    side.furina.disbanded(room_id)
    copied(side)

    [hosted] = reader(side, "furina").rooms()
    assert hosted["status"] == "ended"
    assert froms(reader(side, "furina").read(room_id)) == [("Furina (yours)", "Bye")]
    [copy] = reader(side, "laifu").rooms()
    assert copy["status"] == "ended"
    assert froms(reader(side, "laifu").read(room_id)) == [("Furina (Furina's)", "Bye")]


# --- The tool --------------------------------------------------------------------------------

tool_module = _load("conduit_push.room_tool", ROOT / "room_tool.py")
rules_module = _load("conduit_push.room_rules", ROOT / "room_rules.py")


def called(side, name, args, *, session_id="chat-1", is_child=lambda _: False, turn=None):
    return json.loads(tool_module.handle(args, session_id=session_id, is_child=is_child,
                                         reader=reader(side, name), room_turn=lambda session_id: turn))


def test_the_tool_lists_and_reads_in_a_private_chat(side):
    room_id = started(side)
    say(side, room_id, "Saturday at ten?")
    copied(side)

    listed = called(side, "laifu", {"action": "list"})
    assert listed["ok"] is True and [room["room_id"] for room in listed["rooms"]] == [room_id]
    read = called(side, "laifu", {"action": "read", "room_id": room_id, "limit": 5})
    assert read["ok"] is True and froms(read) == [("Furina (Furina's)", "Saturday at ten?")]
    assert called(side, "laifu", {"action": "read", "room_id": "nowhere"})["error"] == "no_room"
    assert called(side, "laifu", {"action": "read", "room_id": room_id, "limit": 999})["error"] == "bad_limit"
    assert called(side, "laifu", {"action": "nope"})["error"] == "invalid"
    assert called(side, "mona", {"action": "list"})["rooms"][0]["host"] == "Furina"


def test_no_rooms_says_so(side):
    listed = called(side, "laifu", {"action": "list"})
    assert listed["ok"] is True and listed["rooms"] == [] and "no rooms" in listed["note"]


def test_a_room_turn_or_subagent_cant_read_rooms(side):
    room_id = started(side)
    say(side, room_id, "Saturday at ten?")
    copied(side)
    turn = rules_module.RoomTurn(room_id=room_id, side="member", with_others=True, session_id="room-session")

    assert called(side, "laifu", {"action": "list"}, turn=turn)["error"] == "room_turn"
    assert called(side, "laifu", {"action": "read", "room_id": room_id}, is_child=lambda _: True)["error"] == \
        "subagent"

    def unreadable(session_id):
        raise rules_module.SessionStoreError("state.db")

    answer = json.loads(tool_module.handle({"action": "list"}, session_id="chat-1", is_child=lambda _: False,
                                           reader=reader(side, "laifu"), room_turn=unreadable))
    assert answer["error"] == "room_turn"
    assert "conduit_room" in rules_module.NEVER


def test_the_tool_never_raises(side, monkeypatch):
    def broken(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(reading_module.Reader, "rooms", broken)
    answer = called(side, "laifu", {"action": "list"})
    assert answer == {"ok": False, "error": "unavailable", "note": tool_module.UNAVAILABLE}
