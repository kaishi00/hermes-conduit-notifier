"""The member's side of the bridge, for Linked Rooms (designs/cross-owner-agents, N4a).

A home linked with this installation (room_links.py) sends its Hermes'
requests for this installation's agents as roomlink.request envelopes.
This side replays each to this Hermes' own runs API with the request's
HermesRoom grant, answers with a roomlink.response, and watches each run
it admitted, sending its status as roomlink.status when it changes and
every minute while it runs. The owner's choices for the room
(room_choices.py) apply here, on the owner's host: an agent that only
listens, or one that waits to be asked and wasn't, gets a "(pass)" run
with no model call, and one past its daily limit sits out the day.

Only what Hermes' client reads goes back: a run's id and status, its final
reply, the room catalog, a refreshed grant. Approvals stay with the
agent's owner, Hermes' error text and a failure's details stay on this
host, and nothing is answered for a request older than the home waits.
The bindings, and the runs it watches with their grants, are in
bridge.json (room_bridge.py), so a restart picks the watching up.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import room_bridge, room_choices, room_links

logger = logging.getLogger(__name__)

# Requests are answered on their own small pool, so an API server that's
# slow to answer never holds up the inbox (room_links' loop).
WORKERS = 4
# The home waits 15 s for an answer (N4b), and Hermes' client asks again at
# once after a 504: an answer up to twice that late still meets the retry.
# Anything older the home answered already, and asks again if it still needs
# to, so a member that was off for hours never runs the turns it missed.
FRESH_S = 30
API_TIMEOUT_S = 10.0
WATCH_EVERY_S = 1.0
HEARTBEAT_S = 60
CAP_S = 10 * 60
KEPT_S = 3600
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ANSWER_BYTES = 16 * 1024 * 1024
MAX_REFUSAL_BYTES = 64 * 1024
MAX_BACKOFF_S = 300

PASS = "(pass)"
NOTICE = "I've used all my turns in this room for today, so I'll sit out until tomorrow."
FAILED = "The turn failed on this agent's Hermes."
CAPPED = "This agent's Hermes stopped the turn after 10 minutes."
RESTARTED = "This agent's Hermes restarted during the turn."
STOPPED = "The turn was stopped on this agent's Hermes."
REFUSED = "This agent's Hermes refused the request."
_MESSAGES = {
    "not_found": "This agent's Hermes doesn't take that request from a room.",
    "room_approval_elsewhere": "This agent's approvals are answered by its owner, on their own Hermes.",
    "invalid_room_grant": "Room authorization is invalid or expired.",
    "invalid_room_dispatch": "The dispatch isn't one this agent's Hermes takes from a room.",
    "member_unavailable": "This agent's Hermes didn't answer.",
}

# Hermes' run states (tui_gateway/hosted_room_peer_http.py).
ACTIVE = frozenset({"queued", "running", "waiting_for_approval", "stopping"})
ENDED = frozenset({"completed", "failed", "interrupted", "cancelled"})
# A capabilities answer and a refreshed grant, as Hermes' client reads them.
CAPABILITY_FIELDS = ("object", "room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id",
                     "target_profile", "catalog")
GRANT_FIELDS = ("object", "grant", "expires_at", "status_expires_at")

_RUN = r"(?P<run>[A-Za-z0-9_-]{1,64})"
_ROUTES = (
    ("POST", re.compile(r"/v1/runs"), "dispatch"),
    ("GET", re.compile(rf"/v1/runs/{_RUN}"), "read"),
    ("POST", re.compile(rf"/v1/runs/{_RUN}/stop"), "stop"),
    ("POST", re.compile(rf"/v1/runs/{_RUN}/(?:approval|clarify)"), "approval"),
    ("GET", re.compile(r"/v1/room-members/capabilities"), "capabilities"),
    ("POST", re.compile(r"/v1/room-members/grants/refresh"), "refresh"),
    ("POST", re.compile(r"/v1/room-members/grants/revoke"), "revoke"),
)
# Applied however old: the home's Stop and revoke mean the same later.
_ANY_AGE = frozenset({"stop", "revoke"})
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Hermes' client reads these codes from an invalid_room_dispatch's message
# (tui_gateway/hosted_room_peer_http._LEGACY_DISPATCH_MESSAGE_CODES): its
# API server says a grant that doesn't cover the dispatch that way. The
# message stays on this host, so the code goes as the client would read it.
_DISPATCH_MESSAGE_CODES = (("room grant", "invalid_room_grant"),
                           ("capability catalog changed", "room_capability_catalog_changed"),
                           ("execution policy changed", "room_execution_policy_changed"))
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A member's turn prompt, as Hermes builds it
# (gateway/hosted_room_discussion._build_prompt), and its mention rule
# (_MENTION_RE, compared casefolded; @all and @everyone mention everyone).
_OPENING = re.compile(r'\[Discussion: ".*?"\] You are @([A-Za-z0-9][A-Za-z0-9._:-]*), one participant with ')
_NEW = "\n\nNew messages in this thread since your last turn (oldest first):"
_RULES = "\n\nRules for this Discussion:\n"
_AUTHOR = re.compile(r"  (?:User \(user\)|@([A-Za-z0-9][A-Za-z0-9._:-]*)): ")
_MENTION = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._:-]*)", re.IGNORECASE)
_EVERYONE = frozenset({"all", "everyone"})


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


# This Hermes' own API server, never through a proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)


def bind(root: Any, link_id: str, scope: Mapping[str, Any]) -> None:
    """Lets the Link ``link_id`` carry requests whose grants are for
    ``scope`` (``home_install_id``, ``room_id``, ``member_id``,
    ``target_profile``): N5 binds it when this installation's agent joins a
    room over the Link. ValueError for what isn't a scope or a Link's id."""
    checked = room_bridge.checked_scope(scope)
    if checked is None:
        raise ValueError("That isn't a room grant's scope")
    if not room_links._is_id(link_id):
        raise ValueError("That isn't a Link's id")
    with room_bridge.Store(root).update() as state:
        scopes = state["member"]["bindings"].setdefault(link_id, [])
        if checked not in scopes:
            scopes.append(checked)


def unbind(root: Any, link_id: str, scope: Optional[Mapping[str, Any]] = None) -> None:
    """Ends one of the Link's bindings, or all of them with no ``scope``."""
    with room_bridge.Store(root).update() as state:
        bindings = state["member"]["bindings"]
        kept = [] if scope is None else [bound for bound in bindings.get(link_id, []) if bound != dict(scope)]
        if kept:
            bindings[link_id] = kept
        else:
            bindings.pop(link_id, None)


