"""The home's side of the bridge, for Linked Rooms (designs/cross-owner-agents, N4b).

A room on this installation can have members whose agents are on a Hermes
linked with this one (room_links.py). Hermes reaches a member's agent
through that Hermes' runs API, at its route's URL; for an agent across a
Link, the URL is this side's loopback server,
``http://127.0.0.1:<port>/l/<link>/p/<profile>`` (room_bridge.url). Each
request it takes goes over the Link as a roomlink.request, and is answered
with the member's roomlink.response (room_bridge_member.py). Status reads,
which Hermes makes every few seconds while a turn runs, are answered here
from the statuses the member sends, with no envelope; a stop and a revoke
are answered at once and reach the member after.

A member that doesn't answer is never a 4xx, which Hermes would take as a
turn that wasn't admitted and try forever: it's a 503 or a 504, which
Hermes asks again at once and then gives up on as ambiguous. Once the
room's lease is up, Hermes recovers that turn by asking a third time, and
holds the room until the recovery succeeds: that ask gets a run of this
side's own, which reads failed ("<name>'s Hermes didn't answer."), and the
room goes on. The member drops what it missed. A run the member goes
quiet on, or that runs past 12 minutes, reads failed, and a stop goes to
the member.

Only requests with a HermesRoom grant leave this host, and only over the
Link their URL names. The server runs where the rooms lease is, on a port
kept in bridge.json with the runs it answers for, so a room's route
outlives restarts.
"""

from __future__ import annotations

import collections
import http.server
import json
import logging
import re
import secrets
import socketserver
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import room_bridge, room_envelope, room_links

logger = logging.getLogger(__name__)

# The first free one, kept in bridge.json from then on.
PORTS = range(8700, 8800)
# Hermes' client gives a request 30 s and asks again at once after a 504:
# half of it each, so the retry has its own.
WAIT_S = 15.0
# An answer that comes after its wait is still taken this long, so an
# admission then is kept for Hermes' retry.
LATE_S = 120
MAX_WAITING = 16
PER_MINUTE = 60
# Admissions, for Hermes' retry, and runs once they've ended.
KEPT_S = 3600
# Three of the member's heartbeats; its own cap, and two minutes more.
SILENT_S = 180
CAP_S = 12 * 60
# Stops and revokes the mailbox doesn't take go on later, this long.
OUTBOX_S = 3600
WATCH_EVERY_S = 1.0
MAX_BACKOFF_S = 300
REQUEST_TIMEOUT_S = 10
MAX_BODY_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ERROR_BYTES = 4096
MAX_EARLY = 256
# Hermes' client asks twice before it gives a turn up as ambiguous; its next
# ask is the recovery.
MISSED_ASKS = 2

SILENT = "{name}'s Hermes stopped answering during this turn."
CAPPED = "This turn ran past 12 minutes on {name}'s Hermes."
STOPPED = "The turn was stopped on {name}'s Hermes."
UNANSWERED = "{name}'s Hermes didn't answer."
REFUSED = "The member's Hermes refused the request."
_MESSAGES = {
    "invalid_host": "This bridge answers only requests made to its own address on this host.",
    "not_found": "This bridge doesn't carry that request.",
    "invalid_request": "This bridge couldn't read that request.",
    "request_too_large": "That request is larger than this bridge carries.",
    "room_grant_required": "This bridge carries only requests with a room's own grant.",
    "room_approval_elsewhere": "The member's approvals are answered by its owner, on their own Hermes.",
    "invalid_room_dispatch": "That dispatch isn't one this bridge carries.",
    "invalid_room_grant": "This Hermes isn't linked with the member's Hermes.",
    "member_unavailable": "The member's Hermes didn't answer.",
}
_BUSY = "Too many requests are waiting on the member's Hermes."
_NOT_TAKEN = "The rooms mailbox didn't take the request."
_UNCONFIRMED = "The member's Hermes hasn't confirmed the Link yet."

_TARGET = re.compile(r"/l/(?P<link>[A-Za-z0-9_-]{1,48})/p/(?P<profile>[a-z0-9][a-z0-9_-]{0,63})(?P<path>/[^?#]*)")
_GRANT = re.compile(rf"HermesRoom ([\x21-\x7e]{{1,{room_bridge.MAX_GRANT_BYTES}}})")
_KEY = re.compile(rf"[\x21-\x7e]{{1,{room_bridge.MAX_KEY_CHARS}}}")


