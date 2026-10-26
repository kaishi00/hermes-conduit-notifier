"""Links between installations and the mailbox loop (designs/cross-owner-agents, layer 1, N3b)."""
import argparse
import base64
import importlib.util
import json
import logging
import pathlib
import sys
import tempfile
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


identity_module = _load("conduit_push.room_identity", ROOT / "room_identity.py")
envelope = _load("conduit_push.room_envelope", ROOT / "room_envelope.py")
mailbox_module = _load("conduit_push.room_mailbox", ROOT / "room_mailbox.py")
links_module = _load("conduit_push.room_links", ROOT / "room_links.py")

from test_room_mailbox import mailbox_url, needs_node  # noqa: E402,F401

_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_links", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

BASE = "/api/plugins/conduit_push"
DAY = 86400


class Clock:
    def __init__(self, now=None):
        self.now = time.time() if now is None else now

    def __call__(self):
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch, mailbox_url):
    """A Hermes installation's root home, with the test mailbox as its own."""
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)

    def make(name, clock=None):
        return links_module.Links(tmp_path / name, clock=clock or time.time)

    return make


def _run(*sides, passes=3):
    """Each side's loop, a few passes each, the way the dashboard runs it."""
    loops = [links_module.LinkLoop(side.root, clock=side.clock) for side in sides]
    try:
        for _ in range(passes):
            for loop in loops:
                loop.poll_once(wait=0)
    finally:
        for loop in loops:
            loop.stop()


def _linked(home):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    link_id = mona.accept(url, "Mona")["link"]["id"]
    _run(eric, mona)
    return eric, mona, link_id


def _words(side):
    return list(identity_module.load(side.root).fingerprint)


def _received(monkeypatch, kind="test.echo"):
    got = []
    monkeypatch.setitem(links_module.HANDLERS, kind, got.append)
    return got


# --- Linking ------------------------------------------------------------------


@needs_node
def test_an_invite_is_accepted_and_confirmed(home):
    eric, mona = home("eric"), home("mona")

    made = eric.invite("Eric")
    preview = mona.preview(made["url"])
    accepted = mona.accept(made["url"], "Mona")["link"]

    assert made["url"].startswith("conduit://link/")
    assert made["invite"]["name"] == "Eric" and made["invite"]["expires_at"] > time.time() + DAY - 60
    assert preview["name"] == "Eric" and preview["words"] == _words(eric)
    assert preview["expired"] is False and preview["own"] is False and preview["linked_as"] is None
    assert accepted["status"] == "waiting" and accepted["name"] == "Eric"
    assert [invite["id"] for invite in eric.view()["invites"]] == [made["invite"]["id"]]

    _run(eric, mona)

    [eric_side], [mona_side] = eric.view()["links"], mona.view()["links"]
    assert eric_side["id"] == mona_side["id"] == accepted["id"]
    assert eric_side["status"] == mona_side["status"] == "linked"
    assert (eric_side["name"], mona_side["name"]) == ("Mona", "Eric")
    assert eric_side["words"] == _words(mona) and mona_side["words"] == _words(eric)
    assert eric.view()["invites"] == []
    assert mona.preview(made["url"])["linked_as"] == accepted["id"]


