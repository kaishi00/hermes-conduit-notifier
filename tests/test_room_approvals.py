"""Your agents' approvals from rooms hosted elsewhere (designs/cross-owner-agents, N5c): each run of this
installation's agents, in a room another Hermes hosts, that waits for its owner's approval is listed with its room,
who hosts it and the agent, and answered here, once or deny, through this Hermes' own API server.

The rooms are test_room_invites' (Furina hosts one that Laifu joined over his Link), and Laifu's Hermes is its fake
there, with c73cbcc4's runs API beside it: a run belongs to the grant scope that started it (a grant of that scope
reads it; another scope's grant, or an API key, gets 404), and answering its approval takes a grant with approve,
once or deny and the exact request id (legacy), or exactly the request id, execution generation and choice
(canonical, a gateway with a session authority).
"""
import json
import logging
import sqlite3
import threading

import pytest

from test_room_bridge_member import NOW, ROOT, _load, bridge, links_module, member_module
from test_room_invites import LAIFUS, _started, api, commands, invites_module, rooms, routes, slot  # noqa: F401

approvals_module = _load("conduit_push.room_approvals", ROOT / "room_approvals.py")

RUN = "run_" + "a" * 32
OTHER_RUN = "run_" + "b" * 32
COMMAND = "rm -rf ~/zoo/tickets-old"
DESCRIPTION = "recursive delete"
# The key Hermes keeps for the hidden chat a canonical run is in (sessions.session_key).
KEY = "agent:main:api_server:dm:room_abc"
PLACE = ("room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id", "target_install_id",
         "target_profile")


class Runs:
    """Laifu's Hermes as approvals call it: the grants of test_room_invites' fake (``member``), and its API
    server's runs API as c73cbcc4 answers a room grant (api_server_runs.py): the run's status for a grant of the
    scope that started it, live for status, not fenced, its reservation current; its approval answered for one with
    approve."""

    def __init__(self, member):
        self.member = member
        self.runs = {}
        self.calls = []
        self.down = False

    def issue(self, **fields):
        return self.member.issue(**fields)

    def decode(self, grant, permission):
        return self.member.decode(grant, permission)

    def waiting(self, run_id, grant, *, canonical=False, request_id="req-1", command=COMMAND,
                description=DESCRIPTION):
        """A run of ``grant``'s scope that waits for an approval, as the API server shows it."""
        if canonical:
            answer = {"pending_controls": [{
                "kind": "approval", "prompt_id": request_id, "execution_generation": 3, "command": command,
                "description": description, "choices": ["once", "deny", "session", "always"], "timeout_s": 300}],
                "run_id": run_id, "status": "waiting_for_approval", "session_id": "room_abc", "admission_id": "adm-1",
                "execution_generation": 3, "output": "", "usage": {}}
        else:
            answer = {"object": "hermes.run", "run_id": run_id, "status": "waiting_for_approval",
                      "approval": {"command": command, "pattern_key": DESCRIPTION, "pattern_keys": [DESCRIPTION],
                                   "description": description, "allow_permanent": True, "allow_session": True,
                                   "request_id": request_id, "object": "hermes.run.event", "event": "approval.request",
                                   "run_id": run_id, "timestamp": NOW,
                                   "choices": ["once", "session", "always", "deny"]}}
        self.runs[run_id] = {"scope": _place(self.decode(grant, "status")), "answer": answer, "canonical": canonical}

    def run(self, profile, run_id, grant):
        self.calls.append(("GET", profile, run_id, grant, None))
        if self.down:
            return None, {}
        refused, run = self._owned(grant, "status", run_id)
        return refused if refused is not None else (200, json.loads(json.dumps(run["answer"])))

    def respond(self, profile, run_id, grant, body):
        self.calls.append(("POST", profile, run_id, grant, body))
        if self.down:
            return None, {}
        refused, run = self._owned(grant, "approve", run_id)
        if refused is not None:
            return refused
        answer = run["answer"]
        if run["canonical"]:
            if body.get("choice") not in ("once", "deny"):
                return 400, _error("Room approvals require once or deny")
            if set(body) != {"request_id", "execution_generation", "choice"}:
                return 400, _error("invalid_params", "invalid_params")
            if body["execution_generation"] != answer["execution_generation"]:
                return 409, _error("stale_generation", "stale_generation")
            pending = [prompt for prompt in answer["pending_controls"]
                       if prompt["kind"] == "approval" and prompt["prompt_id"] == body["request_id"]]
            if not pending:
                return 409, _error("approval_not_pending", "approval_not_pending")
            answer["pending_controls"].remove(pending[0])
            answer["status"] = "running"
            return 200, {"run_id": run_id, "status": "resolved", "prompt_id": body["request_id"]}
        if body.get("choice") not in ("once", "deny"):
            return 400, _error("Invalid approval choice; expected one of: deny, once", "invalid_approval_choice")
        if body.get("all") or body.get("resolve_all"):
            return 400, _error("Room approvals can resolve only one exact request", "invalid_approval_scope")
        if not body.get("request_id"):
            return 400, _error("Room approvals require the exact request_id.", "approval_request_required")
        if answer["status"] != "waiting_for_approval" or answer["approval"]["request_id"] != body["request_id"]:
            return 409, _error(f"Run has no pending approval: {run_id}", "approval_not_pending")
        answer["status"] = "running"
        del answer["approval"]
        return 200, {"object": "hermes.run.approval_response", "run_id": run_id, "choice": body["choice"],
                     "request_id": body["request_id"], "resolved": 1}

    def _owned(self, grant, permission, run_id):
        try:
            claims = self.decode(grant, permission)
        except ValueError:
            return (401, _error("Room authorization is invalid or expired.", "invalid_room_grant")), None
        scope = (claims["room_id"], claims["member_id"], claims["target_profile"])
        reservation = self.member.reservations.get(scope)
        if claims["issued_at"] <= self.member.fences.get(scope, 0) or reservation is None \
                or reservation["revoked"] or reservation["expires_at"] <= self.member.clock():
            return (403, _error("Room authorization needs to be renewed.", "room_reauthorization_required")), None
        run = self.runs.get(run_id)
        if run is None or run["scope"] != _place(claims):
            return (404, _error(f"Run not found: {run_id}", "run_not_found")), None
        return None, run