class _Answered(Exception):
    """An answer to give now, from deep in a request's handling."""

    def __init__(self, status: int, body: dict) -> None:
        super().__init__(status)
        self.status, self.body = status, body


@dataclass(frozen=True)
class _Asking:
    """Who a request is for: the Link and its name, the member's profile,
    and the request's grant and the scope its payload says it's for."""

    link: str
    name: str
    profile: str
    grant: str = field(repr=False)
    scope: Optional[dict]


@dataclass
class _Asked:
    """A request left for the member, and its answer when it comes."""

    asking: _Asking
    route: str
    run: Optional[str]
    key: Optional[str]
    at: float
    waiting: bool = True
    answer: Optional[tuple[int, dict, Optional[dict]]] = None
    event: threading.Event = field(default_factory=threading.Event)


class Home:
    """The home's side for the installation at ``root``: ``send`` is
    Links.send and ``links`` Links.get (a Link as Links shows it, or None).
    ``leased`` says whether this process's loop holds the rooms lease, and
    ``linked`` whether there's a Link to listen for."""

    def __init__(self, root: Any, *, send: Optional[Callable[..., Any]] = None,
                 links: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 wait_s: float = WAIT_S, ports: range = PORTS, leased: Optional[Callable[[], bool]] = None,
                 linked: Optional[Callable[[], bool]] = None) -> None:
        self.root = Path(root)
        self.store = room_bridge.Store(self.root)
        own = room_links.Links(self.root)
        self.send = send if send is not None else own.send
        self.links = links if links is not None else own.get
        self.leased = leased if leased is not None else (lambda: room_links.holds_lease(self.root))
        self.linked = linked if linked is not None else (lambda: bool(own.view()["links"]))
        self.clock = clock
        self.monotonic = monotonic
        self.wait_s = wait_s
        self.ports = ports
        self.port: Optional[int] = None
        self._server: Optional[_Server] = None
        self._serving_since = 0.0
        self._serve_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._lock = threading.Lock()
        self._asked: dict[str, _Asked] = {}
        self._waiting: collections.Counter[str] = collections.Counter()
        self._recent: dict[str, collections.deque[float]] = {}
        # Statuses for a run this side doesn't have yet: one can come before
        # its admission's answer, which goes on another of the member's threads.
        self._early: dict[tuple[str, str], tuple[float, int, dict]] = {}
        # Dispatches the member left unanswered, by Link and key: when the
        # first went, and how many.
        self._missed: dict[tuple[str, str], tuple[float, int]] = {}

    # --- Requests -----------------------------------------------------------------

    def answer(self, method: str, target: str, headers: Any, body: bytes) -> tuple[int, dict]:
        """A request to the server, its headers an http.server message: the
        status and JSON object to answer with."""
        try:
            return self._answer(method, target, headers, body)
        except _Answered as answered:
            return answered.status, answered.body
        except Exception as exc:  # noqa: BLE001 - whatever went wrong here, never a 4xx
            # Only the type: a message can quote what it was handling.
            logger.warning("Linked Rooms' bridge couldn't answer a request (%s)", type(exc).__name__)
            return 503, _error("member_unavailable")

    def _answer(self, method: str, target: str, headers: Any, body: bytes) -> tuple[int, dict]:
        port = self.port
        if port is None or _one(headers, "Host") != f"127.0.0.1:{port}":
            return 403, _error("invalid_host")
        found = _TARGET.fullmatch(target)
        if found is None:
            return 404, _error("not_found")
        link_id, profile, path = found["link"], found["profile"], found["path"]
        grant = _grant(headers)
        if grant is None:
            # Hermes' Bearer key included (it sends one to mint an invite):
            # nothing but a room's grant leaves this host.
            return 401, _error("room_grant_required")
        route, run_id = room_bridge.route(method, path)
        if route is None:
            return 404, _error("not_found")
        if route == "approval":
            # The agent's owner answers them, on their own Hermes (decision 7).
            return 403, _error("room_approval_elsewhere")
        link = self.links(link_id)
        status = link.get("status") if isinstance(link, Mapping) else None
        if status == "waiting":
            return 503, _error("member_unavailable", _UNCONFIRMED)
        if status != "linked":
            # A Link this installation doesn't have, or has ended: N2 settles
            # the turn as cut off, since that Hermes no longer takes the room.
            return 403, _error("invalid_room_grant")
        asking = _Asking(link_id, str(link.get("name") or "The member"), profile, grant,
                         room_bridge.grant_scope(grant))
        payload = _json(body) if method == "POST" else {}
        if route == "dispatch":
            return self._dispatch(asking, _one(headers, "Idempotency-Key"), payload)
        if route == "read":
            return self._read(asking, run_id)
        if route == "stop":
            return self._stop(asking, run_id)
        if route == "revoke":
            return self._revoke(asking)
        if route == "capabilities":
            status_code, answer, _ = self._ask(asking, "capabilities", "GET", path)
            return _passed(status_code, answer, room_bridge.CAPABILITY_FIELDS)
        sent = {"ttl_seconds": payload["ttl_seconds"]} if "ttl_seconds" in payload else {}
        status_code, answer, _ = self._ask(asking, "refresh", "POST", path, body=sent)
        return _passed(status_code, answer, room_bridge.GRANT_FIELDS)

    def _dispatch(self, asking: _Asking, key: Optional[str], payload: dict) -> tuple[int, dict]:
        if key is not None and not _KEY.fullmatch(key):
            return 400, _error("invalid_request")
        if not isinstance(payload.get("hosted_room_dispatch"), dict):
            return 403, _error("invalid_room_dispatch")
        if key is not None and asking.scope is not None:
            kept = self.store.read()["home"]["kept"].get(f"{asking.link}/{key}")
            if kept is not None and (kept.get("scope"), kept.get("profile")) == (asking.scope, asking.profile):
                # Hermes' retry after a 504, or its recovery a minute later.
                return 202, {"run_id": kept["run"], "status": kept["status"], "replayed": True}
            with self._lock:
                missed = self._missed.get((asking.link, key), (0.0, 0))[1]
            if kept is None and missed >= MISSED_ASKS:
                return self._unanswered(asking, key)
        try:
            status, answer = self._admission(asking, key, payload)
        except _Answered as answered:
            status, answer = answered.status, answered.body
        with self._lock:
            if key is not None and status >= 500:
                first, count = self._missed.get((asking.link, key), (self.monotonic(), 0))
                self._missed[(asking.link, key)] = (first, count + 1)
            elif key is not None:
                self._missed.pop((asking.link, key), None)
        return status, answer

    def _admission(self, asking: _Asking, key: Optional[str], payload: dict) -> tuple[int, dict]:
        """The member's answer to a dispatch, as Hermes' client reads it."""
        # Its input repeats the dispatch's prompt: without it, a 128 KiB
        # prompt fits the mailbox's 256 KiB.
        sent = {name: value for name, value in payload.items() if name != "input"}
        status, answer, _ = self._ask(asking, "dispatch", "POST", "/v1/runs", key=key, body=sent)
        if 200 <= status < 300:
            run_id = answer.get("run_id")
            if not _is_run_id(run_id):
                return 503, _error("member_unavailable")
            return status, {"run_id": run_id, "status": _admission_status(answer),
                            "replayed": answer.get("replayed") is True}
        return _refused(status, answer)

    def _unanswered(self, asking: _Asking, key: str) -> tuple[int, dict]:
        """Hermes' recovery of a dispatch the member left unanswered (or
        answered 5xx) twice: a run of this side's own, which reads failed.
        Hermes holds the room until the recovery succeeds, and asking the
        member again could start a turn the room has moved past; what it
        missed, it drops. Kept as an admission, so asking again gets it."""
        now, run_id = self.clock(), f"unanswered_{secrets.token_hex(12)}"
        with self.store.update() as state:
            home = state["home"]
            kept = home["kept"].setdefault(f"{asking.link}/{key}", {
                "run": run_id, "status": "started", "at": now, "scope": asking.scope, "profile": asking.profile,
                "own": True})
            if kept["run"] == run_id:
                home["runs"][f"{asking.link}/{run_id}"] = {
                    "link": asking.link, "run": run_id, "profile": asking.profile, "grant": asking.grant,
                    "scope": asking.scope, "name": asking.name, "admitted": now, "heard": now, "seq": 0,
                    "shown": {"status": "failed", "error": UNANSWERED.format(name=asking.name)}, "ended": now}
                logger.info("Linked Rooms' bridge settled a turn over Link %s as unanswered", asking.link)
        return 202, {"run_id": kept["run"], "status": kept["status"], "replayed": kept["run"] != run_id}

    def _read(self, asking: _Asking, run_id: str) -> tuple[int, dict]:
        run = self._run(asking, self.store.read()["home"], run_id)
        if run is not None:
            return 200, {"run_id": run_id, **run["shown"]}
        # One this side hasn't seen: its admission's answer was lost, or
        # bridge.json was. Asked of the member once, and kept from then on.
        status, answer, _ = self._ask(asking, "read", "GET", f"/v1/runs/{run_id}", run=run_id)
        if 200 <= status < 300:
            shown = _shown(answer) if answer.get("run_id") == run_id else None
            if shown is None:
                return 503, _error("member_unavailable")
            return status, {"run_id": run_id, **shown}
        return _refused(status, answer)

    def _stop(self, asking: _Asking, run_id: str) -> tuple[int, dict]:
        now, answer = self.clock(), None
        with self.store.update() as state:
            home = state["home"]
            run = self._run(asking, home, run_id)
            if run is not None:
                if run["ended"] is None:
                    # It reads cancelled at once, which ends the turn here;
                    # the member stops it whenever it reads the stop.
                    run.update(shown={"status": "cancelled"}, ended=now)
                    home["outbox"].append(_outgoing(asking.link, asking.profile, f"/v1/runs/{run_id}/stop",
                                                    asking.grant, now))
                answer = {"run_id": run_id, "status": run["shown"]["status"]}
        if answer is not None:
            self._flush()
            return 200, answer
        status, body, _ = self._ask(asking, "stop", "POST", f"/v1/runs/{run_id}/stop")
        if 200 <= status < 300:
            if body.get("run_id") != run_id or _shown(body) is None:
                return 503, _error("member_unavailable")
            return status, {"run_id": run_id, "status": body["status"]}
        return _refused(status, body)

    def _revoke(self, asking: _Asking) -> tuple[int, dict]:
        with self._lock:
            self._take_turn(asking.link)
        now = self.clock()
        with self.store.update() as state:
            state["home"]["outbox"].append(_outgoing(asking.link, asking.profile, "/v1/room-members/grants/revoke",
                                                     asking.grant, now))
        self._flush()
        return 200, {"revoked": True}

    def _run(self, asking: _Asking, home: dict, run_id: str) -> Optional[dict]:
        """The run this side answers for, when the request's grant is for its
        scope (a refreshed grant keeps the scope) and its profile; a request
        for anything else is the member's to answer."""
        run = home["runs"].get(f"{asking.link}/{run_id}") if asking.scope is not None else None
        if run is None or (run.get("scope"), run.get("profile")) != (asking.scope, asking.profile):
            return None
        return run

    def _ask(self, asking: _Asking, route: str, method: str, path: str, *, key: Optional[str] = None,
             body: Optional[dict] = None, run: Optional[str] = None) -> tuple[int, dict, Optional[dict]]:
        """The member's answer to one request: its status, body and, on an
        admission, the run's status then. _Answered with a 503 when it can't
        go, or a 504 when no answer came in time."""
        request = {"v": room_bridge.VERSION, "profile": asking.profile, "method": method, "path": path,
                   "grant": asking.grant}
        if key is not None:
            request["key"] = key
        if body is not None:
            request["body"] = body
        message_id = room_envelope.new_message_id()
        asked = _Asked(asking, route, run, key, self.monotonic())
        with self._lock:
            self._forget_asked()
            if self._waiting[asking.link] >= MAX_WAITING:
                raise _Answered(503, _error("member_unavailable", _BUSY))
            self._take_turn(asking.link)
            # Before it goes: the answer can come before send returns.
            self._asked[message_id] = asked
            self._waiting[asking.link] += 1
        try:
            try:
                self.send(asking.link, room_bridge.REQUEST, request, message_id=message_id)
            except Exception as exc:  # noqa: BLE001 - the mailbox's refusal, or a request sealing can't take
                with self._lock:
                    self._asked.pop(message_id, None)
                logger.info("Linked Rooms' bridge couldn't leave a request for Link %s (%s)", asking.link,
                            exc.code if isinstance(exc, room_links.LinkError) else type(exc).__name__)
                raise _Answered(503, _error("member_unavailable", _NOT_TAKEN)) from None
            asked.event.wait(self.wait_s)
        finally:
            with self._lock:
                asked.waiting = False
                self._waiting[asking.link] -= 1
                answer = asked.answer
        if answer is None:
            # Never a 4xx: Hermes asks again at once, then gives the turn up
            # as ambiguous and recovers it (_unanswered).
            raise _Answered(504, _error("member_unavailable"))
        return answer

    def _take_turn(self, link_id: str) -> None:
        """Counts a request toward the Link's minute: _Answered (503) past
        PER_MINUTE. Called under the lock."""
        recent = self._recent.setdefault(link_id, collections.deque())
        now = self.monotonic()
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if len(recent) >= PER_MINUTE:
            raise _Answered(503, _error("member_unavailable", _BUSY))
        recent.append(now)

    def _forget_asked(self) -> None:
        """Forgets requests answered or given up on more than LATE_S ago.
        Called under the lock."""
        now = self.monotonic()
        for message_id in [message_id for message_id, asked in self._asked.items()
                           if not asked.waiting and now - asked.at > LATE_S]:
            del self._asked[message_id]
        self._early = {key: early for key, early in self._early.items() if now - early[0] <= LATE_S}
        self._missed = {key: missed for key, missed in self._missed.items() if now - missed[0] <= KEPT_S}

    # --- What the member sends --------------------------------------------------------

    def on_response(self, received: room_links.Received) -> None:
        """A roomlink.response, for a request this side left over the same
        Link: what it tells this side is kept, and its wait ends."""
        found = _response(received.body)
        if found is None:
            logger.info("Linked Rooms' bridge dropped a roomlink.response from Link %s that isn't one",
                        received.link_id)
            return
        re_id, status, body, run = found
        with self._lock:
            asked = self._asked.get(re_id)
        if asked is None or asked.asking.link != received.link_id:
            # Asked too long ago (Hermes has asked again since), or not by this side.
            return
        if 200 <= status < 300:
            self._record(asked, body, run)
        with self._lock:
            if asked.answer is None:
                asked.answer = (status, body, run)
        asked.event.set()

    def _record(self, asked: _Asked, body: dict, run: Optional[dict]) -> None:
        """What a 2xx answer tells this side: an admission's run, and the
        admission for Hermes' retry; or the status of a run it read."""
        if asked.asking.scope is None or asked.route not in ("dispatch", "read"):
            return
        run_id = body.get("run_id")
        if not _is_run_id(run_id) or (asked.route == "read" and run_id != asked.run):
            return
        source = run if asked.route == "dispatch" else body
        shown = _shown(source) if isinstance(source, Mapping) and source.get("run_id") == run_id else None
        if shown is None and asked.route == "read":
            return
        with self._lock:
            early = self._early.pop((asked.asking.link, run_id), None)
        asking, now = asked.asking, self.clock()
        with self.store.update() as state:
            home = state["home"]
            key = f"{asking.link}/{run_id}"
            if key not in home["runs"]:
                record = home["runs"][key] = {
                    "link": asking.link, "run": run_id, "profile": asking.profile, "grant": asking.grant,
                    "scope": asking.scope, "name": asking.name, "admitted": now, "heard": now, "seq": 0,
                    "shown": shown or {"status": "queued"}, "ended": None}
                if record["shown"]["status"] in room_bridge.ENDED:
                    record["ended"] = now
                if early is not None:
                    _apply(record, early[1], early[2], now)
                kept = home["kept"].get(f"{asking.link}/{asked.key}") if asked.route == "dispatch" else None
                if kept is not None and kept.get("own") and record["ended"] is None:
                    # Hermes has settled the turn as unanswered: the member's run stops.
                    record.update(shown={"status": "cancelled"}, ended=now)
                    home["outbox"].append(_outgoing(asking.link, asking.profile, f"/v1/runs/{run_id}/stop",
                                                    asking.grant, now))
            if asked.route == "dispatch" and asked.key is not None:
                home["kept"].setdefault(f"{asking.link}/{asked.key}", {
                    "run": run_id, "status": _admission_status(body), "at": now, "scope": asking.scope,
                    "profile": asking.profile})

    def on_status(self, received: room_links.Received) -> None:
        """A roomlink.status: a run's status as the member shows it, newer
        than the last one by its seq."""
        found = _status(received.body)
        if found is None:
            logger.info("Linked Rooms' bridge dropped a roomlink.status from Link %s that isn't one",
                        received.link_id)
            return
        run_id, seq, shown = found
        now = self.clock()
        with self.store.update() as state:
            run = state["home"]["runs"].get(f"{received.link_id}/{run_id}")
            if run is not None:
                _apply(run, seq, shown, now)
                return
        with self._lock:
            key = (received.link_id, run_id)
            known = self._early.get(key)
            if known is None and len(self._early) >= MAX_EARLY:
                return
            if known is None or seq > known[1]:
                self._early[key] = (self.monotonic(), seq, shown)

    # --- Watching ----------------------------------------------------------------------

    def watch_pass(self) -> None:
        """A pass of the watching. The server runs where this copy's loop
        holds the lease, once there's a Link, and goes on while the lease is
        held; elsewhere it stops."""
        if not self.leased():
            self.close()
            return
        if self._server is None and not self.linked():
            return
        self.serve()
        self.tick()

    def tick(self) -> None:
        """One look at the runs this side answers for: one the member went
        quiet on, or one past 12 minutes, reads failed and is stopped there;
        what ended an hour ago is forgotten; the stops and revokes waiting go."""
        now = self.clock()
        with self.store.update() as state:
            home = state["home"]
            for run in home["runs"].values():
                if run["ended"] is not None:
                    continue
                if now - run["admitted"] >= CAP_S:
                    why = CAPPED
                elif now - max(run["heard"], self._serving_since) >= SILENT_S:
                    # Counted from when this side was there to hear it.
                    why = SILENT
                else:
                    continue
                run.update(shown={"status": "failed", "error": why.format(name=run["name"])}, ended=now)
                home["outbox"].append(_outgoing(run["link"], run["profile"], f"/v1/runs/{run['run']}/stop",
                                                run["grant"], now))
            home["runs"] = {key: run for key, run in home["runs"].items()
                            if run["ended"] is None or now - run["ended"] <= KEPT_S}
            home["kept"] = {key: kept for key, kept in home["kept"].items() if now - kept["at"] <= KEPT_S}
            home["outbox"] = [item for item in home["outbox"] if now - item["at"] <= OUTBOX_S]
        self._flush()
        with self._lock:
            self._forget_asked()

    def _flush(self) -> None:
        """Sends the stops and revokes waiting: each goes once the mailbox
        takes it, or when its Link has ended. A Link that refuses one gets
        no more this pass."""
        with self._flush_lock:
            gone, down = set(), set()
            for item in self.store.read()["home"]["outbox"]:
                link_id = item["link"]
                if link_id in down:
                    continue
                link = self.links(link_id)
                status = link.get("status") if isinstance(link, Mapping) else None
                if status == "waiting":
                    continue
                if status == "linked":
                    try:
                        self.send(link_id, room_bridge.REQUEST, item["request"], message_id=item["id"])
                    except room_links.LinkError as exc:
                        logger.debug("Linked Rooms' bridge couldn't send a stop or revoke over Link %s (%s)",
                                     link_id, exc.code)
                        down.add(link_id)
                        continue
                gone.add(item["id"])
            if gone:
                with self.store.update() as state:
                    state["home"]["outbox"] = [item for item in state["home"]["outbox"] if item["id"] not in gone]

    # --- The server --------------------------------------------------------------------

    def serve(self) -> int:
        """Starts the server, if it isn't running: its port. That's the one
        bridge.json keeps while it's free, so rooms' routes outlive
        restarts, otherwise the next free one, kept from then on."""
        with self._serve_lock:
            if self._server is not None and self.port is not None:
                return self.port
            kept = self.store.read()["home"]["port"]
            order = ([kept] if kept in self.ports else []) + [port for port in self.ports if port != kept]
            server = None
            for port in order:
                try:
                    server = _Server(port, self)
                except OSError:
                    continue
                break
            if server is None:
                raise room_bridge.BridgeError(
                    "no_port", f"Linked Rooms' bridge found no free port from {self.ports[0]} to {self.ports[-1]}",
                    503)
            port = server.server_address[1]
            if port != kept:
                if kept in self.ports:
                    # N5 points the rooms' routes at the new one.
                    logger.info("Linked Rooms' bridge took port %d: %d was taken", port, kept)
                with self.store.update() as state:
                    state["home"]["port"] = port
            self._serving_since = self.clock()
            self._server, self.port = server, port
            threading.Thread(target=server.serve_forever, name="conduit-room-bridge-server", daemon=True).start()
            return port

    def close(self) -> None:
        """Stops the server, if it's running."""
        with self._serve_lock:
            server, self._server, self.port = self._server, None, None
        if server is not None:
            server.shutdown()
            server.server_close()


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ConduitRoomBridge/1"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_S

    def do_GET(self) -> None:  # noqa: N802 - http.server's names
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _serve(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            status, answer = 400, _error("invalid_request")
        elif length > MAX_BODY_BYTES:
            status, answer = 413, _error("request_too_large")
        else:
            body = self.rfile.read(length) if length else b""
            status, answer = self.server.home.answer(self.command, self.path, self.headers, body)
        # ASCII, so a lone surrogate from the member goes as its escape.
        raw = json.dumps(answer, separators=(",", ":")).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server's name
        pass  # its lines would name every request; the bridge logs what matters


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, home: Home) -> None:
        self.home = home
        super().__init__(("127.0.0.1", port), _Handler)

    def server_bind(self) -> None:
        # HTTPServer's own looks the host's name up, which can take seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("Linked Rooms' bridge dropped a connection (%s)", sys.exc_info()[0].__name__)


