"""The member's side of the bridge (designs/cross-owner-agents, N4a): a linked home's requests, replayed to this
Hermes' runs API, and the runs it admitted watched and sent back.

Hermes' runs API is a fake of c73cbcc4's (its answers, statuses and error codes), and the Link a fake that keeps
what's sent; test_two_loads_handle_each_request_once runs over the real mailbox.
"""
import base64
import hashlib
import http.server
import importlib
import importlib.util
import itertools
import json
import logging
import pathlib
import socket
import stat
import sys
import tempfile
import threading
import time
import types

import pytest

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


links_module = _load("conduit_push.room_links", ROOT / "room_links.py")
choices_module = _load("conduit_push.room_choices", ROOT / "room_choices.py")
bridge = _load("conduit_push.room_bridge", ROOT / "room_bridge.py")
member_module = _load("conduit_push.room_bridge_member", ROOT / "room_bridge_member.py")

from test_room_mailbox import mailbox_url, needs_node  # noqa: E402,F401

DAY = 86400
# 26 October 2026, 10:00 UTC.
NOW = 1793008800.0
SCOPE = {"home_install_id": "gw-furina", "room_id": "room-zoo", "member_id": "m-laifu", "target_profile": "default"}
PASS = "(pass)"
NOTICE = "I've used all my turns in this room for today, so I'll sit out until tomorrow."
FAILED = "The turn failed on this agent's Hermes."
CAPPED = "This agent's Hermes stopped the turn after 10 minutes."
RESTARTED = "This agent's Hermes restarted during the turn."
STOPPED = "The turn was stopped on this agent's Hermes."
REFUSED = "This agent's Hermes refused the request."
# What Hermes' client looks for in an invalid_room_dispatch's message.
DISPATCH_NEEDLES = ("room grant", "capability catalog changed", "execution policy changed")
_grants = itertools.count(1)
_messages = itertools.count(1)


def grant(scope=None, **payload):
    """A grant shaped like Hermes' (hosted_room_peer.issue_room_grant); a new grant id each time, as a refresh
    mints."""
    fields = {"version": 2, "grant_id": f"grant-{next(_grants)}", "authority_gateway_id": "gw-furina",
              "authority_epoch": 1, "target_install_id": "gw-laifu", "execution_policy_digest": "0" * 64,
              "permissions": ["approve", "dispatch", "status", "stop"], "issued_at": NOW, "expires_at": NOW + DAY,
              "status_expires_at": NOW + 2 * DAY, **(SCOPE if scope is None else scope), **payload}
    encoded = base64.urlsafe_b64encode(json.dumps(fields).encode()).rstrip(b"=").decode()
    return f"{encoded}.{base64.urlsafe_b64encode(hashlib.sha256(encoded.encode()).digest()).rstrip(b'=').decode()}"


GRANT = grant()


def prompt(*lines, me="laifu", peers=("furina",)):
    """A member's turn prompt as Hermes builds it (gateway/hosted_room_discussion._build_prompt)."""
    opening = [f'[Discussion: "Zoo trip"] You are @{me}, one participant with '
               f'{", ".join("@" + peer for peer in peers)} and the user.', "",
               "New messages in this thread since your last turn (oldest first):"]
    rules = ["", "Rules for this Discussion:",
             "- Reply with one conversational message only when you have something new worth adding.",
             '- If you have nothing new to add, reply with exactly "(pass)".',
             "- Mention a teammate by handle to pull them into the next round; do not repeat points already made.",
             "- Never reveal content from private conversations. Your reply is published verbatim."]
    return "\n".join([*opening, *(f"  {line}" for line in lines), *rules])