@needs_node
def test_linked_sides_send_each_other_envelopes(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    got = _received(monkeypatch)

    eric.send(link_id, "test.echo", {"text": "Zoo on the 22nd?"})
    mona.send(link_id, "test.echo", {"text": "Yes!"})
    _run(eric, mona, passes=1)

    assert sorted((item.link_id, item.body["text"]) for item in got) == [(link_id, "Yes!"),
                                                                        (link_id, "Zoo on the 22nd?")]


@needs_node
def test_unlink_from_either_side_ends_it_for_both(home):
    eric, mona, link_id = _linked(home)

    assert mona.remove(link_id) == {"removed": True}
    assert mona.view()["links"] == []
    _run(eric, mona)

    assert eric.view()["links"] == []
    assert mona.remove(link_id) == {"removed": False}
    with pytest.raises(links_module.LinkError) as refused:
        eric.send(link_id, "test.echo", {})
    assert refused.value.code == "not_linked"

    eric, mona, link_id = _linked(home)
    eric.remove(link_id)
    _run(eric, mona)
    assert eric.view()["links"] == [] and mona.view()["links"] == []


@needs_node
def test_an_expired_invite_is_refused(home):
    eric, mona = home("eric", Clock(time.time() - 2 * DAY)), home("mona")
    url = eric.invite("Eric")["url"]

    assert mona.preview(url)["expired"] is True
    with pytest.raises(links_module.LinkError) as refused:
        mona.accept(url, "Mona")
    assert refused.value.code == "expired"
    assert mona.view()["links"] == []


@needs_node
def test_a_used_invite_is_refused(home):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    mona.accept(url, "Mona")
    _run(eric, mona)

    with pytest.raises(links_module.LinkError) as refused:
        home("lumine").accept(url, "Lumine")
    assert refused.value.code == "invite_used"
    assert [link["name"] for link in eric.view()["links"]] == ["Mona"]


@needs_node
def test_your_own_invite_is_refused(home):
    eric = home("eric")
    url = eric.invite("Eric")["url"]

    assert eric.preview(url)["own"] is True
    with pytest.raises(links_module.LinkError) as refused:
        eric.accept(url, "Eric")
    assert refused.value.code == "own_invite"


@pytest.mark.parametrize("url", [
    "https://rooms.milim.dev/link/abc",
    "conduit://link/",
    "conduit://link/not base64!",
    "conduit://link/" + base64.urlsafe_b64encode(b"[]").decode().rstrip("="),
    "conduit://link/" + base64.urlsafe_b64encode(json.dumps({"v": 2}).encode()).decode().rstrip("="),
    "conduit://link/" + base64.urlsafe_b64encode(json.dumps({
        "v": 1, "name": "Eric", "sign": "x", "seal": "y", "mailbox": "http://example.com", "invite": "i",
        "token": "t", "exp": 1}).encode()).decode().rstrip("="),
])
def test_an_invite_url_that_isnt_one_is_refused(tmp_path, url):
    with pytest.raises(links_module.LinkError) as refused:
        links_module.Links(tmp_path).preview(url)
    assert refused.value.code == "bad_invite"


@needs_node
def test_names_are_checked(home):
    eric = home("eric")
    for name in ("", "   ", "x" * 65, "Eric\nAdmin", 7):
        with pytest.raises(links_module.LinkError) as refused:
            eric.invite(name)
        assert refused.value.code == "bad_name"
    assert eric.invite("  Eric  ")["invite"]["name"] == "Eric"


@needs_node
def test_a_cancelled_invite_cant_be_used(home):
    eric, mona = home("eric"), home("mona")
    made = eric.invite("Eric")

    assert eric.cancel(made["invite"]["id"]) == {"cancelled": True}
    assert eric.cancel(made["invite"]["id"]) == {"cancelled": False}
    assert eric.view()["invites"] == []
    with pytest.raises(links_module.LinkError) as refused:
        mona.accept(made["url"], "Mona")
    assert refused.value.code == "invite_used"
    assert mona.view()["links"] == []


@needs_node
def test_pending_invites_are_capped(home):
    eric = home("eric")
    for _ in range(links_module.MAX_INVITES):
        eric.invite("Eric")
    with pytest.raises(links_module.LinkError) as refused:
        eric.invite("Eric")
    assert refused.value.code == "too_many_invites"


@needs_node
def test_someone_already_linked_is_told_so(home):
    eric, mona, link_id = _linked(home)

    with pytest.raises(links_module.LinkError) as refused:
        mona.accept(eric.invite("Eric")["url"], "Mona")
    assert (refused.value.code, refused.value.status) == ("already_linked", 409)
    assert [link["id"] for link in mona.view()["links"]] == [link_id]


@needs_node
def test_an_invite_naming_a_mailbox_on_its_own_computer_is_refused(home, monkeypatch):
    # Only a Hermes whose own mailbox is on this computer reaches one over
    # http, so nobody's invite points it at a service here.
    url = home("eric").invite("Eric")["url"]
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", "https://rooms.milim.dev")

    with pytest.raises(links_module.LinkError) as refused:
        home("mona").accept(url, "Mona")
    assert refused.value.code == "bad_invite"


@needs_node
def test_links_are_capped(home, monkeypatch, caplog):
    monkeypatch.setattr(links_module, "MAX_LINKS", 1)
    eric, mona, lumine = home("eric"), home("mona"), home("lumine")
    first, second = eric.invite("Eric")["url"], eric.invite("Eric")["url"]
    mona.accept(first, "Mona")
    lumine.accept(second, "Lumine")  # an invite made before Eric had a Link

    with caplog.at_level(logging.WARNING, logger=links_module.logger.name):
        _run(eric, mona, lumine)

    assert [link["name"] for link in eric.view()["links"]] == ["Mona"]
    assert "dropped an accept" in caplog.text
    assert [link["status"] for link in lumine.view()["links"]] == ["waiting"]
    for refused_call in (lambda: eric.invite("Eric"),
                         lambda: mona.accept(home("nahida").invite("Nahida")["url"], "Mona")):
        with pytest.raises(links_module.LinkError) as refused:
            refused_call()
        assert (refused.value.code, refused.value.status) == ("too_many_links", 409)


@needs_node
def test_an_inviter_who_unlinks_before_its_confirm_goes_ends_it_for_both(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    link_id = mona.accept(eric.invite("Eric")["url"], "Mona")["link"]["id"]
    real_send = mailbox_module.send

    def unreachable(*args, **kwargs):
        raise mailbox_module.MailboxError("unreachable", "Couldn't reach the mailbox")

    monkeypatch.setattr(mailbox_module, "send", unreachable)
    _run(eric, passes=1)  # the accept is read; the confirm can't go yet
    assert [link["status"] for link in eric.view()["links"]] == ["waiting"]
    eric.remove(link_id)
    monkeypatch.setattr(mailbox_module, "send", real_send)
    _run(eric, mona)

    assert eric.view()["links"] == [] and mona.view()["links"] == []
    stored = json.loads((eric.root / "conduit-rooms" / "links.json").read_text())
    assert stored["outbox"] == [] and stored["cleanup"] == []


@needs_node
def test_the_mailbox_links_name_stays_until_nothing_names_it(home, monkeypatch, caplog, mailbox_url):
    eric = home("eric")
    first = eric.invite("Eric")
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", "https://rooms.example.com")

    with caplog.at_level(logging.WARNING, logger=links_module.logger.name):
        second = eric.invite("Eric")
        shown = eric.view()["mailbox"]["url"]

    assert links_module.parse_invite(second["url"]).mailbox == mailbox_url == shown
    assert caplog.text.count("keeps the mailbox") == 1
    for made in (first, second):
        eric.cancel(made["invite"]["id"])
    # The invites' senders went from the mailbox they were at.
    assert mailbox_module.Mailbox(mailbox_url, identity_module.load(eric.root)).senders() == []
    assert json.loads((eric.root / "conduit-rooms" / "links.json").read_text())["cleanup"] == []
    assert eric.view()["mailbox"]["url"] == "https://rooms.example.com"


# --- The loop -----------------------------------------------------------------


def _until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_loop_with_nothing_to_do_makes_nothing_and_reads_as_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(links_module, "IDLE_S", 0.01)
    loop = links_module.start(tmp_path)
    try:
        time.sleep(0.1)
        assert links_module.reading(tmp_path) is True
        assert links_module.Links(tmp_path).view()["mailbox"]["reading"] is True
        assert not (tmp_path / "conduit-rooms").exists()
    finally:
        loop.stop()
    assert links_module.reading(tmp_path) is False


@needs_node
def test_a_running_loop_answers_an_invite_made_elsewhere(home, monkeypatch):
    # As the CLI makes one: in another process, with nothing to wake the loop.
    monkeypatch.setattr(links_module, "IDLE_S", 0.05)
    eric, mona = home("eric"), home("mona")
    loop = links_module.start(eric.root)
    try:
        url = links_module.Links(eric.root).invite("Eric")["url"]
        _until(lambda: links_module.lease_held(eric.root))
        mona.accept(url, "Mona")
        _until(lambda: [link["status"] for link in eric.view()["links"]] == ["linked"])
    finally:
        loop.stop()
    _run(mona, passes=1)
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


def test_waking_a_loop_has_it_look_now(tmp_path, monkeypatch):
    monkeypatch.setattr(links_module, "IDLE_S", 60)
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", "http://127.0.0.1:9")
    loop = links_module.start(tmp_path)
    try:
        time.sleep(0.05)  # resting, for a minute
        (tmp_path / "conduit-rooms").mkdir()
        (tmp_path / "conduit-rooms" / "links.json").write_text(json.dumps({
            **links_module._empty(), "cleanup": [{"sender": "i-x", "until": int(time.time()) + 60}]}))
        loop.wake()
        _until(lambda: links_module.lease_held(tmp_path), timeout=5)
    finally:
        loop.stop()


def test_two_loops_hold_one_lease(tmp_path):
    first, second = links_module.LinkLoop(tmp_path), links_module.LinkLoop(tmp_path)
    try:
        assert first.lease() is True
        assert second.lease() is False
        first.stop()
        assert second.lease() is True
    finally:
        first.stop()
        second.stop()


@needs_node
def test_an_inviter_offline_for_days_answers_when_back(home):
    clock = Clock()
    eric, mona = home("eric", clock), home("mona")
    url = eric.invite("Eric")["url"]
    mona.accept(url, "Mona")

    clock.now += 3 * DAY  # Eric's Hermes was off; the accept waited at the mailbox
    _run(eric, mona)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_invite_that_ran_out_is_swept_with_its_sender(home):
    clock = Clock()
    eric = home("eric", clock)
    eric.invite("Eric")
    assert [row["sender"][:2] for row in eric.mailbox().senders()] == ["i-"]

    clock.now += links_module.INVITE_TTL_S + 60
    _run(eric, passes=1)

    stored = json.loads((eric.root / "conduit-rooms" / "links.json").read_text())
    assert stored["invites"] == {} and stored["cleanup"] == []
    assert eric.mailbox().senders() == []


@needs_node
def test_the_same_envelope_twice_is_handled_once(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    got = _received(monkeypatch)

    eric.send(link_id, "test.echo", {"text": "once"}, message_id="m1")
    _run(mona, passes=1)
    eric.send(link_id, "test.echo", {"text": "once"}, message_id="m1")  # read and acked, so stored again
    _run(mona, passes=1)

    assert [item.body["text"] for item in got] == ["once"]


@needs_node
def test_an_envelope_of_a_kind_nobody_handles_is_dropped(home, caplog):
    eric, mona, link_id = _linked(home)

    eric.send(link_id, "room.someday", {"text": "for a later version"})
    with caplog.at_level(logging.INFO, logger=links_module.logger.name):
        _run(mona, passes=2)

    assert "room.someday" in caplog.text
    assert mona.mailbox().info()["pending"] == 0


def _one_loop(side, passes):
    loop = links_module.LinkLoop(side.root, clock=side.clock)
    try:
        for _ in range(passes):
            loop.poll_once(wait=0)
    finally:
        loop.stop()


@needs_node
def test_a_failing_handler_gets_the_envelope_again_and_the_next_ones_wait(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    calls = []

    def busy_once(item):
        calls.append(item.message_id)
        if calls == ["m1"]:
            raise RuntimeError("the room is busy")

    monkeypatch.setitem(links_module.HANDLERS, "test.echo", busy_once)
    eric.send(link_id, "test.echo", {"text": "first"}, message_id="m1")
    eric.send(link_id, "test.echo", {"text": "second"}, message_id="m2")
    _one_loop(mona, passes=2)

    assert calls == ["m1", "m1", "m2"]


@needs_node
def test_a_handler_that_keeps_failing_gives_the_envelope_up(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    calls = []

    def failing(item):
        calls.append(item.message_id)
        raise RuntimeError("the room is gone")

    monkeypatch.setitem(links_module.HANDLERS, "test.echo", failing)
    eric.send(link_id, "test.echo", {"text": "hello"}, message_id="m1")
    _one_loop(mona, passes=links_module.HANDLER_TRIES + 1)

    assert calls == ["m1"] * links_module.HANDLER_TRIES
    assert mona.mailbox().info()["pending"] == 0


def _forged_accept(eric_url, acceptor, *, invite_token=None, link_id="forged1"):
    """A link.accept as the acceptor's Hermes would seal it, with its fields as given."""
    invite = links_module.parse_invite(eric_url)
    me = identity_module.load_or_create(acceptor.root)
    body = {"sign": base64.urlsafe_b64encode(me.signing_public).decode().rstrip("="),
            "seal": base64.urlsafe_b64encode(me.sealing_public).decode().rstrip("="),
            "name": "Mallory", "mailbox": invite.mailbox, "token": mailbox_module.new_token(),
            "invite": invite.id, "invite_token": invite.token if invite_token is None else invite_token}
    data = envelope.seal(me, link=link_id, recipient_sign=invite.sign, recipient_seal=invite.seal,
                         message_id=f"accept.{link_id}", kind="link.accept", body=body)
    mailbox_module.send(invite.mailbox, identity_module.inbox_id(invite.sign), invite.token, data,
                        idempotency_key=f"accept.{link_id}")


@needs_node
def test_an_accept_without_the_invite_token_is_dropped(home):
    eric, mallory = home("eric"), home("mallory")
    url = eric.invite("Eric")["url"]

    _forged_accept(url, mallory, invite_token=mailbox_module.new_token())
    _run(eric)

    assert eric.view()["links"] == []
    assert len(eric.view()["invites"]) == 1  # still there for the person it was for


@needs_node
def test_an_accept_that_arrived_after_the_invite_expired_is_dropped(home):
    eric, mona = home("eric", Clock(time.time() - 2 * DAY)), home("mona", Clock(time.time() - 2 * DAY))
    url = eric.invite("Eric")["url"]
    mona.accept(url, "Mona")  # Mona's clock says the invite is good; the mailbox's says it ran out

    _run(eric, mona)

    assert eric.view()["links"] == []
    assert [link["status"] for link in mona.view()["links"]] == ["waiting"]


@needs_node
def test_a_link_still_waiting_after_eight_days_reads_unanswered(home):
    clock = Clock()
    eric, mona = home("eric"), home("mona", clock)
    mona.accept(eric.invite("Eric")["url"], "Mona")

    clock.now += 8 * DAY + 60

    assert [link["status"] for link in mona.view()["links"]] == ["unanswered"]


@needs_node
def test_a_refused_confirm_means_the_acceptor_gave_up(home):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    link_id = mona.accept(url, "Mona")["link"]["id"]
    mona.remove(link_id)  # before Eric's Hermes read the accept

    _run(eric, mona)

    assert eric.view()["links"] == [] and eric.view()["invites"] == []
    assert [row["sender"] for row in eric.mailbox().senders()] == []


@needs_node
def test_an_accept_from_someone_already_linked_replaces_the_old_link(home):
    eric, mona, old_id = _linked(home)
    # Mona's Hermes lost its side (its links.json went), then she linked again.
    (mona.root / "conduit-rooms" / "links.json").unlink()
    new_id = mona.accept(eric.invite("Eric")["url"], "Mona")["link"]["id"]

    _run(eric, mona)

    assert [link["id"] for link in eric.view()["links"]] == [new_id] != [old_id]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_a_lost_inbox_is_made_again_with_its_senders(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    got = _received(monkeypatch)
    eric.mailbox().delete()  # as the mailbox does after 90 idle days

    _run(eric, passes=1)
    mona.send(link_id, "test.echo", {"text": "still there?"})
    _run(eric, passes=1)

    assert [item.body["text"] for item in got] == ["still there?"]


@needs_node
def test_a_remove_that_cant_go_now_goes_later(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    real_send = mailbox_module.send

    def unreachable(*args, **kwargs):
        raise mailbox_module.MailboxError("unreachable", "Couldn't reach the mailbox")

    monkeypatch.setattr(mailbox_module, "send", unreachable)
    mona.remove(link_id)
    monkeypatch.setattr(mailbox_module, "send", real_send)
    _run(eric, mona)

    assert eric.view()["links"] == []


@needs_node
def test_tokens_and_keys_never_leave_the_store(home, caplog):
    with caplog.at_level(logging.DEBUG):
        eric, mona = home("eric"), home("mona")
        made = eric.invite("Eric")
        answers = [mona.preview(made["url"]), mona.accept(made["url"], "Mona")]
        _run(eric, mona)
        answers += [eric.view(), mona.view()]

    secrets = []
    for side in (eric, mona):
        stored = json.loads((side.root / "conduit-rooms" / "links.json").read_text())
        for link in stored["links"].values():
            secrets += [link["send_token"], link["my_token"]]
        keys = json.loads((side.root / "conduit-rooms" / "identity.json").read_text())
        secrets += [keys["sign_key"], keys["seal_key"]]
    secrets.append(links_module.parse_invite(made["url"]).token)
    shown = json.dumps(answers) + caplog.text
    assert all(secret and secret not in shown for secret in secrets)


def test_the_store_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", "http://127.0.0.1:9")
    links = links_module.Links(tmp_path)
    with pytest.raises(links_module.LinkError):
        links.invite("Eric")  # no mailbox there: the invite is taken back

    path = tmp_path / "conduit-rooms" / "links.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["invites"] == {}


# --- Routes -------------------------------------------------------------------


@pytest.fixture
def routes(tmp_path, monkeypatch):
    """Each installation's links routes, as its dashboard serves them, with
    its loop driven by hand (``poll``) rather than in a thread."""
    dashboard_links = api._plugin_package_module("room_links")
    loops, current = {}, {}

    def start(root):
        loop = loops.get(str(root))
        if loop is None:
            loop = loops[str(root)] = dashboard_links.LinkLoop(root)
        return loop

    monkeypatch.setattr(dashboard_links, "start", start)
    monkeypatch.setattr(api, "_calls_home", lambda: current["home"])
    monkeypatch.setattr(api._room_links_limiter, "_mints", {})
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)

    class Installation:
        def __init__(self, name):
            self.root = tmp_path / name

        def call(self, method, path, **kwargs):
            current["home"] = self.root
            return client.request(method, f"{BASE}{path}", **kwargs)

        def poll(self):
            start(self.root).poll_once(wait=0)

    yield Installation
    for loop in loops.values():
        loop.stop()


@needs_node
def test_routes_link_two_installations(routes, monkeypatch, mailbox_url):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    eric, mona = routes("eric"), routes("mona")
    assert eric.call("GET", "/rooms/identity").json() == {"ok": True, "words": None, "crypto": True}

    made = eric.call("POST", "/rooms/links/invites", json={"name": "Eric"})
    assert made.status_code == 200 and made.headers["cache-control"] == "no-store"
    url, words = made.json()["url"], eric.call("GET", "/rooms/identity").json()["words"]
    preview = mona.call("POST", "/rooms/links/preview", json={"url": url}).json()
    accepted = mona.call("POST", "/rooms/links", json={"url": url, "name": "Mona"}).json()["link"]
    for side in (eric, mona, eric, mona):
        side.poll()

    assert len(words) == 4 and (preview["name"], preview["words"], preview["own"]) == ("Eric", words, False)
    assert accepted["status"] == "waiting"
    eric_side, mona_side = eric.call("GET", "/rooms/links").json(), mona.call("GET", "/rooms/links").json()
    assert [(link["name"], link["status"]) for link in eric_side["links"]] == [("Mona", "linked")]
    assert [(link["name"], link["status"], link["words"]) for link in mona_side["links"]] == [
        ("Eric", "linked", words)]
    assert eric_side["mailbox"] == {"url": mailbox_url, "reading": True}

    removed = mona.call("DELETE", "/rooms/links", params={"link_id": accepted["id"]})
    eric.poll()
    assert removed.json() == {"ok": True, "removed": True}
    assert eric.call("GET", "/rooms/links").json()["links"] == []


@needs_node
def test_routes_cancel_an_invite(routes, monkeypatch, mailbox_url):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    eric = routes("eric")
    invite_id = eric.call("POST", "/rooms/links/invites", json={"name": "Eric"}).json()["invite"]["id"]

    assert eric.call("DELETE", "/rooms/links/invites", params={"invite_id": invite_id}).json() == {
        "ok": True, "cancelled": True}
    assert eric.call("GET", "/rooms/links").json()["invites"] == []


@pytest.mark.parametrize("method,path,body,code", [
    ("POST", "/rooms/links/invites", {"name": ""}, "bad_name"),
    ("POST", "/rooms/links/invites", {}, "bad_name"),
    ("POST", "/rooms/links/preview", {"url": "https://rooms.milim.dev/link/abc"}, "bad_invite"),
    ("POST", "/rooms/links", {"url": "conduit://link/x", "name": "Mona"}, "bad_invite"),
])
def test_routes_refuse_with_the_reasons_code(routes, method, path, body, code):
    refused = routes("eric").call(method, path, json=body)

    assert refused.status_code == 400 and refused.headers["cache-control"] == "no-store"
    assert refused.json()["code"] == code and refused.json()["detail"]


@pytest.mark.parametrize("path", ["/rooms/links/invites", "/rooms/links/preview", "/rooms/links"])
def test_routes_refuse_a_body_that_isnt_an_object(routes, path):
    refused = routes("eric").call("POST", path, json=["Eric"])
    assert refused.status_code == 400 and refused.headers["cache-control"] == "no-store"


def test_links_routes_answer_sixty_requests_a_minute(routes, monkeypatch):
    # A screen waiting on a Link's confirmation asks every few seconds.
    now = [1000.0]
    monkeypatch.setattr(api._room_links_limiter, "clock", lambda: now[0])
    eric = routes("eric")
    for _ in range(60):
        assert eric.call("GET", "/rooms/links").status_code == 200

    refused = eric.call("POST", "/rooms/links/invites", json={"name": "Eric"})
    assert refused.status_code == 429 and refused.headers["cache-control"] == "no-store"
    assert refused.json()["detail"] == "Too many link requests; try again shortly"
    now[0] += api.ROOM_CHOICES_WINDOW_S
    assert eric.call("GET", "/rooms/links").status_code == 200


def test_unlinking_or_cancelling_what_isnt_there_says_so(routes):
    eric = routes("eric")

    assert eric.call("DELETE", "/rooms/links", params={"link_id": "nope"}).json() == {"ok": True, "removed": False}
    assert eric.call("DELETE", "/rooms/links/invites", params={"invite_id": "nope"}).json() == {
        "ok": True, "cancelled": False}


def test_links_routes_start_the_installations_loop_and_wake_it(tmp_path, monkeypatch):
    dashboard_links = api._plugin_package_module("room_links")
    seen = []

    class Loop:
        def wake(self):
            seen.append("woken")

    monkeypatch.setattr(dashboard_links, "start", lambda root: seen.append(root) or Loop())
    (tmp_path / "profiles" / "work").mkdir(parents=True)
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path / "profiles" / "work")
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)

    assert TestClient(app).get(f"{BASE}/rooms/links").status_code == 200
    assert seen == [tmp_path, "woken"]


def test_the_loop_starts_where_the_dashboard_loads_the_routes(monkeypatch):
    started = []
    dashboard_links = api._plugin_package_module("room_links")
    monkeypatch.setattr(dashboard_links, "start", started.append)

    api._start_room_links()
    assert started == []  # tests, or a plugin host process: no dashboard here
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", types.ModuleType("hermes_cli.web_server"))
    api._start_room_links()
    assert len(started) == 1


def test_room_links_is_a_listed_capability(routes):
    assert "room-links" in routes("eric").call("GET", "/capabilities").json()["capabilities"]


# --- CLI ------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    module = _load("conduit_push.cli", ROOT / "cli.py")
    current = {"home": tmp_path / "eric"}
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: current["home"])
    monkeypatch.setattr(module, "LINKS_READING_WAIT_S", 0)

    def run(home, action, target="", **values):
        current["home"] = tmp_path / home
        return module.dispatch(argparse.Namespace(conduit_push_action="links", links_action=action, target=target,
                                                  **{"name": "", "yes": False, **values}))

    return run


def _url_in(text):
    return next(word for word in text.split() if word.startswith("conduit://link/"))


@needs_node
def test_cli_links_two_installations(cli, monkeypatch, mailbox_url, tmp_path, capsys):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    assert cli("eric", "invite", name="Eric") == 0
    made = capsys.readouterr().out
    url, words = _url_in(made), " ".join(identity_module.load(tmp_path / "eric").fingerprint)
    assert words in made and "Nothing on this Hermes reads" in made

    assert cli("mona", "preview", url) == 0
    assert "From Eric" in (shown := capsys.readouterr().out) and words in shown
    assert cli("mona", "accept", url, name="Mona") == 0
    assert "once their Hermes confirms" in capsys.readouterr().out
    eric, mona = links_module.Links(tmp_path / "eric"), links_module.Links(tmp_path / "mona")
    _run(eric, mona)

    assert cli("eric", "list") == 0
    listed = capsys.readouterr().out
    assert "Mona" in listed and "linked" in listed
    link_id = eric.view()["links"][0]["id"]
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli("eric", "remove", link_id) == 0
    assert "Cancelled" in capsys.readouterr().out and eric.view()["links"]
    assert cli("eric", "remove", link_id, yes=True) == 0
    assert "Unlinked Mona" in capsys.readouterr().out
    _run(mona)
    assert eric.view()["links"] == [] and mona.view()["links"] == []


@needs_node
def test_cli_says_nothing_of_the_mailbox_while_a_loop_reads_it(cli, monkeypatch, mailbox_url, tmp_path, capsys):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    loop = links_module.LinkLoop(tmp_path / "eric")
    try:
        assert loop.lease()
        assert cli("eric", "invite", name="Eric") == 0
    finally:
        loop.stop()
    assert "Nothing on this Hermes reads" not in capsys.readouterr().out


@needs_node
def test_cli_cancels_an_invite(cli, monkeypatch, mailbox_url, tmp_path, capsys):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    cli("eric", "invite", name="Eric")
    invite_id = links_module.Links(tmp_path / "eric").view()["invites"][0]["id"]
    capsys.readouterr()

    assert cli("eric", "cancel", invite_id) == 0
    assert "Cancelled" in capsys.readouterr().out
    assert cli("eric", "cancel", invite_id) == 1
    assert links_module.Links(tmp_path / "eric").view()["invites"] == []


def test_cli_says_what_it_couldnt_do(cli, capsys):
    assert cli("eric", "invite") == 2
    assert "--name" in capsys.readouterr().out
    assert cli("eric", "preview") == 2
    assert capsys.readouterr().out.startswith("Usage:")
    assert cli("eric", "accept", "conduit://link/x", name="Mona") == 1
    assert capsys.readouterr().out.startswith("Couldn't accept it:")
    assert cli("eric", "remove") == 2
    assert cli("eric", "remove", "nope", yes=True) == 1
    assert "No Link" in capsys.readouterr().out


def test_the_links_command_parses(tmp_path):
    module = _load("conduit_push.cli", ROOT / "cli.py")
    parser = argparse.ArgumentParser()
    module.register_cli(parser)

    args = parser.parse_args(["links", "accept", "conduit://link/abc", "--name", "Mona"])
    assert (args.conduit_push_action, args.links_action, args.target, args.name) == (
        "links", "accept", "conduit://link/abc", "Mona")
    assert parser.parse_args(["links", "remove", "abc", "--yes"]).yes is True
