"""Watch tool grants: wrist-down lookups for a Conduit Watch call through the relay."""

import base64
import hashlib
import importlib.util
import json
import logging
import pathlib
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()

RELAY = "https://relay.example"
GRANT_ID = "G" * 22


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def write_pairing(tmp_path, **overrides):
    path = tmp_path / "conduit-push.json"
    state = {
        "credential": "install-1.gateway-1.secret",
        "installation_id": "install-1",
        "gateway_id": "gateway-1",
        "relay_url": RELAY,
        **overrides,
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


class FakeRelay:
    """Records relay requests and answers them from a script."""

    def __init__(self, answers=None):
        self.requests = []
        self.answers = list(answers or [])
        self.lock = threading.Lock()

    def __call__(self, url, method, credential, payload, timeout):
        with self.lock:
            self.requests.append({"url": url, "method": method, "credential": credential, "payload": payload})
            if self.answers:
                answer = self.answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        if method == "POST" and url.endswith("/v1/watch-tools/grants"):
            return 201, {"grant_id": GRANT_ID, "expires_at": "2026-10-07T12:00:00.000Z"}
        if method == "DELETE":
            return 204, {}
        return 200, {"status": "delivered"}


@pytest.fixture(autouse=True)
def fresh_grants(monkeypatch):
    monkeypatch.setattr(api, "_watch_grants", api._WatchGrants())
    monkeypatch.setattr(api, "_watch_grant_limiter", api._MintLimiter(1000, 60.0))


def make_grant(tools=("web_search", "recall_memory"), secret=None, expires_in=600.0):
    return api._WatchGrant(
        grant_id=GRANT_ID,
        profile=None,
        tools=tuple(tools),
        secret=secret or bytes(range(32)),
        relay_url=RELAY,
        credential="install-1.gateway-1.secret",
        expires_at=time.monotonic() + expires_in,
    )


def sealed_call(grant, payload, rid=None):
    rid = rid or b64u(bytes(16))
    return {"rid": rid, **api.seal_watch_tool(grant.call_key, "call", grant.grant_id, rid, payload)}


# --- Sealing -------------------------------------------------------------------

def test_a_sealed_call_opens_only_for_its_grant_call_id_and_direction():
    keys = api.watch_tool_keys(bytes(range(32)))
    rid = "R" * 22
    sealed = api.seal_watch_tool(keys["call"], "call", GRANT_ID, rid, {"tool": "web_search", "args": {"query": "tokyo"}})
    assert api.open_watch_tool(keys["call"], "call", GRANT_ID, rid, sealed) == {"tool": "web_search", "args": {"query": "tokyo"}}
    for key, direction, grant_id, other_rid in [
        (keys["result"], "call", GRANT_ID, rid),
        (keys["call"], "result", GRANT_ID, rid),
        (keys["call"], "call", "H" * 22, rid),
        (keys["call"], "call", GRANT_ID, "S" * 22),
    ]:
        with pytest.raises(api.WatchToolError):
            api.open_watch_tool(key, direction, grant_id, other_rid, sealed)
    tampered = dict(sealed, ct=sealed["ct"][:-2] + ("AA" if not sealed["ct"].endswith("AA") else "BB"))
    with pytest.raises(api.WatchToolError):
        api.open_watch_tool(keys["call"], "call", GRANT_ID, rid, tampered)


def test_the_directions_use_separate_keys_derived_from_the_root():
    keys = api.watch_tool_keys(bytes(range(32)))
    assert keys["call"] != keys["result"]
    assert bytes(range(32)) not in keys.values()
    with pytest.raises(api.WatchToolError):
        api.watch_tool_keys(b"short")


def test_oversized_or_malformed_envelopes_are_refused_before_decrypting():
    keys = api.watch_tool_keys(bytes(range(32)))
    for sealed in [None, {}, {"n": "x", "ct": "A" * 40}, {"n": "A" * 16, "ct": "not base64!"},
                   {"n": "A" * 16, "ct": "A" * 10_000}]:
        with pytest.raises(api.WatchToolError):
            api.open_watch_tool(keys["call"], "call", GRANT_ID, "R" * 22, sealed)


# relay/src/watch-tools.mjs MAX_CALL_CT_CHARS: a sealed 4 KB call, base64url.
RELAY_MAX_CALL_CT_CHARS = 5483


def test_the_largest_call_the_relay_takes_is_one_the_host_opens():
    keys = api.watch_tool_keys(bytes(range(32)))
    empty = len(json.dumps({"tool": "web_search", "args": {"query": ""}}, separators=(",", ":")))
    payload = {"tool": "web_search", "args": {"query": "x" * (api.WATCH_MAX_CALL_BYTES - empty)}}
    sealed = api.seal_watch_tool(keys["call"], "call", GRANT_ID, "R" * 22, payload)
    assert len(sealed["ct"]) == RELAY_MAX_CALL_CT_CHARS
    assert api.open_watch_tool(keys["call"], "call", GRANT_ID, "R" * 22, sealed) == payload
    # Anything the relay passes on is opened, not refused for its size.
    with pytest.raises(api.WatchToolError, match="did not verify"):
        api.open_watch_tool(keys["call"], "call", GRANT_ID, "R" * 22,
                            {"n": "A" * 16, "ct": "A" * RELAY_MAX_CALL_CT_CHARS})


# Vectors for Conduit's Swift side (WatchToolSealTests in hermes-conduit): a
# fixed root, grant, call id and nonce. Conduit seals VECTOR_CALL_JSON and must
# get CALL_VECTOR_CT, and opens RESULT_VECTOR_CT to VECTOR_RESULT.
VECTOR_SECRET = bytes(range(32))
VECTOR_GRANT = "G" * 22
VECTOR_RID = "R" * 22
VECTOR_NONCE = bytes(range(12))
VECTOR_CALL_JSON = b'{"args":{"query":"weather in Tokyo"},"tool":"web_search"}'
VECTOR_RESULT = {"ok": True, "query": "weather in Tokyo", "results": [
    {"title": "Tokyo weather", "url": "https://example.com/tokyo", "snippet": "Sunny, 21°C"}]}


def test_vector_matches_the_one_conduit_checks():
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    keys = api.watch_tool_keys(VECTOR_SECRET)
    call_ct = ChaCha20Poly1305(keys["call"]).encrypt(
        VECTOR_NONCE, VECTOR_CALL_JSON, api.watch_tool_aad("call", VECTOR_GRANT, VECTOR_RID))
    result = api.seal_watch_tool(keys["result"], "result", VECTOR_GRANT, VECTOR_RID, VECTOR_RESULT, nonce=VECTOR_NONCE)
    assert keys["call"].hex() == "76bee458fa181a0da7695798b6de8a0be9bf13587ecac450ff389d0094b5bbdf"
    assert keys["result"].hex() == "06499568025534ab010493728680fd7bc9216867d0012198cc6d6a9e450fc703"
    assert b64u(call_ct) == CALL_VECTOR_CT
    assert api.open_watch_tool(keys["call"], "call", VECTOR_GRANT, VECTOR_RID,
                               {"n": b64u(VECTOR_NONCE), "ct": b64u(call_ct)})["tool"] == "web_search"
    assert result == {"n": "AAECAwQFBgcICQoL", "ct": RESULT_VECTOR_CT}


CALL_VECTOR_CT = (
    "usQw1Mc1f6DtZfIgf1Dd3VKsJq3JjVCOQUQYKH3nr0mk-Lb0ZEN1IoBA1LY1CEkYy8BAT4aGVN28rrFCyuXKvKmk7un4LOPnaA")
RESULT_VECTOR_CT = (
    "Mmf4pRBwr3HNiVGcy-x-KIXRml6fz7xmr32vPgunMfFVqBvDAEImsfwkDEGZP28NtM89wF7XhlAhL99eH7KBpO0cVQc4roUey5BFrzA84ybx"
    "4K1PD28aY05sabPmNAQBqZoEkZR-bA1IdfVrmkLNhhbS7Qi1TfDsHIIgVOxQ1PVG2ohffj1meUdS-EskwQTMUW_iivE2GQ")


# --- Opening a grant ---------------------------------------------------------

def test_a_grant_carries_its_keys_to_the_phone_and_only_a_hash_of_the_relay_key_to_the_relay(tmp_path):
    relay = FakeRelay()
    started = []
    grant = api.open_watch_grant({"tools": ["recall_memory", "web_search"]}, profile=None,
                                 path=write_pairing(tmp_path), relay=relay, start=started.append)
    assert grant["grant_id"] == GRANT_ID
    assert grant["relay_url"] == RELAY
    assert grant["tools"] == ["web_search", "recall_memory"]
    assert grant["max_calls"] == api.WATCH_GRANT_MAX_CALLS
    assert len(base64.urlsafe_b64decode(grant["key"] + "=")) == 32
    assert len(grant["watch_key"]) == 43
    [request] = relay.requests
    assert request["url"] == f"{RELAY}/v1/watch-tools/grants"
    assert request["credential"] == "install-1.gateway-1.secret"
    assert request["payload"] == {
        "watch_key_sha256": hashlib.sha256(grant["watch_key"].encode()).hexdigest(),
        "ttl_s": 1800,
        "max_calls": 60,
    }
    # The relay gets neither key.
    assert grant["key"] not in json.dumps(request) and grant["watch_key"] not in json.dumps(request)
    [live] = started
    assert live.grant_id == GRANT_ID
    assert live.call_key != base64.urlsafe_b64decode(grant["key"] + "=")


def test_a_grant_ends_here_no_later_than_on_the_relay(tmp_path):
    relay_expiry = datetime.now(timezone.utc) + timedelta(seconds=600)
    relay = FakeRelay([(201, {"grant_id": GRANT_ID, "expires_at": api._timestamp(relay_expiry)})])
    started = []
    grant = api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=write_pairing(tmp_path),
                                 relay=relay, start=started.append)
    [live] = started
    assert 590 < live.expires_at - time.monotonic() <= 600
    told = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
    assert abs((told - relay_expiry).total_seconds()) < 2