def asked(prompt: Any) -> Optional[bool]:
    """Whether a new message in a room turn's prompt mentions the agent it's
    for (the agent's own messages aside), by Hermes' mention rule; None for
    a prompt that isn't one Hermes builds."""
    if not isinstance(prompt, str):
        return None
    opening, start, end = _OPENING.match(prompt), prompt.find(_NEW), prompt.rfind(_RULES)
    if opening is None or start < 0 or end < start:
        return None
    me, section = opening.group(1).casefold(), prompt[start + len(_NEW):end]
    if section and not section.startswith("\n"):
        return None
    own = False
    # A message is a line with two spaces and its author; the lines after
    # it without them are the rest of its text.
    for line in section.split("\n")[1:]:
        author = _AUTHOR.match(line)
        if author is not None:
            own = author.group(1) is not None and author.group(1).casefold() == me
            line = line[author.end():]
        if not own and any(match.group(1).casefold() in (me, *_EVERYONE) for match in _MENTION.finditer(line)):
            return True
    return False


class Member:
    """The member's side for the installation at ``root``: ``send`` is
    Links.send, ``api_url(profile)`` this Hermes' API server for a profile,
    and ``profile_home(profile)`` the home whose room choices apply."""

    def __init__(self, root: Any, *, send: Optional[Callable[..., Any]] = None,
                 api_url: Optional[Callable[[str], str]] = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 profile_home: Optional[Callable[[str], Path]] = None, executor: Any = None,
                 api_timeout: float = API_TIMEOUT_S) -> None:
        self.root = Path(root)
        self.store = room_bridge.Store(self.root)
        self.send = send if send is not None else room_links.Links(self.root).send
        self.api_url = api_url if api_url is not None else self._local_api_url
        self.clock = clock
        self.monotonic = monotonic
        self.profile_home = profile_home if profile_home is not None else self._profile_home
        self.executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=WORKERS, thread_name_prefix="conduit-room-bridge")
        self.api_timeout = api_timeout
        self._hermes: Any = None
        self._told: set[tuple[str, str]] = set()
        self._told_lock = threading.Lock()

    # --- Requests -----------------------------------------------------------------

    def handle(self, received: room_links.Received) -> None:
        """Takes a roomlink.request from the Link's loop, to answer on a worker."""
        request = room_bridge.request_from(received.body)
        if request is None:
            logger.info("Linked Rooms dropped a roomlink.request from Link %s that isn't one", received.link_id)
            return
        self.executor.submit(self._serve, received, request, self.monotonic())

    def _serve(self, received: room_links.Received, request: room_bridge.Request, heard: float) -> None:
        try:
            answer = self._answer(received, request, heard)
        except Exception as exc:  # noqa: BLE001 - the home hears this member couldn't answer
            # Only the type: a message can quote what it was handling.
            logger.warning("Linked Rooms couldn't answer a request from Link %s (%s)", received.link_id,
                           type(exc).__name__)
            answer = room_bridge.response_body(received.message_id, 503, _error("member_unavailable"))
        if answer is None:
            return
        try:
            self.send(received.link_id, room_bridge.RESPONSE, answer)
        except room_links.LinkError as exc:
            # Hermes' client asks again when the home's wait ends.
            logger.info("Linked Rooms couldn't send its answer over Link %s (%s)", received.link_id, exc.code)
        except Exception as exc:  # noqa: BLE001 - on a worker, nothing else would see it
            logger.warning("Linked Rooms couldn't send its answer over Link %s (%s)", received.link_id,
                           type(exc).__name__)

    def _answer(self, received: room_links.Received, request: room_bridge.Request,
                heard: float) -> Optional[dict]:
        route, run_id = _route(request.method, request.path)
        if route not in _ANY_AGE:
            # By the mailbox's clock alone (how long it waited there), then
            # this host's own for the wait for a worker.
            waited = (received.age or 0) + max(0.0, self.monotonic() - heard)
            if waited > FRESH_S:
                logger.debug("Linked Rooms dropped a request from Link %s that waited %d s", received.link_id,
                             waited)
                return None

        def reply(status: int, body: dict, run: Optional[dict] = None) -> dict:
            return room_bridge.response_body(received.message_id, status, body, run=run)

        if route is None:
            return reply(404, _error("not_found"))
        if route == "approval":
            # Its owner answers on their own Hermes (decision 7), never a room's people.
            return reply(403, _error("room_approval_elsewhere"))
        scope = self._bound_scope(received.link_id, request)
        if scope is None:
            return reply(403, _error("invalid_room_grant"))
        profile, grant = request.profile, request.grant
        if route == "dispatch":
            return self._dispatch(received, request, scope, reply)
        if route == "read":
            return self._read(received.link_id, profile, grant, run_id, reply)
        if route == "stop":
            return self._stop(received.link_id, profile, grant, run_id, reply)
        if route == "capabilities":
            status, answer = self._call(profile, "GET", "/v1/room-members/capabilities", grant)
            return _passed(reply, status, answer, CAPABILITY_FIELDS)
        if route == "refresh":
            payload = request.body or {}
            sent = {"ttl_seconds": payload["ttl_seconds"]} if "ttl_seconds" in payload else {}
            status, answer = self._call(profile, "POST", "/v1/room-members/grants/refresh", grant, body=sent)
            return _passed(reply, status, answer, GRANT_FIELDS)
        status, answer = self._call(profile, "POST", "/v1/room-members/grants/revoke", grant, body={})
        return _passed(reply, status, answer, ("revoked",))

    def _bound_scope(self, link_id: str, request: room_bridge.Request) -> Optional[dict[str, str]]:
        """The grant's scope when it's bound to the Link and for the profile
        asked for, or None. The API server checks its signature."""
        scope = room_bridge.grant_scope(request.grant)
        if scope is None:
            self._tell_once(("grant", link_id), "Linked Rooms refused room grants from Link %s whose payload it "
                                                "can't read; a Hermes that changed them needs this plugin updated",
                            link_id)
            return None
        if scope["target_profile"] != request.profile:
            return None
        bound = self.store.read()["member"]["bindings"].get(link_id, [])
        return scope if scope in bound else None

    def _dispatch(self, received: room_links.Received, request: room_bridge.Request, scope: dict[str, str],
                  reply: Callable[..., dict]) -> dict:
        body = request.body or {}
        fields = body.get("hosted_room_dispatch")
        # Hermes checks the dispatch against the grant too, but a pass run
        # is answered here, so it's checked here first, in Hermes' order and
        # refused as Hermes' refusals read to its client: one outside the
        # grant's scope as the grant's, the rest as a dispatch that wasn't
        # admitted.
        if not isinstance(fields, dict) or set(body) - {"hosted_room_dispatch", "input"}:
            return reply(403, _error("invalid_room_dispatch"))
        if any(fields.get(name) != scope[name] for name in room_bridge.SCOPE_FIELDS):
            return reply(403, _error("invalid_room_grant"))
        if request.key is None or request.key != _key_of(fields):
            return reply(403, _error("invalid_room_dispatch"))
        link_id, key, profile, room_id = received.link_id, request.key, request.profile, scope["room_id"]
        choices = self._choices(profile, room_id)
        sits_out = choices.takes_part == "listens" or (
            choices.takes_part == "asked" and not self._asked(fields.get("prompt"), room_id))
        day, counted = _day(self.clock()), f"{profile}/{room_id}"
        with self.store.update() as state:
            member = state["member"]
            known = next(((run_id, run) for run_id, run in member["runs"].items()
                          if run["link"] == link_id and run.get("key") == key), None)
            if known is not None and known[1]["kind"] == "pass":
                run_id, run = known
                return reply(202, {"run_id": run_id, "status": "completed", "replayed": True},
                             run={"run_id": run_id, "status": "completed", "output": run["output"]})
            output = PASS if known is None and sits_out else None
            if known is None and output is None:
                # Real runs a day, per profile and room: a key counts once,
                # whether Hermes' answer comes or not, and comes off again
                # when Hermes refuses it.
                today = member["days"].setdefault(day, {}).setdefault(counted, {"keys": [], "notice": False})
                if key in today["keys"]:
                    pass
                elif len(today["keys"]) < choices.daily_limit:
                    today["keys"].append(key)
                elif not today["notice"]:
                    today["notice"] = True
                    output = NOTICE
                else:
                    output = PASS
            if output is not None:
                run_id = _pass_id(link_id, key)
                member["runs"][run_id] = {"link": link_id, "kind": "pass", "key": key, "output": output,
                                          "at": self.clock()}
                return reply(202, {"run_id": run_id, "status": "completed", "replayed": False},
                             run={"run_id": run_id, "status": "completed", "output": output})
        status, answer = self._call(profile, "POST", "/v1/runs", request.grant, key=key,
                                    body={"hosted_room_dispatch": fields})
        if status is not None and 200 <= status < 300:
            run_id = answer.get("run_id")
            if isinstance(run_id, str) and _RUN_ID.fullmatch(run_id):
                return reply(202, *self._admitted(link_id, key, profile, request.grant, run_id, answer))
        if known is None and status is not None and 400 <= status < 500:
            with self.store.update() as state:
                today = state["member"]["days"].get(day, {}).get(counted)
                if today is not None and key in today["keys"]:
                    today["keys"].remove(key)
        return _refused(reply, status, answer)

    def _admitted(self, link_id: str, key: str, profile: str, grant: str, run_id: str,
                  answer: dict) -> tuple[dict, dict]:
        """An admission's answer and the run's status then: for a run this
        side watches already, the status it has; for a new one, the
        admission's, which reads ``queued`` until it's active."""
        status = answer.get("status") if isinstance(answer.get("status"), str) else "started"
        now = self.clock()
        with self.store.update() as state:
            run = state["member"]["runs"].get(run_id)
            if run is None:
                shown = {"status": status if status in ACTIVE else "queued"}
                run = state["member"]["runs"][run_id] = {
                    "link": link_id, "kind": "run", "key": key, "profile": profile, "grant": grant,
                    "admitted": now, "home_stopped": False, "shown": shown, "sent": shown, "sent_at": now,
                    "seq": 0, "ended": None}
            shown = dict(run["shown"])
        return {"run_id": run_id, "status": status, "replayed": answer.get("replayed") is True}, \
            {"run_id": run_id, **shown}

    def _read(self, link_id: str, profile: str, grant: str, run_id: str, reply: Callable[..., dict]) -> dict:
        run = self.store.read()["member"]["runs"].get(run_id)
        if run is not None and run["kind"] == "pass" and run["link"] == link_id:
            return reply(200, {"run_id": run_id, "status": "completed", "output": run["output"]})
        status, answer = self._call(profile, "GET", f"/v1/runs/{run_id}", grant)
        if status is not None and 200 <= status < 300:
            shown = self._status_of(profile, grant, run_id, answer,
                                    home_stopped=run is not None and run.get("home_stopped") is True)
            if shown is not None:
                return reply(200, {"run_id": run_id, **shown})
        return _refused(reply, status, answer)

    def _stop(self, link_id: str, profile: str, grant: str, run_id: str, reply: Callable[..., dict]) -> dict:
        with self.store.update() as state:
            run = state["member"]["runs"].get(run_id)
            if run is not None and run["kind"] == "pass" and run["link"] == link_id:
                return reply(200, {"run_id": run_id, "status": "completed"})
            if run is not None and run["kind"] == "run":
                # Before the stop goes, so the cancel it brings reads as the home's.
                run["home_stopped"] = True
        status, answer = self._call(profile, "POST", f"/v1/runs/{run_id}/stop", grant, body={})
        if status is not None and 200 <= status < 300:
            shown = _shown(answer, home_stopped=True) or {"status": "stopping"}
            return reply(200, {"run_id": run_id, "status": shown["status"]})
        return _refused(reply, status, answer)

    # --- Watching the runs it admitted ------------------------------------------------

    def watch_pass(self) -> None:
        """A tick, while this copy's loop holds the installation's lease:
        watching goes with reading the inbox, so one process does it."""
        if room_links.holds_lease(self.root):
            self.tick()

    def tick(self) -> None:
        """One look at the runs this side admitted: each one's status read
        until it ends, a change or a minute's heartbeat sent, a turn past
        10 minutes stopped, and what ended an hour ago forgotten."""
        now = self.clock()
        down: set[str] = set()
        for run_id, run in self.store.read()["member"]["runs"].items():
            if run["kind"] != "run":
                continue
            if run.get("ended") is None:
                run = self._look(run_id, run, now)
            if run is not None and run["link"] not in down and _due(run, now) and not self._report(run_id, run, now):
                # One refused send a Link a pass: the rest wait for the next.
                down.add(run["link"])
        with self.store.update() as state:
            member = state["member"]
            member["runs"] = {run_id: run for run_id, run in member["runs"].items() if not _forgotten(run, now)}
            member["days"] = {day: counts for day, counts in member["days"].items() if day == _day(now)}

    def _look(self, run_id: str, run: dict, now: float) -> Optional[dict]:
        """Reads the run's status: the run as it is now, or None when it's gone."""
        profile, grant = run["profile"], run["grant"]
        status, answer = self._call(profile, "GET", f"/v1/runs/{run_id}", grant)
        fixed: Optional[dict] = None
        if status == 404:
            # The API server lost it: it restarted without its record.
            fixed = {"status": "interrupted", "error": RESTARTED}
        elif status is not None and 200 <= status < 300 and answer.get("status") == "unknown":
            fixed = self._status_of(profile, grant, run_id, answer, home_stopped=False)
        read = status is not None and 200 <= status < 300 and fixed is None
        if fixed is None and now - run["admitted"] >= CAP_S and not (read and answer.get("status") in ENDED):
            self._call(profile, "POST", f"/v1/runs/{run_id}/stop", grant, body={})
            fixed = {"status": "interrupted", "error": CAPPED}
        with self.store.update() as state:
            fresh = state["member"]["runs"].get(run_id)
            if fresh is not None and fresh.get("ended") is None:
                # Whether a cancel was the home's is read now: its Stop can
                # have come while this read was out.
                shown = fixed if fixed is not None else (
                    _shown(answer, home_stopped=fresh.get("home_stopped") is True) if read else None)
                if shown is not None:
                    fresh["shown"] = shown
                    if shown["status"] in ENDED:
                        fresh["ended"] = now
            return None if fresh is None else dict(fresh)

    def _report(self, run_id: str, run: dict, now: float) -> bool:
        """Sends the run's status: False when the Link didn't take it."""
        seq = run["seq"] + 1
        try:
            self.send(run["link"], room_bridge.STATUS, room_bridge.status_body(run_id, seq, run["shown"]),
                      message_id=f"st.{run_id}.{seq}")
        except room_links.LinkError as exc:
            logger.debug("Linked Rooms couldn't send a run's status over Link %s (%s)", run["link"], exc.code)
            return False
        with self.store.update() as state:
            fresh = state["member"]["runs"].get(run_id)
            if fresh is not None and fresh["seq"] < seq:
                fresh.update(seq=seq, sent=run["shown"], sent_at=now)
        return True

    def _status_of(self, profile: str, grant: str, run_id: str, answer: dict, *,
                   home_stopped: bool) -> Optional[dict]:
        """A status this Hermes read, as the home hears it. A run it reads
        ``unknown`` (it restarted mid-turn, and holds later turns behind it)
        is resolved there first, as Hermes' client does."""
        if answer.get("status") == "unknown":
            self._call(profile, "POST", f"/v1/runs/{run_id}/resolve-unknown", grant,
                       body={"admission_id": answer.get("admission_id"),
                             "execution_generation": answer.get("execution_generation")})
            return {"status": "interrupted", "error": RESTARTED}
        return _shown(answer, home_stopped=home_stopped)

    # --- This Hermes ----------------------------------------------------------------

    def _call(self, profile: str, method: str, path: str, grant: str, *, key: Optional[str] = None,
              body: Optional[dict] = None) -> tuple[Optional[int], dict]:
        """This Hermes' answer to one request for a room: its status and JSON
        object, or None and {} when it didn't answer, a 2xx without a JSON
        object in it included."""
        try:
            base = self.api_url(profile)
        except Exception as exc:  # noqa: BLE001 - no API server to replay to is no answer
            self._tell_once(("api", type(exc).__name__), "Linked Rooms couldn't find this Hermes' API server (%s)",
                            getattr(exc, "code", type(exc).__name__))
            return None, {}
        headers = {"Authorization": f"HermesRoom {grant}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            request = urllib.request.Request(f"{base.rstrip('/')}{path}", data=data, method=method, headers=headers)
            with _opener.open(request, timeout=self.api_timeout) as answer:
                status, raw = answer.status, answer.read(MAX_ANSWER_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                raw = error.read(MAX_REFUSAL_BYTES)
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            logger.debug("Linked Rooms couldn't reach this Hermes' API server (%s)", type(error).__name__)
            return None, {}
        if len(raw) > MAX_ANSWER_BYTES:
            return None, {}
        try:
            parsed = json.loads(raw) if raw else None
        except (ValueError, RecursionError):
            parsed = None
        if not isinstance(parsed, dict):
            # A refusal is one by its status alone; a 2xx is nothing without its answer.
            return (None, {}) if 200 <= status < 300 else (status, {})
        return status, parsed

    def _local_api_url(self, profile: str) -> str:
        from . import room_setup

        if self._hermes is None:
            self._hermes = room_setup.Hermes.load()
        return room_setup.local_api_url(self._hermes, self.root, profile)

    def _profile_home(self, profile: str) -> Path:
        # Where Hermes keeps a profile (hermes_cli.profiles.get_profile_dir).
        return self.root if profile == "default" else self.root / "profiles" / profile

    def _choices(self, profile: str, room_id: str) -> room_choices.RoomChoices:
        return room_choices.ChoicesStore(self.profile_home(profile)).get(room_id)

    def _asked(self, prompt: Any, room_id: str) -> bool:
        found = asked(prompt)
        if found is None:
            self._tell_once(("prompt", room_id), "Linked Rooms couldn't read a turn's prompt in room %s, so When "
                                                 "asked takes it as not asked", room_id)
        return found is True

    def _tell_once(self, key: tuple[str, str], message: str, *args: Any) -> None:
        with self._told_lock:
            if key in self._told:
                return
            self._told.add(key)
        logger.info(message, *args)


# --- Where it runs ---------------------------------------------------------------------


_members: dict[str, Member] = {}
_members_lock = threading.Lock()


def start(root: Any) -> None:
    """Starts the member's side for the installation at ``root``: its handler
    on this copy of room_links, and the watching of the runs it admitted,
    which runs while this copy's loop holds the lease. Again does nothing."""
    key = str(Path(root))
    with _members_lock:
        if key in _members:
            return
        member = _members[key] = Member(root)
        room_links.handles(room_bridge.REQUEST)(_on_request)
    threading.Thread(target=_watch, args=(member,), name="conduit-room-bridge-watch", daemon=True).start()


def _on_request(received: room_links.Received) -> None:
    member = _members.get(str(received.root)) if received.root is not None else None
    if member is None:
        logger.info("Linked Rooms dropped a roomlink.request for an installation whose bridge isn't running")
        return
    member.handle(received)


def _watch(member: Member) -> None:
    backoff = 0
    while True:
        try:
            member.watch_pass()
            backoff = 0
            time.sleep(WATCH_EVERY_S)
        except Exception as exc:  # noqa: BLE001 - the watching outlives any one pass
            backoff = min(MAX_BACKOFF_S, max(5, backoff * 2))
            logger.warning("Linked Rooms couldn't look at the room turns it's watching (%s); trying again in %d s",
                           getattr(exc, "code", type(exc).__name__), backoff)
            time.sleep(backoff)


# --- Helpers -----------------------------------------------------------------------


def _route(method: str, path: str) -> tuple[Optional[str], Optional[str]]:
    for route_method, pattern, name in _ROUTES:
        found = pattern.fullmatch(path) if method == route_method else None
        if found is not None:
            return name, found.groupdict().get("run")
    return None, None


def _key_of(fields: Mapping[str, Any]) -> Optional[str]:
    """The Idempotency-Key Hermes' client sends with a dispatch."""
    task_id, generation = fields.get("task_id"), fields.get("execution_generation")
    if not isinstance(task_id, str) or type(generation) is not int:
        return None
    return f"room:{task_id}:{generation}"


def _pass_id(link_id: str, key: str) -> str:
    """A pass or notice run's id: from the Link and the key, so asking again gets the same run."""
    return "run_" + hashlib.sha256(f"{link_id}\0{key}".encode("utf-8")).hexdigest()[:32]


def _day(now: float) -> str:
    """The day on this host's clock."""
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _forgotten(run: dict, now: float) -> bool:
    """Whether a run ended (a pass run, was made) more than KEPT_S ago."""
    ended = run["at"] if run["kind"] == "pass" else run.get("ended")
    return ended is not None and now - ended > KEPT_S


def _due(run: dict, now: float) -> bool:
    shown = run.get("shown")
    if shown is None:
        return False
    return shown != run.get("sent") or (shown["status"] in ACTIVE and now - run["sent_at"] >= HEARTBEAT_S)


def _shown(answer: Any, *, home_stopped: bool) -> Optional[dict]:
    """A run's status as the home hears it: a completed run's final reply,
    a fixed sentence for a run that didn't complete, and nothing about an
    approval it waits on. The home's own Stop reads ``cancelled``; any other
    cancel ``interrupted``, which gives the home a reply to settle. None
    for an answer with no status."""
    status = answer.get("status") if isinstance(answer, Mapping) else None
    if not isinstance(status, str) or not status:
        return None
    if status == "completed":
        output = answer.get("output")
        return {"status": "completed", "output": _cut(output if isinstance(output, str) else "")}
    if status == "failed":
        return {"status": "failed", "error": FAILED}
    if status in ("interrupted", "unknown"):
        # Hermes interrupts a run only when it restarts or shuts down.
        return {"status": "interrupted", "error": RESTARTED}
    if status == "cancelled":
        return {"status": "cancelled"} if home_stopped else {"status": "interrupted", "error": STOPPED}
    # A state this plugin doesn't know is one Hermes' client wouldn't: the
    # run's still on, until it ends or the cap stops it.
    return {"status": status if status in ACTIVE else "running"}


def _cut(text: str) -> str:
    """``text`` cut to MAX_OUTPUT_BYTES of UTF-8, at a character. A lone
    surrogate, which JSON carries and UTF-8 can't, reads as ``?``."""
    return text.encode("utf-8", "replace")[:MAX_OUTPUT_BYTES].decode("utf-8", "ignore")


def _error(code: str) -> dict:
    return {"error": {"code": code, "message": _MESSAGES[code]}}


def _refused(reply: Callable[..., dict], status: Optional[int], answer: dict) -> dict:
    """Hermes' refusal with its status and code, as its client reads them,
    never its text; anything else, no answer included, is 503, which
    Hermes' client takes as one it can ask again, never as a turn that
    wasn't admitted."""
    if status is None or not 400 <= status < 600:
        return reply(503, _error("member_unavailable"))
    error = answer.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        return reply(status, {"error": {"message": REFUSED}})
    if code == "invalid_room_dispatch":
        message = str(error.get("message") or "").lower()
        code = next((read for needle, read in _DISPATCH_MESSAGE_CODES if needle in message), code)
    return reply(status, {"error": {"code": code, "message": REFUSED}})


def _passed(reply: Callable[..., dict], status: Optional[int], answer: dict, names: tuple[str, ...]) -> dict:
    """A 2xx's ``names``, and only those; a refusal as _refused has it."""
    if status is not None and 200 <= status < 300:
        return reply(status, {name: answer[name] for name in names if name in answer})
    return _refused(reply, status, answer)
