"""Copies of rooms over Links (designs/cross-owner-agents, N6): the home sends each person whose agents are in a room
its log, a page at a time, and their Hermes keeps a copy with Hermes' replica store, which their Conduit reads.

Furina hosts; Laifu and Mona join over link1 and link2 (test_room_invites' installations and mailbox). The home's
room log is a fake of c73cbcc4's gateway/hosted_rooms.read_events, and the member's replica store is
gateway/hosted_room_replicas' schema (verbatim) with ingest_page's refusals (9e01b44 and 930e5ed have the same).
"""
import hashlib
import json
import logging
import sqlite3
import stat
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_room_bridge_member import NOW, ROOT, _load
from test_room_invites import (BASE, FURINA, LAIFUS, LEGACY, MONAS, WORKY, _rooms, _sent, _started, api, draft_of,
                               invite_of, invites_module, rooms, slot)

copies_module = _load("conduit_push.room_copies", ROOT / "room_copies.py")

assert rooms  # the fixture, imported for these tests


class Log:
    """The home's room log as gateway/hosted_rooms.read_events reads it: pages after ``since_seq``, at most
    ``limit`` events, with the room's authority and latest seq; a room it doesn't have, or a since_seq ahead of
    the log, is a HostedRoomError."""

    def __init__(self):
        self.rooms = {}
        self.reads = []

    def add(self, room_id, kind, payload, actor=None):
        events = self.rooms.setdefault(room_id, [])
        seq = len(events) + 1
        events.append({"room_id": room_id, "seq": seq, "event_id": f"e{seq}", "kind": kind,
                       "actor": actor or {"kind": "gateway", "id": FURINA}, "authority_epoch": 1,
                       "payload": payload, "created_at": NOW + seq, "idempotent": False})
        return seq

    def read_events(self, db_path, *, room_id, since_seq=0, limit=100, include_disbanded=False):
        self.reads.append((db_path, room_id, since_seq, limit, include_disbanded))
        if room_id not in self.rooms:
            raise HermesError("hosted room not found")
        events = self.rooms[room_id]
        if since_seq > len(events):
            raise HermesError("since_seq is ahead of the hosted room log")
        page = events[since_seq:since_seq + limit]
        cursor = page[-1]["seq"] if page else since_seq
        return {"events": json.loads(json.dumps(page)), "cursor": cursor, "latest_seq": len(events),
                "has_more": cursor < len(events), "authority": {"gateway_id": FURINA, "epoch": 1}}


class HermesError(Exception):
    """gateway/hosted_rooms.HostedRoomError."""


class ReplicaError(HermesError):
    """gateway/hosted_room_replicas.ReplicaError."""


class ReplicaGapError(ReplicaError):
    pass


class ReplicaEpochRegressionError(ReplicaError):
    pass


def ingest_page(db_path, *, room_id, room_name, members, page, now=None):
    """gateway/hosted_room_replicas.ingest_page, with its schema and its refusals: a page that skips seqs the
    copy hasn't stored, or carries an older epoch, changes nothing."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS hosted_room_replicas (
            room_id TEXT PRIMARY KEY, name TEXT NOT NULL, members_json TEXT NOT NULL,
            authority_gateway_id TEXT NOT NULL,
            authority_epoch INTEGER NOT NULL CHECK (authority_epoch >= 1),
            last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
            latest_seq INTEGER NOT NULL DEFAULT 0, event_bytes INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hosted_room_replica_events (
            room_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK (seq >= 1), event_id TEXT NOT NULL,
            kind TEXT NOT NULL, actor_json TEXT NOT NULL, authority_epoch INTEGER,
            payload_json TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (room_id, seq)
        )""")
        row = conn.execute("SELECT authority_epoch, last_seq FROM hosted_room_replicas WHERE room_id=?",
                           (room_id,)).fetchone()
        stored_epoch, last_seq = row if row is not None else (0, 0)
        authority = page["authority"]
        if authority["epoch"] < stored_epoch:
            raise ReplicaEpochRegressionError("page authority epoch is older than the stored replica epoch")
        new = [event for event in page["events"] if event["seq"] > last_seq]
        if new and new[0]["seq"] != last_seq + 1:
            raise ReplicaGapError("page skips sequences the replica has not stored")
        for event in new:
            conn.execute("INSERT INTO hosted_room_replica_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (room_id, event["seq"], event["event_id"], event["kind"], json.dumps(event["actor"]),
                          event.get("authority_epoch"), json.dumps(event["payload"]), event["created_at"]))
        new_last = new[-1]["seq"] if new else last_seq
        latest = max(page.get("latest_seq") or new_last, new_last)
        if row is None:
            conn.execute("INSERT INTO hosted_room_replicas VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                         (room_id, room_name, json.dumps(members), authority["gateway_id"], authority["epoch"],
                          new_last, latest, NOW, NOW))
        else:
            conn.execute("""UPDATE hosted_room_replicas SET name=?, members_json=?, authority_epoch=?, last_seq=?,
                         latest_seq=? WHERE room_id=?""", (room_name, json.dumps(members), authority["epoch"],
                                                          new_last, latest, room_id))
    return {"room_id": room_id, "stored_seq": new_last, "ingested": len(new), "authority": authority,
            "caught_up": new_last >= latest}