def test_the_grant_lifetime_is_capped_only_by_a_relay_time_it_can_use():
    now = datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc)
    assert api._watch_grant_ttl("2026-10-07T11:10:00.000Z", now) == 600
    assert api._watch_grant_ttl("2026-10-07T12:00:00Z", now) == 1800
    for value in (None, 7, "", "soon", "2026-10-07T11:10:00", "2026-10-07T10:00:00Z"):
        assert api._watch_grant_ttl(value, now) == 1800, value


def test_a_grant_whose_poller_cant_start_is_closed_on_the_relay(tmp_path, monkeypatch):
    relay = FakeRelay()
    told = []
    monkeypatch.setattr(api, "_close_watch_grant_on_relay_later", lambda grant, relay=None: told.append(grant))

    def start(grant):
        raise RuntimeError("can't start new thread")

    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=write_pairing(tmp_path),
                             relay=relay, start=start)
    assert err.value.status == 503
    [grant] = told
    assert grant.grant_id == GRANT_ID and grant.closed.is_set()
    assert api._watch_grants.all() == []


def test_a_full_relay_reads_as_at_capacity_not_broken(tmp_path):
    relay = FakeRelay([(503, {"error": "watch_grant_capacity"})])
    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=write_pairing(tmp_path), relay=relay,
                             start=lambda g: None)
    assert (err.value.status, str(err.value)) == (503, "The push relay is at capacity for Watch tools")