def dispatch(task="t1", generation=1, text=None, scope=None):
    """A dispatch's body and its Idempotency-Key, as Hermes' client sends them (with ``input``)."""
    text = prompt("User (user): Zoo on the 22nd?") if text is None else text
    scope = SCOPE if scope is None else scope
    fields = {"protocol_version": 2, "room_id": scope["room_id"], "home_install_id": scope["home_install_id"],
              "authority_gateway_id": "gw-furina", "authority_epoch": 1, "member_id": scope["member_id"],
              "target_install_id": "gw-laifu", "target_profile": scope["target_profile"], "task_id": task,
              "execution_generation": generation, "source_event_seq": 4, "cancellation_scope_id": "cancel-zoo",
              "prompt": text, "prompt_digest": hashlib.sha256(text.encode()).hexdigest(),
              "capability_digest": "1" * 64, "execution_policy_digest": "0" * 64, "trace_id": "trace-1"}
    return {"input": text, "hosted_room_dispatch": fields}, f"room:{task}:{generation}"


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Inline:
    """Runs what the member hands its workers at once, so a test sees it done."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class Link:
    """A fake of Links.send: what the member sends the home, in order."""

    def __init__(self):
        self.sent = []
        self.tried = []
        self.refusing = False

    def send(self, link_id, kind, body, message_id=None):
        self.tried.append((link_id, kind))
        if self.refusing:
            raise links_module.LinkError("unreachable", "Couldn't reach the mailbox", 503)
        self.sent.append((link_id, kind, json.loads(json.dumps(body)), message_id))
        return message_id or f"sent{len(self.sent)}"

    def responses(self):
        return {body["re"]: body for _, kind, body, _ in self.sent if kind == bridge.RESPONSE}

    def statuses(self):
        return [body for _, kind, body, _ in self.sent if kind == bridge.STATUS]


class FakeHermes:
    """Hermes' runs API (gateway/platforms/api_server_runs.py, api_server_room_grants.py) for one installation,
    answering as c73cbcc4 does, and keeping every call."""

    def __init__(self):
        self.calls = []
        self.runs = {}
        self.keys = {}
        self.refuse = {}
        self.answers = {}
        self.slow = 0.0
        hermes = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hermes._answer(self, "GET")

            def do_POST(self):
                hermes._answer(self, "POST")

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that stopped waiting (the slow test) closed its end

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, profile):
        base = f"http://127.0.0.1:{self.server.server_port}"
        return base if profile == "default" else f"{base}/p/{profile}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def calls_to(self, method, route):
        return [call for call in self.calls if (call["method"], call["route"]) == (method, route)]

    def run(self, run_id, **fields):
        self.runs[run_id].update(fields)

    def _answer(self, handler, method):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        path, profile = handler.path, "default"
        if path.startswith("/p/"):
            _, _, profile, rest = path.split("/", 3)
            path = "/" + rest
        parts = path.strip("/").split("/")
        run_id = parts[2] if len(parts) >= 3 and parts[:2] == ["v1", "runs"] else None
        route = path if run_id is None else "/v1/runs/{id}" + (f"/{parts[3]}" if len(parts) > 3 else "")
        call = {"method": method, "route": route, "run_id": run_id, "profile": profile,
                "authorization": handler.headers.get("Authorization"), "key": handler.headers.get("Idempotency-Key"),
                "body": json.loads(raw) if raw else None}
        self.calls.append(call)
        if self.slow:
            time.sleep(self.slow)
        refusal = self.refuse.get((method, route))
        if refusal is not None:
            status, code, message = refusal
            return self._send(handler, status, {"error": {"message": message, "type": "invalid_request_error",
                                                          "param": None, "code": code}})
        status, body = self.answers.get((method, route)) or self._route(method, route, run_id, call)
        self._send(handler, status, body)

    def _route(self, method, route, run_id, call):
        if (method, route) == ("POST", "/v1/runs"):
            key = call["key"]
            if key in self.keys:
                known = self.keys[key]
                return 202, {"run_id": known, "status": self.runs[known]["status"], "replayed": True}
            run_id = f"run_{len(self.runs) + 1:032x}"
            self.keys[key] = run_id
            self.runs[run_id] = {"object": "hermes.run", "run_id": run_id, "status": "queued",
                                 "session_id": "room_abc", "admission_id": f"adm-{len(self.runs) + 1}",
                                 "execution_generation": 1, "output": "", "usage": {}, "pending_controls": []}
            return 202, {"run_id": run_id, "status": "started", "replayed": False}
        if route.startswith("/v1/runs/{id}") and run_id not in self.runs:
            return 404, {"error": {"message": f"Run not found: {run_id}", "type": "invalid_request_error",
                                   "param": None, "code": "run_not_found"}}
        if (method, route) == ("GET", "/v1/runs/{id}"):
            return 200, self.runs[run_id]
        if (method, route) == ("POST", "/v1/runs/{id}/stop"):
            if self.runs[run_id]["status"] in ("completed", "failed", "cancelled", "interrupted"):
                return 200, self.runs[run_id]
            self.runs[run_id]["status"] = "stopping"
            return 200, {"run_id": run_id, "status": "stopping", "admission_id": self.runs[run_id]["admission_id"]}
        if (method, route) == ("POST", "/v1/runs/{id}/resolve-unknown"):
            self.runs[run_id]["status"] = "cancelled"
            return 200, {"run_id": run_id, "admission_id": self.runs[run_id]["admission_id"], "outcome": "resolved"}
        if (method, route) == ("GET", "/v1/room-members/capabilities"):
            return 200, {"object": "hermes.room_member.capabilities", "room_id": "room-zoo",
                         "home_install_id": "gw-furina", "authority_gateway_id": "gw-furina", "authority_epoch": 1,
                         "member_id": "m-laifu", "target_profile": call["profile"],
                         "catalog": {"installation_id": "gw-laifu", "catalog_digest": "1" * 64},
                         "debug": "/home/laifu/.hermes"}
        if (method, route) == ("POST", "/v1/room-members/grants/refresh"):
            return 200, {"object": "hermes.room_member.grant", "grant": "refreshed-grant", "expires_at": NOW + DAY,
                         "status_expires_at": NOW + 2 * DAY, "execution_policy": {"policy_digest": "0" * 64}}
        if (method, route) == ("POST", "/v1/room-members/grants/revoke"):
            return 200, {"object": "hermes.room_member.grant.revocation", "revoked": True}
        return 200, {"answered": route}

    @staticmethod
    def _send(handler, status, body):
        raw = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


@pytest.fixture
def hermes():
    fake = FakeHermes()
    yield fake
    fake.close()


def _homes(root):
    return lambda profile: root if profile == "default" else root / "profiles" / profile


@pytest.fixture
def laifu(tmp_path, hermes):
    """Laifu's installation, a member of Furina's rooms over Link ``link1``."""
    root = tmp_path / "laifu"
    side = types.SimpleNamespace(root=root, link=Link(), clock=Clock(), monotonic=Clock(0.0), hermes=hermes)
    side.member = _member(side)
    member_module.bind(root, "link1", SCOPE)
    return side


def _member(side, **changes):
    options = dict(send=side.link.send, api_url=side.hermes.url, clock=side.clock, monotonic=side.monotonic,
                   profile_home=_homes(side.root), executor=Inline(), api_timeout=2.0)
    options.update(changes)
    return member_module.Member(side.root, **options)


def ask(side, method="POST", path="/v1/runs", *, body=None, key=None, grant_value=None, profile="default", age=0,
        link="link1"):
    """Hands the member a roomlink.request as the Link's loop would: its response, or None."""
    message_id = f"m{next(_messages)}"
    payload = {"v": 1, "profile": profile, "method": method, "path": path,
               "grant": GRANT if grant_value is None else grant_value}
    if key is not None:
        payload["key"] = key
    if body is not None:
        payload["body"] = body
    side.member.handle(links_module.Received(link_id=link, kind=bridge.REQUEST, message_id=message_id, at=int(NOW),
                                             body=payload, age=age, root=side.root))
    return side.link.responses().get(message_id)


