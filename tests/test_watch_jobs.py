"""Watch jobs: a Conduit Watch call's Hermes jobs, run through Hermes' session API in this process."""

import base64
import contextlib
import importlib.util
import json
import pathlib
import sys
import threading
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELAY = "https://relay.example"
GRANT_ID = "J" * 22


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch_jobs", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class FakeHermes:
    """tui_gateway.server as the dashboard loads it: dispatch() and _sessions."""

    def __init__(self):
        self._sessions = {}
        self.calls = []
        self.fail = {}
        self.slow = set()
        self.resolved = 1
        self.on_create = None
        # Canned results by method: a dict, or a function of the params.
        self.results = {}
        self.lock = threading.Lock()

    def dispatch(self, req, transport=None):
        method, params = req["method"], req["params"]
        with self.lock:
            self.calls.append((method, dict(params)))
        if method in self.fail:
            return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": 4000, "message": self.fail[method]}}
        if method == "session.create":
            n = sum(1 for m, _ in self.calls if m == "session.create")
            sid = f"rt-{n}"
            self._sessions[sid] = {"transport": transport}
            if self.on_create:
                self.on_create()
            result = {"session_id": sid, "stored_session_id": f"st-{n}"}
        elif method == "session.close":
            result = {"closed": self._sessions.pop(params["session_id"], None) is not None}
        elif method == "approval.respond":
            result = {"resolved": self.resolved}
        elif method == "session.interrupt":
            result = {"status": "interrupted"}
        elif method in self.results:
            canned = self.results[method]
            result = canned(params) if callable(canned) else canned
        else:
            result = {"status": "started"}
        response = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        if method in self.slow:
            # A long handler: answered from Hermes' worker pool.
            threading.Timer(0.05, transport.write, args=(response,)).start()
            return None
        return response

    def methods(self, name):
        return [params for method, params in self.calls if method == name]

    def emit(self, sid, kind, payload=None):
        transport = self._sessions[sid]["transport"]
        params = {"type": kind, "session_id": sid}
        if payload is not None:
            params["payload"] = payload
        transport.write({"jsonrpc": "2.0", "method": "event", "params": params})

    def ask_approval(self, sid, request_id="appr-1", server_id="srq-1", command="rm -rf build"):
        self._sessions[sid]["transport"].write({
            "jsonrpc": "2.0", "id": server_id, "method": "approval",
            "params": {"session_id": sid, "request_id": request_id, "command": command,
                       "description": "Delete the build folder", "choices": ["once", "session", "always", "deny"]},
        })


class FakeDB:
    def __init__(self, meta):
        self.meta = meta

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_session(self, sid):
        return {"id": sid}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(api, "_watch_grants", api._WatchGrants())
    monkeypatch.setattr(api, "_watch_grant_limiter", api._MintLimiter(1000, 60.0))
    meta = {}
    monkeypatch.setattr(api, "_open_voice_db", lambda needs=None: FakeDB(meta))
    monkeypatch.setattr(api, "_voice_lock", lambda: threading.Lock())
    monkeypatch.setattr(api, "_watch_job_numbers", api.itertools.count(1))
    return meta


@pytest.fixture
def tags(fresh):
    return lambda: json.loads(fresh.get(api.VOICE_TAGS_KEY) or "{}")


def make_jobs(server=None, max_jobs=5, options=None, profile=None):
    grant = api._WatchGrant(grant_id=GRANT_ID, profile=profile, tools=api.WATCH_JOB_TOOLS + api.WATCH_JOB_CALLS,
                            secret=bytes(range(32)), relay_url=RELAY, credential="install-1.gateway-1.secret",
                            expires_at=time.monotonic() + 600, max_calls=api.WATCH_JOB_GRANT_MAX_CALLS)
    grant.jobs = api._WatchJobs(grant, max_jobs=max_jobs, options=options or {}, server=server or FakeHermes())
    return grant


def start(grant, instructions="Check the build logs"):
    return api.run_watch_job_call(grant, "start_job", {"instructions": instructions})


def news(grant, wait_s=0):
    return api.run_watch_job_call(grant, "job_news", {"wait_s": wait_s})


def write_pairing(tmp_path):
    path = tmp_path / "conduit-push.json"
    path.write_text(json.dumps({"credential": "install-1.gateway-1.secret", "installation_id": "install-1",
                                "gateway_id": "gateway-1", "relay_url": RELAY}), encoding="utf-8")
    return path


class FakeRelay:
    def __init__(self):
        self.requests = []

    def __call__(self, url, method, credential, payload, timeout):
        self.requests.append({"url": url, "method": method, "payload": payload})
        if method == "POST" and url.endswith("/v1/watch-tools/grants"):
            return 201, {"grant_id": GRANT_ID, "expires_at": "2026-10-07T12:00:00.000Z"}
        return 200, {"status": "delivered"}


# --- Granting jobs -----------------------------------------------------------

def test_a_grant_with_jobs_carries_them_with_the_users_cap_and_more_calls(tmp_path):
    relay, started, server = FakeRelay(), [], FakeHermes()
    grant = api.open_watch_grant(
        {"tools": ["web_search", "recall_memory", "start_job", "list_jobs", "cancel_job"], "max_jobs": 2,
         "job_options": {"model": "gpt-5.5", "provider": "openai", "reasoning_effort": "low", "cwd": "/"}},
        profile="coder", path=write_pairing(tmp_path), relay=relay, start=started.append,
        session_api=lambda: server)
    assert grant["tools"] == ["web_search", "recall_memory", "start_job", "list_jobs", "cancel_job",
                              "job_news", "answer_approval", "interrupt_job"]
    assert (grant["max_calls"], grant["max_jobs"]) == (120, 2)
    assert relay.requests[0]["payload"]["max_calls"] == 120
    [live] = started
    assert live.max_calls == 120
    assert (live.jobs.max_jobs, live.jobs.server) == (2, server)
    assert live.jobs.options == {"model": "gpt-5.5", "provider": "openai", "reasoning_effort": "low"}


def test_a_provider_goes_only_with_its_model():
    assert api._clean_job_options({"provider": "openai", "reasoning_effort": "high"}) == {"reasoning_effort": "high"}
    assert api._clean_job_options({"model": " ", "provider": "openai"}) == {}
    assert api._clean_job_options({"model": "x" * 121}) == {}
    assert api._clean_job_options("gpt") == {}


@pytest.mark.parametrize("body, api_available", [
    ({"tools": ["web_search", "start_job"], "max_jobs": 0}, True),
    ({"tools": ["web_search", "start_job"]}, False),
])
def test_without_jobs_allowed_or_runnable_the_grant_is_lookups_only(tmp_path, body, api_available):
    relay, started = FakeRelay(), []
    grant = api.open_watch_grant(body, profile=None, path=write_pairing(tmp_path), relay=relay,
                                 start=started.append, session_api=lambda: FakeHermes() if api_available else None)
    assert (grant["tools"], grant["max_calls"], grant["max_jobs"]) == (["web_search"], 60, 0)
    assert started[0].jobs is None