@pytest.mark.parametrize("body, status", [
    (None, 400),
    ({}, 400),
    ({"tools": []}, 400),
    ({"tools": ["terminal"]}, 400),
    ({"tools": ["web_search", "execute_code"]}, 400),
    ({"tools": "web_search"}, 400),
    # Jobs alone where this process can't run them (tests/test_watch_jobs.py).
    ({"tools": ["start_job"]}, 501),
])
def test_only_lookups_memory_and_jobs_can_be_granted(tmp_path, body, status):
    relay = FakeRelay()
    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant(body, profile=None, path=write_pairing(tmp_path), relay=relay, start=lambda g: None)
    assert err.value.status == status
    assert relay.requests == []


def test_a_grant_needs_a_paired_profile_on_an_https_relay(tmp_path):
    relay = FakeRelay()
    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=tmp_path / "missing.json", relay=relay,
                             start=lambda g: None)
    assert err.value.status == 409
    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant({"tools": ["web_search"]}, profile=None,
                             path=write_pairing(tmp_path, relay_url="http://relay.example"), relay=relay,
                             start=lambda g: None)
    assert err.value.status == 409
    assert relay.requests == []


def test_an_older_relay_reads_as_unsupported(tmp_path):
    relay = FakeRelay([(404, {"error": "not_found"})])
    with pytest.raises(api.TokenError) as err:
        api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=write_pairing(tmp_path), relay=relay,
                             start=lambda g: None)
    assert err.value.status == 501