def admit(side, task="t1", **changes):
    body, key = dispatch(task, **changes)
    answer = ask(side, body=body, key=key)
    assert answer["status"] == 202, answer
    return answer["body"]["run_id"]


def choose(side, profile="default", **choices):
    home = _homes(side.root)(profile)
    home.mkdir(parents=True, exist_ok=True)
    choices_module.ChoicesStore(home).set(SCOPE["room_id"], choices)


# --- Requests -----------------------------------------------------------------------


def test_a_dispatch_is_replayed_with_its_grant_and_key_and_no_input(laifu, hermes):
    body, key = dispatch()

    ask(laifu, body=body, key=key)

    [call] = hermes.calls
    assert (call["method"], call["route"], call["profile"]) == ("POST", "/v1/runs", "default")
    assert call["authorization"] == f"HermesRoom {GRANT}"
    assert call["key"] == key
    assert call["body"] == {"hosted_room_dispatch": body["hosted_room_dispatch"]}


def test_a_named_profiles_agent_is_reached_at_its_own_path(laifu, hermes):
    scope = dict(SCOPE, target_profile="work")
    member_module.bind(laifu.root, "link1", scope)
    body, key = dispatch(scope=scope)

    answer = ask(laifu, body=body, key=key, grant_value=grant(scope), profile="work")

    assert answer["status"] == 202
    assert [call["profile"] for call in hermes.calls] == ["work"]


def test_the_answer_goes_back_with_the_runs_status(laifu, hermes):
    body, key = dispatch()

    answer = ask(laifu, body=body, key=key)
    again = ask(laifu, body=body, key=key)

    run_id = answer["body"]["run_id"]
    assert answer["v"] == 1 and answer["status"] == 202
    assert answer["body"] == {"run_id": run_id, "status": "started", "replayed": False}
    assert answer["run"] == {"run_id": run_id, "status": "queued"}
    assert again["body"] == {"run_id": run_id, "status": "queued", "replayed": True}
    assert [kind for _, kind, _, _ in laifu.link.sent] == [bridge.RESPONSE, bridge.RESPONSE]
    assert {link for link, _, _, _ in laifu.link.sent} == {"link1"}


@pytest.mark.parametrize("age, answered", [(0, True), (30, True), (31, False), (3600, False)])
def test_a_request_older_than_30_seconds_by_the_mailbox_clock_is_dropped(laifu, hermes, age, answered):
    # The mailbox's times alone: this host's clock hours off changes nothing.
    laifu.clock.now = NOW + 5 * 3600
    body, key = dispatch()

    answer = ask(laifu, body=body, key=key, age=age)

    assert (answer is not None) is answered
    assert len(hermes.calls_to("POST", "/v1/runs")) == (1 if answered else 0)


def test_a_request_that_waited_for_a_worker_past_30_seconds_is_dropped(laifu, hermes):
    class Later:
        def __init__(self):
            self.waiting = []

        def submit(self, fn, *args, **kwargs):
            self.waiting.append((fn, args, kwargs))

    workers = Later()
    laifu.member = _member(laifu, executor=workers)
    body, key = dispatch()
    ask(laifu, body=body, key=key, age=20)
    laifu.monotonic.now += 11

    for fn, args, kwargs in workers.waiting:
        fn(*args, **kwargs)

    assert hermes.calls == [] and laifu.link.sent == []


def test_stops_and_revokes_apply_however_old(laifu, hermes):
    run_id = admit(laifu)

    stopped = ask(laifu, path=f"/v1/runs/{run_id}/stop", body={}, age=3600)
    revoked = ask(laifu, path="/v1/room-members/grants/revoke", body={}, age=3600)
    read = ask(laifu, method="GET", path=f"/v1/runs/{run_id}", age=3600)

    assert stopped["status"] == 200 and stopped["body"] == {"run_id": run_id, "status": "stopping"}
    assert revoked["status"] == 200 and revoked["body"] == {"revoked": True}
    assert read is None
    assert len(hermes.calls_to("POST", "/v1/runs/{id}/stop")) == 1
    assert len(hermes.calls_to("POST", "/v1/room-members/grants/revoke")) == 1


def test_a_grant_not_bound_to_the_link_is_refused(laifu, hermes):
    other_room = dict(SCOPE, room_id="room-park")
    body, key = dispatch(scope=other_room)

    unbound = ask(laifu, body=body, key=key, grant_value=grant(other_room))
    elsewhere = ask(laifu, method="GET", path="/v1/room-members/capabilities", link="link2")
    refreshed = ask(laifu, method="GET", path="/v1/room-members/capabilities", grant_value=grant())
    member_module.unbind(laifu.root, "link1", SCOPE)
    after = ask(laifu, method="GET", path="/v1/room-members/capabilities")

    for answer in (unbound, elsewhere, after):
        assert answer["status"] == 403 and answer["body"]["error"]["code"] == "invalid_room_grant"
    assert refreshed["status"] == 200  # a refresh's new grant id, for the same scope
    assert len(hermes.calls) == 1


def test_a_grant_for_another_profile_is_refused(laifu, hermes, caplog):
    work = dict(SCOPE, target_profile="work")
    member_module.bind(laifu.root, "link1", work)

    other_profile = ask(laifu, method="GET", path="/v1/room-members/capabilities", grant_value=grant(work))
    with caplog.at_level(logging.INFO):
        unreadable = [ask(laifu, method="GET", path="/v1/room-members/capabilities", grant_value="no-payload.sig")
                      for _ in range(3)]

    for answer in (other_profile, *unreadable):
        assert answer["status"] == 403 and answer["body"]["error"]["code"] == "invalid_room_grant"
    assert hermes.calls == []
    assert len([record for record in caplog.records if "grant" in record.getMessage()]) == 1