@pytest.mark.parametrize("value, expected", [(None, 5), (0, 0), (7, 7), (50, 20)])
def test_the_job_cap_defaults_to_five_and_is_capped_at_twenty(value, expected):
    assert api._watch_max_jobs(value) == expected


@pytest.mark.parametrize("value", [-1, "5", True, 2.5])
def test_a_job_cap_that_isnt_a_whole_number_is_refused(tmp_path, value):
    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant({"tools": ["start_job"], "max_jobs": value}, profile=None, path=write_pairing(tmp_path),
                             relay=FakeRelay(), start=lambda g: None, session_api=FakeHermes)
    assert err.value.status == 400


def test_only_a_session_api_the_dashboard_loaded_counts(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    assert api._hermes_session_api() is None
    module = types.ModuleType("tui_gateway.server")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", module)
    assert api._hermes_session_api() is None
    module.dispatch = lambda req, transport=None: None
    module._sessions = {}
    assert api._hermes_session_api() is module


# --- Starting ------------------------------------------------------------------

def test_a_job_is_an_ordinary_hermes_chat_on_the_grants_profile_filed_as_a_voice_job(tags, monkeypatch):
    scoped = []
    monkeypatch.setattr(api, "_profile_scope", lambda profile: scoped.append(profile) or contextlib.nullcontext())
    grant = make_jobs(options={"model": "gpt-5.5"}, profile="coder")
    server = grant.jobs.server
    answer = start(grant, "Quick: summarise   today's\nbuild failures")
    assert answer == {"ok": True, "status": "started", "job_id": "watch-1", "title": "summarise today's build failures",
                      "session_id": "st-1",
                      "message": "The job is running on Hermes. Its result will arrive later as a message; don't wait for it."}
    [create] = server.methods("session.create")
    assert create == {"cols": 96, "source": "desktop", "title": "summarise today's build failures", "model": "gpt-5.5",
                      "profile": "coder"}
    [submit] = server.methods("prompt.submit")
    assert submit["session_id"] == "rt-1"
    assert submit["text"].startswith("[Background job started from a Conduit voice conversation.")
    assert submit["text"].endswith("]\n\nsummarise   today's\nbuild failures")
    assert server._sessions["rt-1"]["transport"] is grant.jobs.transport
    assert tags() == {"st-1": {"kind": "job"}}
    assert scoped == ["coder"]


def test_a_job_title_mirrors_the_phones():
    assert api.watch_job_title("  a   b ") == "a b"
    long = "word " * 20
    assert api.watch_job_title(long) == ("word " * 12).strip() + "…"
    assert api.watch_job_title("x" * 70) == "x" * 60 + "…"


@pytest.mark.parametrize("args, detail", [
    ({}, "instructions is required"),
    ({"instructions": "quick:  "}, "instructions is required"),
    ({"instructions": "do it", "profile": "fam"}, "Jobs on another profile start from the iPhone"),
    ({"instructions": "x" * 3001}, "The task is too long for a Watch job"),
    # Bytes, as the sealed call counts them.
    ({"instructions": "結" * 1001}, "The task is too long for a Watch job"),
])
def test_a_job_the_watch_shouldnt_send_is_refused_without_ending_the_grant(args, detail):
    grant = make_jobs()
    answer = api.run_watch_job_call(grant, "start_job", args)
    assert answer["ok"] is False and answer["detail"] == detail
    # 403 and 410 tell the Watch its grant is over; these don't.
    assert answer["status"] not in (403, 410)
    assert grant.jobs.server.calls == []


def test_a_call_starts_no_more_jobs_than_the_user_allows():
    grant = make_jobs(max_jobs=2)
    server = grant.jobs.server
    start(grant)
    server.emit("rt-1", "message.complete", {"text": "done", "status": "complete"})
    start(grant)
    answer = start(grant)
    assert answer["status"] == "not_started"
    assert answer["message"].startswith("This call has started 2 jobs, the most the user allows per call.")
    assert len(server.methods("session.create")) == 2


def test_a_renewed_grants_jobs_never_reuse_the_last_grants_ids():
    first, second = make_jobs(), make_jobs()
    assert start(first)["job_id"] == "watch-1"
    assert start(second)["job_id"] == "watch-2"
    assert api.run_watch_job_call(second, "cancel_job", {"job_id": "watch-1"}) == {
        "ok": True, "message": "There are no background jobs to cancel."}
    assert first.jobs.jobs["watch-1"].status != "cancelled"


class RenewingRelay(FakeRelay):
    """Gives each grant its own id, as the relay does."""

    def __init__(self):
        super().__init__()
        self.ids = iter(c * 22 for c in "ABCDEFGH")

    def __call__(self, url, method, credential, payload, timeout):
        if method == "POST" and url.endswith("/v1/watch-tools/grants"):
            self.requests.append({"url": url, "method": method, "payload": payload})
            return 201, {"grant_id": next(self.ids), "expires_at": "2026-10-07T12:00:00.000Z"}
        return super().__call__(url, method, credential, payload, timeout)


def open_jobs_grant(tmp_path, server, relay, profile="coder", started=None, **extra):
    started = [] if started is None else started
    answer = api.open_watch_grant({"tools": ["web_search", "start_job"], "max_jobs": 3, **extra}, profile=profile,
                                  path=write_pairing(tmp_path), relay=relay, start=started.append,
                                  session_api=lambda: server)
    return answer, api._watch_grants.get(answer["grant_id"])


def test_a_renewal_carries_the_last_grants_jobs_and_their_news(tmp_path):
    server, relay = FakeHermes(), RenewingRelay()
    first, old = open_jobs_grant(tmp_path, server, relay)
    assert "jobs_carried_from" not in first
    assert start(old)["job_id"] == "watch-1"
    server.ask_approval("rt-1")
    assert news(old)["news"][0]["status"] == "needs_approval"
    renewed, new = open_jobs_grant(tmp_path, server, relay, carry_jobs_from=old.grant_id, max_jobs=4)
    assert renewed["jobs_carried_from"] == old.grant_id
    assert new.jobs is old.jobs and new.jobs.grant is new
    assert new.jobs.max_jobs == 4
    # The approval already told isn't told again; the new grant answers it,
    # lists the job, and hears it finish.
    assert news(new)["news"] == []
    assert api.run_watch_job_call(new, "answer_approval", {"job_id": "watch-1", "request_id": "appr-1",
                                                            "choice": "once"})["status"] == "approved"
    assert "watch-1" in api.run_watch_job_call(new, "list_jobs", {})["job_1"]
    # The old grant closing (the Watch lets it go) leaves the jobs running.
    assert api._close_watch_grant_here(old)
    assert not new.jobs.ended
    server.emit("rt-1", "message.complete", {"text": "All green", "status": "complete"})
    [item] = news(new)["news"]
    assert (item["job_id"], item["status"], item["result"]) == ("watch-1", "finished", "All green")
    # The cap counts across the call: one started, three more.
    for _ in range(2):
        start(new)
    assert start(new)["status"] == "started"
    assert start(new)["message"].startswith("This call has started 4 jobs")
    assert api._close_watch_grant_here(new)
    assert new.jobs.ended


@pytest.mark.parametrize("case", ["other profile", "closed", "unknown", "already carried", "no jobs"])
def test_only_an_open_grant_of_the_same_profile_with_its_own_jobs_is_carried(tmp_path, case):
    server, relay = FakeHermes(), RenewingRelay()
    _, old = open_jobs_grant(tmp_path, server, relay)
    start(old)
    carry = old.grant_id
    profile = "coder"
    if case == "other profile":
        profile = "writer"
    elif case == "closed":
        api._close_watch_grant_here(old)
    elif case == "unknown":
        carry = "Z" * 22
    elif case == "already carried":
        open_jobs_grant(tmp_path, server, relay, carry_jobs_from=old.grant_id)
    elif case == "no jobs":
        old.jobs = None
    renewed, new = open_jobs_grant(tmp_path, server, relay, profile=profile, carry_jobs_from=carry)
    assert "jobs_carried_from" not in renewed
    assert new.jobs is not None and new.jobs.jobs == {}
    if case == "other profile":
        assert old.jobs.grant is old


def test_a_renewal_without_a_cap_or_options_keeps_the_calls(tmp_path):
    server, relay = FakeHermes(), RenewingRelay()
    _, old = open_jobs_grant(tmp_path, server, relay, job_options={"reasoning_effort": "low"})
    renewed = api.open_watch_grant({"tools": ["start_job"], "carry_jobs_from": old.grant_id}, profile="coder",
                                   path=write_pairing(tmp_path), relay=relay, start=lambda g: None,
                                   session_api=lambda: server)
    assert (renewed["jobs_carried_from"], renewed["max_jobs"]) == (old.grant_id, 3)
    assert (old.jobs.max_jobs, old.jobs.options) == (3, {"reasoning_effort": "low"})


@pytest.mark.parametrize("value", [7, "", "not a grant id", ["A" * 22]])
def test_carry_jobs_from_must_be_a_grant_id(tmp_path, value):
    with pytest.raises(api.TokenError) as err:
        open_jobs_grant(tmp_path, FakeHermes(), RenewingRelay(), carry_jobs_from=value)
    assert err.value.status == 400


def test_a_renewal_that_cant_start_answering_gives_the_jobs_back(tmp_path):
    server, relay = FakeHermes(), RenewingRelay()
    _, old = open_jobs_grant(tmp_path, server, relay)
    start(old)
    jobs = old.jobs

    def broken(grant):
        raise RuntimeError("no thread to spare")

    with pytest.raises(api.TokenError):
        api.open_watch_grant({"tools": ["start_job"], "carry_jobs_from": old.grant_id}, profile="coder",
                             path=write_pairing(tmp_path), relay=relay, start=broken, session_api=lambda: server)
    assert jobs.grant is old and not jobs.ended
    assert news(old)["running"] == 1


def test_three_jobs_run_at_once():
    grant = make_jobs(max_jobs=10)
    for _ in range(3):
        assert start(grant)["status"] == "started"
    answer = start(grant)
    assert answer == {"ok": True, "status": "not_started",
                      "message": "You already have 3 background jobs running. Cancel them before starting another."}
    # The refused start didn't spend one of the call's jobs.
    assert grant.jobs.started == 3


def test_a_grant_carries_only_the_job_tools_asked_for(tmp_path):
    grant = api.open_watch_grant({"tools": ["list_jobs"]}, profile=None, path=write_pairing(tmp_path),
                                 relay=FakeRelay(), start=lambda g: None, session_api=FakeHermes)
    # Corrections come with the tools that start or stop jobs, not with a list.
    assert grant["tools"] == ["list_jobs", "job_news", "answer_approval"]


def test_a_job_hermes_refuses_to_start_is_reported_and_its_session_closed():
    grant = make_jobs()
    server = grant.jobs.server
    server.fail["prompt.submit"] = "model not configured"
    answer = start(grant)
    assert answer == {"ok": True, "status": "not_started", "title": "Check the build logs",
                      "message": "Hermes couldn't start the job: model not configured"}
    assert server.methods("session.close") == [{"session_id": "rt-1"}]
    # Told in the answer: no news later.
    assert news(grant)["news"] == []
    # Nothing ran, so it didn't spend one of the call's jobs.
    assert grant.jobs.started == 0


def test_a_job_cancelled_while_it_starts_never_gets_its_prompt():
    grant = make_jobs()
    server = grant.jobs.server
    server.on_create = lambda: api.run_watch_job_call(grant, "cancel_job", {})
    answer = start(grant)
    assert answer["status"] == "not_started"
    assert answer["message"] == "Check the build logs was cancelled before it started."
    assert server.methods("prompt.submit") == []
    assert server.methods("session.close") == [{"session_id": "rt-1"}]


def test_a_slow_hermes_method_is_answered_through_the_transport():
    grant = make_jobs()
    grant.jobs.server.slow.add("session.create")
    assert start(grant)["status"] == "started"


def test_a_method_hermes_never_answers_times_out(monkeypatch):
    grant = make_jobs()
    server = grant.jobs.server
    server.dispatch = lambda req, transport=None: None
    with pytest.raises(api.WatchJobError, match="session.create timed out"):
        grant.jobs.rpc("session.create", {}, timeout=0.05)
    assert grant.jobs._waiters == {}


# --- News ----------------------------------------------------------------------

def test_a_finished_jobs_result_is_news_once():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.emit("rt-1", "message.complete", {"text": "  All green.  ", "status": "complete"})
    assert news(grant) == {"ok": True, "news": [{"job_id": "watch-1", "title": "Check the build logs",
                                                 "status": "finished", "session_id": "st-1", "result": "All green."}],
                           "running": 0, "more": False, "approvals": []}
    assert news(grant)["news"] == []


def test_job_news_waits_for_a_job_to_settle():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    threading.Timer(0.2, server.emit, args=("rt-1", "message.complete", {"text": "Done", "status": "complete"})).start()
    began = time.monotonic()
    answer = news(grant, wait_s=10)
    assert [item["result"] for item in answer["news"]] == ["Done"]
    assert 0.1 < time.monotonic() - began < 5


def test_job_news_returns_at_once_when_nothing_runs_and_at_its_wait_otherwise():
    grant = make_jobs()
    began = time.monotonic()
    assert news(grant, wait_s=10) == {"ok": True, "news": [], "running": 0, "more": False, "approvals": []}
    start(grant)
    assert news(grant, wait_s=0) == {"ok": True, "news": [], "running": 1, "more": False, "approvals": []}
    assert time.monotonic() - began < 1


def test_a_failed_or_interrupted_turn_settles_the_job():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    start(grant, "Second")
    server.emit("rt-1", "message.complete", {"text": "", "status": "error", "error": "provider overloaded"})
    server.emit("rt-2", "message.complete", {"text": "partial", "status": "interrupted"})
    items = {item["job_id"]: item for item in news(grant)["news"]}
    assert (items["watch-1"]["status"], items["watch-1"]["error"]) == ("failed", "provider overloaded")
    assert items["watch-2"]["status"] == "cancelled" and "result" not in items["watch-2"]


def test_a_session_error_fails_its_job():
    grant = make_jobs()
    start(grant)
    grant.jobs.server.emit("rt-1", "error", {"message": "agent init failed"})
    [item] = news(grant)["news"]
    assert (item["status"], item["error"]) == ("failed", "agent init failed")


def test_events_of_other_sessions_are_ignored():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server._sessions["rt-9"] = {"transport": grant.jobs.transport}
    server.emit("rt-9", "message.complete", {"text": "not ours", "status": "complete"})
    grant.jobs.transport.write("garbage")
    grant.jobs.transport.write({"method": "event", "params": "garbage"})
    assert news(grant)["news"] == []


def test_more_news_than_one_answer_holds_waits_for_the_next_call():
    grant = make_jobs()
    server = grant.jobs.server
    for i in range(3):
        start(grant, f"Job {i}")
        server.emit(f"rt-{i + 1}", "message.complete", {"text": "結果" * 3_000, "status": "complete"})
    first = news(grant)
    assert first["more"] is True and 1 <= len(first["news"]) < 3
    second = news(grant)
    told = first["news"] + second["news"] + (news(grant)["news"] if second["more"] else [])
    assert sorted(item["job_id"] for item in told) == ["watch-1", "watch-2", "watch-3"]
    # Each result was cut to fit, and each answer fits the relay's bound sealed.
    for item in told:
        assert item["result"].endswith("\n[…]")
        assert len(item["result"].encode()) <= api.WATCH_JOB_RESULT_BYTES
    for answer in (first, second):
        sealed = api.seal_watch_tool(grant.result_key, "result", GRANT_ID, b64u(bytes(16)), answer)
        assert len(sealed["ct"]) <= api.WATCH_MAX_RESULT_CT_CHARS


def test_any_mix_of_long_results_and_approvals_fits_the_relays_bound_sealed():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant, "Job 1")
    # Quotes and control characters take two to six bytes each as JSON.
    server.emit("rt-1", "message.complete", {"text": '"\x01' * 3_000, "status": "complete"})
    for n in (2, 3, 4):
        start(grant, f"Job {n}")
        server._sessions[f"rt-{n}"]["transport"].write({
            "jsonrpc": "2.0", "id": f"srq-{n}", "method": "approval",
            "params": {"session_id": f"rt-{n}", "request_id": "\x02" * 200, "command": "\x03" * 3_000,
                       "description": "\x04" * 900, "choices": ["once", "deny"]},
        })
    told, answers = [], 0
    while True:
        answer = news(grant)
        answers += 1
        sealed = api.seal_watch_tool(grant.result_key, "result", GRANT_ID, b64u(bytes(16)), answer)
        assert len(sealed["ct"]) <= api.WATCH_MAX_RESULT_CT_CHARS
        told += answer["news"]
        if not answer["more"]:
            break
        assert answers < 5
    assert sorted(item["job_id"] for item in told) == ["watch-1", "watch-2", "watch-3", "watch-4"]
    assert all(item["status"] == "needs_approval" for item in told if item["job_id"] != "watch-1")
    assert all("approval" in item for item in told if item["job_id"] != "watch-1")
    assert next(item for item in told if item["job_id"] == "watch-1")["result"].endswith("\n[…]")