class Hermes:
    """room_copies.Hermes with the fakes above: the home's log, and the member's replica store."""

    def __init__(self, log, *, copies=True, logs=True):
        self.log, self.copies, self.logs = log, copies, logs

    def reads_logs(self):
        return self.logs

    def keeps_copies(self):
        return self.copies

    def read_events(self, db_path, **fields):
        return self.log.read_events(db_path, **fields)

    def ingest_page(self, db_path, **fields):
        return ingest_page(db_path, **fields)

    @property
    def replica_error(self):
        return ReplicaError


@pytest.fixture
def copies(rooms):
    """The installations of test_room_invites, with copies: one log for the home, the replica store for each."""
    log = Log()
    rooms.log = log
    rooms.statuses = {}
    rooms.home.legacy.status = lambda room_id: rooms.statuses.get(room_id, {"working": False})
    for name in ("furina", "laifu", "mona"):
        rooms.wire.sides[name] = _rooms(rooms, name, copier=Hermes(log))
        setattr(rooms, name, rooms.wire.sides[name])
    return rooms


def started(side, links=("link1", "link2")):
    room_id = _started(side, links=links)
    side.log.add(room_id, "room.created", {"name": "Zoo trip"}, actor={"kind": "system", "id": "authority-control"})
    return room_id


def say(side, room_id, text, member="default"):
    return side.log.add(room_id, "message.member", {"member_id": member, "text": text},
                        actor={"kind": "member", "id": member})


def copy_of(side, name, room_id):
    return next(each for each in copies_module.copies(side.tmp_path / name) if each["room_id"] == room_id)


def log_of(side, name, room_id, since_seq=0):
    return copies_module.log(side.tmp_path / name, room_id, since_seq=since_seq)


def pages(side, link_id=None):
    return [(link, body, message_id) for link, body, message_id in _sent(side, "room.page")
            if link_id in (None, link)]


def texts(page):
    return [event["payload"].get("text") for event in page["events"]]


MEMBERS = [{"member_id": "default", "handle": "furina", "display_name": "Furina", "owner": None},
           {"member_id": slot("link1", 1), "handle": "laifu", "display_name": "Laifu", "owner": "Laifu"},
           {"member_id": slot("link2", 1), "handle": "mona", "display_name": "Mona", "owner": "Mona"}]


# --- The home's pages, and the copies they make -------------------------------------------


