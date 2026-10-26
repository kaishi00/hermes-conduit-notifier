"""Links between installations and the mailbox loop (designs/cross-owner-agents, layer 1, N3b)."""
import argparse
import base64
import concurrent.futures
import contextlib
import errno
import importlib.util
import json
import logging
import pathlib
import sys
import tempfile
import threading
import time
import types
import urllib.request

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

from test_room_mailbox import _resolving, mailbox_url, needs_node  # noqa: E402,F401

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
    assert made["words"] == _words(eric)
    assert preview["name"] == "Eric" and preview["words"] == _words(eric)
    assert preview["mailbox"] == mailbox_module.url_host(eric.view()["mailbox"]["url"])
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
def test_links_are_capped_and_a_waiting_invite_keeps_its_links_place(home, monkeypatch):
    # So an accept for one of this Hermes' invites always finds room.
    monkeypatch.setattr(links_module, "MAX_LINKS", 1)
    eric, mona, lumine = home("eric"), home("mona"), home("lumine")
    url = eric.invite("Eric")["url"]
    for refused_call in (lambda: eric.invite("Eric"),
                         lambda: eric.accept(lumine.invite("Lumine")["url"], "Eric")):
        with pytest.raises(links_module.LinkError) as refused:
            refused_call()
        assert (refused.value.code, refused.value.status) == ("too_many_links", 409)
    mona.accept(url, "Mona")

    _run(eric, mona)

    assert [link["name"] for link in eric.view()["links"]] == ["Mona"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]
    with pytest.raises(links_module.LinkError) as refused:
        mona.accept(home("nahida").invite("Nahida")["url"], "Mona")
    assert (refused.value.code, refused.value.status) == ("too_many_links", 409)


@needs_node
def test_an_invite_that_ran_out_keeps_its_links_place_until_its_accept_is_read(home, monkeypatch):
    # Its Hermes was off: an accept that came in time is still answered.
    monkeypatch.setattr(links_module, "MAX_LINKS", 1)
    clock = Clock()
    eric, mona = home("eric", clock), home("mona")
    mona.accept(eric.invite("Eric")["url"], "Mona")
    clock.now += links_module.INVITE_TTL_S + 60

    with pytest.raises(links_module.LinkError) as refused:
        eric.accept(home("nahida", Clock(clock.now)).invite("Nahida")["url"], "Eric")
    assert (refused.value.code, refused.value.status) == ("too_many_links", 409)
    _run(eric, mona)

    assert [(link["name"], link["status"]) for link in eric.view()["links"]] == [("Mona", "linked")]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_a_refusal_for_the_link_count_says_how_many_places_the_list_doesnt_show(home, monkeypatch):
    # An invite that ran out keeps its place until the loop has read what came for it, and a Link
    # unlinked while it waited keeps its own until its confirm comes.
    monkeypatch.setattr(links_module, "MAX_LINKS", 2)
    clock = Clock()
    eric, mona = home("eric", clock), home("mona", Clock(clock.now))
    eric.invite("Eric")
    eric.remove(eric.accept(mona.invite("Mona")["url"], "Eric")["link"]["id"])
    clock.now += links_module.INVITE_TTL_S + 60
    assert (eric.view()["links"], eric.view()["invites"]) == ([], [])

    with pytest.raises(links_module.LinkError) as refused:
        eric.invite("Eric")
    assert refused.value.code == "too_many_links"
    assert "2 places are held by invites that ran out or Links unlinked while they waited" in str(refused.value)


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


def test_a_wake_as_a_rest_ends_isnt_lost(tmp_path):
    loop = links_module.LinkLoop(tmp_path)

    class WokenAsTheRestEnds(threading.Event):
        def wait(self, timeout=None):
            self.set()  # a route's wake, just after the wait timed out
            return False

    loop._woken = WokenAsTheRestEnds()
    loop._rest(0)

    assert loop._woken.is_set()  # so the next rest ends at once


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
    # Once a read that began after it ran out by the mailbox's clock too, which can be a few minutes
    # behind this host's, has found nothing more for it.
    clock = Clock()
    eric = home("eric", clock)
    eric.invite("Eric")
    assert [row["sender"][:2] for row in eric.mailbox().senders()] == ["i-"]

    clock.now += links_module.INVITE_TTL_S + 60
    _run(eric, passes=1)
    assert len(eric._store.read()["invites"]) == 1
    clock.now += mailbox_module.SIGNATURE_WINDOW_S
    _run(eric, passes=1)

    stored = json.loads((eric.root / "conduit-rooms" / "links.json").read_text())
    assert stored["invites"] == {} and stored["cleanup"] == []
    assert eric.mailbox().senders() == []


@needs_node
@pytest.mark.parametrize("before_read, during_pass", [
    (0, links_module.INVITE_TTL_S + 3600), (links_module.INVITE_TTL_S + 200, 0),
], ids=["a-pass-that-runs-past-it", "a-mailbox-clock-minutes-behind"])
def test_an_accept_that_comes_in_time_after_a_pass_read_the_inbox_is_answered(home, monkeypatch, before_read,
                                                                               during_pass):
    # It comes while the pass makes its mailbox calls, which can take minutes, in time by the mailbox's
    # clock, which can be a few minutes behind this host's: the next pass reads it.
    clock = Clock()
    eric, mona = home("eric", clock), home("mona")
    url = eric.invite("Eric")["url"]
    clock.now += before_read
    real_flush, coming = links_module.Links.flush, [True]

    def flush(self):
        if self.root == eric.root and coming:
            coming.clear()
            mona.accept(url, "Mona")
            clock.now += during_pass
        real_flush(self)

    monkeypatch.setattr(links_module.Links, "flush", flush)
    _run(eric, passes=1)
    _run(eric, mona)

    assert [(link["name"], link["status"]) for link in eric.view()["links"]] == [("Mona", "linked")]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


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