def test_more_counts_approval_requests_not_yet_told(monkeypatch):
    grant = make_jobs()
    server = grant.jobs.server
    for n in (1, 2):
        start(grant, f"Job {n}")
        server.ask_approval(f"rt-{n}", request_id=f"appr-{n}", server_id=f"srq-{n}")
    monkeypatch.setattr(api, "WATCH_JOB_ANSWER_BYTES", 450)
    first = news(grant)
    assert (len(first["news"]), first["more"]) == (1, True)
    second = news(grant)
    assert (len(second["news"]), second["more"]) == (1, False)


def test_an_item_too_large_for_any_answer_still_tells_its_status(monkeypatch):
    grant = make_jobs()
    start(grant)
    grant.jobs.server.emit("rt-1", "message.complete", {"text": "done " * 100, "status": "complete"})
    monkeypatch.setattr(api, "WATCH_JOB_ANSWER_BYTES", 200)
    answer = news(grant)
    assert answer["news"] == [{"job_id": "watch-1", "title": "Check the build logs", "status": "finished"}]
    assert answer["more"] is False


def test_an_approval_request_without_ids_is_still_told_once():
    grant = make_jobs()
    start(grant)
    grant.jobs.server.ask_approval("rt-1", request_id="", server_id="")
    [item] = news(grant)["news"]
    assert item["status"] == "needs_approval" and item["approval"]["request_id"] == ""
    assert news(grant)["news"] == []