@pytest.mark.parametrize("action", ["approval", "clarify"])
@pytest.mark.parametrize("bound", [True, False])
def test_approvals_and_clarify_are_never_replayed(laifu, hermes, action, bound):
    run_id = admit(laifu)
    hermes.calls.clear()
    grant_value = GRANT if bound else grant(dict(SCOPE, room_id="room-park"))

    answer = ask(laifu, path=f"/v1/runs/{run_id}/{action}", grant_value=grant_value,
                 body={"request_id": "a1", "execution_generation": 1, "choice": "once"})

    assert answer["status"] == 403 and answer["body"]["error"]["code"] == "room_approval_elsewhere"
    assert hermes.calls == []


@pytest.mark.parametrize("method, path", [
    ("GET", "/v1/runs"), ("POST", "/v1/runs/run_1"), ("GET", "/v1/runs/run_1/events"),
    ("POST", "/v1/runs/run_1/steer"), ("POST", "/v1/runs/run_1/resolve-unknown"), ("GET", "/v1/models"),
    ("POST", "/v1/room-members/invitations"), ("GET", "/v1/runs/../../health"), ("POST", "/api/sessions"),
])
def test_anything_else_is_not_found_and_never_replayed(laifu, hermes, method, path):
    answer = ask(laifu, method=method, path=path, body={} if method == "POST" else None)

    assert answer["status"] == 404 and answer["body"]["error"]["code"] == "not_found"
    assert hermes.calls == []


def test_a_dispatch_for_another_room_than_its_grants_is_refused(laifu, hermes):
    choose(laifu, takes_part="listens")
    body, key = dispatch(scope=dict(SCOPE, room_id="room-park"))

    answer = ask(laifu, body=body, key=key)
    no_key = ask(laifu, body=dispatch()[0])
    wrong_key = ask(laifu, body=dispatch()[0], key="room:other:1")
    more = ask(laifu, body={**dispatch()[0], "stream": True}, key=dispatch()[1])

    # As Hermes' own refusals read to its client: a dispatch outside its
    # grant's scope as the grant's, which cuts the member off (N2), and the
    # rest as a dispatch that wasn't admitted.
    assert answer["status"] == 403 and answer["body"]["error"]["code"] == "invalid_room_grant"
    for refused in (no_key, wrong_key, more):
        assert refused["status"] == 403 and refused["body"]["error"]["code"] == "invalid_room_dispatch"
        assert not any(needle in refused["body"]["error"]["message"].lower() for needle in DISPATCH_NEEDLES)
    assert hermes.calls == []


# --- Taking part and the daily limit -------------------------------------------------


@pytest.mark.parametrize("text, asked", [
    (prompt("User (user): @laifu, zoo on the 22nd?"), True),
    (prompt("User (user): Zoo on the 22nd?", "@furina: What does @Laifu think?"), True),
    (prompt("User (user): Anyone free? @everyone"), True),
    (prompt("User (user): Zoo on the 22nd?", "@laifu: I could ask @laifu's calendar"), False),
    (prompt("User (user): Zoo on the 22nd?", "@furina: I can do the 22nd"), False),
    (prompt("User (user): Zoo on the 22nd?\nand bring snacks, @laifu"), True),
    (prompt("User (user): mail me at x@laifu.example"), False),
    (prompt(), False),
    ("Not a room's prompt, @laifu", False),
])
def test_when_asked_passes_unless_a_new_message_mentions_the_agent(laifu, hermes, text, asked):
    choose(laifu, takes_part="asked")
    body, key = dispatch(text=text)

    answer = ask(laifu, body=body, key=key)

    assert answer["status"] == 202
    if asked:
        assert len(hermes.calls_to("POST", "/v1/runs")) == 1
    else:
        assert hermes.calls == []
        assert answer["run"] == {"run_id": answer["body"]["run_id"], "status": "completed", "output": PASS}


def test_a_prompt_it_cant_read_is_logged_once_a_room(laifu, hermes, caplog):
    choose(laifu, takes_part="asked")

    with caplog.at_level(logging.INFO):
        for task in ("t1", "t2", "t3"):
            body, key = dispatch(task, text="Not a room's prompt")
            ask(laifu, body=body, key=key)

    assert len([record for record in caplog.records if "prompt" in record.getMessage()]) == 1


def test_listens_only_passes(laifu, hermes):
    choose(laifu, takes_part="listens")
    body, key = dispatch(text=prompt("User (user): @laifu, zoo on the 22nd?"))

    answer = ask(laifu, body=body, key=key)

    assert hermes.calls == []
    assert answer["run"]["output"] == PASS and answer["run"]["status"] == "completed"


def test_the_daily_limit_tells_once_then_passes(laifu, hermes):
    choose(laifu, daily_limit=1)

    first = admit(laifu, "t1")
    notice = ask(laifu, body=dispatch("t2")[0], key=dispatch("t2")[1])
    quiet = ask(laifu, body=dispatch("t3")[0], key=dispatch("t3")[1])
    laifu.clock.now += DAY
    next_day = ask(laifu, body=dispatch("t4")[0], key=dispatch("t4")[1])

    assert first.startswith("run_")
    assert notice["run"]["output"] == NOTICE and quiet["run"]["output"] == PASS
    assert [call["key"] for call in hermes.calls_to("POST", "/v1/runs")] == ["room:t1:1", "room:t4:1"]
    assert next_day["run"]["status"] == "queued"