def _error(message, code=None):
    return {"error": {"message": message, "type": "invalid_request_error", "param": None, "code": code}}


def _place(claims):
    return tuple(claims[name] for name in PLACE)


def _agent_grant(side, room_id, profile="default"):
    """The grant Laifu's agent of ``profile`` joined the room with, as his rooms.json keeps it."""
    state = invites_module.Store(side.tmp_path / "laifu").read()
    return next(agent["grant"] for agent in state["invites"][f"link1/{room_id}"]["joined"]
                if agent["profile"] == profile)


def _watched(side, run_id, grant, *, profile="default", status="waiting_for_approval", ended=None, at=NOW):
    """A run of Laifu's agent that the bridge's member side admitted, as bridge.json keeps it."""
    with bridge.Store(side.tmp_path / "laifu").update() as state:
        state["member"]["runs"][run_id] = {
            "link": "link1", "kind": "run", "key": f"room:{run_id}:1", "profile": profile, "grant": grant,
            "admitted": at, "home_stopped": False, "shown": {"status": status}, "sent": {"status": status},
            "sent_at": at, "seq": 1, "ended": ended}


@pytest.fixture
def waiting(rooms):
    """A room Furina hosts with Laifu's agent in it, whose turn there waits for his approval on his Hermes."""
    room_id = _started(rooms)
    runs = Runs(rooms.member["laifu"])
    grant = _agent_grant(rooms, room_id)
    runs.waiting(RUN, grant)
    _watched(rooms, RUN, grant)
    rooms.runs, rooms.room_id = runs, room_id
    rooms.approvals = approvals_module.Approvals(rooms.tmp_path / "laifu", hermes=runs,
                                                 link=lambda link_id: rooms.links["laifu"].get(link_id),
                                                 clock=rooms.clock)
    return rooms