@pytest.mark.parametrize("wait_s, expected", [(True, 15.0), (2.5, 2.5), (10 ** 400, 15.0), (-1, 15.0), ("3", 15.0)])
def test_job_news_reads_its_wait_as_a_number_of_seconds(monkeypatch, wait_s, expected):
    grant = make_jobs()
    waits = []
    monkeypatch.setattr(api.time, "monotonic", lambda: 0.0)

    class Changed:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            grant.jobs.ended = True

    start(grant)
    monkeypatch.setattr(grant.jobs, "changed", Changed())
    grant.jobs.news({"wait_s": wait_s})
    assert waits == [min(expected, 1.0)]


def test_a_long_ascii_result_is_cut_at_the_phones_length():
    text = "a" * 7_000
    assert api._clip_job_result(text) == "a" * 6_000 + "\n[…]"
    assert api._clip_job_result("short") == "short"
    # Cut by length and by size, it's marked once.
    assert api._clip_job_result("😀" * 7_000).count("[…]") == 1


@pytest.mark.parametrize("limit", range(2, 14))
def test_a_clip_never_takes_more_than_its_limit(limit):
    clipped = api._json_clip("abcdefghijklmnop", limit)
    assert api._json_bytes(clipped) <= limit
    assert clipped.endswith("\n[…]") or limit < 9


def test_a_start_that_breaks_unexpectedly_says_why(monkeypatch):
    grant = make_jobs()

    def broken(job, instructions):
        raise RuntimeError("boom")

    monkeypatch.setattr(grant.jobs, "_start_session", broken)
    answer = start(grant)
    assert (answer["status"], answer["message"]) == ("not_started", "Hermes couldn't start the job: RuntimeError")


# --- Approvals -------------------------------------------------------------------