def test_each_person_in_the_room_gets_its_log_and_keeps_a_copy(copies):
    room_id = started(copies)
    say(copies, room_id, "Saturday at ten?")

    copies.furina.pass_once()

    for link_id in ("link1", "link2"):
        [(_, body, message_id)] = pages(copies, link_id)
        assert (body["v"], body["room_id"], body["name"], body["members"]) == (1, room_id, "Zoo trip", MEMBERS)
        assert [event["seq"] for event in body["page"]["events"]] == [1, 2]
        assert (body["page"]["latest_seq"], body["page"]["authority"]) == (2, {"gateway_id": FURINA, "epoch": 1})
        assert body["state"] == {"ended": None, "working": False, "activity": [], "routes": []}
        assert message_id.startswith("room.page:")
    # Each copy holds the log, and its Hermes said so.
    for name, link_id in (("laifu", "link1"), ("mona", "link2")):
        shown = copy_of(copies, name, room_id)
        assert (shown["name"], shown["link"], shown["from"], shown["last_seq"]) == ("Zoo trip", link_id, "Furina", 2)
        assert shown["members"] == MEMBERS and shown["status"] == "started"
        assert texts(log_of(copies, name, room_id)) == [None, "Saturday at ten?"]
    assert [body for _, body, _ in _sent(copies, "room.copied")] == [
        {"v": 1, "room_id": room_id, "seq": 2}, {"v": 1, "room_id": room_id, "seq": 2}]
    # Nothing new: nothing goes.
    copies.furina.pass_once()
    assert len(pages(copies)) == 2

    say(copies, room_id, "Ten works.", member=slot("link1", 1))
    copies.furina.pass_once()

    [(_, body, _)] = pages(copies, "link1")[1:]
    assert texts(body["page"]) == ["Ten works."]
    assert texts(log_of(copies, "laifu", room_id, since_seq=2)) == ["Ten works."]
    assert copy_of(copies, "mona", room_id)["last_seq"] == 3


def test_a_copy_is_owner_only_and_out_of_hermes_own_rooms(copies):
    room_id = started(copies)
    copies.furina.pass_once()

    path = copies.tmp_path / "laifu" / invites_module.DIRECTORY / copies_module.FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (copies.tmp_path / "laifu" / "shared-state.db").exists()
    assert copy_of(copies, "laifu", room_id)["room_id"] == room_id


def test_one_page_waits_for_its_answer_and_goes_again_after_a_minute(copies):
    room_id = started(copies, links=("link1",))
    copies.wire.holding = True

    copies.furina.pass_once()
    copies.furina.pass_once()
    assert len(pages(copies)) == 1
    copies.clock.now += copies_module.WAIT_S

    copies.furina.pass_once()

    [(_, first, first_id), (_, again, again_id)] = pages(copies)
    assert first == again and first_id != again_id  # a Link drops an id it has had
    copies.wire.release()
    assert copy_of(copies, "laifu", room_id)["last_seq"] == 1
    # Both answers came; the second changes nothing.
    assert [body["seq"] for _, body, _ in _sent(copies, "room.copied")] == [1, 1]
    copies.furina.pass_once()
    assert len(pages(copies)) == 2


def test_a_page_the_copy_cant_take_is_answered_with_what_it_holds(copies):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()
    for n in range(3):
        say(copies, room_id, f"line {n}")
    # The home thinks the copy has more than it does (its answer got lost, say, and rooms.json came back older).
    with invites_module.Store(copies.tmp_path / "furina").update() as state:
        state["drafts"][room_id]["copies"]["link1"]["acked"] = 3

    copies.furina.pass_once()

    assert [event["seq"] for event in pages(copies)[-1][1]["page"]["events"]] == [4]
    assert _sent(copies, "room.copied")[-1][1]["seq"] == 1  # refused: a gap
    copies.furina.pass_once()
    assert [event["seq"] for event in pages(copies)[-1][1]["page"]["events"]] == [2, 3, 4]
    assert copy_of(copies, "laifu", room_id)["last_seq"] == 4


def test_a_page_that_went_but_failed_goes_again_under_a_new_id(copies):
    room_id = started(copies, links=("link1",))
    copies.wire.holding = True
    send = copies.furina.send

    def lost(link_id, kind, body, message_id=None):
        send(link_id, kind, body, message_id=message_id)  # the mailbox took it; its answer didn't come back
        raise invites_module.room_links.LinkError("unreachable", "Couldn't reach the mailbox", 503)

    copies.furina.send = lost
    copies.furina.pass_once()
    copies.furina.send = send
    copies.furina.pass_once()

    [(_, first, first_id), (_, again, again_id)] = pages(copies)
    assert first == again and first_id != again_id
    copies.wire.release()
    assert copy_of(copies, "laifu", room_id)["last_seq"] == 1