def _shown(room_id, request_id="req-1", run_id=RUN, agent=None, session_key=None):
    return {"run_id": run_id, "request_id": request_id, "room_id": room_id, "room": "Zoo trip", "link": "link1",
            "from": "Furina", "agent": agent or LAIFUS, "session_key": session_key or run_id,
            "description": DESCRIPTION, "command": COMMAND, "choices": ["once", "deny"]}


def _session_store(home, rows):
    """Hermes' session store in a profile's home, with ``rows`` of (session id, session key)."""
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(home / "state.db")
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, session_key TEXT)")
        connection.executemany("INSERT INTO sessions (id, source, session_key) VALUES (?, 'bot_room', ?)", rows)
    connection.close()


def test_waiting_runs_are_listed_with_their_room_and_agent(waiting):
    # A turn that runs on, and one that ended, have nothing to answer.
    _watched(waiting, OTHER_RUN, _agent_grant(waiting, waiting.room_id), status="running")
    _watched(waiting, "run_" + "c" * 32, _agent_grant(waiting, waiting.room_id), status="completed", ended=NOW)

    assert waiting.approvals.waiting() == [_shown(waiting.room_id)]
    # Read with the grant the bridge keeps for the run: Hermes ties a room's run to its grant's scope.
    assert [(method, run_id, grant) for method, _, run_id, grant, _ in waiting.runs.calls] == [
        ("GET", RUN, _agent_grant(waiting, waiting.room_id))]


def test_an_approval_on_a_gateway_with_a_session_authority_is_listed_the_same(waiting):
    waiting.runs.waiting(RUN, _agent_grant(waiting, waiting.room_id), canonical=True, request_id="prompt-7")
    _session_store(waiting.tmp_path / "laifu", [("room_abc", KEY)])

    assert waiting.approvals.waiting() == [_shown(waiting.room_id, "prompt-7", session_key=KEY)]


def test_each_approval_names_the_session_its_notification_names(waiting):
    # Conduit tells which "Approval needed" notification is whose by it. A run store keys its approvals by the
    # run (api_server_runs._RunLaunch.approval_session_key).
    assert [shown["session_key"] for shown in waiting.approvals.waiting()] == [RUN]

    # A session authority keys them by the chat the run is in, as Hermes' session store keeps it.
    waiting.runs.waiting(RUN, _agent_grant(waiting, waiting.room_id), canonical=True)
    store = waiting.tmp_path / "laifu" / "state.db"
    assert [shown["session_key"] for shown in waiting.approvals.waiting()] == [None]
    store.write_bytes(b"not a session store")
    assert [shown["session_key"] for shown in waiting.approvals.waiting()] == [None]
    store.unlink()
    _session_store(waiting.tmp_path / "laifu", [("room_zzz", "agent:main:api_server:dm:room_zzz")])
    assert [shown["session_key"] for shown in waiting.approvals.waiting()] == [None]
    _session_store(waiting.tmp_path / "laifu", [("room_abc", KEY)])
    assert waiting.approvals.waiting() == [_shown(waiting.room_id, session_key=KEY)]


def test_a_profiles_approval_names_the_key_its_own_session_store_keeps(rooms):
    namesake = {"profile": "work", "handle": "furina", "display_name": "Furina"}
    room_id = _started(rooms, laifus=(LAIFUS, namesake))
    runs = Runs(rooms.member["laifu"])
    work = _agent_grant(rooms, room_id, "work")
    runs.waiting(OTHER_RUN, work, canonical=True)
    _watched(rooms, OTHER_RUN, work, profile="work")
    _session_store(rooms.tmp_path / "laifu", [("room_abc", KEY)])
    _session_store(rooms.tmp_path / "laifu" / "profiles" / "work", [("room_abc", "agent:work:api_server:dm:room_abc")])
    approvals = approvals_module.Approvals(rooms.tmp_path / "laifu", hermes=runs,
                                           link=lambda link_id: rooms.links["laifu"].get(link_id), clock=rooms.clock)

    assert [shown["session_key"] for shown in approvals.waiting()] == ["agent:work:api_server:dm:room_abc"]