def test_an_approval_request_is_news_once_and_approving_it_answers_hermes():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.ask_approval("rt-1")
    [item] = news(grant)["news"]
    assert item["status"] == "needs_approval"
    assert item["approval"] == {"request_id": "appr-1", "command": "rm -rf build",
                                "description": "Delete the build folder"}
    # Told once, but listed as open on every answer until it settles.
    again = news(grant)
    assert again["news"] == [] and again["approvals"] == [{"job_id": "watch-1", "request_id": "appr-1"}]
    answer = api.run_watch_job_call(grant, "answer_approval",
                                    {"job_id": "watch-1", "request_id": "appr-1", "choice": "once"})
    assert answer == {"ok": True, "status": "approved", "job_id": "watch-1"}
    assert news(grant)["approvals"] == []
    assert server.methods("approval.respond") == [{"session_id": "rt-1", "choice": "once", "request_id": "appr-1"}]
    assert grant.jobs.jobs["watch-1"].status == "running"
    # A second request is news again.
    server.ask_approval("rt-1", request_id="appr-2", server_id="srq-2")
    assert news(grant)["news"][0]["approval"]["request_id"] == "appr-2"


@pytest.mark.parametrize("choice", ["session", "always", "", "yes"])
def test_the_watch_can_only_approve_once_or_deny(choice):
    grant = make_jobs()
    start(grant)
    grant.jobs.server.ask_approval("rt-1")
    answer = api.run_watch_job_call(grant, "answer_approval",
                                    {"job_id": "watch-1", "request_id": "appr-1", "choice": choice})
    assert (answer["ok"], answer["status"]) == (False, 400)
    assert grant.jobs.server.methods("approval.respond") == []


def test_an_answer_for_a_replaced_or_settled_request_isnt_sent():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.ask_approval("rt-1", request_id="appr-2")
    for args in ({"job_id": "watch-1", "request_id": "appr-1", "choice": "once"},
                 {"job_id": "watch-1", "choice": "once"},
                 {"job_id": "watch-7", "request_id": "appr-2", "choice": "once"}):
        assert api.run_watch_job_call(grant, "answer_approval", args)["status"] == "not_pending"
    assert server.methods("approval.respond") == []


def test_an_approval_answered_elsewhere_or_timed_out_is_withdrawn():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    start(grant, "Second")
    server.ask_approval("rt-1", server_id="srq-1")
    server.ask_approval("rt-2", request_id="appr-9", server_id="srq-9")
    server.emit("rt-1", "request.cancel", {"id": "srq-other", "method": "approval", "reason": "resolved"})
    assert grant.jobs.jobs["watch-1"].status == "needs_approval"
    server.emit("rt-1", "request.cancel", {"id": "srq-1", "method": "approval", "reason": "timeout"})
    server.emit("rt-2", "approval.cancelled", {"session_id": "rt-2", "stored_session_id": "st-2", "reason": "interrupt",
                                               "cancelled_count": 1, "request_ids": ["appr-9"]})
    assert [job.status for job in grant.jobs.jobs.values()] == ["running", "running"]
    assert news(grant)["news"] == []


@pytest.mark.parametrize("resolved", [0, None, "1"])
def test_hermes_resolving_nothing_reads_as_not_pending(resolved):
    grant = make_jobs()
    server = grant.jobs.server
    server.resolved = resolved
    start(grant)
    server.ask_approval("rt-1")
    answer = api.run_watch_job_call(grant, "answer_approval",
                                    {"job_id": "watch-1", "request_id": "appr-1", "choice": "deny"})
    assert answer["status"] == "not_pending"


# --- Listing and cancelling -----------------------------------------------------

def test_list_jobs_mirrors_the_phones_answer():
    grant = make_jobs()
    server = grant.jobs.server
    assert api.run_watch_job_call(grant, "list_jobs", {}) == {"ok": True, "summary": "No background jobs are running."}
    start(grant)
    start(grant, "Second")
    server.emit("rt-2", "message.complete", {"text": "ok", "status": "complete"})
    assert api.run_watch_job_call(grant, "list_jobs", {}) == {
        "ok": True,
        "summary": "Check the build logs is still running. Second has finished.",
        "job_1": "id=watch-1; title=Check the build logs; status=running",
        "job_2": "id=watch-2; title=Second; status=finished",
    }
    news(grant)
    assert "job_2" not in api.run_watch_job_call(grant, "list_jobs", {})


def test_cancel_interrupts_the_running_jobs_and_says_so_once():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    start(grant, "Second")
    answer = api.run_watch_job_call(grant, "cancel_job", {})
    assert answer == {"ok": True, "message": "Cancelled 2 background jobs."}
    assert server.methods("session.interrupt") == [{"session_id": "rt-1"}, {"session_id": "rt-2"}]
    server.emit("rt-1", "message.complete", {"text": "", "status": "interrupted"})
    assert news(grant)["news"] == []
    assert api.run_watch_job_call(grant, "cancel_job", {}) == {"ok": True,
                                                               "message": "There are no background jobs to cancel."}


def test_cancelling_one_job_by_id():
    grant = make_jobs()
    start(grant)
    start(grant, "Second")
    assert api.run_watch_job_call(grant, "cancel_job", {"job_id": "watch-2"}) == {"ok": True,
                                                                                 "message": "Second was cancelled."}
    assert [job.status for job in grant.jobs.jobs.values()] == ["running", "cancelled"]


def test_a_cancel_hermes_refuses_leaves_the_job_followed():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.ask_approval("rt-1")
    server.fail["session.interrupt"] = "no such session"
    answer = api.run_watch_job_call(grant, "cancel_job", {"job_id": "watch-1"})
    assert answer == {"ok": True, "message": "Couldn't cancel Check the build logs. It may still be running."}
    job = grant.jobs.jobs["watch-1"]
    assert (job.status, job.approval["request_id"], job.outcome_told) == ("needs_approval", "appr-1", False)


def test_a_long_result_is_kept_only_as_long_as_news_can_tell_it():
    grant = make_jobs()
    start(grant)
    grant.jobs.server.emit("rt-1", "message.complete", {"text": "a" * 50_000, "status": "complete"})
    assert len(grant.jobs.jobs["watch-1"].result) == api.WATCH_JOB_RESULT_CHARS + 1
    [item] = news(grant)["news"]
    assert item["result"] == "a" * api.WATCH_JOB_RESULT_CHARS + "\n[…]"


def test_a_turn_that_ends_while_its_cancel_is_refused_is_told():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.fail["session.interrupt"] = "no such session"
    dispatch = server.dispatch

    def ending(req, transport=None):
        if req["method"] == "session.interrupt":
            server.emit("rt-1", "message.complete", {"text": "All green", "status": "complete"})
        return dispatch(req, transport)

    server.dispatch = ending
    answer = api.run_watch_job_call(grant, "cancel_job", {"job_id": "watch-1"})
    assert answer == {"ok": True, "message": "Couldn't cancel Check the build logs. It may still be running."}
    [item] = news(grant)["news"]
    assert (item["job_id"], item["status"], item["result"]) == ("watch-1", "finished", "All green")
    job = grant.jobs.jobs["watch-1"]
    assert (job.cancelling, job.held_end) == (False, None)