def test_a_profile_keeps_two_live_grants_and_a_third_closes_the_oldest(tmp_path, monkeypatch):
    ids = iter(["A" * 22, "B" * 22, "C" * 22])
    relay = FakeRelay()

    def answers(url, method, credential, payload, timeout):
        if method == "POST":
            return 201, {"grant_id": next(ids)}
        return relay(url, method, credential, payload, timeout)

    closed = threading.Event()
    real_close = api._close_watch_grant

    def close(grant, **kwargs):
        real_close(grant, **kwargs)
        closed.set()

    monkeypatch.setattr(api, "_close_watch_grant", close)
    path = write_pairing(tmp_path)
    for _ in range(3):
        api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=path, relay=answers, start=lambda g: None)
    assert closed.wait(5)
    assert {g.grant_id for g in api._watch_grants.all()} == {"B" * 22, "C" * 22}
    assert any(r["method"] == "DELETE" and r["url"].endswith("/grants/" + "A" * 22) for r in relay.requests)


# --- Answering calls ---------------------------------------------------------

def opened_answer(grant, relay, rid):
    [post] = [r for r in relay.requests if r["url"].endswith(f"/results/{rid}")]
    assert post["method"] == "POST"
    return api.open_watch_tool(grant.result_key, "result", grant.grant_id, rid, post["payload"], max_bytes=64 * 1024)


def test_a_call_runs_and_its_answer_goes_back_sealed_for_the_watch():
    grant = make_grant()
    relay = FakeRelay()
    ran = []

    def run(g, request):
        ran.append(request)
        return {"ok": True, "query": "tokyo", "results": [{"title": "T", "url": "https://t.example", "snippet": "s"}]}

    call = sealed_call(grant, {"tool": "web_search", "args": {"query": "tokyo"}})
    assert api.answer_watch_call(grant, call, relay=relay, run=run) == "answered"
    assert ran == [{"tool": "web_search", "args": {"query": "tokyo"}}]
    assert opened_answer(grant, relay, call["rid"])["results"][0]["title"] == "T"


def test_a_call_not_sealed_with_the_grant_or_repeated_is_dropped_unanswered():
    grant = make_grant()
    other = make_grant(secret=bytes(32))
    relay = FakeRelay()
    run = lambda g, r: pytest.fail("must not run")  # noqa: E731
    assert api.answer_watch_call(grant, sealed_call(other, {"tool": "web_search"}), relay=relay, run=run) is None
    assert api.answer_watch_call(grant, {"rid": "short", "n": "A" * 16, "ct": "A" * 30}, relay=relay, run=run) is None
    assert relay.requests == []

    call = sealed_call(grant, {"tool": "web_search", "args": {"query": "q"}})
    assert api.answer_watch_call(grant, call, relay=relay, run=lambda g, r: {"ok": True, "results": []}) == "answered"
    assert api.answer_watch_call(grant, call, relay=relay, run=run) is None
    assert len(relay.requests) == 1