def test_each_agent_is_shown_by_its_handle_in_the_room(rooms):
    # Laifu's work agent is called Furina too: the room made its handle unique at the start, and the home takes
    # that one (rooms remove, @furina-2 in the room).
    namesake = {"profile": "work", "handle": "furina", "display_name": "Furina"}
    room_id = _started(rooms, laifus=(LAIFUS, namesake))
    runs = Runs(rooms.member["laifu"])
    work = _agent_grant(rooms, room_id, "work")
    runs.waiting(OTHER_RUN, work, request_id="req-2")
    _watched(rooms, OTHER_RUN, work, profile="work")
    approvals = approvals_module.Approvals(rooms.tmp_path / "laifu", hermes=runs,
                                           link=lambda link_id: rooms.links["laifu"].get(link_id), clock=rooms.clock)

    [shown] = approvals.waiting()

    assert shown["agent"] == {**namesake, "handle": "furina-2"}
    assert (shown["request_id"], shown["run_id"]) == ("req-2", OTHER_RUN)
    assert [member["handle"] for member in invites_module.people(rooms.tmp_path / "laifu", room_id)["members"]
            if member["member_id"] == slot("link1", 2)] == ["furina-2"]


def test_a_run_its_hermes_wont_show_is_left_out(waiting, caplog):
    caplog.set_level(logging.DEBUG)
    waiting.runs.down = True

    assert waiting.approvals.waiting() == []

    waiting.runs.down = False
    waiting.runs.runs[RUN]["answer"]["status"] = "running"
    del waiting.runs.runs[RUN]["answer"]["approval"]

    assert waiting.approvals.waiting() == []


def test_an_approval_is_listed_while_the_rooms_or_links_store_cant_be_read(waiting, caplog):
    # The answer reads neither, so the list still shows what waits, with what it can say of the room.
    def unreadable(link_id):
        raise links_module.LinkError("unreadable", "links.json isn't a links file this plugin wrote", 500)

    approvals = approvals_module.Approvals(waiting.tmp_path / "laifu", hermes=waiting.runs, link=unreadable,
                                           clock=waiting.clock)

    assert approvals.waiting() == [_shown(waiting.room_id)]  # who hosts it, as the invite named them
    assert "Linked Rooms couldn't read its Links" in caplog.text

    (waiting.tmp_path / "laifu" / invites_module.DIRECTORY / "rooms.json").write_text("not a rooms file")

    assert approvals.waiting() == [{**_shown(waiting.room_id), "room": None, "from": None,
                                    "agent": {"profile": "default", "handle": None, "display_name": None}}]
    assert approvals.answer(RUN, "req-1", "deny") == {"run_id": RUN, "request_id": "req-1", "choice": "deny"}


def test_an_answer_goes_to_this_hermes_with_a_grant_of_its_own(waiting):
    shown = waiting.approvals.answer(RUN, "req-1", "once")

    assert shown == {"run_id": RUN, "request_id": "req-1", "choice": "once"}
    [_, (method, profile, run_id, grant, body)] = waiting.runs.calls
    assert (method, profile, run_id, body) == ("POST", "default", RUN, {"choice": "once", "request_id": "req-1"})
    # A grant minted for this answer alone: the run's own scope and policy, approve only, for a minute.
    claims, joined = waiting.member["laifu"].decode(grant, "approve"), waiting.member["laifu"].decode(
        _agent_grant(waiting, waiting.room_id), "status")
    assert _place(claims) == _place(joined)
    assert claims["execution_policy_digest"] == joined["execution_policy_digest"]
    assert claims["permissions"] == ["approve"]
    assert claims["expires_at"] - claims["issued_at"] == 60 and claims["status_expires_at"] == claims["expires_at"]
    assert claims["grant_id"] != joined["grant_id"]
    # It isn't kept, and the place in the room isn't touched: nothing reserved or revoked for it.
    assert grant not in (waiting.tmp_path / "laifu" / "conduit-rooms" / "bridge.json").read_text()
    assert grant not in (waiting.tmp_path / "laifu" / invites_module.DIRECTORY / "rooms.json").read_text()
    assert waiting.member["laifu"].revoked == []
    assert waiting.runs.runs[RUN]["answer"]["status"] == "running"