# --- Where it runs ---------------------------------------------------------------------


_homes: dict[str, Home] = {}
_homes_lock = threading.Lock()


def start(root: Any) -> None:
    """Starts the home's side for the installation at ``root``: its handlers
    for the member's answers and statuses on this copy of room_links, and the
    watching, which runs the server while this copy's loop holds the lease.
    Again does nothing."""
    key = str(Path(root))
    with _homes_lock:
        if key in _homes:
            return
        home = _homes[key] = Home(root)
        room_links.handles(room_bridge.RESPONSE)(_on_response)
        room_links.handles(room_bridge.STATUS)(_on_status)
    threading.Thread(target=_watch, args=(home,), name="conduit-room-bridge-home", daemon=True).start()


def _home_for(received: room_links.Received) -> Optional[Home]:
    home = _homes.get(str(received.root)) if received.root is not None else None
    if home is None:
        logger.info("Linked Rooms dropped a %s for an installation whose bridge isn't running", received.kind)
    return home


def _on_response(received: room_links.Received) -> None:
    home = _home_for(received)
    if home is not None:
        home.on_response(received)


def _on_status(received: room_links.Received) -> None:
    home = _home_for(received)
    if home is not None:
        home.on_status(received)


def _watch(home: Home) -> None:
    backoff = 0
    while True:
        try:
            home.watch_pass()
            backoff = 0
            time.sleep(WATCH_EVERY_S)
        except Exception as exc:  # noqa: BLE001 - the watching outlives any one pass
            backoff = min(MAX_BACKOFF_S, max(5, backoff * 2))
            logger.warning("Linked Rooms' bridge couldn't look at the room turns it answers for (%s); trying again "
                           "in %d s", getattr(exc, "code", type(exc).__name__), backoff)
            time.sleep(backoff)