def test_a_tool_outside_the_grant_is_refused_by_the_host():
    grant = make_grant(tools=("web_search",))
    relay = FakeRelay()
    call = sealed_call(grant, {"tool": "recall_memory", "args": {"query": "q"}})
    assert api.answer_watch_call(grant, call, relay=relay) == "answered"
    assert opened_answer(grant, relay, call["rid"]) == {
        "ok": False, "status": 403, "detail": "This tool isn't available to the Watch"}
    for tool in ("start_job", "cancel_job", "list_jobs"):
        call = sealed_call(grant, {"tool": tool, "args": {"instructions": "rm -rf /"}}, rid=b64u(tool.encode().ljust(16, b"x")))
        api.answer_watch_call(grant, call, relay=relay)
        assert opened_answer(grant, relay, call["rid"])["status"] == 403


def test_a_grant_answers_at_most_its_call_limit(monkeypatch):
    monkeypatch.setattr(api, "WATCH_GRANT_MAX_CALLS", 2)
    grant = make_grant()
    relay = FakeRelay()
    rids = [b64u(bytes([i]) * 16) for i in range(3)]
    for rid in rids:
        api.answer_watch_call(grant, sealed_call(grant, {"tool": "web_search", "args": {"query": "q"}}, rid=rid),
                              relay=relay, run=lambda g, r: {"ok": True, "results": []})
    assert opened_answer(grant, relay, rids[1])["ok"] is True
    assert opened_answer(grant, relay, rids[2]) == {
        "ok": False, "status": 429, "detail": "This call has used all its Watch lookups"}


def test_an_answer_too_large_for_the_relay_becomes_an_error():
    grant = make_grant()
    relay = FakeRelay()
    call = sealed_call(grant, {"tool": "recall_memory", "args": {"query": "q"}})
    api.answer_watch_call(grant, call, relay=relay, run=lambda g, r: {"ok": True, "results": "x" * 40_000})
    assert opened_answer(grant, relay, call["rid"]) == {
        "ok": False, "status": 502, "detail": "The answer was too large for the Watch"}


def test_a_closed_grant_answers_that_its_lookups_ended():
    grant = make_grant()
    grant.closed.set()
    relay = FakeRelay()
    call = sealed_call(grant, {"tool": "web_search", "args": {"query": "q"}})
    api.answer_watch_call(grant, call, relay=relay, run=lambda g, r: pytest.fail("must not run"))
    assert opened_answer(grant, relay, call["rid"])["status"] == 410


def test_web_search_runs_the_same_code_as_the_dashboard_route(monkeypatch):
    seen = []

    def search(query, limit):
        seen.append((query, limit))
        return json.dumps({"success": True, "data": {"web": [
            {"title": "Weather", "url": "https://example.com/w", "description": "Sunny"}]}})

    monkeypatch.setattr(api, "_hermes_web_search", search)
    grant = make_grant()
    answer = api.run_watch_tool(grant, {"tool": "web_search", "args": {"query": " weather  tokyo ", "limit": 3}})
    assert answer == {"ok": True, "query": "weather tokyo",
                      "results": [{"title": "Weather", "url": "https://example.com/w", "snippet": "Sunny"}]}
    assert seen == [("weather tokyo", 3)]
    assert api.run_watch_tool(grant, {"tool": "web_search", "args": {}}) == {
        "ok": False, "status": 400, "detail": "query is required"}


def test_memory_recall_runs_the_same_code_as_the_dashboard_route(monkeypatch):
    queries = []
    monkeypatch.setattr(api, "run_memory_recall",
                        lambda query, key, limiter_key=None: queries.append(query) or {"available": True, "results": "Likes tea"})
    answer = api.run_watch_tool(make_grant(), {"tool": "recall_memory", "args": {"query": "drinks"}})
    assert answer == {"ok": True, "available": True, "results": "Likes tea"}
    assert queries == ["drinks"]


def test_watch_args_keep_text_and_whole_numbers_only():
    assert api._clean_watch_args({"query": "q", "limit": 3, "flag": True, "ratio": 0.5, 4: "x"}) == {
        "query": "q", "limit": 3}
    assert api._clean_watch_args(["query"]) == {}