def test_a_link_that_ended_gets_no_more_pages(copies):
    room_id = started(copies, links=("link1",))
    tried = []

    def unlinked(link_id, kind, body, message_id=None):
        tried.append(kind)
        raise invites_module.room_links.LinkError("not_linked", "That Link is gone", 404)

    copies.furina.send = unlinked
    copies.furina.pass_once()
    copies.clock.now += copies_module.WAIT_S
    say(copies, room_id, "anyone?")
    copies.furina.pass_once()

    assert tried.count("room.page") == 1


def test_a_page_that_cant_fit_isnt_sent(copies, monkeypatch, caplog):
    started(copies, links=("link1",))
    monkeypatch.setattr(copies_module, "PAGE_BYTES", 200)  # less than the room's members alone

    with caplog.at_level(logging.INFO):
        copies.furina.pass_once()
        copies.furina.pass_once()

    assert pages(copies) == []
    assert sum("fit" in record.getMessage() for record in caplog.records) == 1


def test_a_page_fits_an_envelope(copies):
    room_id = started(copies, links=("link1",))
    for n in range(4):
        say(copies, room_id, f"{n}" * 60_000)
    say(copies, room_id, "x" * (copies_module.PAGE_BYTES + 1))

    copies.furina.pass_once()
    copies.furina.pass_once()
    copies.furina.pass_once()

    sizes = [len(json.dumps(body, separators=(",", ":"))) for _, body, _ in pages(copies)]
    assert all(size <= copies_module.PAGE_BYTES for size in sizes)
    assert [[event["seq"] for event in body["page"]["events"]] for _, body, _ in pages(copies)] == [
        [1, 2, 3], [4, 5], [6]]
    # One event too big for any page goes without its payload, so the copy has no gap.
    assert pages(copies)[-1][1]["page"]["events"][-1]["payload"] == {"omitted": True}
    assert copy_of(copies, "laifu", room_id)["last_seq"] == 6


def test_a_page_counts_bytes_as_the_envelope_does(copies):
    room_id = started(copies, links=("link1",))
    say(copies, room_id, "日" * 30_000)  # 90 KB in UTF-8
    say(copies, room_id, "本" * 30_000)

    copies.furina.pass_once()
    copies.furina.pass_once()

    assert [[event["seq"] for event in body["page"]["events"]] for _, body, _ in pages(copies)] == [[1, 2], [3]]
    assert all(len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()) <= copies_module.PAGE_BYTES
               for _, body, _ in pages(copies))


def test_the_homes_own_agents_errors_stay_home(copies):
    room_id = started(copies, links=("link1",))
    turn = {"discussion_event_id": "d", "member_index": 0, "round_index": 0, "task_id": "t", "thread_id": "th",
            "turn_id": "u", "seen_through_seq": 1}
    copies.log.add(room_id, "turn.failed", {**turn, "member_id": "default",
                                            "error": "openai: 401 at /home/furina/.hermes/auth.json"})
    copies.log.add(room_id, "turn.failed", {**turn, "member_id": slot("link1", 1),
                                            "error": "Laifu's Hermes didn't answer.", "reason_code": "peer_timeout"})

    copies.furina.pass_once()

    failed = [event["payload"] for event in pages(copies)[0][1]["page"]["events"] if event["kind"] == "turn.failed"]
    assert [each["error"] for each in failed] == ["", "Laifu's Hermes didn't answer."]
    assert failed[1]["reason_code"] == "peer_timeout"
    # The home's own log is as it was.
    assert copies.log.rooms[room_id][1]["payload"]["error"].startswith("openai")


def test_what_the_room_is_doing_goes_with_it_and_a_change_sends_a_page(copies):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()
    copies.statuses[room_id] = {
        "running": True, "working": True, "member_activity": [{"member_id": slot("link1", 1), "status": "running"}],
        "peer_routes": [{"room_id": room_id, "member_id": slot("link1", 1), "status": "unavailable"}],
        "pending_actions": [{"kind": "approval", "member_id": "default", "command": "rm -rf ~/secret"}]}

    copies.furina.pass_once()

    [(_, body, _)] = pages(copies)[1:]
    assert body["page"]["events"] == []
    assert body["state"] == {"ended": None, "working": True,
                             "activity": [{"member_id": slot("link1", 1), "status": "running"}],
                             "routes": [{"member_id": slot("link1", 1), "status": "unavailable"}]}
    assert "secret" not in json.dumps(body)
    assert copy_of(copies, "laifu", room_id)["state"] == body["state"]
    copies.furina.pass_once()
    assert len(pages(copies)) == 2