def _forged_accept(eric_url, acceptor, *, invite_token=None, link_id="forged1", mailbox=None):
    """A link.accept as the acceptor's Hermes would seal it, with its fields as given."""
    invite = links_module.parse_invite(eric_url)
    me = identity_module.load_or_create(acceptor.root)
    body = {"sign": base64.urlsafe_b64encode(me.signing_public).decode().rstrip("="),
            "seal": base64.urlsafe_b64encode(me.sealing_public).decode().rstrip("="),
            "name": "Mallory", "mailbox": mailbox or invite.mailbox, "token": mailbox_module.new_token(),
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
def test_an_accept_whose_invite_token_isnt_one_is_dropped_and_the_next_still_links(home, monkeypatch):
    # "\ud800" in its JSON reads as half a character, which nothing can compare.
    eric, mallory, mona = home("eric"), home("mallory"), home("mona")
    url = eric.invite("Eric")["url"]
    escaped = types.SimpleNamespace(
        loads=json.loads, dumps=lambda value, **options: json.dumps(value, **dict(options, ensure_ascii=True)))
    with monkeypatch.context() as patched:
        patched.setattr(envelope, "json", escaped)
        _forged_accept(url, mallory, invite_token="\ud800")
    mona.accept(url, "Mona")

    _run(eric, mona)

    assert [link["name"] for link in eric.view()["links"]] == ["Mona"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]
    assert eric.mailbox().info()["pending"] == 0


@needs_node
def test_an_envelope_that_trips_up_its_handling_is_dropped_and_the_next_ones_go(home, monkeypatch, caplog):
    eric, mona, lisa = home("eric"), home("mona"), home("lisa")
    mona.accept(eric.invite("Eric")["url"], "Mona")
    lisa.accept(eric.invite("Eric")["url"], "Lisa")
    real_fields = links_module._accept_fields

    def trips(body):
        if body["name"] == "Mona":
            raise TypeError("something this plugin didn't expect")
        return real_fields(body)

    monkeypatch.setattr(links_module, "_accept_fields", trips)
    with caplog.at_level(logging.WARNING):
        _run(eric, mona, lisa)

    assert [link["name"] for link in eric.view()["links"]] == ["Lisa"]
    assert [link["status"] for link in lisa.view()["links"]] == ["linked"]
    assert eric.mailbox().info()["pending"] == 0
    assert "Linked Rooms dropped a link.accept it couldn't handle (TypeError)" in caplog.text
    assert "didn't expect" not in caplog.text  # what's in an envelope stays out of the log


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
def test_an_acceptor_who_unlinks_before_the_accept_is_read_ends_it_for_both(home):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    link_id = mona.accept(url, "Mona")["link"]["id"]
    mona.remove(link_id)  # before Eric's Hermes read the accept

    _run(eric, mona)

    assert eric.view()["links"] == [] and eric.view()["invites"] == []
    assert mona._store.read()["links"] == {}
    assert eric.mailbox().senders() == [] and mona.mailbox().senders() == []


@needs_node
def test_an_acceptor_who_unlinks_while_the_confirm_waits_ends_it_for_both(home):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    link_id = mona.accept(url, "Mona")["link"]["id"]
    _run(eric, passes=1)  # Eric's Hermes answers; the confirm waits in Mona's inbox
    assert [link["status"] for link in eric.view()["links"]] == ["linked"]

    assert mona.remove(link_id) == {"removed": True}
    assert mona.view()["links"] == [] and mona.get(link_id) is None
    assert mona.preview(url)["linked_as"] is None and mona.remove(link_id) == {"removed": False}
    _run(mona, eric)

    assert eric.view()["links"] == [] and mona._store.read()["links"] == {}
    assert eric.mailbox().senders() == [] and mona.mailbox().senders() == []


@needs_node
def test_an_unlinked_accept_nobody_answers_goes_after_the_wait(home):
    clock = Clock()
    eric, mona = home("eric"), home("mona", clock)
    link_id = mona.accept(eric.invite("Eric")["url"], "Mona")["link"]["id"]
    mona.remove(link_id)  # and Eric's Hermes stays off

    _run(mona, passes=1)
    assert link_id in mona._store.read()["links"]  # for the confirm, if it comes
    clock.now += 8 * DAY + 60
    _run(mona, passes=1)

    assert mona._store.read()["links"] == {} and mona.mailbox().senders() == []


@needs_node
def test_an_accept_that_never_went_is_unlinked_at_once(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    _accept_cut_off(mona, url, monkeypatch)
    link_id = next(iter(mona._store.read()["links"]))

    mona.remove(link_id)

    assert mona._store.read()["links"] == {} and mona._store.read()["outbox"] == []


@needs_node
def test_an_accept_unlinked_just_before_it_goes_doesnt_go(home, monkeypatch):
    # The unlink found it not yet tried, so it took the Link back at once.
    eric, mona = home("eric"), home("mona")
    _accept_cut_off(mona, eric.invite("Eric")["url"], monkeypatch)
    state = mona._store.read()
    link_id, [item] = next(iter(state["links"])), state["outbox"]
    sent = _sends(monkeypatch)

    mona.remove(link_id)
    mona._send_outgoing(identity_module.load(mona.root), item, mona.mailbox().url)

    assert sent == []


@needs_node
def test_accepting_an_invite_again_after_unlinking_it_links(home):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    link_id = mona.accept(url, "Mona")["link"]["id"]
    mona.remove(link_id)

    again = mona.accept(url, "Mona")["link"]
    _run(eric, mona)

    assert (again["id"], again["status"]) == (link_id, "waiting")
    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


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
def test_invites_that_cross_leave_both_sides_with_the_same_links(home):
    # Each accepts the other's invite before either Hermes reads its inbox.
    eric, mona = home("eric"), home("mona")
    eric_url, mona_url = eric.invite("Eric")["url"], mona.invite("Mona")["url"]
    mona.accept(eric_url, "Mona")
    eric.accept(mona_url, "Eric")

    _run(eric, mona)

    eric_side = sorted((link["id"], link["status"]) for link in eric.view()["links"])
    mona_side = sorted((link["id"], link["status"]) for link in mona.view()["links"])
    assert eric_side == mona_side
    assert [status for _, status in eric_side] == ["linked", "linked"]


@needs_node
def test_a_crossing_accept_keeps_a_link_unlinked_while_it_waits_so_its_confirm_is_answered(home):
    eric, mona = home("eric"), home("mona")
    eric_url, mona_url = eric.invite("Eric")["url"], mona.invite("Mona")["url"]
    unlinked = eric.accept(mona_url, "Eric")["link"]["id"]
    mona.accept(eric_url, "Mona")
    eric.remove(unlinked)

    _run(eric, mona)

    eric_side = [(link["id"], link["status"]) for link in eric.view()["links"]]
    assert eric_side == [(link["id"], link["status"]) for link in mona.view()["links"]]
    assert [status for _, status in eric_side] == ["linked"] and eric_side[0][0] != unlinked
    assert unlinked not in eric._store.read()["links"] and unlinked not in mona._store.read()["links"]


@needs_node
def test_the_acceptor_takes_the_inviters_mailbox_from_its_confirm(home, monkeypatch, mailbox_url):
    # The invite's mailbox was changed on its way, which the words don't show, and that mailbox
    # passed the accept on. The confirm, signed by the inviter's Hermes, says which is its own.
    eric, mona = home("eric"), home("mona")
    link_id = mona.accept(eric.invite("Eric")["url"], "Mona")["link"]["id"]
    with mona._store.update() as state:
        state["links"][link_id]["mailbox"] = "https://mailbox.example"
    got = _received(monkeypatch)

    _run(eric, mona)

    assert mona._store.read()["links"][link_id]["mailbox"] == mailbox_url
    mona.send(link_id, "test.echo", {"text": "through yours"})
    _run(eric, passes=1)
    assert [item.body["text"] for item in got] == ["through yours"]


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
def test_a_mailbox_that_doesnt_answer_gets_one_call_a_pass(home, monkeypatch):
    # Each call waits out its timeout while the send lock is held.
    eric, mona, _ = _linked(home)
    lisa = home("lisa")
    lisa.accept(eric.invite("Eric")["url"], "Lisa")
    _run(eric, lisa)
    calls = []

    def unreachable(*args, **kwargs):
        calls.append(kwargs.get("idempotency_key"))
        raise mailbox_module.MailboxError("unreachable", "Couldn't reach the mailbox")

    with monkeypatch.context() as patched:
        patched.setattr(links_module.Links, "_flush_quietly", lambda self: None)
        for link in eric.view()["links"]:
            eric.remove(link["id"])
    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", unreachable)
        eric.flush()
    assert len(calls) == 1
    assert [item["kind"] for item in eric._store.read()["outbox"]] == ["link.remove", "link.remove"]

    _run(eric, mona, lisa)

    assert mona.view()["links"] == [] and lisa.view()["links"] == []


@needs_node
@pytest.mark.parametrize("code,status,kept", [
    ("envelope_too_large", 413, False), ("bad_request", 0, False), ("not_found", 404, True),
    ("rate_limited", 429, True), ("unreachable", 0, True), ("internal_error", 500, True)])
def test_only_an_envelope_that_can_go_later_is_tried_again(home, monkeypatch, code, status, kept):
    # An inbox the mailbox lost (not_found) is made again by its Hermes.
    eric, mona, link_id = _linked(home)

    def refused(*args, **kwargs):
        raise mailbox_module.MailboxError(code, "Refused", status)

    monkeypatch.setattr(mailbox_module, "send", refused)
    eric.remove(link_id)  # its link.remove waits in the outbox
    eric.flush()

    assert [item["kind"] for item in eric._store.read()["outbox"]] == (["link.remove"] if kept else [])


@needs_node
@pytest.mark.parametrize("code,status,kept", [
    ("bad_request", 0, False), ("rate_limited", 429, True), ("internal_error", 500, True)])
def test_only_a_sender_that_can_go_later_is_deleted_again(home, monkeypatch, code, status, kept):
    eric, mona, link_id = _linked(home)

    def refused(self, sender):
        raise mailbox_module.MailboxError(code, "Refused", status)

    monkeypatch.setattr(mailbox_module.Mailbox, "delete_sender", refused)
    eric.remove(link_id)
    eric.flush()

    assert bool(eric._store.read()["cleanup"]) is kept


@needs_node
@pytest.mark.parametrize("seal", ["not a key!", "AAAA"])
def test_an_envelope_that_cant_be_made_is_dropped_and_the_rest_go(home, monkeypatch, seal):
    # A hand-edited links.json: the key an unlink was to go with can't make its envelope.
    eric, mona, link_id = _linked(home)
    lisa = home("lisa")
    lisa_link = lisa.accept(eric.invite("Eric")["url"], "Lisa")["link"]["id"]
    _run(eric, lisa)
    with monkeypatch.context() as patched:
        patched.setattr(links_module.Links, "_flush_quietly", lambda self: None)
        eric.remove(link_id)
        eric.remove(lisa_link)
    with eric._store.update() as state:
        next(item for item in state["outbox"] if item["link"] == link_id)["seal"] = seal

    _run(eric, mona, lisa)

    assert eric._store.read()["outbox"] == []
    assert lisa.view()["links"] == []
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]  # its unlink couldn't be made


@needs_node
@pytest.mark.parametrize("error", [envelope.EnvelopeError("bad_key", "A key that can't seal"), OSError("disk")])
def test_an_unlink_whose_envelope_cant_go_now_still_unlinks(home, monkeypatch, error):
    eric, mona, link_id = _linked(home)

    def flush(self):
        raise error

    monkeypatch.setattr(links_module.Links, "flush", flush)

    assert eric.remove(link_id) == {"removed": True}
    assert eric.view()["links"] == []


def _refusing(code, status=0):
    def refuse(*args, **kwargs):
        raise mailbox_module.MailboxError(code, "Refused", status)

    return refuse


@needs_node
def test_senders_to_remove_go_before_new_ones_are_put(home, monkeypatch):
    # An inbox with as many senders as the mailbox keeps takes a new one once old ones go.
    eric, mona, link_id = _linked(home)
    invite_id = eric.invite("Eric")["invite"]["id"]
    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module.Mailbox, "delete_sender", _refusing("unreachable"))
        eric.cancel(invite_id)
    with eric._store.update() as state:
        state["links"][link_id]["sender_up"] = False
    monkeypatch.setattr(mailbox_module.Mailbox, "put_sender", _refusing("too_many_senders", 409))

    with pytest.raises(mailbox_module.MailboxError):
        eric.flush()

    assert eric._store.read()["cleanup"] == []
    assert [row["sender"] for row in eric.mailbox().senders()] == [f"l-{link_id}"]