# --- Helpers -----------------------------------------------------------------------


def _apply(run: dict, seq: int, shown: dict, now: float) -> None:
    """A status the member sent, onto the run it's for: one older than the
    last, or for a run that ended here, changes nothing."""
    if run["ended"] is not None or seq <= run["seq"]:
        return
    if shown["status"] == "cancelled":
        # The home's own Stop ends the run here first, so this cancel is
        # another's: it reads interrupted, which gives Hermes a reply to
        # settle (the member sends those so already).
        shown = {"status": "interrupted", "error": STOPPED.format(name=run["name"])}
    run.update(seq=seq, heard=now, shown=shown)
    if shown["status"] in room_bridge.ENDED:
        run["ended"] = now


def _shown(value: Any) -> Optional[dict]:
    """A run's status as the member sent it, with only what Hermes' client
    reads: a completed run's reply, a failure's sentence. None for one whose
    status Hermes' client wouldn't know."""
    status = value.get("status") if isinstance(value, Mapping) else None
    if not isinstance(status, str) or (status not in room_bridge.ACTIVE and status not in room_bridge.ENDED):
        return None
    shown = {"status": status}
    output, error = value.get("output"), value.get("error")
    if status == "completed" and isinstance(output, str):
        shown["output"] = room_bridge.cut(output, MAX_OUTPUT_BYTES)
    if status in ("failed", "interrupted") and isinstance(error, str):
        shown["error"] = room_bridge.cut(error, MAX_ERROR_BYTES)
    return shown