def test_someone_who_left_gets_no_more_pages_and_keeps_their_copy(copies):
    room_id = started(copies)
    copies.furina.pass_once()
    copies.laifu.leave("link1", room_id, None)
    say(copies, room_id, "Bye Laifu")

    copies.furina.pass_once()

    assert len(pages(copies, "link1")) == 1 and len(pages(copies, "link2")) == 2
    shown = copy_of(copies, "laifu", room_id)
    assert (shown["status"], shown["last_seq"]) == ("left", 1)
    assert texts(log_of(copies, "laifu", room_id)) == [None]


def test_a_disbanded_room_pages_until_every_copy_has_its_end(copies):
    room_id = started(copies)
    copies.furina.pass_once()
    copies.log.add(room_id, "room.disbanded", {}, actor={"kind": "system", "id": "authority-control"})
    invites_module.disbanded(copies.tmp_path / "furina", room_id)
    copies.home.legacy.status = lambda room_id: (_ for _ in ()).throw(KeyError(room_id))

    copies.furina.pass_once()

    for link_id, name in (("link1", "laifu"), ("link2", "mona")):
        [(_, body, _)] = pages(copies, link_id)[1:]
        assert [event["kind"] for event in body["page"]["events"]] == ["room.disbanded"]
        assert body["state"] == {"ended": "disbanded", "working": False, "activity": [], "routes": []}
        shown = copy_of(copies, name, room_id)
        assert (shown["status"], shown["reason"], shown["last_seq"]) == ("ended", "disbanded", 2)
    # The next pass finds both copies caught up, and the ones after it don't read the log for them again.
    copies.furina.pass_once()
    reads = len(copies.log.reads)
    copies.clock.now += 3600
    copies.furina.pass_once()
    assert len(pages(copies)) == 4 and len(copies.log.reads) == reads


def test_pages_from_anyone_but_the_rooms_home_are_dropped(copies, caplog):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()
    [(_, body, _)] = pages(copies)
    received = lambda link_id, changed: invites_module.room_links.Received(
        link_id=link_id, kind="room.page", message_id=f"m{len(caplog.records)}", at=int(NOW),
        body={**body, **changed}, root=copies.tmp_path / "laifu")
    caplog.set_level(logging.INFO)
    forged = {"page": {**body["page"], "events": [{**body["page"]["events"][0], "seq": 2, "event_id": "f"}]}}

    copies.laifu.handle(received("link2", forged))  # another Link
    copies.laifu.handle(received("link1", {"room_id": "conduit-other", **forged}))  # a room it wasn't invited to
    copies.laifu.handle(received("link1", {"page": {**forged["page"], "authority": {"gateway_id": "x", "epoch": 1}}}))
    copies.laifu.handle(received("link1", {"members": "everyone"}))

    assert copy_of(copies, "laifu", room_id)["last_seq"] == 1
    assert sum("Linked Rooms dropped a room.page" in record.message for record in caplog.records) == 4
    assert len(_sent(copies, "room.copied")) == 1


def test_answers_that_arent_from_a_copy_are_dropped(copies):
    room_id = started(copies, links=("link1",))
    copies.wire.holding = True
    copies.furina.pass_once()
    copies.wire.held.clear()
    copies.wire.holding = False

    for link_id, body in (("link2", {"v": 1, "room_id": room_id, "seq": 1}),
                          ("link1", {"v": 1, "room_id": room_id, "seq": -1}),
                          ("link1", {"v": 1, "room_id": "conduit-other", "seq": 1}),
                          # More than the home sent: the next page would read ahead of its log.
                          ("link1", {"v": 1, "room_id": room_id, "seq": 99})):
        copies.furina.handle(invites_module.room_links.Received(link_id=link_id, kind="room.copied",
                                                                message_id="m", at=int(NOW), body=body))

    copy = invites_module.Store(copies.tmp_path / "furina").read()["drafts"][room_id]["copies"]["link1"]
    assert copy["acked"] == 0 and copy["sent"]["to"] == 1