def test_a_cancelled_turns_own_end_reads_as_the_cancel():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    dispatch = server.dispatch

    def ending(req, transport=None):
        if req["method"] == "session.interrupt":
            server.emit("rt-1", "message.complete", {"text": "", "status": "interrupted"})
        return dispatch(req, transport)

    server.dispatch = ending
    assert api.run_watch_job_call(grant, "cancel_job", {})["message"] == "Cancelled 1 background jobs."
    job = grant.jobs.jobs["watch-1"]
    assert (job.status, job.cancelling, job.held_end) == ("cancelled", False, None)
    assert news(grant)["news"] == []


# --- Ending ----------------------------------------------------------------------

def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_when_the_grant_ends_settled_jobs_close_now_and_running_ones_once_they_finish():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    start(grant, "Second")
    server.emit("rt-1", "message.complete", {"text": "done", "status": "complete"})
    assert api._close_watch_grant_here(grant)
    wait_for(lambda: server.methods("session.close") == [{"session_id": "rt-1"}])
    assert not grant.jobs.transport._closed
    # The running job keeps running, and can't be told to the Watch any more.
    assert api.run_watch_job_call(grant, "start_job", {"instructions": "more"})["status"] == 410
    server.emit("rt-2", "message.complete", {"text": "late", "status": "complete"})
    wait_for(lambda: {"session_id": "rt-2"} in server.methods("session.close"))
    wait_for(lambda: grant.jobs.transport._closed)


def test_a_session_the_app_joined_is_left_to_the_app():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server._sessions["rt-1"]["transport"] = object()  # Conduit opened the chat: a fan-out now
    grant.jobs.transport.write({"jsonrpc": "2.0", "method": "event", "params": {
        "type": "message.complete", "session_id": "rt-1", "payload": {"text": "done", "status": "complete"}}})
    grant.jobs.end()
    wait_for(lambda: grant.jobs.transport._closed)
    assert server.methods("session.close") == []


def test_job_news_stops_waiting_when_the_grant_ends():
    grant = make_jobs()
    start(grant)
    threading.Timer(0.2, api._close_watch_grant_here, args=(grant,)).start()
    began = time.monotonic()
    assert news(grant, wait_s=10)["news"] == []
    assert time.monotonic() - began < 5


def test_job_news_through_a_carried_grant_stops_waiting_when_that_grant_ends(tmp_path):
    server, relay = FakeHermes(), RenewingRelay()
    _, old = open_jobs_grant(tmp_path, server, relay)
    start(old)
    _, new = open_jobs_grant(tmp_path, server, relay, carry_jobs_from=old.grant_id)
    assert new.jobs is old.jobs
    threading.Timer(0.2, api._close_watch_grant_here, args=(old,)).start()
    began = time.monotonic()
    assert news(old, wait_s=10)["news"] == []
    assert time.monotonic() - began < 5
    assert not new.jobs.ended


# --- Through the relay --------------------------------------------------------------

def test_a_sealed_start_job_runs_and_its_answer_goes_back_sealed():
    grant = make_jobs()
    relay = FakeRelay()
    rid = b64u(bytes(16))
    call = {"rid": rid, **api.seal_watch_tool(grant.call_key, "call", GRANT_ID, rid,
                                               {"tool": "start_job", "args": {"instructions": "Check the build logs"}})}
    assert api.answer_watch_call(grant, call, relay=relay) == "answered"
    [post] = relay.requests
    answer = api.open_watch_tool(grant.result_key, "result", GRANT_ID, rid, post["payload"], max_bytes=64 * 1024)
    assert (answer["status"], answer["job_id"]) == ("started", "watch-1")


def sealed(grant, tool, args, n):
    rid = b64u(bytes([n]) * 16)
    return rid, {"rid": rid, **api.seal_watch_tool(grant.call_key, "call", GRANT_ID, rid, {"tool": tool, "args": args})}


@pytest.mark.parametrize("delivery", ["refused", "raised"])
def test_job_news_that_never_reaches_the_watch_is_sent_again(delivery):
    grant = make_jobs()
    start(grant)
    grant.jobs.server.emit("rt-1", "message.complete", {"text": "All green.", "status": "complete"})

    def lost(url, method, credential, payload, timeout):
        if delivery == "raised":
            raise OSError("relay unreachable")
        return 404, {"error": "unknown_call"}

    _, call = sealed(grant, "job_news", {"wait_s": 0}, 1)
    assert api.answer_watch_call(grant, call, relay=lost) == "gone"
    relay = FakeRelay()
    rid, call = sealed(grant, "job_news", {"wait_s": 0}, 2)
    assert api.answer_watch_call(grant, call, relay=relay) == "answered"
    answer = api.open_watch_tool(grant.result_key, "result", GRANT_ID, rid, relay.requests[0]["payload"],
                                 max_bytes=64 * 1024)
    assert [(item["job_id"], item["result"]) for item in answer["news"]] == [("watch-1", "All green.")]
    # Delivered once, it isn't sent again.
    assert news(grant)["news"] == []


def test_a_start_hermes_is_slow_to_take_is_answered_accepted_and_followed(monkeypatch):
    monkeypatch.setattr(api, "WATCH_JOB_START_ANSWER_S", 0.05)
    grant = make_jobs()
    server = grant.jobs.server
    release = threading.Event()
    server.on_create = lambda: release.wait(5)
    answer = start(grant)
    assert answer == {"ok": True, "status": "accepted", "job_id": "watch-1", "title": "Check the build logs",
                      "message": "Hermes is starting the job. Its result will arrive later as a message; don't "
                                 "wait for it."}
    release.set()
    deadline = time.monotonic() + 5
    while grant.jobs.jobs["watch-1"].status == "starting" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert grant.jobs.jobs["watch-1"].status == "running"
    assert news(grant)["news"] == []
    server.emit("rt-1", "message.complete", {"text": "All green.", "status": "complete"})
    assert [item["status"] for item in news(grant)["news"]] == ["finished"]


def test_a_slow_start_that_fails_is_told_as_news(monkeypatch):
    monkeypatch.setattr(api, "WATCH_JOB_START_ANSWER_S", 0.05)
    grant = make_jobs()
    server = grant.jobs.server
    release = threading.Event()
    server.on_create = lambda: release.wait(5)
    server.fail["prompt.submit"] = "model not configured"
    assert start(grant)["status"] == "accepted"
    release.set()
    [item] = news(grant, wait_s=5)["news"]
    assert (item["job_id"], item["status"], item["error"]) == ("watch-1", "failed", "model not configured")
    assert grant.jobs.started == 0


def test_a_failed_start_whose_answer_is_lost_is_told_as_news():
    grant = make_jobs()
    grant.jobs.server.fail["session.create"] = "no provider"
    _, call = sealed(grant, "start_job", {"instructions": "Check the build logs"}, 3)
    assert api.answer_watch_call(grant, call, relay=lambda *a: (404, {})) == "gone"
    [item] = news(grant)["news"]
    assert (item["status"], item["error"]) == ("failed", "no provider")