class _Future:
    def __init__(self, error):
        self.error = error
        self.cancelled = False

    def result(self, timeout):
        raise self.error

    def cancel(self):
        self.cancelled = True
        return True


def test_a_lookup_that_outlasts_its_wait_is_cancelled(monkeypatch):
    future = _Future(api.FutureTimeoutError())
    monkeypatch.setattr(api, "_search_executor", types.SimpleNamespace(submit=lambda *args: future))
    answer = api.run_watch_tool(make_grant(), {"tool": "web_search", "args": {"query": "q"}})
    assert answer == {"ok": False, "status": 504, "detail": "Web search timed out"}
    assert future.cancelled


def test_an_unexpected_failure_is_logged_without_its_message_at_any_level(monkeypatch, caplog):
    def fail():
        raise RuntimeError("weather in tokyo")

    try:
        fail()
    except RuntimeError as error:
        future = _Future(error)
    monkeypatch.setattr(api, "_memory_executor", types.SimpleNamespace(submit=lambda *args: future))
    with caplog.at_level(logging.DEBUG, logger=api.logger.name):
        answer = api.run_watch_tool(make_grant(), {"tool": "recall_memory", "args": {"query": "weather in tokyo"}})
    assert answer == {"ok": False, "status": 500, "detail": "Memory recall failed on the host (RuntimeError)"}
    assert "RuntimeError" in caplog.text
    # Debug carries where it failed, still not what it said.
    assert "in fail" in caplog.text
    assert "weather in tokyo" not in caplog.text


# --- Polling -----------------------------------------------------------------

def test_the_poller_answers_each_call_and_stops_when_the_relay_closes_the_grant():
    grant = make_grant()
    api._watch_grants.add(grant)
    call = sealed_call(grant, {"tool": "web_search", "args": {"query": "q"}})
    relay = FakeRelay([(200, {"calls": [call]}), (200, {"calls": []}), (410, {"error": "grant_closed"})])
    submitted = []
    api.poll_watch_grant(grant, relay=relay, submit=submitted.append)
    assert len(submitted) == 1
    assert [r["method"] for r in relay.requests] == ["GET", "GET", "GET"]
    assert relay.requests[0]["url"] == f"{RELAY}/v1/watch-tools/grants/{GRANT_ID}/calls?wait_ms=25000"
    assert grant.closed.is_set()
    assert api._watch_grants.all() == []


def test_what_escapes_answering_a_call_is_logged_by_type(monkeypatch, caplog):
    grant = make_grant()
    call = sealed_call(grant, {"tool": "web_search", "args": {"query": "q"}})
    relay = FakeRelay([(200, {"calls": [call]}), (410, {})])
    submitted = []
    api.poll_watch_grant(grant, relay=relay, submit=submitted.append)

    def broken(grant, call, relay=None):
        raise ValueError("weather in tokyo")

    monkeypatch.setattr(api, "answer_watch_call", broken)
    with caplog.at_level(logging.DEBUG, logger=api.logger.name):
        [job] = submitted
        assert job() is None
    assert "ValueError" in caplog.text
    assert "weather in tokyo" not in caplog.text


def test_the_poller_closes_the_grant_on_the_relay_when_it_expires_here():
    grant = make_grant(expires_in=-1)
    relay = FakeRelay()
    api.poll_watch_grant(grant, relay=relay, submit=lambda fn: None)
    assert [(r["method"], r["url"]) for r in relay.requests] == [("DELETE", f"{RELAY}/v1/watch-tools/grants/{GRANT_ID}")]


def test_the_poller_retries_after_a_network_failure(monkeypatch):
    grant = make_grant()
    relay = FakeRelay([OSError("down"), (410, {})])
    waits = []
    monkeypatch.setattr(grant.closed, "wait", lambda timeout: waits.append(timeout))
    api.poll_watch_grant(grant, relay=relay, submit=lambda fn: None)
    assert waits == [1.0]
    assert len(relay.requests) == 2


# --- Revoking ----------------------------------------------------------------