@needs_node
def test_an_invite_whose_sender_wasnt_answered_takes_the_sender_back(home, monkeypatch):
    # The mailbox may have put it.
    eric = home("eric")
    real_put = mailbox_module.Mailbox.put_sender

    def put_unanswered(self, sender, token):
        real_put(self, sender, token)
        raise mailbox_module.MailboxError("bad_answer", "The mailbox answered oddly")

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module.Mailbox, "put_sender", put_unanswered)
        with pytest.raises(links_module.LinkError):
            eric.invite("Eric")
    [sender] = [item["sender"] for item in eric._store.read()["cleanup"]]
    assert sender.startswith("i-")

    eric.flush()
    assert eric._store.read()["cleanup"] == []
    assert eric.mailbox().senders() == []


@needs_node
def test_an_invite_the_mailbox_refused_removes_the_senders_still_to_go(home, monkeypatch):
    # An inbox with as many senders as the mailbox keeps takes the next one
    # once those have gone, as it does after a refused accept.
    eric = home("eric")
    old = eric.invite("Eric")["invite"]["id"]

    def unreachable(*args, **kwargs):
        raise mailbox_module.MailboxError("unreachable", "Couldn't reach the mailbox")

    def full(self, sender, token):
        raise mailbox_module.MailboxError("too_many_senders", "This inbox has as many links as the mailbox allows",
                                          409)

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module.Mailbox, "delete_sender", unreachable)
        eric.cancel(old)
    assert [item["sender"] for item in eric._store.read()["cleanup"]] == [f"i-{old}"]
    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module.Mailbox, "put_sender", full)
        with pytest.raises(links_module.LinkError) as refused:
            eric.invite("Eric")

    assert refused.value.code == "too_many_senders"
    assert eric._store.read()["cleanup"] == []
    assert eric.mailbox().senders() == []
    assert eric.invite("Eric")["invite"]["id"]


