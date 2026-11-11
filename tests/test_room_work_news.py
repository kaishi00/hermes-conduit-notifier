"""Work news for a bot in a Linked Room someone else hosts (room_work_news): the bot's Hermes asks the room's home
over the Link to post the notice that wakes it, and the home posts room_work's LINKED_POST for that agent only.

Furina hosts; Laifu joins over link1 and Mona over link2 (test_room_invites' installations and mailbox).
"""
import pytest

from test_room_bridge_member import ROOT, _load
from test_room_invites import _sent, _started, rooms

assert rooms  # the fixture, imported for these tests

news_module = _load("conduit_push.room_work_news", ROOT / "room_work_news.py")
work_module = _load("conduit_push.room_work", ROOT / "room_work.py")
links_module = _load("conduit_push.room_links", ROOT / "room_links.py")


@pytest.fixture
def news(rooms):
    rooms.delivered = []
    rooms.refuse = None

    def deliver(job, text, address):
        rooms.delivered.append((job, text, address))
        return rooms.refuse

    rooms.furina.news.deliver = deliver
    return rooms


def ask(side, name, room_id, profile="default"):
    rooms = side.wire.sides[name]
    return news_module.ask_home(rooms.root, room_id, profile, rooms=rooms)


def test_a_guests_bot_is_woken_by_the_homes_post(news):
    room_id = _started(news, links=("link1", "link2"))

    assert ask(news, "laifu", room_id) is None

    [(link_id, body, _)] = _sent(news, news_module.NEWS)
    member = body["member_id"]
    assert link_id == "link1" and body == {"v": 1, "room_id": room_id, "member_id": member}
    [(job, text, address)] = news.delivered
    assert text == work_module.LINKED_POST.format(handle="laifu")
    assert address == f"{room_id}@default", "posted from the home's own profile in the room"
    assert job["name"] == "Laifu's work"


def test_only_the_notice_crosses_never_the_report(news):
    room_id = _started(news)
    ask(news, "laifu", room_id)
    [(_, body, _)] = _sent(news, news_module.NEWS)
    assert set(body) == {"v", "room_id", "member_id"}


def test_a_link_cant_wake_another_links_agent(news):
    room_id = _started(news, links=("link1", "link2"))
    ask(news, "mona", room_id)
    [(_, body, _)] = _sent(news, news_module.NEWS)
    news.delivered.clear()
    forged = links_module.Received(link_id="link1", kind=news_module.NEWS, message_id="x1", at=0, body=body)
    news.furina.handle(forged)
    assert news.delivered == [], "Mona's agent isn't link1's"


def test_a_room_that_isnt_started_or_an_agent_that_left_gets_no_post(news):
    room_id = _started(news)
    member = next(each for each in news.furina.store.read()["drafts"][room_id]["members"] if each["link"])
    with news.furina.store.update() as state:
        state["drafts"][room_id]["members"][1]["status"] = "left"
    received = links_module.Received(link_id="link1", kind=news_module.NEWS, message_id="x2", at=0,
                                     body={"v": 1, "room_id": room_id, "member_id": member["member_id"]})
    news.furina.handle(received)
    assert news.delivered == []
    news.furina.handle(links_module.Received(link_id="link1", kind=news_module.NEWS, message_id="x3", at=0,
                                             body={"v": 1, "room_id": "nope", "member_id": member["member_id"]}))
    assert news.delivered == []


@pytest.mark.parametrize("body", [None, {}, {"v": 2, "room_id": "r", "member_id": "m"},
                                  {"v": 1, "room_id": "r", "member_id": "m", "text": "the report"}])
def test_a_body_that_isnt_one_is_dropped(news, body):
    room_id = _started(news)
    if isinstance(body, dict) and "room_id" in body:
        body = {**body, "room_id": room_id}
    news.furina.handle(links_module.Received(link_id="link1", kind=news_module.NEWS, message_id="x4", at=0,
                                             body=body))
    assert news.delivered == []


def test_notices_for_one_agent_are_spaced_and_a_close_one_waits(news):
    room_id = _started(news)
    ask(news, "laifu", room_id)
    ask(news, "laifu", room_id)
    ask(news, "laifu", room_id)
    assert len(news.delivered) == 1, "the next within the minute waits"
    news.clock.now += news_module.GAP_S - 1
    news.furina.pass_once()
    assert len(news.delivered) == 1
    news.clock.now += 1
    news.furina.pass_once()
    assert len(news.delivered) == 2, "the ones that waited are posted once, when it may"
    news.clock.now += news_module.GAP_S
    news.furina.pass_once()
    assert len(news.delivered) == 2
    for _ in range(news_module.PER_HOUR * 2):
        news.clock.now += news_module.GAP_S
        ask(news, "laifu", room_id)
    assert len(news.delivered) == news_module.PER_HOUR


def test_a_waiting_notice_isnt_posted_once_its_agent_left(news):
    room_id = _started(news)
    ask(news, "laifu", room_id)
    ask(news, "laifu", room_id)
    with news.furina.store.update() as state:
        state["drafts"][room_id]["members"][1]["status"] = "left"
    news.clock.now += news_module.GAP_S
    news.furina.pass_once()
    assert len(news.delivered) == 1


def test_a_bot_that_isnt_in_the_room_asks_nothing(news):
    room_id = _started(news)
    assert ask(news, "laifu", room_id, profile="work") == "this bot isn't in that room"
    assert ask(news, "mona", room_id) == "this bot isn't in that room"
    assert _sent(news, news_module.NEWS) == []


def test_a_home_without_room_delivery_says_so_in_the_log(news, caplog):
    caplog.set_level("INFO")
    room_id = _started(news)
    news.refuse = "no live Group Chat"
    ask(news, "laifu", room_id)
    assert "couldn't post work news" in caplog.text
    # A notice that wasn't posted doesn't use up the agent's: the next one goes at once.
    news.refuse = None
    ask(news, "laifu", room_id)
    assert len(news.delivered) == 2
