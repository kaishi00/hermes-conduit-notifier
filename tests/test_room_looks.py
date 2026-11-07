"""How a room over Links' agents and people look (designs/cross-owner-agents, N8): each Conduit gives its own Hermes
its bots' pictures and glow colors and its person's, each installation sends the others in a started room how its
own look, and the home passes on how everyone else in the room looks.

Furina hosts; Laifu and Mona join over link1 and link2 (test_room_invites' installations and mailbox).
"""
import base64
import json
import stat

import pytest

from test_room_bridge_member import ROOT, _load
from test_room_invites import _sent, _started, rooms, slot

assert rooms  # the fixture, imported for these tests

looks_module = _load("conduit_push.room_looks", ROOT / "room_looks.py")

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"j" * 100).decode()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 100).decode()


def look(picture=JPEG, color="#AA3300"):
    return {"picture": picture, "color": color}


def give(side, name, agents=None, person=None):
    return looks_module.put_own(side.tmp_path / name, {"agents": agents or {}, "person": person})


def shown(side, name, room_id):
    return looks_module.shown(side.tmp_path / name, room_id)


def looks_sent(side, link_id=None):
    return [(link, body) for link, body, _ in _sent(side, "room.looks") if link_id in (None, link)]


def everyone_passes(side, times=2):
    for _ in range(times):
        for name in ("laifu", "mona", "furina"):
            side.wire.sides[name].pass_once()


# --- What a Conduit gives its Hermes --------------------------------------------------------


def test_a_conduit_gives_its_hermes_its_bots_and_its_own_look(rooms):
    answer = give(rooms, "furina", {"default": look(), "work": {"picture": None, "color": "#00ff00"}},
                  person=look(PNG, None))

    assert answer == {"agents": ["default", "work"], "person": True}
    own = looks_module.own(rooms.tmp_path / "furina")
    # Colors are kept upper case, as Conduit compares them.
    assert own == {"agents": {"default": look(), "work": {"picture": None, "color": "#00FF00"}},
                   "person": {"picture": PNG, "color": None}}
    file = rooms.tmp_path / "furina" / "conduit-rooms" / "looks.json"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600

    # The next takes its place.
    give(rooms, "furina", {"work": look()})
    assert looks_module.own(rooms.tmp_path / "furina") == {"agents": {"work": look()}, "person": None}


@pytest.mark.parametrize("agents, person", [
    ({"default": {"picture": base64.b64encode(b"GIF89a....").decode(), "color": None}}, None),
    ({"default": {"picture": base64.b64encode(b"\xff\xd8\xff" + b"x" * 9000).decode(), "color": None}}, None),
    ({"default": {"picture": "not base64!", "color": None}}, None),
    ({"default": {"picture": None, "color": "red"}}, None),
    ({"default": {"picture": None, "color": None, "glow": "#FFFFFF"}}, None),
    ({"../etc": look()}, None),
    ({}, {"picture": None, "color": None}),
    ({f"p{n}": look() for n in range(33)}, None),
])
def test_a_look_that_isnt_a_small_picture_and_a_color_is_refused(rooms, agents, person):
    with pytest.raises(looks_module.LooksError) as refused:
        give(rooms, "furina", agents, person)
    assert refused.value.code == "bad_looks"
    assert looks_module.own(rooms.tmp_path / "furina") == {"agents": {}, "person": None}


# --- In a room ------------------------------------------------------------------------------


def test_each_side_sends_its_own_and_the_home_passes_on_everyone_elses(rooms):
    room_id = _started(rooms, links=("link1", "link2"))
    give(rooms, "furina", {"default": look(color="#111111")}, person=look(color="#222222"))
    give(rooms, "laifu", {"default": look(color="#333333")}, person=look(PNG, "#444444"))
    give(rooms, "mona", {"default": look(color="#555555")})

    everyone_passes(rooms)

    furina, laifus, monas = "default", slot("link1", 1), slot("link2", 1)
    # The home has each person's agents and person, by member id and by their Link's name.
    assert shown(rooms, "furina", room_id) == {
        "room_id": room_id, "home": None,
        "members": {laifus: look(color="#333333"), monas: look(color="#555555")},
        "people": [{"name": "Laifu", "poster": "Laifu", "look": look(PNG, "#444444")}]}
    # Each member's Hermes has the home's and the other person's, never its own back.
    assert shown(rooms, "laifu", room_id) == {
        "room_id": room_id, "home": look(color="#222222"),
        "members": {furina: look(color="#111111"), monas: look(color="#555555")}, "people": []}
    assert shown(rooms, "mona", room_id) == {
        "room_id": room_id, "home": look(color="#222222"),
        "members": {furina: look(color="#111111"), laifus: look(color="#333333")},
        "people": [{"name": "Laifu", "poster": "Laifu", "look": look(PNG, "#444444")}]}


