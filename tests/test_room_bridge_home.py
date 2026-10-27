"""The home's side of the bridge (designs/cross-owner-agents, N4b): Hermes' own RoomLink client, talking to the
loopback server that carries its requests over a Link and answers its status reads from what the member sends.

The client is Hermes' (tests/roomlink_client.py, verbatim from c73cbcc4). Across the Link is the real member side
(room_bridge_member.py) with test_room_bridge_member's fake of Hermes' runs API, and between them an in-process
mailbox that carries an envelope as it's sent, or holds or loses it when a test says so.
test_two_loads_run_one_server runs over the real mailbox.
"""
import dataclasses
import http.client
import importlib
import itertools
import json
import logging
import socket
import stat
import sys
import threading
import time
import types

import pytest

import roomlink_client as hermes_client
from test_room_bridge_member import (DAY, GRANT, NOW, ROOT, SCOPE, Clock, FakeHermes, Inline, _homes, _load, bridge,
                                     dispatch, grant, links_module, member_module)
from test_room_mailbox import mailbox_url, needs_node  # noqa: F401

home_module = _load("conduit_push.room_bridge_home", ROOT / "room_bridge_home.py")

LINK = "link1"
SILENT = "Laifu's Hermes stopped answering during this turn."
CAPPED = "This turn ran past 12 minutes on Laifu's Hermes."
STOPPED = "The turn was stopped on Laifu's Hermes."
UNANSWERED = "Laifu's Hermes didn't answer."
REFUSED = "The member's Hermes refused the request."
PeerRunsHTTPError = hermes_client.PeerRunsHTTPError


class Wire:
    """The rooms mailbox between Furina's home and Laifu's Hermes, over Link ``link1``. An envelope reaches the
    other side as it's sent, unless the test holds that way (until ``release``) or loses what goes on it."""

    def __init__(self, furina, laifu):
        self.roots = {"home": furina, "member": laifu}
        self.home = self.member = None
        self.sent = {"home": [], "member": []}  # (kind, body), by the side they went to
        self.ways = {"home": "deliver", "member": "deliver"}
        self.held = {"home": [], "member": []}
        self.refusing = False
        self._ids = itertools.count(1)

    def to_member(self, link_id, kind, body, message_id=None):
        if self.refusing:
            raise links_module.LinkError("mailbox_refused", "The mailbox didn't take it", 503)
        return self._carry("member", link_id, kind, body, message_id or f"h{next(self._ids)}")

    def to_home(self, link_id, kind, body, message_id=None):
        return self._carry("home", link_id, kind, body, message_id or f"l{next(self._ids)}")

    def release(self, way, age=0):
        held, self.held[way] = self.held[way], []
        self.ways[way] = "deliver"
        for received in held:
            self._deliver(way, dataclasses.replace(received, age=age))

    def requests(self):
        """What the home asked of the member, in order."""
        return [body for kind, body in self.sent["member"] if kind == bridge.REQUEST]

    def _carry(self, way, link_id, kind, body, message_id):
        body = json.loads(json.dumps(body))
        self.sent[way].append((kind, body))
        received = links_module.Received(link_id=link_id, kind=kind, message_id=message_id, at=int(NOW), body=body,
                                         age=0, root=self.roots[way])
        if self.ways[way] == "hold":
            self.held[way].append(received)
        elif self.ways[way] == "deliver":
            self._deliver(way, received)
        return message_id

    def _deliver(self, way, received):
        if way == "member":
            self.member.handle(received)
        elif received.kind == bridge.RESPONSE:
            self.home.on_response(received)
        else:
            self.home.on_status(received)


def _free_ports(count=1):
    """``count`` ports in a row that nothing listens on now."""
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            first = probe.getsockname()[1]
        if first + count > 65535:
            continue
        try:
            for port in range(first, first + count):
                with socket.socket() as taken:
                    taken.bind(("127.0.0.1", port))
        except OSError:
            continue
        return range(first, first + count)
    raise RuntimeError("no free ports")


@pytest.fixture
def hermes():
    fake = FakeHermes()
    yield fake
    fake.close()


def _home(side, **changes):
    options = dict(send=side.wire.to_member, links=side.links.get, clock=side.clock, monotonic=side.monotonic,
                   wait_s=0.3, ports=side.ports, leased=lambda: True, linked=lambda: True)
    options.update(changes)
    return home_module.Home(side.root, **options)


@pytest.fixture
def room(tmp_path, hermes):
    """Furina's home and Laifu's Hermes, linked over ``link1``: Laifu's agent is a member of Furina's room-zoo, its
    grant bound to the Link, and Furina's bridge is listening."""
    furina, laifu = tmp_path / "furina", tmp_path / "laifu"
    side = types.SimpleNamespace(root=furina, laifu=laifu, wire=Wire(furina, laifu), clock=Clock(),
                                 monotonic=Clock(0.0), links={LINK: {"status": "linked", "name": "Laifu"}},
                                 ports=_free_ports(), hermes=hermes)
    side.member = side.wire.member = member_module.Member(
        laifu, send=side.wire.to_home, api_url=hermes.url, clock=Clock(), monotonic=Clock(0.0),
        profile_home=_homes(laifu), executor=Inline(), api_timeout=2.0)
    member_module.bind(laifu, LINK, SCOPE)
    side.home = side.wire.home = _home(side)
    side.port = side.home.serve()
    yield side
    side.home.close()