def test_a_retried_dispatch_counts_once_toward_the_limit(laifu, hermes):
    choose(laifu, daily_limit=1)
    body, key = dispatch("t1")

    ask(laifu, body=body, key=key)
    again = ask(laifu, body=body, key=key)

    assert again["body"]["replayed"] is True and again["run"]["status"] == "queued"
    assert len(hermes.calls_to("POST", "/v1/runs")) == 2


def test_a_dispatch_hermes_refused_doesnt_count_toward_the_limit(laifu, hermes):
    choose(laifu, daily_limit=1)
    hermes.refuse[("POST", "/v1/runs")] = (429, "rate_limited", "Too many runs")

    refused = ask(laifu, body=dispatch("t1")[0], key=dispatch("t1")[1])
    del hermes.refuse[("POST", "/v1/runs")]
    admitted = ask(laifu, body=dispatch("t2")[0], key=dispatch("t2")[1])

    assert refused["status"] == 429 and admitted["run"]["status"] == "queued"


def test_a_dispatch_hermes_didnt_answer_keeps_its_turn_for_the_retry(laifu, hermes):
    # Hermes may have admitted it: the retry with its key gets the day's turn.
    choose(laifu, daily_limit=1)
    hermes.refuse[("POST", "/v1/runs")] = (503, "unavailable", "Busy")
    body, key = dispatch("t1")

    first = ask(laifu, body=body, key=key)
    del hermes.refuse[("POST", "/v1/runs")]
    other = ask(laifu, body=dispatch("t2")[0], key=dispatch("t2")[1])
    retried = ask(laifu, body=body, key=key)

    assert first["status"] == 503 and first["body"]["error"]["code"] == "unavailable"
    assert other["run"]["output"] == NOTICE
    assert retried["status"] == 202 and retried["run"]["status"] == "queued"


def test_a_named_profiles_own_choices_apply(laifu, hermes):
    work = dict(SCOPE, target_profile="work")
    member_module.bind(laifu.root, "link1", work)
    choose(laifu, profile="work", takes_part="listens")
    laifu.member = _member(laifu, profile_home=None)
    body, key = dispatch(scope=work)

    answer = ask(laifu, body=body, key=key, grant_value=grant(work), profile="work")

    assert answer["run"]["output"] == PASS and hermes.calls == []


def test_asking_again_gets_the_same_pass_run(laifu, hermes):
    choose(laifu, takes_part="listens")
    body, key = dispatch()

    first = ask(laifu, body=body, key=key)
    choose(laifu, takes_part="joins")
    again = ask(laifu, body=body, key=key)
    run_id = first["body"]["run_id"]
    read = ask(laifu, method="GET", path=f"/v1/runs/{run_id}")
    stopped = ask(laifu, path=f"/v1/runs/{run_id}/stop", body={})

    assert again["body"] == {"run_id": run_id, "status": "completed", "replayed": True}
    assert again["run"] == first["run"] == {"run_id": run_id, "status": "completed", "output": PASS}
    assert read["status"] == 200 and read["body"] == {"run_id": run_id, "status": "completed", "output": PASS}
    assert stopped["status"] == 200 and stopped["body"] == {"run_id": run_id, "status": "completed"}
    assert hermes.calls == []
    # The id comes from the Link and the key.
    choose(laifu, takes_part="listens")
    member_module.bind(laifu.root, "link2", SCOPE)
    elsewhere = ask(laifu, body=body, key=key, link="link2")
    assert elsewhere["run"]["output"] == PASS and elsewhere["body"]["run_id"] != run_id


# --- Watching the runs it admitted ----------------------------------------------------


def test_status_goes_on_change_and_every_minute(laifu, hermes):
    run_id = admit(laifu)

    hermes.run(run_id, status="running")
    laifu.member.tick()
    laifu.member.tick()
    laifu.clock.now += 59
    laifu.member.tick()
    laifu.clock.now += 1
    laifu.member.tick()
    hermes.run(run_id, status="completed", output="Yes! The 22nd works.")
    laifu.member.tick()
    reads = len(hermes.calls_to("GET", "/v1/runs/{id}"))
    laifu.clock.now += 120
    laifu.member.tick()

    assert laifu.link.statuses() == [
        {"v": 1, "run_id": run_id, "seq": 1, "status": "running"},
        {"v": 1, "run_id": run_id, "seq": 2, "status": "running"},
        {"v": 1, "run_id": run_id, "seq": 3, "status": "completed", "output": "Yes! The 22nd works."},
    ]
    assert len(hermes.calls_to("GET", "/v1/runs/{id}")) == reads  # an ended run isn't read again
    assert all(call["authorization"] == f"HermesRoom {GRANT}" for call in hermes.calls)


def test_a_failure_goes_back_as_a_fixed_sentence(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="failed", output="partial text", error="Traceback (most recent call last)")

    laifu.member.tick()

    assert laifu.link.statuses() == [{"v": 1, "run_id": run_id, "seq": 1, "status": "failed", "error": FAILED}]


def test_a_turn_past_10_minutes_is_stopped_and_sent_interrupted(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="running")
    laifu.member.tick()

    laifu.clock.now += 599
    laifu.member.tick()
    assert hermes.calls_to("POST", "/v1/runs/{id}/stop") == []
    laifu.clock.now += 1
    laifu.member.tick()
    laifu.member.tick()

    assert len(hermes.calls_to("POST", "/v1/runs/{id}/stop")) == 1
    assert laifu.link.statuses()[-1] == {"v": 1, "run_id": run_id, "seq": 3, "status": "interrupted",
                                         "error": CAPPED}