def test_a_hermes_without_the_replica_store_keeps_no_copies(copies, caplog):
    copies.wire.sides["laifu"] = copies.laifu = _rooms(copies, "laifu", copier=Hermes(copies.log, copies=False))
    room_id = started(copies, links=("link1",))
    caplog.set_level(logging.INFO)

    copies.furina.pass_once()
    copies.clock.now += copies_module.WAIT_S
    copies.furina.pass_once()

    assert copies_module.copies(copies.tmp_path / "laifu") == []
    assert _sent(copies, "room.copied") == []
    assert sum("can't keep copies" in record.message for record in caplog.records) == 1
    with pytest.raises(copies_module.CopiesError) as refused:
        copies_module.log(copies.tmp_path / "laifu", room_id)
    assert refused.value.code == "no_copy"


def test_a_home_without_the_log_reader_sends_nothing(copies, caplog):
    copies.wire.sides["furina"] = copies.furina = _rooms(copies, "furina", copier=Hermes(copies.log, logs=False))
    caplog.set_level(logging.INFO)
    started(copies, links=("link1",))

    copies.furina.pass_once()
    copies.furina.pass_once()

    assert pages(copies) == []
    assert sum("can't send copies" in record.message for record in caplog.records) == 1


def test_forget_deletes_a_copy_once_the_room_is_over(copies):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()

    # Still in it: its posts and their framing need it (leave first).
    with pytest.raises(copies_module.CopiesError) as refused:
        copies_module.forget(copies.tmp_path / "laifu", room_id)
    assert (refused.value.code, refused.value.status) == ("still_in", 409)
    assert copy_of(copies, "laifu", room_id)["last_seq"] == 1

    copies.furina.disbanded(room_id)
    for _ in range(3):
        copies.furina.pass_once()
        copies.laifu.pass_once()
    assert copies_module.forget(copies.tmp_path / "laifu", room_id) == {"room_id": room_id, "forgotten": True}

    assert copies_module.copies(copies.tmp_path / "laifu") == []
    assert copies_module.forget(copies.tmp_path / "laifu", room_id) == {"room_id": room_id, "forgotten": False}


def test_forget_refuses_a_room_this_hermes_hosts(copies):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()

    with pytest.raises(copies_module.CopiesError) as refused:
        copies_module.forget(copies.tmp_path / "furina", room_id)
    assert (refused.value.code, refused.value.status) == ("hosted", 409)


def test_the_log_reads_a_page_at_a_time(copies):
    room_id = started(copies, links=("link1",))
    for n in range(5):
        say(copies, room_id, f"line {n}")
    copies.furina.pass_once()

    page = log_of(copies, "laifu", room_id, since_seq=1)
    assert texts(page) == [f"line {n}" for n in range(5)]
    assert (page["cursor"], page["latest_seq"], page["has_more"]) == (6, 6, False)
    first = copies_module.log(copies.tmp_path / "laifu", room_id, since_seq=0, limit=2)
    assert ([event["seq"] for event in first["events"]], first["has_more"]) == ([1, 2], True)
    assert set(first["events"][0]) == {"room_id", "seq", "event_id", "kind", "actor", "authority_epoch", "payload",
                                      "created_at"}
    assert first["authority"] == {"gateway_id": FURINA, "epoch": 1}
    for since, limit in ((-1, 10), (0, 0), ("x", 10), (0, copies_module.MAX_LOG_LIMIT + 1)):
        with pytest.raises(copies_module.CopiesError):
            copies_module.log(copies.tmp_path / "laifu", room_id, since_seq=since, limit=limit)


def test_the_list_says_which_agents_of_yours_are_in_it(copies):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()

    shown = copy_of(copies, "laifu", room_id)

    assert shown["yours"] == [{"member_id": slot("link1", 1), "profile": "default", "handle": "laifu"}]
    assert shown["started_at"] == int(NOW) and shown["updated_at"] == NOW
    assert (shown["reason"], shown["ended_at"]) == (None, None)
    # Its invite forgotten a month after it ended, the copy stays, with what the copy knows: your agents too.
    with invites_module.Store(copies.tmp_path / "laifu").update() as state:
        state["invites"].clear()
    shown = copy_of(copies, "laifu", room_id)
    assert (shown["status"], shown["yours"], shown["from"], shown["started_at"]) == (
        "ended", [{"member_id": slot("link1", 1), "profile": "default", "handle": "laifu"}], "Furina", None)
    assert all(copies_module.YOURS not in member for member in shown["members"])