def test_an_answer_on_a_gateway_with_a_session_authority_names_the_turns_generation(waiting):
    waiting.runs.waiting(RUN, _agent_grant(waiting, waiting.room_id), canonical=True, request_id="prompt-7")

    assert waiting.approvals.answer(RUN, "prompt-7", "deny") == {"run_id": RUN, "request_id": "prompt-7",
                                                                  "choice": "deny"}
    assert waiting.runs.calls[-1][-1] == {"request_id": "prompt-7", "execution_generation": 3, "choice": "deny"}
    assert waiting.runs.runs[RUN]["answer"]["pending_controls"] == []


@pytest.mark.parametrize("choice", ["session", "always", "", None, "ONCE", ["once"]])
def test_only_once_or_deny(waiting, choice):
    with pytest.raises(approvals_module.ApprovalsError) as refused:
        waiting.approvals.answer(RUN, "req-1", choice)

    assert (refused.value.code, refused.value.status) == ("bad_choice", 400)
    assert waiting.runs.calls == []
    assert all(issued["permissions"] != ["approve"] for issued in waiting.member["laifu"].issued)


@pytest.mark.parametrize("run_id, request_id, code, status", [
    (OTHER_RUN, "req-1", "no_approval", 404),    # a run the bridge doesn't watch
    ("../runs", "req-1", "no_approval", 404),    # not a run id
    (None, "req-1", "no_approval", 404),
    (RUN, "", "bad_request", 400),               # no request id
    (RUN, "r" * 257, "bad_request", 400),        # longer than Hermes takes
    (RUN, 7, "bad_request", 400),
])
def test_an_answer_for_no_approval_here_is_refused(waiting, run_id, request_id, code, status):
    with pytest.raises(approvals_module.ApprovalsError) as refused:
        waiting.approvals.answer(run_id, request_id, "once")

    assert (refused.value.code, refused.value.status) == (code, status)
    assert waiting.runs.calls == []


def test_an_ended_turns_approval_cant_be_answered(waiting):
    _watched(waiting, RUN, _agent_grant(waiting, waiting.room_id), status="cancelled", ended=NOW)

    with pytest.raises(approvals_module.ApprovalsError) as refused:
        waiting.approvals.answer(RUN, "req-1", "once")

    assert (refused.value.code, refused.value.status) == ("no_approval", 404)
    assert waiting.runs.calls == []


def test_an_approval_answered_already_is_refused_with_nothing_minted(waiting):
    assert waiting.approvals.answer(RUN, "req-1", "once")["choice"] == "once"
    minted = len(waiting.member["laifu"].issued)

    for request_id in ("req-1", "req-9"):
        with pytest.raises(approvals_module.ApprovalsError) as refused:
            waiting.approvals.answer(RUN, request_id, "deny")
        assert (refused.value.code, refused.value.status) == ("not_pending", 409)
    assert len(waiting.member["laifu"].issued) == minted


def test_an_approval_its_hermes_answers_meanwhile_is_not_pending(waiting, monkeypatch):
    respond = waiting.runs.respond

    def answered_first(profile, run_id, grant, body):
        respond(profile, run_id, grant, {**body, "choice": "deny"})  # the owner, from another device
        return respond(profile, run_id, grant, body)

    monkeypatch.setattr(waiting.runs, "respond", answered_first)

    with pytest.raises(approvals_module.ApprovalsError) as refused:
        waiting.approvals.answer(RUN, "req-1", "once")

    assert (refused.value.code, refused.value.status) == ("not_pending", 409)