def test_a_start_cancelled_first_whose_answer_is_lost_is_told_as_news():
    grant = make_jobs()
    grant.jobs.server.on_create = lambda: api.run_watch_job_call(grant, "cancel_job", {})
    _, call = sealed(grant, "start_job", {"instructions": "Check the build logs"}, 5)
    assert api.answer_watch_call(grant, call, relay=lambda *a: (404, {})) == "gone"
    [item] = news(grant)["news"]
    assert (item["job_id"], item["status"]) == ("watch-1", "cancelled")
    assert grant.jobs.started == 0


def test_a_cancel_whose_answer_is_lost_is_told_as_news():
    grant = make_jobs()
    start(grant)
    _, call = sealed(grant, "cancel_job", {"job_id": "watch-1"}, 4)
    assert api.answer_watch_call(grant, call, relay=lambda *a: (404, {})) == "gone"
    [item] = news(grant)["news"]
    assert (item["job_id"], item["status"]) == ("watch-1", "cancelled")
    assert news(grant)["news"] == []


def test_a_start_with_no_thread_to_spare_never_was(monkeypatch):
    grant = make_jobs()

    class NoThreads:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(api.threading, "Thread", NoThreads)
    answer = start(grant)
    assert (answer["status"], answer["message"]) == ("not_started", "Hermes couldn't start the job: the host is "
                                                                      "too busy right now.")
    assert (grant.jobs.jobs, grant.jobs.started) == ({}, 0)
    assert news(grant)["running"] == 0


def test_a_new_approval_under_the_same_id_is_news_again():
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.ask_approval("rt-1", request_id="", server_id="srq-1")
    assert [item["status"] for item in news(grant)["news"]] == ["needs_approval"]
    server.emit("rt-1", "request.cancel", {"id": "srq-1"})
    assert grant.jobs.jobs["watch-1"].status == "running"
    server.ask_approval("rt-1", request_id="", server_id="srq-1")
    assert [item["status"] for item in news(grant)["news"]] == ["needs_approval"]


@pytest.mark.parametrize("server_id", [5, 0])
def test_a_numeric_server_request_id_is_withdrawn_too(server_id):
    grant = make_jobs()
    server = grant.jobs.server
    start(grant)
    server.ask_approval("rt-1", request_id="appr-1", server_id=server_id)
    assert grant.jobs.jobs["watch-1"].status == "needs_approval"
    server.emit("rt-1", "request.cancel", {"id": server_id + 1})
    assert grant.jobs.jobs["watch-1"].status == "needs_approval"
    server.emit("rt-1", "request.cancel", {"id": server_id})
    assert grant.jobs.jobs["watch-1"].status == "running"


def test_a_grant_without_jobs_refuses_job_calls():
    grant = make_jobs()
    grant.jobs = None
    assert api.run_watch_job_call(grant, "job_news", {})["status"] == 403


def test_a_failure_in_a_job_call_is_answered_by_type(monkeypatch):
    grant = make_jobs()
    monkeypatch.setattr(grant.jobs, "list", lambda: 1 / 0)
    assert api.run_watch_job_call(grant, "list_jobs", {}) == {
        "ok": False, "status": 500, "detail": "The job call failed on the host (ZeroDivisionError)"}


def test_capabilities_name_watch_jobs():
    assert "watch-jobs" in api.ROUTE_CAPABILITIES
    assert "watch-job-follow-ups" in api.ROUTE_CAPABILITIES


# --- Follow-ups (interrupt_job) ------------------------------------------------

@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_RETRY_S", 0)


def follow_up(grant, words="make it Alex", job_id="watch-1"):
    return api.run_watch_job_call(grant, "interrupt_job", {"job_id": job_id, "message": words})


def running_job(server=None):
    grant = make_jobs(server)
    assert start(grant)["status"] == "started"
    return grant, grant.jobs.server


def test_a_follow_up_goes_into_the_running_turn_and_its_end_is_the_jobs_result():
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "redirected"}
    assert follow_up(grant) == {"ok": True, "title": "Check the build logs", "outcome": "interrupted"}
    assert server.methods("session.redirect") == [{"session_id": "rt-1", "text": "make it Alex"}]
    # The cut-off reply's marker is no result.
    server.emit("rt-1", "message.complete", {"text": "[This response was interrupted by a user correction.]"})
    server.emit("rt-1", "message.delta", {"text": "Alex"})
    assert grant.jobs.jobs["watch-1"].status == "running"
    server.emit("rt-1", "message.complete", {"text": "Done for Alex."})
    [item] = news(grant)["news"]
    assert (item["status"], item["result"]) == ("finished", "Done for Alex.")


def test_an_end_that_comes_while_the_words_go_in_waits_for_hermes_answer():
    grant, server = running_job()

    def queued(params):
        server.emit("rt-1", "message.complete", {"text": "Step one done."})
        return {"status": "queued"}

    server.results["session.redirect"] = queued
    assert follow_up(grant)["outcome"] == "queued"
    # That end was the step Hermes was finishing: the words' turn ends the job.
    assert grant.jobs.jobs["watch-1"].status == "running"
    server.emit("rt-1", "message.start")
    server.emit("rt-1", "message.complete", {"text": "Done for Alex."})
    [item] = news(grant)["news"]
    assert item["result"] == "Done for Alex."


def test_words_hermes_runs_next_keep_the_current_turns_end_from_ending_the_job():
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "queued"}
    assert follow_up(grant)["outcome"] == "queued"
    server.emit("rt-1", "message.complete", {"text": "Step one done."})
    assert grant.jobs.jobs["watch-1"].status == "running"
    server.emit("rt-1", "message.complete", {"text": "Done for Alex."})
    assert news(grant)["news"][0]["result"] == "Done for Alex."


def test_an_end_held_for_words_hermes_didnt_take_is_the_result_after_all(no_retry_wait):
    grant, server = running_job()

    def not_running(params):
        server.emit("rt-1", "message.complete", {"text": "All done."})
        return {"status": "idle"}

    server.results["session.redirect"] = not_running
    assert follow_up(grant) == {"ok": True, "title": "Check the build logs", "outcome": "finished"}
    assert news(grant)["news"][0]["result"] == "All done."


def test_words_for_a_job_hermes_isnt_working_on_are_tried_a_few_times(no_retry_wait):
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "idle"}
    assert follow_up(grant) == {"ok": True, "title": "Check the build logs", "outcome": "failed",
                                "error": "Hermes wasn't working on \"Check the build logs\" just then."}
    assert len(server.methods("session.redirect")) == api.WATCH_FOLLOW_UP_ATTEMPTS
    assert grant.jobs.jobs["watch-1"].follow_up is None


def test_a_hermes_without_redirect_gets_a_steer():
    grant, server = running_job()
    server.fail["session.redirect"] = "Method not found"
    assert follow_up(grant)["outcome"] == "interrupted"
    assert server.methods("session.steer") == [{"session_id": "rt-1", "text": "make it Alex"}]