def test_the_homes_stop_is_cancelled_and_other_cancels_interrupted(laifu, hermes):
    stopped_by_home = admit(laifu, "t1")
    stopped_here = admit(laifu, "t2")

    ask(laifu, path=f"/v1/runs/{stopped_by_home}/stop", body={})
    hermes.run(stopped_by_home, status="cancelled")
    hermes.run(stopped_here, status="cancelled")
    laifu.member.tick()

    assert sorted(laifu.link.statuses(), key=lambda body: body["run_id"]) == [
        {"v": 1, "run_id": stopped_by_home, "seq": 1, "status": "cancelled"},
        {"v": 1, "run_id": stopped_here, "seq": 1, "status": "interrupted", "error": STOPPED},
    ]


def test_an_unknown_run_is_resolved_and_sent_interrupted(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="unknown", admission_id="adm-7", execution_generation=3)

    laifu.member.tick()

    [resolved] = hermes.calls_to("POST", "/v1/runs/{id}/resolve-unknown")
    assert resolved["body"] == {"admission_id": "adm-7", "execution_generation": 3}
    assert resolved["authorization"] == f"HermesRoom {GRANT}"
    assert laifu.link.statuses() == [{"v": 1, "run_id": run_id, "seq": 1, "status": "interrupted",
                                      "error": RESTARTED}]


def test_a_run_its_hermes_lost_is_sent_interrupted(laifu, hermes):
    run_id = admit(laifu)
    del hermes.runs[run_id]

    laifu.member.tick()

    assert laifu.link.statuses() == [{"v": 1, "run_id": run_id, "seq": 1, "status": "interrupted",
                                      "error": RESTARTED}]


def test_approval_details_never_leave_the_host(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="waiting_for_approval", last_event="approval.request",
               approval={"command": "rm -rf ~/photos", "request_id": "a1"},
               pending_controls=[{"kind": "approval", "prompt_id": "a1", "command": "rm -rf ~/photos"}])

    laifu.member.tick()
    read = ask(laifu, method="GET", path=f"/v1/runs/{run_id}")

    assert laifu.link.statuses() == [{"v": 1, "run_id": run_id, "seq": 1, "status": "waiting_for_approval"}]
    assert read["body"] == {"run_id": run_id, "status": "waiting_for_approval"}
    assert "photos" not in json.dumps(laifu.link.sent)


def test_a_state_hermes_client_wouldnt_know_reads_running(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="paused")

    laifu.member.tick()

    assert laifu.link.statuses() == [{"v": 1, "run_id": run_id, "seq": 1, "status": "running"}]


def test_a_link_that_refuses_a_status_gets_no_more_that_pass(laifu, hermes):
    runs = [admit(laifu, task) for task in ("t1", "t2")]
    for run_id in runs:
        hermes.run(run_id, status="running")
    laifu.link.refusing = True

    laifu.member.tick()
    laifu.link.refusing = False
    laifu.member.tick()

    assert [kind for _, kind in laifu.link.tried].count(bridge.STATUS) == 3
    assert sorted(body["run_id"] for body in laifu.link.statuses()) == sorted(runs)


def test_an_admission_without_a_run_id_is_503(laifu, hermes):
    hermes.answers[("POST", "/v1/runs")] = (202, {"status": "started", "replayed": False})

    answer = ask(laifu, body=dispatch()[0], key=dispatch()[1])

    assert answer["status"] == 503 and answer["body"]["error"]["code"] == "member_unavailable"
    assert bridge.Store(laifu.root).read()["member"]["runs"] == {}


def test_a_status_read_hermes_answers_is_shown_as_the_watch_shows_it(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="completed", output="Yes!", usage={"input_tokens": 4})

    read = ask(laifu, method="GET", path=f"/v1/runs/{run_id}")
    not_admitted_here = ask(laifu, method="GET", path="/v1/runs/run_unknown")

    assert read["status"] == 200 and read["body"] == {"run_id": run_id, "status": "completed", "output": "Yes!"}
    assert not_admitted_here["status"] == 404 and not_admitted_here["body"]["error"]["code"] == "run_not_found"


def test_errors_go_back_as_their_code_never_their_text(laifu, hermes):
    hermes.refuse[("POST", "/v1/runs")] = (403, "room_execution_policy_changed", "policy at /home/laifu/x changed")
    hermes.refuse[("GET", "/v1/room-members/capabilities")] = (401, "invalid_room_grant", "Room authorization is "
                                                                                          "invalid or expired.")
    hermes.refuse[("POST", "/v1/room-members/grants/refresh")] = (400, "Not A Code!", "ttl_seconds was 'abc'")

    dispatched = ask(laifu, body=dispatch()[0], key=dispatch()[1])
    probed = ask(laifu, method="GET", path="/v1/room-members/capabilities")
    refreshed = ask(laifu, path="/v1/room-members/grants/refresh", body={"ttl_seconds": 3600})

    assert dispatched["status"] == 403
    assert dispatched["body"] == {"error": {"code": "room_execution_policy_changed", "message": REFUSED}}
    assert probed["status"] == 401 and probed["body"] == {"error": {"code": "invalid_room_grant", "message": REFUSED}}
    assert refreshed["status"] == 400 and refreshed["body"] == {"error": {"message": REFUSED}}
    assert "/home/laifu" not in json.dumps(laifu.link.sent) and "abc" not in json.dumps(laifu.link.sent)


@pytest.mark.parametrize("message, code", [
    ("room grant scope does not match dispatch", "invalid_room_grant"),
    ("Room grant is expired or not active", "invalid_room_grant"),
    ("room capability catalog changed", "room_capability_catalog_changed"),
    ("room execution policy changed", "room_execution_policy_changed"),
    ("room dispatch idempotency key is invalid", "invalid_room_dispatch"),
])
def test_a_dispatch_refusal_goes_back_with_the_code_hermes_client_reads(laifu, hermes, message, code):
    # Hermes' client reads these codes from an invalid_room_dispatch's message
    # (hosted_room_peer_http._LEGACY_DISPATCH_MESSAGE_CODES); the message stays here.
    hermes.refuse[("POST", "/v1/runs")] = (403, "invalid_room_dispatch", message)

    answer = ask(laifu, body=dispatch()[0], key=dispatch()[1])

    assert answer["status"] == 403 and answer["body"] == {"error": {"code": code, "message": REFUSED}}