def test_an_answer_its_hermes_doesnt_take_says_why(waiting):
    waiting.runs.down = True
    with pytest.raises(approvals_module.ApprovalsError) as unanswered:
        waiting.approvals.answer(RUN, "req-1", "once")
    assert (unanswered.value.code, unanswered.value.status) == ("api_server", 503)

    # The agent left the room meanwhile: its place's grants are revoked, a fresh one with them.
    waiting.runs.down = False
    waiting.laifu.leave("link1", waiting.room_id)
    with pytest.raises(approvals_module.ApprovalsError) as refused:
        waiting.approvals.answer(RUN, "req-1", "once")
    assert (refused.value.code, refused.value.status) == ("refused", 409)


def test_nothing_about_an_approval_leaves_the_host(waiting, caplog):
    caplog.set_level(logging.DEBUG)
    sent = len(waiting.wire.sent)

    listed = waiting.approvals.waiting()
    waiting.approvals.answer(RUN, "req-1", "once")
    with pytest.raises(approvals_module.ApprovalsError):
        waiting.approvals.answer(RUN, "req-1", "once")

    assert listed and waiting.wire.sent[sent:] == []
    grants = [call[3] for call in waiting.runs.calls]
    for secret in (*grants, COMMAND, DESCRIPTION):
        assert secret not in caplog.text
    # The home hears only that the run waits (room_bridge_member's status, N4a).
    shown = member_module._shown(waiting.runs.runs[RUN]["answer"] | {
        "status": "waiting_for_approval", "approval": {"command": COMMAND, "description": DESCRIPTION}},
        home_stopped=False)
    assert shown == {"status": "waiting_for_approval"}


# --- Routes --------------------------------------------------------------------------


@pytest.fixture
def approval_routes(routes, monkeypatch):
    """test_room_invites' routes, with Laifu's dashboard answering his agents' approvals over the fakes."""
    room_id = _started(routes)
    runs = Runs(routes.member["laifu"])
    grant = _agent_grant(routes, room_id)
    runs.waiting(RUN, grant)
    _watched(routes, RUN, grant)
    dashboard = api._plugin_package_module("room_approvals")
    monkeypatch.setattr(dashboard, "here", lambda root: dashboard.Approvals(
        root, hermes=runs, link=lambda link_id: routes.links["laifu"].get(link_id), clock=routes.clock))
    monkeypatch.setattr(api._room_approvals_limiter, "_mints", {})
    routes.runs, routes.room_id = runs, room_id
    return routes


def test_routes_list_and_answer_your_agents_approvals(approval_routes):
    listed = approval_routes.call("laifu", "GET", "/rooms/approvals")

    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert listed.json() == {"ok": True, "approvals": [_shown(approval_routes.room_id)]}
    # Furina's Hermes hosts the room: none of its turns are hers to approve.
    assert approval_routes.call("furina", "GET", "/rooms/approvals").json() == {"ok": True, "approvals": []}

    answered = approval_routes.call("laifu", "POST", "/rooms/approvals/answer",
                                    json={"run_id": RUN, "request_id": "req-1", "choice": "once"})

    assert answered.status_code == 200
    assert answered.json() == {"ok": True, "approval": {"run_id": RUN, "request_id": "req-1", "choice": "once"}}
    again = approval_routes.call("laifu", "POST", "/rooms/approvals/answer",
                                 json={"run_id": RUN, "request_id": "req-1", "choice": "once"})
    assert again.status_code == 409 and again.json()["code"] == "not_pending"
    wider = approval_routes.call("laifu", "POST", "/rooms/approvals/answer",
                                 json={"run_id": RUN, "request_id": "req-1", "choice": "always"})
    assert wider.status_code == 400 and wider.json()["code"] == "bad_choice"
    for answer in (listed, answered, again, wider):
        assert all(call[3] not in answer.text for call in approval_routes.runs.calls)