def test_looks_go_again_only_when_they_change(rooms):
    room_id = _started(rooms, links=("link1",))
    give(rooms, "laifu", {"default": look()})
    everyone_passes(rooms)
    before = len(looks_sent(rooms))

    everyone_passes(rooms)
    assert len(looks_sent(rooms)) == before

    give(rooms, "laifu", {"default": look(color="#123456")})
    everyone_passes(rooms)
    assert shown(rooms, "furina", room_id)["members"] == {slot("link1", 1): look(color="#123456")}

    # Back to the first: a new message id, so it isn't taken for the one before.
    give(rooms, "laifu", {"default": look()})
    everyone_passes(rooms)
    assert shown(rooms, "furina", room_id)["members"] == {slot("link1", 1): look()}
    ids = [message_id for link, _, message_id in _sent(rooms, "room.looks") if link == "link1"]
    assert len(ids) == len(set(ids))


def test_a_send_the_mailbox_refuses_goes_on_a_later_pass(rooms):
    room_id = _started(rooms, links=("link1",))
    give(rooms, "laifu", {"default": look()})
    rooms.wire.refusing.add("laifu")
    rooms.laifu.pass_once()
    assert shown(rooms, "furina", room_id)["members"] == {}

    rooms.wire.refusing.clear()
    rooms.laifu.pass_once()
    assert shown(rooms, "furina", room_id)["members"] == {slot("link1", 1): look()}


def test_the_home_takes_only_the_senders_own_agents(rooms):
    room_id = _started(rooms, links=("link1", "link2"))
    body = {"v": 1, "room_id": room_id, "members": {"default": look(), slot("link2", 1): look(),
                                                     slot("link1", 1): look(color="#ABCDEF")},
            "people": [{"name": "Mona", "poster": "Mona", "look": look()}], "person": None}

    rooms.wire.sender("laifu")("link1", "room.looks", body, message_id="room.looks:x")

    # Laifu can't say how Furina's or Mona's agents look, or who else is there.
    assert shown(rooms, "furina", room_id) == {"room_id": room_id, "home": None,
                                               "members": {slot("link1", 1): look(color="#ABCDEF")}, "people": []}


def test_a_member_takes_looks_only_from_its_rooms_home_and_never_for_its_own(rooms):
    room_id = _started(rooms, links=("link1",))
    body = {"v": 1, "room_id": room_id, "members": {"default": look(), slot("link1", 1): look(color="#ABCDEF")},
            "people": [], "person": look()}

    rooms.wire.sender("furina")("link1", "room.looks", body, message_id="room.looks:y")
    assert shown(rooms, "laifu", room_id)["members"] == {"default": look()}

    # A room it isn't in is dropped.
    rooms.wire.sender("furina")("link1", "room.looks", {**body, "room_id": "conduit-other"},
                                message_id="room.looks:z")
    assert shown(rooms, "laifu", "conduit-other")["members"] == {}


def test_a_body_that_isnt_one_or_a_look_that_isnt_is_dropped(rooms):
    room_id = _started(rooms, links=("link1",))
    send = rooms.wire.sender("laifu")
    send("link1", "room.looks", {"v": 2, "room_id": room_id, "members": {}, "people": [], "person": None},
         message_id="a")
    send("link1", "room.looks", {"v": 1, "room_id": room_id, "members": {
        slot("link1", 1): {"picture": "<svg/>", "color": None}}, "people": [], "person": {"color": "#FFF"}},
         message_id="b")

    assert shown(rooms, "furina", room_id) == {"room_id": room_id, "home": None, "members": {}, "people": []}