class _CutOff(BaseException):
    """A process stopping mid-call: nothing handles it, as nothing handles a kill."""


def _accept_cut_off(side, url, monkeypatch):
    """``side`` accepts ``url`` and stops before its accept is sent, as it
    puts its sender."""
    def put_sender(*args, **kwargs):
        raise _CutOff()

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module.Mailbox, "put_sender", put_sender)
        with pytest.raises(_CutOff):
            side.accept(url, "Mona")


def _accept_cut_off_after_it_went(side, url, monkeypatch):
    """``side`` accepts ``url`` and stops just after the mailbox took its
    accept, before the store says so."""
    real_send = mailbox_module.send

    def send(*args, **kwargs):
        real_send(*args, **kwargs)
        if str(kwargs.get("idempotency_key")).startswith("accept."):
            raise _CutOff()

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", send)
        with pytest.raises(_CutOff):
            side.accept(url, "Mona")


def _sends(monkeypatch):
    """The idempotency keys of what's sent from now on."""
    sent, real_send = [], mailbox_module.send

    def send(*args, **kwargs):
        sent.append(kwargs.get("idempotency_key"))
        return real_send(*args, **kwargs)

    monkeypatch.setattr(mailbox_module, "send", send)
    return sent


@needs_node
def test_an_accept_cut_off_before_it_went_goes_on_the_next_pass(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    _accept_cut_off(mona, eric.invite("Eric")["url"], monkeypatch)

    _run(mona, eric)

    [eric_side], [mona_side] = eric.view()["links"], mona.view()["links"]
    assert eric_side["id"] == mona_side["id"]
    assert eric_side["status"] == mona_side["status"] == "linked"


@needs_node
def test_accepting_an_invite_again_finishes_an_accept_cut_off_before(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    _accept_cut_off(mona, url, monkeypatch)
    [waiting] = mona.view()["links"]

    again = mona.accept(url, "Mona")["link"]
    _run(eric, mona)

    assert (again["id"], again["status"]) == (waiting["id"], "waiting")
    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_invite_accepted_twice_sends_one_accept(home, monkeypatch):
    # A second tap, before the inviter's Hermes answered the first.
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    first = mona.accept(url, "Mona")["link"]
    sent = _sends(monkeypatch)

    second = mona.accept(url, "Mona")["link"]
    _run(eric, mona)

    assert second["id"] == first["id"]
    assert [key for key in sent if key.startswith("accept.")] == []
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]
    with pytest.raises(links_module.LinkError) as refused:
        mona.accept(url, "Mona")
    assert refused.value.code == "already_linked"


@needs_node
def test_an_accept_cut_off_until_its_invite_ran_out_is_given_up(home, monkeypatch):
    clock = Clock()
    eric, mona = home("eric"), home("mona", clock=clock)
    _accept_cut_off(mona, eric.invite("Eric")["url"], monkeypatch)
    clock.now += DAY + 60
    sent = _sends(monkeypatch)

    _run(mona, eric)

    assert mona.view()["links"] == [] and eric.view()["links"] == []
    assert [key for key in sent if key.startswith("accept.")] == []
    assert json.loads((mona.root / "conduit-rooms" / "links.json").read_text())["outbox"] == []


@needs_node
def test_an_accept_sent_again_after_its_first_copy_went_keeps_its_link(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    _accept_cut_off_after_it_went(mona, eric.invite("Eric")["url"], monkeypatch)
    _run(eric, passes=1)  # confirmed, and the invite's sender gone with it

    mona.flush()  # sent again before the confirm is read: refused, as the invite is used
    _run(mona, passes=1)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_accepting_again_after_an_accept_that_went_keeps_its_link(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    _accept_cut_off_after_it_went(mona, url, monkeypatch)
    _run(eric, passes=1)

    again = mona.accept(url, "Mona")["link"]
    _run(mona, passes=1)

    assert again["status"] == "waiting"
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_accept_that_may_have_gone_waits_past_its_invite(home, monkeypatch):
    # Its inviter was offline: the accept came in time, and the confirm comes later.
    clock = Clock()
    eric, mona = home("eric"), home("mona", clock=clock)
    _accept_cut_off_after_it_went(mona, eric.invite("Eric")["url"], monkeypatch)
    clock.now += DAY + 60
    _run(mona, passes=1)
    assert [link["status"] for link in mona.view()["links"]] == ["waiting"]

    _run(eric, mona)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_accept_answered_before_it_went_again_isnt_sent_again(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    _accept_cut_off_after_it_went(mona, eric.invite("Eric")["url"], monkeypatch)
    _run(eric, passes=1)
    sent = _sends(monkeypatch)

    _run(mona, passes=1)  # the confirm is read before the outbox is sent

    assert [key for key in sent if key.startswith("accept.")] == []
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_accept_the_mailbox_didnt_answer_waits_with_its_link(home, monkeypatch):
    # The mailbox took it and its answer was lost: the inviter's Hermes
    # answers it, so the Link stays for that confirm.
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    real_send = mailbox_module.send

    def no_answer(*args, **kwargs):
        real_send(*args, **kwargs)
        raise mailbox_module.MailboxError("unreachable", "Couldn't reach the mailbox")

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", no_answer)
        with pytest.raises(links_module.LinkError) as refused:
            mona.accept(url, "Mona")
    assert (refused.value.code, refused.value.status) == ("unreachable", 503)
    assert "while the invite lasts" in str(refused.value)
    assert [link["status"] for link in mona.view()["links"]] == ["waiting"]

    _run(eric, mona)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
@pytest.mark.parametrize("code,status", [("not_found", 404), ("internal_error", 500)])
def test_an_accept_the_mailbox_couldnt_take_yet_waits_with_its_link(home, monkeypatch, code, status):
    # As the loop's pass does: the inviter's inbox may be being made again.
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]

    def refused_send(*args, **kwargs):
        raise mailbox_module.MailboxError(code, "Refused", status)

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", refused_send)
        with pytest.raises(links_module.LinkError) as refused:
            mona.accept(url, "Mona")
    assert (refused.value.code, refused.value.status) == (code, 503)
    assert "while the invite lasts" in str(refused.value)
    assert [link["status"] for link in mona.view()["links"]] == ["waiting"]

    _run(eric, mona)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_accept_the_mailbox_failed_on_may_have_gone_and_waits_past_its_invite(home, monkeypatch):
    # A server error can come after the mailbox kept it.
    clock = Clock()
    eric, mona = home("eric"), home("mona", clock=clock)
    url = eric.invite("Eric")["url"]
    real_send = mailbox_module.send

    def kept_then_failed(*args, **kwargs):
        real_send(*args, **kwargs)
        raise mailbox_module.MailboxError("internal_error", "The mailbox failed", 500)

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", kept_then_failed)
        with pytest.raises(links_module.LinkError):
            mona.accept(url, "Mona")
    clock.now += DAY + 60
    _run(mona, passes=1)
    assert [link["status"] for link in mona.view()["links"]] == ["waiting"]

    _run(eric, mona)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_accept_refused_on_its_first_try_takes_its_link_back(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]

    def refused_send(*args, **kwargs):
        raise mailbox_module.MailboxError("envelope_too_large", "That's more than the mailbox takes", 413)

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", refused_send)
        with pytest.raises(links_module.LinkError) as refused:
            mona.accept(url, "Mona")

    assert (refused.value.code, refused.value.status) == ("envelope_too_large", 502)
    assert mona.view()["links"] == []
    assert json.loads((mona.root / "conduit-rooms" / "links.json").read_text())["outbox"] == []


@needs_node
def test_accepting_again_an_accept_refused_on_its_first_try_takes_its_link_back(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    _accept_cut_off(mona, url, monkeypatch)  # recorded, never sent

    def refused_send(*args, **kwargs):
        raise mailbox_module.MailboxError("bad_request", "The mailbox didn't take that", 400)

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", refused_send)
        with pytest.raises(links_module.LinkError) as refused:
            mona.accept(url, "Mona")

    assert refused.value.code == "bad_request"
    assert mona.view()["links"] == []
    assert json.loads((mona.root / "conduit-rooms" / "links.json").read_text())["outbox"] == []


@needs_node
def test_an_accept_the_mailbox_refused_each_time_goes_when_its_invite_runs_out(home, monkeypatch):
    # Refused, it never reached the inviter, so its Link doesn't wait for a confirm.
    clock = Clock()
    eric, mona = home("eric"), home("mona", clock=clock)
    _accept_cut_off(mona, eric.invite("Eric")["url"], monkeypatch)

    def not_found(*args, **kwargs):
        raise mailbox_module.MailboxError("not_found", "The mailbox doesn't have that", 404)

    with monkeypatch.context() as patched:
        patched.setattr(mailbox_module, "send", not_found)
        _run(mona, passes=1)
        assert [link["status"] for link in mona.view()["links"]] == ["waiting"]
        clock.now += DAY + 60
        _run(mona, passes=1)

    assert mona.view()["links"] == []
    assert json.loads((mona.root / "conduit-rooms" / "links.json").read_text())["outbox"] == []


@needs_node
def test_an_accept_and_the_loop_never_both_send_it(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]
    real_send = mailbox_module.send
    sent, sending, go_on = [], threading.Event(), threading.Event()

    def send(*args, **kwargs):
        sent.append(kwargs.get("idempotency_key"))
        if sent[-1].startswith("accept.") and not sending.is_set():
            sending.set()
            go_on.wait(10)
        return real_send(*args, **kwargs)

    monkeypatch.setattr(mailbox_module, "send", send)
    accepting = threading.Thread(target=mona.accept, args=(url, "Mona"))
    accepting.start()
    assert sending.wait(10)
    # The dashboard's loop, in the same installation, makes its pass meanwhile.
    flushing = threading.Thread(target=links_module.Links(mona.root).flush)
    flushing.start()
    flushing.join(0.5)
    waited = flushing.is_alive()
    go_on.set()
    accepting.join(10)
    flushing.join(10)

    assert waited
    assert len([key for key in sent if key.startswith("accept.")]) == 1


@contextlib.contextmanager
def _held_elsewhere(self):
    # The send lock a long pass holds, which Windows gives up on after a minute.
    raise OSError(errno.EDEADLK, "Resource deadlock avoided")
    yield


@needs_node
def test_an_invite_that_cant_take_the_send_lock_leaves_no_invite(home, monkeypatch):
    eric = home("eric")
    monkeypatch.setattr(links_module.Links, "_sending", _held_elsewhere)

    with pytest.raises(links_module.LinkError) as refused:
        eric.invite("Eric")

    assert (refused.value.code, refused.value.status) == ("busy", 503)
    assert eric.view()["invites"] == []


@needs_node
def test_an_accept_that_cant_take_the_send_lock_waits_for_the_loop(home, monkeypatch):
    # The pass holding it may be sending this very accept, so the Link stays.
    eric, mona = home("eric"), home("mona")
    url = eric.invite("Eric")["url"]

    with monkeypatch.context() as patched:
        patched.setattr(links_module.Links, "_sending", _held_elsewhere)
        with pytest.raises(links_module.LinkError) as refused:
            mona.accept(url, "Mona")
    assert (refused.value.code, refused.value.status) == ("busy", 503)
    assert "while the invite lasts" in str(refused.value) and "deadlock" not in str(refused.value)
    assert [link["status"] for link in mona.view()["links"]] == ["waiting"]

    _run(eric, mona)

    assert [link["status"] for link in eric.view()["links"]] == ["linked"]
    assert [link["status"] for link in mona.view()["links"]] == ["linked"]


@needs_node
def test_an_invite_cancelled_as_its_sender_goes_in_leaves_none_at_the_mailbox(home, monkeypatch):
    # The cancel's delete waits for the put, so it can't come first and leave the sender there.
    eric = home("eric")
    first = eric.invite("Eric")["invite"]["id"]
    real_put = mailbox_module.Mailbox.put_sender
    cancelling, waited = [], []

    def put_sender(self, sender, token):
        if sender.startswith("i-") and sender != f"i-{first}" and not cancelling:
            cancelling.append(threading.Thread(target=eric.cancel, args=(sender[2:],)))
            cancelling[0].start()
            cancelling[0].join(0.5)
            waited.append(cancelling[0].is_alive())
        real_put(self, sender, token)

    monkeypatch.setattr(mailbox_module.Mailbox, "put_sender", put_sender)
    eric.invite("Eric")
    cancelling[0].join(10)

    assert waited == [True]
    assert [row["sender"] for row in eric.mailbox().senders()] == [f"i-{first}"]
    assert [invite["id"] for invite in eric.view()["invites"]] == [first]


@needs_node
def test_a_link_unlinked_as_its_lost_inbox_is_made_again_leaves_no_sender(home, monkeypatch):
    # The unlink's delete waits for the senders the inbox gets back, so it can't come first and leave one.
    eric, mona, link_id = _linked(home)
    eric.mailbox().delete()  # as the mailbox does after 90 idle days
    real_put = mailbox_module.Mailbox.put_sender
    unlinking, waited = [], []

    def put_sender(self, sender, token):
        if sender == f"l-{link_id}" and not unlinking:
            unlinking.append(threading.Thread(target=eric.remove, args=(link_id,)))
            unlinking[0].start()
            unlinking[0].join(0.5)
            waited.append(unlinking[0].is_alive())
        real_put(self, sender, token)

    monkeypatch.setattr(mailbox_module.Mailbox, "put_sender", put_sender)
    _run(eric, passes=1)  # it finds the inbox gone and makes it again, with its senders
    unlinking[0].join(10)

    assert waited == [True]
    assert eric.mailbox().senders() == []
    _run(eric, mona)
    assert mona.view()["links"] == []


@needs_node
def test_a_word_list_that_cant_be_read_is_refused_with_its_code(home, monkeypatch):
    eric, mona, link_id = _linked(home)
    url = home("nahida").invite("Nahida")["url"]

    def missing(*args):
        raise identity_module.IdentityError("no_word_list", "This plugin install is missing bip39_english.txt")

    monkeypatch.setattr(identity_module, "fingerprint_words", missing)
    for call in (eric.view, lambda: eric.get(link_id), lambda: mona.preview(url), eric.words):
        with pytest.raises(links_module.LinkError) as refused:
            call()
        assert (refused.value.code, refused.value.status) == ("no_word_list", 503)


def _no_word_list(*args):
    raise identity_module.IdentityError("no_word_list", "This plugin install is missing bip39_english.txt")


@needs_node
def test_an_invite_isnt_made_without_its_words(home, monkeypatch):
    venti = home("venti")
    monkeypatch.setattr(identity_module, "fingerprint_words", _no_word_list)

    with pytest.raises(links_module.LinkError) as refused:
        venti.invite("Venti")

    assert (refused.value.code, refused.value.status) == ("no_word_list", 503)
    assert venti._store.read()["invites"] == {}


@needs_node
def test_an_accept_isnt_made_without_its_words(home, monkeypatch):
    eric, mona = home("eric"), home("mona")
    made = eric.invite("Eric")

    with monkeypatch.context() as patched:
        patched.setattr(identity_module, "fingerprint_words", _no_word_list)
        with pytest.raises(links_module.LinkError) as refused:
            mona.accept(made["url"], "Mona")
    _run(eric, mona)

    assert (refused.value.code, refused.value.status) == ("no_word_list", 503)
    assert mona._store.read()["links"] == {} and mona._store.read()["outbox"] == []
    assert eric.view()["links"] == [] and [invite["id"] for invite in eric.view()["invites"]] == [made["invite"]["id"]]


def _invite_with(url, **changes):
    """The invite in ``url``, with some of its fields changed."""
    fields = json.loads(links_module.sealing.unb64u(url[len(links_module.URL_PREFIX):], ValueError))
    fields.update(changes)
    return links_module.URL_PREFIX + links_module.sealing.b64u(json.dumps(fields).encode("utf-8"))


@needs_node
@pytest.mark.parametrize("mailbox", ["https://localhost", "https://127.0.0.1:8443", "https://10.0.0.5",
                                     "https://192.168.1.20", "https://169.254.169.254", "https://[::1]",
                                     "https://[fd00::5]", "https://rooms.localhost"])
def test_an_invite_naming_a_mailbox_on_a_private_network_is_refused(home, mailbox):
    # Accepting it would have this Hermes call a service there.
    url = _invite_with(home("eric").invite("Eric")["url"], mailbox=mailbox)
    mona = home("mona")

    for call in (lambda: mona.preview(url), lambda: mona.accept(url, "Mona")):
        with pytest.raises(links_module.LinkError) as refused:
            call()
        assert refused.value.code == "bad_invite"
    assert mona.view()["links"] == []


def _resolving_here(monkeypatch, *addresses):
    """Host names point at ``addresses``, and requests go straight out, as on
    a host with no proxy, so names are looked up here: the names asked for."""
    monkeypatch.setattr(mailbox_module, "_opener",
                        urllib.request.build_opener(urllib.request.ProxyHandler({}), mailbox_module._NoRedirects))
    return _resolving(monkeypatch, *addresses)


@needs_node
def test_an_invite_whose_mailbox_name_points_inside_this_network_isnt_accepted(home, monkeypatch):
    # A name passes for anyone's mailbox until it's looked up, as the accept goes.
    asked = _resolving_here(monkeypatch, "10.0.0.5")
    url = _invite_with(home("eric").invite("Eric")["url"], mailbox="https://rooms.example.net")
    mona = home("mona")

    with pytest.raises(links_module.LinkError) as refused:
        mona.accept(url, "Mona")
    assert (refused.value.code, refused.value.status) == ("bad_url", 502)
    assert asked == [("rooms.example.net", 443)]
    assert mona._store.read()["links"] == {} and mona._store.read()["outbox"] == []


@needs_node
def test_an_accept_naming_a_mailbox_whose_name_points_inside_this_network_gets_no_link(home, monkeypatch):
    # Its confirm would go there.
    asked = _resolving_here(monkeypatch, "127.0.0.1")
    eric, mallory = home("eric"), home("mallory")
    _forged_accept(eric.invite("Eric")["url"], mallory, mailbox="https://rooms.example.net")

    _run(eric)

    assert eric.view()["links"] == [] and eric._store.read()["outbox"] == []
    assert asked == [("rooms.example.net", 443)]


@needs_node
def test_sends_tell_the_mailbox_client_which_mailbox_is_this_hermes_own(home, monkeypatch):
    # It looks up only a mailbox someone else named, so one on a home network that both Hermes use links
    # them: an accept, a confirm, an envelope and a remove.
    calls = []
    real_send = mailbox_module.send

    def send(url, *args, **kwargs):
        calls.append((url, kwargs.get("own")))
        return real_send(url, *args, **kwargs)

    monkeypatch.setattr(mailbox_module, "send", send)
    eric, mona, link_id = _linked(home)
    mona.send(link_id, "test.echo", {"text": "hi"})
    mona.remove(link_id)

    assert len(calls) == 4 and all(url == own for url, own in calls)


@needs_node
@pytest.mark.parametrize("lasts", [2 * DAY + 60, 10 ** 12])
def test_an_invite_that_says_it_lasts_longer_than_invites_do_is_refused(home, lasts):
    # 10 ** 12 is past any date this computer can show.
    url = home("eric").invite("Eric")["url"]
    url = _invite_with(url, exp=int(time.time()) + lasts)
    mona = home("mona")

    for call in (lambda: mona.preview(url), lambda: mona.accept(url, "Mona")):
        with pytest.raises(links_module.LinkError) as refused:
            call()
        assert refused.value.code == "bad_invite"
    assert mona._store.read()["links"] == {} and mona._store.read()["outbox"] == []


def test_an_invite_naming_this_hermes_own_private_mailbox_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", "https://192.168.1.20")
    mona = links_module.Links(tmp_path / "mona")
    keys = identity_module.load_or_create(tmp_path / "eric")
    fields = {"v": links_module.VERSION, "name": "Eric", "sign": links_module.sealing.b64u(keys.signing_public),
              "seal": links_module.sealing.b64u(keys.sealing_public), "mailbox": "https://192.168.1.20",
              "invite": "abcdefghijkl", "token": "t" * 32, "exp": int(time.time()) + DAY}
    url = links_module.URL_PREFIX + links_module.sealing.b64u(json.dumps(fields).encode("utf-8"))

    assert mona.preview(url)["mailbox"] == "192.168.1.20"


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


@needs_node
@pytest.mark.parametrize("key", ["sign", "seal"])
def test_a_links_file_with_a_link_whose_keys_arent_keys_is_refused(home, key):
    # Edited by hand: refused whole, as any file this plugin didn't write is,
    # rather than failing on that Link wherever it's read.
    eric, mona, link_id = _linked(home)
    path = eric.root / "conduit-rooms" / "links.json"
    stored = json.loads(path.read_text())
    stored["links"][link_id][key] = "not a key"
    path.write_text(json.dumps(stored))

    for call in (eric.view, lambda: links_module.LinkLoop(eric.root).poll_once()):
        with pytest.raises(links_module.LinkError) as refused:
            call()
        assert refused.value.code == "unreadable"


@needs_node
@pytest.mark.parametrize("field", ["kind", "link", "mailbox", "inbox", "sign", "seal", "token", "body"])
def test_a_links_file_with_an_outbox_item_missing_what_it_needs_is_refused(home, monkeypatch, field):
    eric, mona, link_id = _linked(home)
    monkeypatch.setattr(links_module.Links, "_flush_quietly", lambda self: None)
    eric.remove(link_id)  # its link.remove waits in the outbox
    path = eric.root / "conduit-rooms" / "links.json"
    stored = json.loads(path.read_text())
    del stored["outbox"][0][field]
    path.write_text(json.dumps(stored))

    for call in (eric.view, lambda: links_module.LinkLoop(eric.root).poll_once()):
        with pytest.raises(links_module.LinkError) as refused:
            call()
        assert refused.value.code == "unreadable"


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


def test_links_routes_that_wait_on_the_mailbox_have_their_own_threads_and_time(routes, monkeypatch):
    dashboard_links = api._plugin_package_module("room_links")
    threads, go_on = {}, threading.Event()

    def invite(self, name):
        threads["invite"] = threading.current_thread().name
        go_on.wait(10)  # a mailbox that doesn't answer
        return {"url": "conduit://link/x", "invite": {"id": "x", "name": name, "expires_at": 0}}

    def view(self):
        threads["list"] = threading.current_thread().name
        return {"links": [], "invites": [], "mailbox": {"url": "", "reading": False}}

    monkeypatch.setattr(dashboard_links.Links, "invite", invite)
    monkeypatch.setattr(dashboard_links.Links, "view", view)
    monkeypatch.setattr(api, "ROOM_LINKS_TIMEOUT_S", 0.2)
    eric = routes("eric")
    try:
        slow = eric.call("POST", "/rooms/links/invites", json={"name": "Eric"})
        listed = eric.call("GET", "/rooms/links")
    finally:
        go_on.set()

    assert slow.status_code == 504 and slow.headers["cache-control"] == "no-store"
    assert slow.json()["code"] == "timed_out" and slow.json()["detail"]
    assert threads["invite"].startswith("conduit-room-links")
    assert listed.status_code == 200 and not threads["list"].startswith("conduit-room-links")


def test_requests_waiting_for_the_links_threads_count_toward_the_limit(routes, monkeypatch):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(api._room_links_limiter, "clock", lambda: 1000.0)
    monkeypatch.setattr(api, "ROOM_LINKS_TIMEOUT_S", 0.05)
    pool, go_on = concurrent.futures.ThreadPoolExecutor(max_workers=1), threading.Event()
    monkeypatch.setattr(api, "_room_links_executor", pool)
    pool.submit(go_on.wait, 10)  # its one thread waits on a mailbox that doesn't answer
    eric = routes("eric")
    try:
        waited = [eric.call("POST", "/rooms/links/invites", json={"name": "Eric"}) for _ in range(60)]
        refused = eric.call("POST", "/rooms/links/invites", json={"name": "Eric"})
    finally:
        go_on.set()
        pool.shutdown(wait=True, cancel_futures=True)

    assert {answer.json()["code"] for answer in waited} == {"timed_out"}
    assert refused.status_code == 429


def test_the_identity_route_refuses_a_word_list_that_cant_be_read(routes, monkeypatch):
    dashboard_identity = api._plugin_package_module("room_identity")
    eric = routes("eric")
    dashboard_identity.load_or_create(eric.root)

    def missing(*args):
        raise dashboard_identity.IdentityError("no_word_list", "This plugin install is missing bip39_english.txt")

    monkeypatch.setattr(dashboard_identity, "fingerprint_words", missing)

    refused = eric.call("GET", "/rooms/identity")

    assert refused.status_code == 503 and refused.json()["code"] == "no_word_list"


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
def test_cli_makes_no_invite_without_its_words(cli, monkeypatch, mailbox_url, tmp_path, capsys):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    monkeypatch.setattr(identity_module, "fingerprint_words", _no_word_list)

    assert cli("eric", "invite", name="Eric") == 1

    said = capsys.readouterr().out
    assert "Couldn't make the invite" in said and "conduit://link/" not in said
    assert links_module.Links(tmp_path / "eric")._store.read()["invites"] == {}


@needs_node
def test_cli_preview_says_which_mailbox_the_invite_uses(cli, monkeypatch, mailbox_url, capsys):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    assert cli("eric", "invite", name="Eric") == 0
    url = _url_in(capsys.readouterr().out)

    assert cli("mona", "preview", url) == 0
    assert f"through the mailbox at {mailbox_module.url_host(mailbox_url)}" in capsys.readouterr().out


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


@needs_node
def test_cli_says_an_accept_the_mailbox_didnt_take_yet_waits_here(cli, monkeypatch, mailbox_url, tmp_path, capsys):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    cli("eric", "invite", name="Eric")
    url = _url_in(capsys.readouterr().out)

    def unreachable(*args, **kwargs):
        raise mailbox_module.MailboxError("unreachable", "Couldn't reach the mailbox at the inviter's")

    monkeypatch.setattr(mailbox_module, "send", unreachable)
    assert cli("mona", "accept", url, name="Mona") == 1

    said = capsys.readouterr().out
    assert said.startswith("The accept didn't go yet: Couldn't reach the mailbox at the inviter's.")
    assert "tries it again while the invite lasts" in said and "Couldn't accept it" not in said
    assert "so the accept waits here until Hermes' dashboard runs" in said
    assert [link["status"] for link in links_module.Links(tmp_path / "mona").view()["links"]] == ["waiting"]


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