def test_revoke_ends_the_grant_here_at_once_and_tells_the_relay_after():
    grant = make_grant()
    api._watch_grants.add(grant)
    told = []
    assert api.revoke_watch_grant({"grant_id": GRANT_ID}, profile=None,
                                  tell_relay=lambda g, relay: told.append(g)) == {"revoked": True}
    assert grant.closed.is_set()
    assert api._watch_grants.all() == []
    assert told == [grant]
    assert api.revoke_watch_grant({"grant_id": GRANT_ID}, profile=None,
                                  tell_relay=lambda g, relay: told.append(g)) == {"revoked": False}
    assert told == [grant]


def test_revoke_tells_the_relay_on_a_thread_of_its_own():
    grant = make_grant()
    delivered = threading.Event()
    seen = []

    def relay(url, method, credential, payload, timeout):
        seen.append((method, url, threading.current_thread().name))
        delivered.set()
        return 204, {}

    api._close_watch_grant_on_relay_later(grant, relay)
    assert delivered.wait(5)
    assert seen == [("DELETE", f"{RELAY}/v1/watch-tools/grants/{GRANT_ID}", "conduit-watch-revoke")]


def test_without_a_thread_to_spare_a_lookup_worker_tells_the_relay(monkeypatch, caplog):
    grant = make_grant()
    relay = FakeRelay()

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(api.threading.Thread, "start", no_thread)
    monkeypatch.setattr(api, "_watch_executor", types.SimpleNamespace(submit=lambda fn, *args: fn(*args)))
    with caplog.at_level(logging.INFO, logger=api.logger.name):
        api._close_watch_grant_on_relay_later(grant, relay)
    assert [(r["method"], r["url"]) for r in relay.requests] == [("DELETE", f"{RELAY}/v1/watch-tools/grants/{GRANT_ID}")]
    assert "queueing it" in caplog.text

    def no_worker(fn, *args):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(api, "_watch_executor", types.SimpleNamespace(submit=no_worker))
    with caplog.at_level(logging.INFO, logger=api.logger.name):
        api._close_watch_grant_on_relay_later(grant, relay)
    assert len(relay.requests) == 1
    assert "it ends there at its expiry" in caplog.text


def test_another_profile_cannot_revoke_a_grant():
    grant = make_grant()
    api._watch_grants.add(grant)
    assert api.revoke_watch_grant({"grant_id": GRANT_ID}, profile="other",
                                  tell_relay=lambda g, relay: pytest.fail("must not close")) == {"revoked": False}
    assert not grant.closed.is_set()


# --- Routes ------------------------------------------------------------------

@pytest.fixture
def told_relay(monkeypatch):
    told = []
    monkeypatch.setattr(api, "_close_watch_grant_on_relay_later", lambda grant, relay=None: told.append(grant.grant_id))
    return told


@pytest.fixture
def client(tmp_path, monkeypatch, told_relay):
    monkeypatch.setattr(api, "_pairing_state_path", lambda: write_pairing(tmp_path))
    monkeypatch.setattr(api, "_relay_request", FakeRelay())
    monkeypatch.setattr(api, "_start_watch_poller", lambda grant: None)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_grant_route_returns_the_grant_uncached_and_revoke_closes_it(client, told_relay):
    response = client.post(f"{BASE}/watch-tools/grant", json={"tools": ["web_search"]})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ok"] is True and body["grant_id"] == GRANT_ID and body["tools"] == ["web_search"]
    revoked = client.post(f"{BASE}/watch-tools/revoke", json={"grant_id": GRANT_ID})
    assert revoked.json() == {"ok": True, "revoked": True}
    assert told_relay == [GRANT_ID]
    assert client.post(f"{BASE}/watch-tools/revoke", json={"grant_id": GRANT_ID}).json() == {"ok": True, "revoked": False}


def test_grant_route_refuses_other_tools(client):
    response = client.post(f"{BASE}/watch-tools/grant", json={"tools": ["terminal"]})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_capabilities_name_watch_tools():
    assert "watch-tools" in api.ROUTE_CAPABILITIES