def test_a_room_too_big_for_an_envelope_leaves_out_pictures_and_keeps_colors(rooms, monkeypatch):
    monkeypatch.setattr(looks_module, "BODY_BYTES", 600)
    room_id = _started(rooms, links=("link1", "link2"))
    give(rooms, "furina", {"default": look(color="#111111")}, person=look(color="#222222"))
    give(rooms, "laifu", {"default": look(color="#333333")}, person=look(color="#444444"))

    everyone_passes(rooms)

    [(_, body)] = looks_sent(rooms, "link2")[-1:]
    assert len(json.dumps(body, separators=(",", ":"))) <= 600 + len(JPEG) + 200
    assert {member_id: each["color"] for member_id, each in body["members"].items()} == {
        "default": "#111111", slot("link1", 1): "#333333"}
    assert [person["look"] for person in body["people"]] == [{"picture": None, "color": "#444444"}]


def test_a_room_no_one_keeps_any_more_is_forgotten(rooms):
    room_id = _started(rooms, links=("link1",))
    give(rooms, "laifu", {"default": look()})
    everyone_passes(rooms)
    assert room_id in looks_module.Store(rooms.tmp_path / "furina").read()["rooms"]

    with rooms.furina.store.update() as state:
        del state["drafts"][room_id]
    rooms.furina.pass_once()

    assert looks_module.Store(rooms.tmp_path / "furina").read()["rooms"] == {}


def test_an_unreadable_file_draws_without_looks(rooms):
    file = rooms.tmp_path / "furina" / "conduit-rooms" / "looks.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("{not json")

    assert looks_module.own(rooms.tmp_path / "furina") == {"agents": {}, "person": None}
    rooms.furina.pass_once()


def test_the_routes_take_your_looks_and_show_a_rooms(rooms, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_room_invites import BASE, Loop, api

    room_id = _started(rooms, links=("link1",))
    give(rooms, "laifu", {"default": look()}, person=look(PNG, None))
    everyone_passes(rooms)
    dashboard = api.core._plugin_package_module("room_invites")
    root = rooms.tmp_path / "furina"
    side = dashboard.Rooms(root, send=rooms.wire.sender("furina"),
                           link=lambda link_id: rooms.links["furina"].get(link_id), clock=rooms.clock)
    monkeypatch.setattr(dashboard, "start", lambda root: side)
    monkeypatch.setattr(api.linked_rooms, "_room_links_root", lambda: root)
    monkeypatch.setattr(api.linked_rooms, "_start_room_handlers", lambda root: None)
    monkeypatch.setattr(api.core._plugin_package_module("room_links"), "start", lambda root: Loop())
    monkeypatch.setattr(api.linked_rooms._room_invites_limiter, "_mints", {})
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)

    put = client.put(f"{BASE}/rooms/looks", json={"agents": {"default": look()}, "person": None})
    assert put.status_code == 200 and put.headers["cache-control"] == "no-store"
    assert put.json() == {"ok": True, "looks": {"agents": ["default"], "person": False}}
    got = client.get(f"{BASE}/rooms/looks", params={"room_id": room_id})
    assert got.json()["members"] == {slot("link1", 1): look()}
    assert got.json()["people"] == [{"name": "Laifu", "poster": "Laifu", "look": look(PNG, None)}]
    refused = client.put(f"{BASE}/rooms/looks", json={"agents": {"default": {"picture": None, "color": "red"}}})
    assert (refused.status_code, refused.json()["code"]) == (400, "bad_looks")
    assert "room-looks" in api.capabilities.ROUTE_CAPABILITIES


def test_a_person_with_no_name_is_dropped(rooms):
    room_id = _started(rooms, links=("link1",))
    body = {"v": 1, "room_id": room_id, "members": {},
            "people": [{"name": None, "poster": "Guest", "look": look()}], "person": None}

    rooms.wire.sender("furina")("link1", "room.looks", body, message_id="room.looks:n")

    assert shown(rooms, "laifu", room_id)["people"] == []


def test_a_file_holding_a_look_that_isnt_one_reads_as_empty(rooms):
    give(rooms, "furina", {"default": look()})
    file = rooms.tmp_path / "furina" / "conduit-rooms" / "looks.json"
    state = json.loads(file.read_text())
    state["own"]["agents"]["default"] = "not a look"
    file.write_text(json.dumps(state))

    assert looks_module.own(rooms.tmp_path / "furina") == {"agents": {}, "person": None}
    rooms.furina.pass_once()