def test_grant_routes_answer_only_what_hermes_client_reads(laifu, hermes):
    probed = ask(laifu, method="GET", path="/v1/room-members/capabilities")
    refreshed = ask(laifu, path="/v1/room-members/grants/refresh", body={"ttl_seconds": 3600, "extra": True})

    assert probed["body"] == {"object": "hermes.room_member.capabilities", "room_id": "room-zoo",
                              "home_install_id": "gw-furina", "authority_gateway_id": "gw-furina",
                              "authority_epoch": 1, "member_id": "m-laifu", "target_profile": "default",
                              "catalog": {"installation_id": "gw-laifu", "catalog_digest": "1" * 64}}
    assert refreshed["body"] == {"object": "hermes.room_member.grant", "grant": "refreshed-grant",
                                 "expires_at": NOW + DAY, "status_expires_at": NOW + 2 * DAY}
    assert hermes.calls_to("POST", "/v1/room-members/grants/refresh")[0]["body"] == {"ttl_seconds": 3600}


def test_no_answer_from_the_api_server_is_503(laifu, hermes):
    run_id = admit(laifu)
    port = hermes.server.server_port
    hermes.close()
    laifu.member = _member(laifu, api_url=lambda profile: f"http://127.0.0.1:{port}")

    answers = [ask(laifu, body=dispatch("t2")[0], key=dispatch("t2")[1]),
               ask(laifu, method="GET", path=f"/v1/runs/{run_id}"),
               ask(laifu, path=f"/v1/runs/{run_id}/stop", body={}),
               ask(laifu, method="GET", path="/v1/room-members/capabilities")]
    laifu.member.tick()

    assert [answer["status"] for answer in answers] == [503, 503, 503, 503]
    assert all(answer["body"]["error"]["code"] == "member_unavailable" for answer in answers)
    assert laifu.link.statuses() == []  # nothing said about a run it couldn't read


def test_a_slow_api_server_is_503_after_its_time(laifu, hermes):
    hermes.slow = 0.5
    laifu.member = _member(laifu, api_timeout=0.2)

    answer = ask(laifu, body=dispatch()[0], key=dispatch()[1])

    assert answer["status"] == 503


def test_a_mailbox_that_takes_no_status_now_gets_it_on_the_next_pass(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="completed", output="Yes!")
    laifu.link.refusing = True
    laifu.member.tick()
    laifu.link.refusing = False
    laifu.member.tick()
    laifu.member.tick()

    assert laifu.link.statuses() == [{"v": 1, "run_id": run_id, "seq": 1, "status": "completed", "output": "Yes!"}]


def test_output_is_cut_at_64_kib(laifu, hermes):
    run_id = admit(laifu)
    long_reply = "é" * 40_000  # 80,000 bytes
    hermes.run(run_id, status="completed", output=long_reply)

    laifu.member.tick()

    [sent] = laifu.link.statuses()
    assert len(sent["output"].encode("utf-8")) == 65536
    assert long_reply.startswith(sent["output"])


def test_watching_survives_a_restart(laifu, hermes):
    run_id = admit(laifu)
    hermes.run(run_id, status="running")
    laifu.member.tick()

    restarted = _member(laifu)
    hermes.run(run_id, status="completed", output="Back with an answer")
    restarted.tick()

    assert laifu.link.statuses()[-1] == {"v": 1, "run_id": run_id, "seq": 2, "status": "completed",
                                         "output": "Back with an answer"}


def test_ended_runs_go_after_an_hour_and_day_counts_after_their_day(laifu, hermes):
    choose(laifu, takes_part="listens")
    ask(laifu, body=dispatch("t1")[0], key=dispatch("t1")[1])
    choose(laifu, takes_part="joins")
    run_id = admit(laifu, "t2")
    hermes.run(run_id, status="completed", output="Yes!")
    laifu.member.tick()

    laifu.clock.now += 3601
    laifu.member.tick()
    laifu.clock.now += DAY
    laifu.member.tick()

    member = bridge.Store(laifu.root).read()["member"]
    assert member["runs"] == {} and member["days"] == {}
    assert member["bindings"] == {"link1": [SCOPE]}


def test_bridge_json_is_owner_only_and_no_grant_is_logged(laifu, hermes, caplog):
    with caplog.at_level(logging.DEBUG):
        run_id = admit(laifu)
        hermes.run(run_id, status="running")
        laifu.member.tick()
        ask(laifu, method="GET", path="/v1/room-members/capabilities", grant_value=grant(dict(SCOPE, room_id="x")))
        ask(laifu, method="GET", path="/v1/room-members/capabilities", grant_value="unreadable.grant")
        hermes.refuse[("POST", "/v1/runs/{id}/stop")] = (409, "run_not_active", "no")
        ask(laifu, path=f"/v1/runs/{run_id}/stop", body={})

    path = laifu.root / "conduit-rooms" / "bridge.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert GRANT in path.read_text()  # kept there, for the watching
    assert GRANT not in caplog.text and GRANT.split(".")[0] not in caplog.text
    assert "unreadable.grant" not in caplog.text


# --- Binding ------------------------------------------------------------------------