# --- Routes ---------------------------------------------------------------------------------


@pytest.fixture
def client(copies, monkeypatch):
    monkeypatch.setattr(api, "_room_links_root", lambda: copies.tmp_path / "laifu")
    monkeypatch.setattr(api, "_start_room_handlers", lambda root: None)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_routes_list_read_and_forget_copies(copies, client):
    room_id = started(copies, links=("link1",))
    say(copies, room_id, "Hi")
    copies.furina.pass_once()

    listed = client.get(f"{BASE}/rooms/copies")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert [(each["room_id"], each["last_seq"]) for each in listed.json()["copies"]] == [(room_id, 2)]
    read = client.get(f"{BASE}/rooms/copies/log", params={"room_id": room_id, "since_seq": 1})
    assert read.json()["ok"] and texts(read.json()) == ["Hi"]
    missing = client.get(f"{BASE}/rooms/copies/log", params={"room_id": "conduit-other"})
    assert (missing.status_code, missing.json()["code"]) == (404, "no_copy")
    refused = client.delete(f"{BASE}/rooms/copies", params={"room_id": room_id})
    assert (refused.status_code, refused.json()["code"]) == (409, "still_in")
    copies.furina.disbanded(room_id)
    for _ in range(3):
        copies.furina.pass_once()
        copies.laifu.pass_once()
    gone = client.delete(f"{BASE}/rooms/copies", params={"room_id": room_id})
    assert gone.json() == {"ok": True, "room_id": room_id, "forgotten": True}
    assert client.get(f"{BASE}/rooms/copies").json()["copies"] == []
    assert "room-copies" in api.ROUTE_CAPABILITIES


def test_the_cli_lists_copies(copies, capsys):
    room_id = started(copies, links=("link1",))
    copies.furina.pass_once()
    cli = _load("conduit_push.cli", ROOT / "cli.py")

    assert cli._room_copies("", copies.tmp_path / "laifu") == 0

    out = capsys.readouterr().out
    assert room_id in out and "Zoo trip" in out and "Furina" in out and "1 event" in out
    assert cli._room_copies(room_id, copies.tmp_path / "laifu") == 0
    out = capsys.readouterr().out
    assert "@laifu" in out and "Laifu's" in out
    assert cli._room_copies("conduit-other", copies.tmp_path / "laifu") == 1


def test_a_corrupt_copy_row_is_skipped(copies, caplog):
    first = started(copies, links=("link1",))
    second = started(copies, links=("link1",))
    say(copies, second, "Saturday?")
    copies.furina.pass_once()
    with sqlite3.connect(copies_module.path(copies.tmp_path / "laifu")) as conn:
        conn.execute("UPDATE conduit_room_copies SET state_json='{' WHERE room_id=?", (first,))
        conn.execute("UPDATE hosted_room_replica_events SET payload_json='{' WHERE room_id=? AND seq=1", (second,))

    assert [each["room_id"] for each in copies_module.copies(copies.tmp_path / "laifu")] == [second]
    assert [event["seq"] for event in log_of(copies, "laifu", second)["events"]] == [2]
    assert log_of(copies, "laifu", second)["cursor"] == 2
    assert "skipped its copy of room" in caplog.text and "skipped an event of room" in caplog.text


def test_a_copy_path_with_a_question_mark_reads(copies, tmp_path):
    root = tmp_path / "odd?home#1"
    file = copies_module.path(root)
    file.parent.mkdir(parents=True)
    ingest_page(file, room_id="conduit-odd", room_name="Odd", members=[], page={
        "events": [{"seq": 1, "event_id": "e1", "kind": "room.created", "actor": {"kind": "system", "id": "x"},
                    "authority_epoch": 1, "payload": {}, "created_at": NOW}],
        "latest_seq": 1, "authority": {"gateway_id": FURINA, "epoch": 1}})

    assert [event["seq"] for event in copies_module.log(root, "conduit-odd")["events"]] == [1]