def _response(body: Any) -> Optional[tuple[str, int, dict, Optional[dict]]]:
    """A roomlink.response's request id, status, body and run, or None."""
    if not isinstance(body, dict) or type(body.get("v")) is not int or body["v"] != room_bridge.VERSION:
        return None
    re_id, status, answer, run = body.get("re"), body.get("status"), body.get("body"), body.get("run")
    if not isinstance(re_id, str) or type(status) is not int or not isinstance(answer, dict) \
            or not (run is None or isinstance(run, dict)):
        return None
    return re_id, status, answer, run


def _status(body: Any) -> Optional[tuple[str, int, dict]]:
    """A roomlink.status's run, seq and status, or None."""
    if not isinstance(body, dict) or type(body.get("v")) is not int or body["v"] != room_bridge.VERSION:
        return None
    run_id, seq, shown = body.get("run_id"), body.get("seq"), _shown(body)
    if not _is_run_id(run_id) or type(seq) is not int or seq < 1 or shown is None:
        return None
    return run_id, seq, shown


def _passed(status: int, answer: dict, names: tuple[str, ...]) -> tuple[int, dict]:
    """A 2xx's ``names``, and only those; a refusal as _refused has it."""
    if 200 <= status < 300:
        return status, {name: answer[name] for name in names if name in answer}
    return _refused(status, answer)