def test_the_approvals_routes_answer_sixty_requests_a_minute(approval_routes, monkeypatch):
    # Conduit asks for them every few seconds while it shows a room, beside the drafts and invites.
    now = [1000.0]
    monkeypatch.setattr(api._room_approvals_limiter, "clock", lambda: now[0])
    for _ in range(60):
        assert approval_routes.call("laifu", "GET", "/rooms/approvals").status_code == 200

    refused = approval_routes.call("laifu", "GET", "/rooms/approvals")
    assert refused.status_code == 429 and refused.headers["cache-control"] == "no-store"
    assert approval_routes.call("laifu", "GET", "/rooms/invites").status_code == 200


def test_the_approvals_routes_wait_on_threads_of_their_own(approval_routes, monkeypatch):
    # Each read and answer waits on this Hermes' API server, so a slow one
    # holds only these routes' threads, never the dashboard's shared ones.
    threads = []
    for name in ("run", "respond"):
        real = getattr(approval_routes.runs, name)

        def call(*args, real=real):
            threads.append(threading.current_thread().name)
            return real(*args)

        monkeypatch.setattr(approval_routes.runs, name, call)
    assert approval_routes.call("laifu", "GET", "/rooms/approvals").status_code == 200
    assert approval_routes.call("laifu", "POST", "/rooms/approvals/answer",
                                json={"run_id": RUN, "request_id": "req-1", "choice": "deny"}).status_code == 200
    assert len(threads) == 3 and all(name.startswith("conduit-room-approvals") for name in threads)


def test_an_api_server_too_slow_for_the_route_reads_as_not_answering(approval_routes, monkeypatch):
    release = threading.Event()

    def stuck(*args):
        release.wait(5)
        return None, {}

    monkeypatch.setattr(approval_routes.runs, "run", stuck)
    monkeypatch.setattr(api, "ROOM_APPROVALS_TIMEOUT_S", 0.2)
    try:
        refused = approval_routes.call("laifu", "GET", "/rooms/approvals")
    finally:
        release.set()
    assert refused.status_code == 503 and refused.headers["cache-control"] == "no-store"
    assert refused.json()["code"] == "api_server"


# --- The CLI -------------------------------------------------------------------------


def test_the_cli_lists_and_answers_your_agents_approvals(commands, capsys, monkeypatch):
    room_id = _started(commands)
    runs = Runs(commands.member["laifu"])
    grant = _agent_grant(commands, room_id)
    runs.waiting(RUN, grant)
    _watched(commands, RUN, grant)
    monkeypatch.setattr(approvals_module, "here", lambda root: approvals_module.Approvals(
        root, hermes=runs, link=lambda link_id: commands.links["laifu"].get(link_id), clock=commands.clock))
    capsys.readouterr()

    assert commands.run("laifu", "approvals") == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Zoo trip, from Furina (room {room_id}): @laifu asks to run {COMMAND}",
        f"  {DESCRIPTION}",
        f"  hermes conduit-push rooms approve --run {RUN} --request req-1 (once), or rooms deny --run {RUN} "
        "--request req-1"]
    assert commands.run("laifu", "approve", run=RUN) == 2
    assert capsys.readouterr().out == ("Usage: hermes conduit-push rooms approve --run <run id> --request "
                                       "<request id> (rooms approvals shows them)\n")
    assert commands.run("laifu", "approve", run=RUN, request="req-1") == 0
    assert capsys.readouterr().out == "Approved it, once: the turn goes on.\n"
    assert runs.calls[-1][-1] == {"choice": "once", "request_id": "req-1"}
    assert commands.run("laifu", "deny", run=RUN, request="req-1") == 1
    assert capsys.readouterr().out == "Couldn't deny it: That approval was answered already, or its turn moved on\n"
    assert commands.run("laifu", "approvals") == 0
    assert capsys.readouterr().out == ("None of your agents' turns in rooms other people host waits for your "
                                       "approval.\n")