def test_bind_takes_only_a_scope_and_unbind_takes_one_or_all(tmp_path):
    root = tmp_path / "laifu"
    park = dict(SCOPE, room_id="room-park")

    member_module.bind(root, "link1", SCOPE)
    member_module.bind(root, "link1", SCOPE)
    member_module.bind(root, "link1", park)
    member_module.bind(root, "link2", SCOPE)
    for bad in ({**SCOPE, "extra": "x"}, dict(SCOPE, room_id="room zoo"), dict(SCOPE, target_profile="Work")):
        with pytest.raises(ValueError):
            member_module.bind(root, "link1", bad)
    with pytest.raises(ValueError):
        member_module.bind(root, "not a link id", SCOPE)

    assert bridge.Store(root).read()["member"]["bindings"] == {"link1": [SCOPE, park], "link2": [SCOPE]}
    member_module.unbind(root, "link1", park)
    member_module.unbind(root, "link2")
    assert bridge.Store(root).read()["member"]["bindings"] == {"link1": [SCOPE]}


# --- Where it runs --------------------------------------------------------------------


def test_start_takes_requests_for_its_installation_and_starts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(member_module, "_members", {})
    monkeypatch.setattr(member_module, "_watch", lambda member: None)
    monkeypatch.delitem(links_module.HANDLERS, bridge.REQUEST, raising=False)
    root = tmp_path / "laifu"

    bridge.start(root)
    started = member_module._members[str(root)]
    bridge.start(root)
    handled = []
    monkeypatch.setattr(started, "handle", handled.append)
    for went_to in (root, tmp_path / "furina", None):
        links_module.HANDLERS[bridge.REQUEST](links_module.Received(
            link_id="link1", kind=bridge.REQUEST, message_id="m1", at=int(NOW), body={}, age=0, root=went_to))

    assert member_module._members == {str(root): started}
    assert [received.root for received in handled] == [root]


def test_it_replays_to_the_api_server_room_setup_finds(laifu, hermes, monkeypatch):
    setup = importlib.import_module("conduit_push.room_setup")
    loads = []
    monkeypatch.setattr(setup.Hermes, "load", classmethod(lambda cls: loads.append(cls) or "this Hermes"))
    monkeypatch.setattr(setup, "local_api_url", lambda found, home, profile: hermes.url(profile)
                        if (found, home) == ("this Hermes", laifu.root) else "http://127.0.0.1:9")
    laifu.member = _member(laifu, api_url=None)

    admit(laifu, "t1")
    admit(laifu, "t2")

    assert len(hermes.calls_to("POST", "/v1/runs")) == 2 and len(loads) == 1


def test_a_hermes_rooms_cant_run_on_is_503(laifu, hermes, monkeypatch):
    setup = importlib.import_module("conduit_push.room_setup")

    def unsupported(cls):
        raise setup.SetupError("unsupported", "This Hermes is missing what Linked Rooms setup needs", 501)

    monkeypatch.setattr(setup.Hermes, "load", classmethod(unsupported))
    laifu.member = _member(laifu, api_url=None)

    answer = ask(laifu, body=dispatch()[0], key=dispatch()[1])

    assert answer["status"] == 503 and answer["body"]["error"]["code"] == "member_unavailable"


def _second_copy():
    """The plugin loaded a second time, as `hermes serve` loads it for the gateway and again for the dashboard."""
    name = "conduit_push_second_load"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT)]
        sys.modules[name] = package
    return importlib.import_module(f"{name}.room_bridge_member"), importlib.import_module(f"{name}.room_links")


@needs_node
def test_two_loads_handle_each_request_once(tmp_path, monkeypatch, mailbox_url, hermes):
    monkeypatch.setenv("CONDUIT_ROOMS_MAILBOX_URL", mailbox_url)
    furina, laifu = links_module.Links(tmp_path / "furina"), links_module.Links(tmp_path / "laifu")
    link_id = laifu.accept(furina.invite("Furina")["url"], "Laifu")["link"]["id"]
    loops = [links_module.LinkLoop(side.root) for side in (furina, laifu)]
    for _ in range(3):
        for loop in loops:
            loop.poll_once()
    for loop in loops:
        loop.stop()
    member_module.bind(laifu.root, link_id, SCOPE)
    got = []
    monkeypatch.setitem(links_module.HANDLERS, bridge.RESPONSE, got.append)
    monkeypatch.setitem(links_module.HANDLERS, bridge.STATUS, got.append)
    second_member, second_links = _second_copy()
    members = [module.Member(laifu.root, api_url=hermes.url, executor=Inline())
               for module in (member_module, second_member)]
    monkeypatch.setitem(links_module.HANDLERS, bridge.REQUEST, members[0].handle)
    monkeypatch.setitem(second_links.HANDLERS, bridge.REQUEST, members[1].handle)
    first_loop, second_loop = links_module.LinkLoop(laifu.root), second_links.LinkLoop(laifu.root)
    monkeypatch.setitem(links_module._loops, str(laifu.root), first_loop)
    monkeypatch.setitem(second_links._loops, str(laifu.root), second_loop)
    body, key = dispatch()
    furina.send(link_id, bridge.REQUEST, {"v": 1, "profile": "default", "method": "POST", "path": "/v1/runs",
                                         "grant": GRANT, "key": key, "body": {"hosted_room_dispatch":
                                                                              body["hosted_room_dispatch"]}})
    try:
        for _ in range(2):
            first_loop.poll_once()
            second_loop.poll_once()
        run_id = next(iter(hermes.runs))
        hermes.run(run_id, status="completed", output="Yes!")
        for member in members:
            member.watch_pass()
        furina_loop = links_module.LinkLoop(furina.root)
        furina_loop.poll_once()
        furina_loop.stop()
    finally:
        first_loop.stop()
        second_loop.stop()

    assert len(hermes.calls_to("POST", "/v1/runs")) == 1
    assert [item.kind for item in got] == [bridge.RESPONSE, bridge.STATUS]
    assert got[1].body["status"] == "completed"