def client(side, profile="default", api_key=""):
    """Hermes' RoomLink client for the member's agent, at the bridge's URL. Its clock is a test's to move, past
    the waits between its status reads."""
    return hermes_client.PeerRunsHTTPClient(base_url=bridge.url(side.root, LINK, profile), api_key=api_key,
                                            timeout_seconds=5, clock=Clock(0.0))


def admit(peer, task="t1", generation=1):
    fields = dispatch(task, generation)[0]["hosted_room_dispatch"]
    return peer.dispatch(dispatch=fields, grant=GRANT)


def session(peer):
    return peer.prepare(room_id=SCOPE["room_id"], profile="default", source="bot_room", grant=GRANT,
                        create=True)["session_id"]


def status(peer):
    peer.clock.now += 10
    return peer.status(room_id=SCOPE["room_id"], profile="default", session_id=session(peer), grant=GRANT)


def history(peer):
    peer.clock.now += 10
    return peer.history(room_id=SCOPE["room_id"], profile="default", session_id=session(peer), grant=GRANT)


def raw(side, method, path, *, body=None, headers=None, prefix=None):
    """One request as anything on this host could make it: the status and the JSON answer."""
    connection = http.client.HTTPConnection("127.0.0.1", side.port, timeout=5)
    try:
        sent = {"Authorization": f"HermesRoom {GRANT}", "Content-Type": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        connection.request(method, (f"/l/{LINK}/p/default" if prefix is None else prefix) + path, body=data,
                           headers={name: value for name, value in sent.items() if value is not None})
        answer = connection.getresponse()
        return answer.status, json.loads(answer.read() or b"{}")
    finally:
        connection.close()


def report(side, run_id, seq, **shown):
    """A roomlink.status from Laifu's side, as its watching sends it."""
    side.wire.to_home(LINK, bridge.STATUS, bridge.status_body(run_id, seq, shown))


class Answering:
    """A member's side that answers each request with what ``answer(request)`` gives (status, body[, run]),
    as another plugin, or a changed one, might."""

    def __init__(self, wire, answer):
        self.wire, self.answer = wire, answer

    def handle(self, received):
        status, body, *run = self.answer(received.body)
        self.wire.to_home(LINK, bridge.RESPONSE, bridge.response_body(received.message_id, status, body,
                                                                      run=run[0] if run else None))


# --- Requests -----------------------------------------------------------------------


def test_a_dispatch_crosses_the_link_and_comes_back_accepted(room):
    peer = client(room)

    accepted = admit(peer)

    [run_id] = room.hermes.runs
    assert accepted == {"status": "accepted", "task_id": "t1", "execution_generation": 1, "run_id": run_id,
                        "session_id": session(peer), "replayed": False}
    [request] = room.wire.requests()
    assert (request["method"], request["path"], request["profile"]) == ("POST", "/v1/runs", "default")
    assert request["grant"] == GRANT and request["key"] == "room:t1:1"
    assert set(request["body"]) == {"hosted_room_dispatch"}  # without input, which repeats the prompt
    [call] = room.hermes.calls_to("POST", "/v1/runs")
    assert call["authorization"] == f"HermesRoom {GRANT}" and call["key"] == "room:t1:1"


def test_status_reads_are_answered_here_with_no_envelope(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    sent = len(room.wire.sent["member"])

    queued = status(peer)
    room.hermes.run(run_id, status="running")
    room.member.tick()
    running = status(peer)
    room.hermes.run(run_id, status="completed", output="The 22nd works for me.")
    room.member.tick()
    replies = history(peer)

    assert (queued["active"], queued["status"]) == (True, "queued")
    assert running == {"active": True, "task_id": "t1", "execution_generation": 1, "status": "running",
                       "run_id": run_id, "approval": None}
    assert replies == [{"role": "assistant", "task_id": "t1", "execution_generation": 1, "status": "settled",
                        "message_id": f"peer-run:{run_id}", "content": "The 22nd works for me."}]
    assert len(room.wire.sent["member"]) == sent
    assert room.hermes.calls_to("GET", "/v1/runs/{id}") and all(
        call["authorization"] == f"HermesRoom {GRANT}" for call in room.hermes.calls)  # the member's own reads


def test_a_member_that_doesnt_answer_gets_504_never_a_4xx(room):
    room.wire.ways["member"] = "lose"
    peer = client(room)

    with pytest.raises(PeerRunsHTTPError) as admitted:
        admit(peer)
    with pytest.raises(PeerRunsHTTPError) as probed:
        peer.probe(grant=GRANT)
    status_code, answer = raw(room, "GET", "/v1/runs/run_0123")

    # A dispatch with no answer is ambiguous: Hermes asks once more at once, then looks again later, and in
    # the end defers it as unavailable. A 4xx would be "not admitted", tried again forever.
    assert admitted.value.status_code == 504 and admitted.value.ambiguous and not admitted.value.not_admitted
    assert not admitted.value.needs_reauthorization
    assert probed.value.status_code == 504 and probed.value.retryable
    assert status_code == 504 and answer["error"]["code"] == "member_unavailable"
    assert len(room.wire.requests()) == 4


def test_a_late_answer_is_kept_for_the_retry_with_its_key(room):
    room.wire.ways["home"] = "hold"
    peer = client(room)
    with pytest.raises(PeerRunsHTTPError):
        admit(peer)
    room.wire.release("home")
    asked = len(room.wire.requests())

    recovered = recover(peer)

    [run_id] = room.hermes.runs
    assert recovered["run_id"] == run_id and recovered["replayed"] is True
    assert len(room.wire.requests()) == asked == 2
    assert len(room.hermes.keys) == 1


def recover(peer, task="t1", generation=1):
    """Hermes' recovery of a turn it gave up on as ambiguous: once the room's lease is up, its driver replays the
    dispatch (hosted_room_service._recover_peer_admission), and c73cbcc4 holds the room until that succeeds."""
    return peer.recover_dispatch(dispatch=dispatch(task, generation)[0]["hosted_room_dispatch"], grant=GRANT)


def test_an_offline_member_settles_as_unanswered_and_the_room_goes_on(room):
    room.wire.ways["member"] = "hold"  # Laifu's Hermes is off: what's left for it waits at the mailbox
    peer = client(room)
    with pytest.raises(PeerRunsHTTPError) as admitted:
        admit(peer)

    recovered = recover(peer)
    replies = history(peer)
    room.wire.release("member", age=3600)  # back an hour later
    accepted = admit(client(room), "t2")

    assert admitted.value.ambiguous and recovered["status"] == "accepted"
    assert replies == [{"role": "assistant", "task_id": "t1", "execution_generation": 1, "status": "failed",
                        "message_id": f"peer-run:{recovered['run_id']}", "content": UNANSWERED}]
    # The recovery isn't asked of him, and none of what he missed runs; his next turn does.
    assert [request["key"] for request in room.wire.requests()] == ["room:t1:1", "room:t1:1", "room:t2:1"]
    [run_id] = room.hermes.runs
    assert accepted["run_id"] == run_id and len(room.hermes.keys) == 1


def test_a_member_whose_hermes_answers_503_settles_as_unanswered_too(room):
    room.wire.member = Answering(room.wire, lambda request: (503, {"error": {"code": "member_unavailable"}}))
    peer = client(room)
    with pytest.raises(PeerRunsHTTPError):
        admit(peer)

    recover(peer)

    assert history(peer)[0]["content"] == UNANSWERED
    assert len(room.wire.requests()) == 2


@pytest.mark.parametrize("failure", [
    links_module.LinkError("mailbox_refused", "The mailbox didn't take it", 503),
    # Sealing can't take a lone surrogate, which a prompt can carry from a reply.
    UnicodeEncodeError("utf-8", "Zoo \ud83d", 4, 5, "surrogates not allowed"),
], ids=["mailbox", "sealing"])
def test_a_dispatch_that_cant_leave_settles_as_unanswered(room, caplog, failure):
    caplog.set_level(logging.INFO)

    def refusing(link_id, kind, body, message_id=None):
        raise failure

    room.home.send = refusing
    peer = client(room)
    with pytest.raises(PeerRunsHTTPError) as admitted:
        admit(peer)

    recover(peer)

    assert admitted.value.status_code == 503 and admitted.value.ambiguous
    assert history(peer)[0]["content"] == UNANSWERED
    reason = getattr(failure, "code", type(failure).__name__)
    assert f"couldn't leave a request for Link link1 ({reason})" in caplog.text


def test_an_admission_that_comes_after_the_room_gave_up_on_it_is_stopped(room):
    room.wire.ways["home"] = "hold"  # Laifu's Hermes admits the turn, and its answers come late
    peer = client(room)
    with pytest.raises(PeerRunsHTTPError):
        admit(peer)
    recover(peer)

    room.wire.release("home")
    room.home.tick()

    [run_id] = room.hermes.runs
    assert history(peer)[0]["content"] == UNANSWERED
    assert room.wire.requests()[-1]["path"] == f"/v1/runs/{run_id}/stop"
    assert len(room.hermes.calls_to("POST", "/v1/runs/{id}/stop")) == 1


def test_a_status_that_comes_before_its_admission_is_kept_for_it(room):
    # Laifu's watching and his answer go on different threads there, so his first status can come first.
    room.wire.ways["home"] = "hold"
    peer = client(room)
    with pytest.raises(PeerRunsHTTPError):
        admit(peer)
    [run_id] = room.hermes.runs
    room.hermes.run(run_id, status="completed", output="Quick.")
    room.member.tick()
    held, room.wire.held["home"] = room.wire.held["home"], []
    room.wire.ways["home"] = "deliver"
    for received in sorted(held, key=lambda received: received.kind != bridge.STATUS):
        room.wire._deliver("home", received)

    recovered = recover(peer)

    assert recovered["run_id"] == run_id and recovered["replayed"] is True
    assert [reply["content"] for reply in history(peer)] == ["Quick."]


def test_a_read_for_a_run_it_hasnt_seen_asks_the_member_once(room):
    room.wire.ways["home"] = "lose"
    with pytest.raises(PeerRunsHTTPError):
        admit(client(room))
    room.wire.ways["home"] = "deliver"
    [run_id] = room.hermes.runs
    room.hermes.run(run_id, status="running")
    asked = len(room.wire.requests())

    first = raw(room, "GET", f"/v1/runs/{run_id}")
    second = raw(room, "GET", f"/v1/runs/{run_id}")
    unknown = raw(room, "GET", "/v1/runs/run_nobody")

    assert first == second == (200, {"run_id": run_id, "status": "running"})
    assert unknown[0] == 404 and unknown[1]["error"]["code"] == "run_not_found"
    assert len(room.wire.requests()) == asked + 2


def forged(value=GRANT):
    """``value``'s payload under a signature that isn't the member's, as anything on this host could write it."""
    return f"{value.split('.')[0]}.forged"


def test_a_grant_that_only_says_its_for_the_runs_scope_is_asked_of_the_member(room):
    # Only the member's Hermes can check a grant's signature: this side answers for a run only to the grants
    # the member took for it.
    peer = client(room)
    run_id = admit(peer)["run_id"]
    for route in (("GET", "/v1/runs/{id}"), ("POST", "/v1/runs/{id}/stop"), ("POST", "/v1/runs")):
        room.hermes.refuse[route] = (403, "invalid_room_grant", "Invalid room grant.")
    asked = len(room.wire.requests())
    headers = {"Authorization": f"HermesRoom {forged()}"}

    read = raw(room, "GET", f"/v1/runs/{run_id}", headers=headers)
    stopped = raw(room, "POST", f"/v1/runs/{run_id}/stop", body={}, headers=headers)
    replayed = raw(room, "POST", "/v1/runs", body=dispatch()[0], headers={**headers, "Idempotency-Key": "room:t1:1"})

    assert [answer for answer, _ in (read, stopped, replayed)] == [403, 403, 403]
    assert {body["error"]["code"] for _, body in (read, stopped, replayed)} == {"invalid_room_grant"}
    assert [request["grant"] for request in room.wire.requests()[asked:]] == [forged()] * 3
    del room.hermes.refuse[("GET", "/v1/runs/{id}")]
    assert status(peer)["status"] == "queued"  # still running, and still read here
    assert len(room.wire.requests()) == asked + 3


def test_a_grant_the_member_gave_in_place_of_the_runs_is_answered_here(room):
    refreshed_grant = grant()
    room.hermes.answers[("POST", "/v1/room-members/grants/refresh")] = (200, {
        "object": "hermes.room_member.grant", "grant": refreshed_grant, "expires_at": NOW + DAY,
        "status_expires_at": NOW + 2 * DAY, "execution_policy": {"policy_digest": "0" * 64}})
    peer = client(room)
    run_id = admit(peer)["run_id"]
    peer.refresh_grant(grant=GRANT)
    asked = len(room.wire.requests())

    read = raw(room, "GET", f"/v1/runs/{run_id}", headers={"Authorization": f"HermesRoom {refreshed_grant}"})
    again = client(room).recover_dispatch(dispatch=dispatch()[0]["hosted_room_dispatch"], grant=refreshed_grant)

    assert read == (200, {"run_id": run_id, "status": "queued"})
    assert (again["run_id"], again["replayed"]) == (run_id, True)
    assert len(room.wire.requests()) == asked


def test_a_grant_the_member_takes_for_a_run_is_answered_here_from_then_on(room):
    # One a refresh this side didn't carry gave Hermes: the member's answer vouches for it.
    peer = client(room)
    run_id = admit(peer)["run_id"]
    other = grant()
    asked = len(room.wire.requests())

    first = raw(room, "GET", f"/v1/runs/{run_id}", headers={"Authorization": f"HermesRoom {other}"})
    second = raw(room, "GET", f"/v1/runs/{run_id}", headers={"Authorization": f"HermesRoom {other}"})

    assert first == second == (200, {"run_id": run_id, "status": "queued"})
    assert len(room.wire.requests()) == asked + 1


def test_a_run_silent_for_3_minutes_fails_and_is_stopped_there(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    room.hermes.run(run_id, status="running")
    room.wire.ways["home"] = "lose"

    room.clock.now += 179
    room.home.tick()
    before = status(peer)["status"]
    room.clock.now += 1
    room.home.tick()

    assert before == "queued"
    assert history(peer)[0]["status"] == "failed" and history(peer)[0]["content"] == SILENT
    assert room.wire.requests()[-1] == {"v": 1, "profile": "default", "method": "POST",
                                        "path": f"/v1/runs/{run_id}/stop", "grant": GRANT}
    assert len(room.hermes.calls_to("POST", "/v1/runs/{id}/stop")) == 1


def test_a_run_past_12_minutes_fails_and_is_stopped_there(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    for minute in range(1, 12):
        room.clock.now += 60
        report(room, run_id, minute, status="running")
        room.home.tick()
        assert status(peer)["status"] == "running"

    room.clock.now += 60
    room.home.tick()
    report(room, run_id, 13, status="completed", output="Late.")

    assert history(peer)[0]["status"] == "failed" and history(peer)[0]["content"] == CAPPED
    assert room.wire.requests()[-1]["path"] == f"/v1/runs/{run_id}/stop"


def test_stop_reads_cancelled_at_once_and_reaches_the_member_later(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    room.hermes.run(run_id, status="running")
    room.member.tick()
    room.wire.ways["member"] = "hold"

    stopped = peer.stop(dispatch=dispatch()[0]["hosted_room_dispatch"], grant=GRANT)
    shown = status(peer)
    reached_before = len(room.hermes.calls_to("POST", "/v1/runs/{id}/stop"))
    room.wire.release("member", age=3600)  # an hour on: a stop applies however old

    assert stopped == {"run_id": run_id, "status": "cancelled"}
    assert (shown["active"], shown["status"]) == (False, "cancelled")
    assert reached_before == 0 and len(room.hermes.calls_to("POST", "/v1/runs/{id}/stop")) == 1


def test_a_stop_the_mailbox_didnt_take_goes_on_a_later_tick(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    room.wire.refusing = True

    stopped = peer.stop(dispatch=dispatch()[0]["hosted_room_dispatch"], grant=GRANT)
    room.home.tick()
    room.wire.refusing = False
    room.home.tick()
    room.home.tick()

    assert stopped["status"] == "cancelled"
    assert [request["path"] for request in room.wire.requests()] == ["/v1/runs", f"/v1/runs/{run_id}/stop"]
    assert len(room.hermes.calls_to("POST", "/v1/runs/{id}/stop")) == 1


def test_revoke_answers_at_once(room):
    room.wire.ways["member"] = "hold"

    revoked = client(room).revoke_grant(grant=GRANT)
    room.wire.release("member", age=3600)

    assert revoked == {"revoked": True}
    assert len(room.hermes.calls_to("POST", "/v1/room-members/grants/revoke")) == 1


@pytest.mark.parametrize("action", ["approval", "clarify"])
def test_approvals_are_refused_with_nothing_sent(room, action):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    sent = len(room.wire.sent["member"])

    with pytest.raises(PeerRunsHTTPError) as refused:
        peer._post_run_action({"run_id": run_id}, action, body={"choice": "once", "request_id": "req-1"},
                              grant=GRANT)

    assert refused.value.status_code == 403 and refused.value.error_code == "room_approval_elsewhere"
    assert len(room.wire.sent["member"]) == sent


def test_capabilities_and_a_refreshed_grant_come_back(room):
    refreshed_grant = grant()  # a new grant id for the same scope, as Hermes' refresh mints
    room.hermes.answers[("POST", "/v1/room-members/grants/refresh")] = (200, {
        "object": "hermes.room_member.grant", "grant": refreshed_grant, "expires_at": NOW + DAY,
        "status_expires_at": NOW + 2 * DAY, "execution_policy": {"policy_digest": "0" * 64}})
    peer = client(room)

    probed = peer.probe(grant=GRANT)
    refreshed = peer.refresh_grant(grant=GRANT)

    assert probed["catalog"] == {"installation_id": "gw-laifu", "catalog_digest": "1" * 64}
    assert "debug" not in probed
    assert refreshed == {"object": "hermes.room_member.grant", "grant": refreshed_grant, "expires_at": NOW + DAY,
                         "status_expires_at": NOW + 2 * DAY, "catalog": probed["catalog"]}
    # Hermes' client tries the new grant before it keeps it.
    assert [(request["path"], request["grant"]) for request in room.wire.requests()] == [
        ("/v1/room-members/capabilities", GRANT), ("/v1/room-members/grants/refresh", GRANT),
        ("/v1/room-members/capabilities", refreshed_grant)]
    assert room.wire.requests()[1]["body"] == {"ttl_seconds": DAY}


def test_an_ended_link_reads_invalid_room_grant_and_a_waiting_one_503(room):
    peer = client(room)
    room.links[LINK] = {"status": "waiting", "name": "Laifu"}
    with pytest.raises(PeerRunsHTTPError) as waiting:
        admit(peer)
    for ended in ({"status": "unanswered", "name": "Laifu"}, None):
        room.links[LINK] = ended
        with pytest.raises(PeerRunsHTTPError) as gone:
            admit(peer, "t2")
        # N2 settles the turn as cut off: the member's Hermes no longer takes this room.
        assert gone.value.status_code == 403 and gone.value.error_code == "invalid_room_grant"
        assert gone.value.needs_reauthorization and gone.value.not_admitted

    assert waiting.value.status_code == 503 and waiting.value.retryable and not waiting.value.needs_reauthorization
    assert room.wire.requests() == []


def test_only_hermesroom_grants_leave_the_host(room, caplog):
    key = "sk-furinas-own-api-key-0123456789"
    peer = client(room, api_key=key)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(PeerRunsHTTPError) as invited:
        peer.issue_invitation(room_id="room-zoo", home_install_id="gw-furina", authority_gateway_id="gw-furina",
                              authority_epoch=1, member_id="m-laifu", grant_id="grant-9")
    none = raw(room, "GET", "/v1/room-members/capabilities", headers={"Authorization": None})
    basic = raw(room, "GET", "/v1/room-members/capabilities", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    empty = raw(room, "GET", "/v1/room-members/capabilities", headers={"Authorization": "HermesRoom "})

    assert invited.value.status_code == 401
    assert none[0] == basic[0] == empty[0] == 401
    assert room.wire.sent["member"] == []
    assert key not in caplog.text


@pytest.mark.parametrize("host", ["localhost:{port}", "127.0.0.1:1", "rooms.example:{port}", None])
def test_a_request_for_another_host_is_refused(room, host):
    sent = host.format(port=room.port) if host else ""
    status_code, answer = raw(room, "GET", "/v1/room-members/capabilities", headers={"Host": sent})

    assert status_code == 403 and answer["error"]["code"] == "invalid_host"
    assert room.wire.sent["member"] == []


@pytest.mark.parametrize("path", ["/v1/models", "/v1/runs/run_1/events", "/v1/runs/run_1/resolve-unknown",
                                  "/v1/room-members/invitations", "/v1/runs?x=1"])
def test_anything_else_is_not_found_with_nothing_sent(room, path):
    status_code, answer = raw(room, "POST", path, body={})

    assert status_code == 404 and answer["error"]["code"] == "not_found"
    assert room.wire.sent["member"] == []


@pytest.mark.parametrize("prefix", ["/l/link 1/p/default", "/l/link1/p/Work", "/l/link1/default", "/x/link1/p/default"])
def test_a_path_that_isnt_a_routes_is_not_found(room, prefix):
    status_code, _ = raw(room, "GET", "/v1/room-members/capabilities", prefix=prefix.replace(" ", "%20"))

    assert status_code == 404 and room.wire.sent["member"] == []


def test_a_named_profiles_agent_is_asked_for_by_its_profile(room):
    status_code, _ = raw(room, "GET", "/v1/room-members/capabilities", prefix=f"/l/{LINK}/p/work")

    [request] = room.wire.requests()
    assert request["profile"] == "work"
    # The grant is for the default profile, which Laifu's side checks.
    assert status_code == 403


# --- What comes back -------------------------------------------------------------------------


def test_status_that_comes_late_keeps_the_newest(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]

    report(room, run_id, 2, status="completed", output="Yes.")
    report(room, run_id, 1, status="running")
    report(room, run_id, 3, status="running")

    assert history(peer)[0]["content"] == "Yes."


def test_a_status_for_another_links_run_or_one_it_doesnt_know_changes_nothing(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    room.links["link2"] = {"status": "linked", "name": "Mona"}

    room.wire.to_home("link2", bridge.STATUS, bridge.status_body(run_id, 5, {"status": "completed", "output": "Mona"}))
    report(room, "run_nobody", 1, status="completed", output="?")
    report(room, run_id, 6, status="exploded")
    room.wire.to_home(LINK, bridge.STATUS, {"v": 1, "run_id": run_id, "seq": "7", "status": "completed"})

    assert status(peer)["status"] == "queued"
    assert set(room.home.store.read()["home"]["runs"]) == {f"{LINK}/{run_id}"}


def test_a_cancel_the_home_didnt_ask_for_reads_interrupted(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]

    report(room, run_id, 1, status="cancelled")

    [reply] = history(peer)
    assert reply["status"] == "failed" and reply["content"] == STOPPED


def test_answers_carry_only_what_hermes_client_reads(room):
    answers = iter([
        (202, {"run_id": "run_a", "status": "started", "replayed": False, "path": "/home/laifu"},
         {"run_id": "run_a", "status": "running", "output": "", "approval": {"command": "rm -rf"}}),
        (202, {"status": "started"}),
        (403, {"error": {"code": "invalid_room_grant", "message": "room grant at /home/laifu expired"}}),
        (400, {"error": {"code": "Not A Code!", "message": "ttl 'abc'"}}),
        (700, {}),
        (200, {"run_id": "run_b", "status": "exploded"}),
        (200, {"object": "hermes.room_member.capabilities", "catalog": {"catalog_digest": "1" * 64},
               "debug": "/home/laifu/.hermes"}),
    ])
    room.wire.member = Answering(room.wire, lambda request: next(answers))
    body, key = dispatch()

    admitted = raw(room, "POST", "/v1/runs", body=body, headers={"Idempotency-Key": key})
    no_run = raw(room, "POST", "/v1/runs", body=dispatch("t2")[0], headers={"Idempotency-Key": "room:t2:1"})
    refused = raw(room, "POST", "/v1/runs", body=dispatch("t3")[0], headers={"Idempotency-Key": "room:t3:1"})
    no_code = raw(room, "POST", "/v1/room-members/grants/refresh", body={"ttl_seconds": 60})
    odd = raw(room, "GET", "/v1/room-members/capabilities")
    odd_read = raw(room, "GET", "/v1/runs/run_b")
    probed = raw(room, "GET", "/v1/room-members/capabilities")
    read = raw(room, "GET", "/v1/runs/run_a")

    assert admitted == (202, {"run_id": "run_a", "status": "started", "replayed": False})
    assert read == (200, {"run_id": "run_a", "status": "running"})
    assert no_run[0] == 503 and no_run[1]["error"]["code"] == "member_unavailable"
    assert refused == (403, {"error": {"code": "invalid_room_grant", "message": REFUSED}})
    assert no_code == (400, {"error": {"message": REFUSED}})
    assert odd[0] == odd_read[0] == 503
    assert probed == (200, {"object": "hermes.room_member.capabilities", "catalog": {"catalog_digest": "1" * 64}})
    assert "/home/laifu" not in json.dumps([admitted, refused, no_code, probed, read])


# --- Limits ---------------------------------------------------------------------------


def test_an_answer_utf_8_cant_hold_still_reaches_hermes(room):
    # A lone surrogate, which JSON carries and UTF-8 can't, in what a member sent.
    room.wire.member = Answering(room.wire, lambda request: (200, {"object": "room_member.capabilities",
                                                                   "catalog": {"note": "Zoo \ud83d trip"}}))

    status_code, answer = raw(room, "GET", "/v1/room-members/capabilities")

    assert status_code == 200
    assert answer == {"object": "room_member.capabilities", "catalog": {"note": "Zoo \ud83d trip"}}


def test_the_servers_backlog_holds_every_request_a_link_can_have_waiting():
    # One it overflowed could be refused, which Hermes' client takes as a turn that wasn't admitted.
    assert home_module._Server.request_queue_size >= 4 * home_module.MAX_WAITING


def test_a_full_mailbox_or_too_many_requests_is_503(room):
    peer = client(room)
    room.wire.refusing = True
    with pytest.raises(PeerRunsHTTPError) as refused:
        peer.probe(grant=GRANT)
    room.wire.refusing = False

    for _ in range(59):
        peer.probe(grant=GRANT)
    with pytest.raises(PeerRunsHTTPError) as busy:
        peer.probe(grant=GRANT)
    room.monotonic.now += 60
    again = peer.probe(grant=GRANT)

    assert refused.value.status_code == 503 and refused.value.retryable
    assert busy.value.status_code == 503 and busy.value.retryable
    assert again["catalog"]
    assert len(room.wire.requests()) == 60  # the refused one counts toward the minute, but never reached it


def test_16_requests_waiting_on_a_link_is_the_most(room):
    room.home.close()
    room.home = room.wire.home = _home(room, wait_s=5.0)
    room.port = room.home.serve()
    room.wire.ways["member"] = "hold"
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(raw(room, "GET", "/v1/room-members/capabilities")))
               for _ in range(16)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while len(room.wire.held["member"]) < 16 and time.monotonic() < deadline:
        time.sleep(0.01)

    seventeenth = raw(room, "GET", "/v1/room-members/capabilities")
    room.wire.release("member")
    for thread in threads:
        thread.join(5)

    assert seventeenth[0] == 503 and seventeenth[1]["error"]["code"] == "member_unavailable"
    assert sorted(status_code for status_code, _ in answers) == [200] * 16


# --- What's kept -------------------------------------------------------------------------


def test_runs_are_answered_after_a_restart(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    room.hermes.run(run_id, status="running")
    room.member.tick()
    room.home.close()

    room.home = room.wire.home = _home(room)
    port = room.home.serve()
    asked = len(room.wire.requests())
    shown = status(peer)
    # A home Hermes that restarted too asks again with the dispatch's key.
    again = client(room).recover_dispatch(dispatch=dispatch()[0]["hosted_room_dispatch"], grant=GRANT)

    assert port == room.port
    assert shown["status"] == "running" and (again["run_id"], again["replayed"]) == (run_id, True)
    assert len(room.wire.requests()) == asked


def test_kept_answers_and_ended_runs_go_after_an_hour(room):
    peer = client(room)
    run_id = admit(peer)["run_id"]
    report(room, run_id, 1, status="completed", output="Yes.")

    room.clock.now += 3600
    room.home.tick()
    kept = room.home.store.read()["home"]
    room.clock.now += 1
    room.home.tick()
    forgotten = room.home.store.read()["home"]

    assert list(kept["runs"]) == [f"{LINK}/{run_id}"] and list(kept["kept"]) == [f"{LINK}/room:t1:1"]
    assert forgotten["runs"] == {} and forgotten["kept"] == {}


def test_bridge_json_is_owner_only_and_no_grant_is_logged(room, caplog):
    caplog.set_level(logging.DEBUG)
    peer = client(room)
    run_id = admit(peer)["run_id"]
    room.wire.ways["home"] = "lose"
    room.clock.now += 180
    room.home.tick()
    other = grant(dict(SCOPE, room_id="x"))
    raw(room, "GET", "/v1/room-members/capabilities", headers={"Authorization": f"HermesRoom {other}"})

    path = room.root / "conduit-rooms" / "bridge.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert GRANT in path.read_text()  # kept there, for the stop the bridge sends itself
    assert GRANT not in caplog.text and GRANT.split(".")[0] not in caplog.text and run_id


# --- Where it runs -----------------------------------------------------------------------


def test_the_url_is_one_hermes_takes(room, tmp_path):
    for profile in ("default", "work"):
        url = bridge.url(room.root, LINK, profile)
        assert hermes_client.validate_room_link_url(url) == (
            f"http://127.0.0.1:{room.port}/l/{LINK}/p/{profile}", "loopback")
    with pytest.raises(bridge.BridgeError) as not_yet:
        bridge.url(tmp_path / "mona", LINK, "default")
    for link_id, profile in (("link 1", "default"), (LINK, "Work"), (LINK, "")):
        with pytest.raises(ValueError):
            bridge.url(room.root, link_id, profile)

    assert not_yet.value.code == "not_listening" and not_yet.value.status == 503


def test_the_server_runs_where_the_lease_is_and_keeps_its_port(room, caplog):
    room.home.close()
    leased, linked = [False], [True]
    ports = _free_ports(3)

    def make():
        return _home(room, ports=ports, leased=lambda: leased[0], linked=lambda: linked[0])

    home = make()
    home.watch_pass()
    not_leased = home.port
    leased[0], linked[0] = True, False
    home.watch_pass()
    no_link = home.port
    linked[0] = True
    home.watch_pass()
    first = home.port
    home.close()
    again = make()
    again.watch_pass()
    kept = again.port
    again.close()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", ports[0]))
        taken.listen()
        caplog.set_level(logging.INFO)
        moved = make()
        moved.watch_pass()
        moved.watch_pass()
        took = moved.port
        leased[0] = False
        moved.watch_pass()
        after_lease = moved.port

    assert not_leased is None and no_link is None
    assert first == kept == ports[0] and took == ports[1] and after_lease is None
    assert bridge.Store(room.root).read()["home"]["port"] == ports[1]
    assert caplog.text.count(f"took port {ports[1]}") == 1


def test_start_handles_answers_for_its_installation_and_starts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(home_module, "_homes", {})
    monkeypatch.setattr(home_module, "_watch", lambda home: None)
    monkeypatch.setattr(member_module, "_members", {})
    monkeypatch.setattr(member_module, "_watch", lambda member: None)
    for kind in (bridge.REQUEST, bridge.RESPONSE, bridge.STATUS):
        monkeypatch.delitem(links_module.HANDLERS, kind, raising=False)
    root = tmp_path / "furina"

    bridge.start(root)
    started = home_module._homes[str(root)]
    bridge.start(root)
    handled = []
    monkeypatch.setattr(started, "on_response", lambda received: handled.append(("response", received.root)))
    monkeypatch.setattr(started, "on_status", lambda received: handled.append(("status", received.root)))
    for kind in (bridge.RESPONSE, bridge.STATUS):
        for went_to in (root, tmp_path / "laifu", None):
            links_module.HANDLERS[kind](links_module.Received(link_id=LINK, kind=kind, message_id="m1", at=int(NOW),
                                                              body={}, age=0, root=went_to))

    assert home_module._homes == {str(root): started}
    assert handled == [("response", root), ("status", root)]


def _second_copy():
    """The plugin loaded a second time, as `hermes serve` loads it for the gateway and again for the dashboard."""
    name = "conduit_push_second_load"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT)]
        sys.modules[name] = package
    return importlib.import_module(f"{name}.room_bridge_home"), importlib.import_module(f"{name}.room_links")


@needs_node
def test_two_loads_run_one_server(tmp_path, monkeypatch, mailbox_url):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    furina, laifu = links_module.Links(tmp_path / "furina"), links_module.Links(tmp_path / "laifu")
    laifu.accept(furina.invite("Furina")["url"], "Laifu")
    loops = [links_module.LinkLoop(side.root) for side in (furina, laifu)]
    for _ in range(3):
        for loop in loops:
            loop.poll_once()
    for loop in loops:
        loop.stop()
    second_home, second_links = _second_copy()
    ports = _free_ports(2)
    homes = [module.Home(furina.root, ports=ports) for module in (home_module, second_home)]
    first_loop, second_loop = links_module.LinkLoop(furina.root), second_links.LinkLoop(furina.root)
    monkeypatch.setitem(links_module._loops, str(furina.root), first_loop)
    monkeypatch.setitem(second_links._loops, str(furina.root), second_loop)
    try:
        second_loop.poll_once()  # the second copy's loop takes the lease
        first_loop.poll_once()
        for home in homes:
            home.watch_pass()
        serving = [home.port for home in homes]
    finally:
        for home in homes:
            home.close()
        first_loop.stop()
        second_loop.stop()

    assert serving == [None, ports[0]]