def test_a_redirect_hermes_refuses_says_why():
    grant, server = running_job()
    server.fail["session.redirect"] = "session not found"
    assert follow_up(grant) == {"ok": True, "title": "Check the build logs", "outcome": "failed",
                                "error": "session not found"}


def test_a_follow_up_to_a_settled_or_unknown_job_isnt_sent():
    grant, server = running_job()
    assert follow_up(grant, job_id="watch-9") == {"ok": True, "outcome": "unknown_job"}
    server.emit("rt-1", "message.complete", {"text": "All done."})
    assert follow_up(grant)["outcome"] == "finished"
    assert server.methods("session.redirect") == []


@pytest.mark.parametrize("args, status", [({"job_id": "watch-1"}, 400), ({"message": "make it Alex"}, 400),
                                          ({"job_id": "watch-1", "message": "x" * 4_000}, 413)])
def test_a_follow_up_without_fitting_words_is_refused(args, status):
    grant, _ = running_job()
    assert api.run_watch_job_call(grant, "interrupt_job", args)["status"] == status


def test_a_correction_marker_is_never_a_result():
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "redirected"}
    assert follow_up(grant)["outcome"] == "interrupted"
    server.emit("rt-1", "message.complete", {"text": " [This response was interrupted by a user correction.] "})
    assert grant.jobs.jobs["watch-1"].status == "running"


def test_the_marker_text_with_no_correction_sent_is_the_jobs_own_result():
    grant, server = running_job()
    server.emit("rt-1", "message.complete", {"text": "[This response was interrupted by a user correction.]"})
    assert grant.jobs.jobs["watch-1"].status == "finished"


def test_an_end_held_long_after_the_call_ended_still_settles_and_closes(monkeypatch):
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "queued"}
    assert follow_up(grant)["outcome"] == "queued"
    grant.jobs.end()
    # The reaper had nothing to hold and stopped; the step ends much later.
    wait_for(lambda: not grant.jobs.reaping)
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_HOLD_S", 0)
    server.emit("rt-1", "message.complete", {"text": "Step one done."})
    wait_for(lambda: server.methods("session.close") == [{"session_id": "rt-1"}])
    wait_for(lambda: grant.jobs.transport._closed)


def test_follow_ups_reach_the_job_through_the_sealed_grant_route():
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "redirected"}
    grant.tools = grant.tools + (api.WATCH_JOB_FOLLOW_UP,)
    answer = api.run_watch_tool(grant, {"tool": "interrupt_job", "args": {"job_id": "watch-1", "message": "hold that"}})
    assert answer["outcome"] == "interrupted"


def test_an_end_held_for_queued_words_whose_turn_never_comes_settles_the_job(monkeypatch):
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "queued"}
    assert follow_up(grant)["outcome"] == "queued"
    server.emit("rt-1", "message.complete", {"text": "Step one done."})
    assert news(grant)["news"] == []
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_HOLD_S", 0)
    [item] = news(grant)["news"]
    assert (item["status"], item["result"]) == ("finished", "Step one done.")


def test_an_end_held_when_the_call_ends_still_settles_and_closes(monkeypatch):
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "queued"}
    assert follow_up(grant)["outcome"] == "queued"
    server.emit("rt-1", "message.complete", {"text": "Step one done."})
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_HOLD_S", 0)
    grant.jobs.end()
    wait_for(lambda: server.methods("session.close") == [{"session_id": "rt-1"}])
    wait_for(lambda: grant.jobs.transport._closed)


def test_a_follow_up_waiting_its_turn_when_the_call_ends_still_has_its_hold_reaped(monkeypatch):
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "queued"}
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_HOLD_S", 0)
    job = grant.jobs.jobs["watch-1"]
    job.follow_up_lock.acquire()  # another follow-up still running
    answers = []
    waiting = threading.Thread(target=lambda: answers.append(follow_up(grant)))
    waiting.start()
    wait_for(lambda: grant.jobs.follow_ups_in_flight == 1)
    grant.jobs.end()
    job.follow_up_lock.release()
    waiting.join(5)
    assert answers[0]["outcome"] == "queued"
    server.emit("rt-1", "message.complete", {"text": "Step one done."})
    wait_for(lambda: server.methods("session.close") == [{"session_id": "rt-1"}])
    wait_for(lambda: grant.jobs.transport._closed)


def test_words_taken_as_the_turn_failed_are_reported_too_late():
    grant, server = running_job()

    def failed_meanwhile(params):
        server.emit("rt-1", "error", {"message": "model overloaded"})
        return {"status": "redirected"}

    server.results["session.redirect"] = failed_meanwhile
    assert follow_up(grant)["outcome"] == "finished"
    assert news(grant)["news"][0]["status"] == "failed"


def test_a_follow_up_is_answered_inside_the_relays_wait(monkeypatch):
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_DEADLINE_S", 0.5)
    monkeypatch.setattr(api, "WATCH_FOLLOW_UP_RETRY_S", 0.2)
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "idle"}
    started = time.monotonic()
    assert follow_up(grant)["outcome"] == "failed"
    assert time.monotonic() - started < 1.0


def test_a_follow_up_after_the_call_ended_is_refused():
    grant, server = running_job()
    grant.jobs.end()
    assert follow_up(grant)["status"] == 410
    assert server.methods("session.redirect") == []


def test_a_grant_that_names_interrupt_job_is_taken(tmp_path):
    grant = api.open_watch_grant({"tools": ["start_job", "interrupt_job"]}, profile=None, path=write_pairing(tmp_path),
                                 relay=FakeRelay(), start=lambda g: None, session_api=FakeHermes)
    assert grant["tools"] == ["start_job", "job_news", "answer_approval", "interrupt_job"]


def test_a_grant_that_names_only_interrupt_job_opens_the_jobs(tmp_path):
    grant = api.open_watch_grant({"tools": ["interrupt_job"]}, profile=None, path=write_pairing(tmp_path),
                                 relay=FakeRelay(), start=lambda g: None, session_api=FakeHermes)
    assert grant["tools"] == ["job_news", "answer_approval", "interrupt_job"]


def test_a_second_follow_up_while_words_wait_keeps_the_job_for_their_turn():
    grant, server = running_job()
    server.results["session.redirect"] = {"status": "queued"}
    assert follow_up(grant)["outcome"] == "queued"
    server.results["session.redirect"] = {"status": "redirected"}
    assert follow_up(grant, "and cc Sam")["outcome"] == "interrupted"
    # The cut-off step, then the corrected step: the queued words still run.
    server.emit("rt-1", "message.complete", {"text": "[This response was interrupted by a user correction.]"})
    server.emit("rt-1", "message.complete", {"text": "Step one done, cc Sam."})
    assert grant.jobs.jobs["watch-1"].status == "running"
    server.emit("rt-1", "message.start", {})
    server.emit("rt-1", "message.complete", {"text": "Done for Alex, cc Sam."})
    [item] = news(grant)["news"]
    assert (item["status"], item["result"]) == ("finished", "Done for Alex, cc Sam.")