def _refused(status: int, answer: dict) -> tuple[int, dict]:
    """The member's refusal: its status and code, as Hermes' client reads
    them, with this side's sentence. Anything that isn't a refusal is 503,
    which Hermes' client asks again, never a turn that wasn't admitted."""
    if not 400 <= status < 600:
        return 503, _error("member_unavailable")
    error = answer.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or not room_bridge.CODE.fullmatch(code):
        return status, {"error": {"message": REFUSED}}
    return status, {"error": {"code": code, "message": REFUSED}}


def _outgoing(link_id: str, profile: str, path: str, grant: str, now: float) -> dict:
    """A stop or revoke for the outbox, with the message id it keeps, so
    sending it again is the same envelope to the member."""
    return {"id": room_envelope.new_message_id(), "link": link_id, "at": now,
            "request": {"v": room_bridge.VERSION, "profile": profile, "method": "POST", "path": path,
                        "grant": grant}}


def _admission_status(answer: dict) -> str:
    status = answer.get("status")
    return status if isinstance(status, str) and room_bridge.CODE.fullmatch(status) else "started"


def _is_run_id(value: Any) -> bool:
    return isinstance(value, str) and room_bridge.RUN_ID.fullmatch(value) is not None


def _one(headers: Any, name: str) -> Optional[str]:
    """The header's value when the request has it once, else None."""
    values = headers.get_all(name) or []
    return values[0] if len(values) == 1 else None


def _grant(headers: Any) -> Optional[str]:
    value = _one(headers, "Authorization")
    found = _GRANT.fullmatch(value) if value is not None else None
    return found.group(1) if found is not None else None


def _json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        raise _Answered(400, _error("invalid_request")) from None
    if not isinstance(value, dict):
        raise _Answered(400, _error("invalid_request"))
    return value


def _error(code: str, message: Optional[str] = None) -> dict:
    return {"error": {"code": code, "message": message or _MESSAGES[code]}}
