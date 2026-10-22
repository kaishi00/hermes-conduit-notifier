"""The delivery worker: transient relay failures are retried with the same
event id, and a relay 401 is recorded on the pairing for `status`."""

import http.server
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import types
import urllib.error

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


def _private_copy(name, path):
    # A private copy: other test files replace functions on the shared modules.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _private_copy("conduit_push._client_delivery_under_test", ROOT / "client.py")
cli = _private_copy("conduit_push._cli_delivery_under_test", ROOT / "cli.py")
api = _private_copy("conduit_plugin_api_delivery", ROOT / "dashboard" / "plugin_api.py")
e2e = client.e2e

KID = "0123456789abcdef0123456789abcdef"
BARE_403 = "bare 403"
CREDENTIAL = "inst.gw.secret"
SECRET = bytes(range(32))


class _Relay:
    """A local relay that answers /v1/events with the given statuses in turn
    (200 once they run out) and records the event ids it was sent."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.event_ids = []
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                relay.event_ids.append(body.get("event_id"))
                status = relay.statuses.pop(0) if relay.statuses else 200
                if status == BARE_403:
                    # Something in front of the relay: no JSON error body.
                    status, answer = 403, b""
                else:
                    answer = json.dumps({"accepted": True} if status < 300 else {"error": "nope"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def paired(tmp_path, monkeypatch):
    path = tmp_path / "conduit-push.json"
    monkeypatch.setattr(client, "state_path", lambda: path)
    monkeypatch.setattr(cli, "load_state", client.load_state)
    monkeypatch.setattr(api, "_pairing_state_path", lambda: path)
    # Retries without the real waits.
    monkeypatch.setattr(client, "RETRY_DELAYS", (0.01, 0.01, 0.01), raising=False)
    client._refusals.clear()
    relays = []

    def pair(statuses, **extra):
        relay = _Relay(statuses)
        relays.append(relay)
        state = {"credential": "inst.gw.secret", "installation_id": "inst", "gateway_id": "gw",
                 "gateway_name": "box", "relay_url": relay.url}
        state.update(extra)
        client.save_state(state)
        return relay

    yield pair
    for relay in relays:
        relay.close()


def _deliver(event):
    assert client.enqueue(event)
    client._events.join()


def _event():
    return {"event_id": "response:0123456789abcdef", "type": "response.ready", "title": "Hermes", "body": "Done."}


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
@pytest.mark.parametrize("failures", [[503, 503], [429], [408, 502, 504]])
def test_a_transient_relay_failure_is_sent_again_with_the_same_event_id(paired, failures, encrypted):
    extra = {"e2e": {"kid": KID, "secret": e2e.b64u(SECRET)}} if encrypted else {}
    relay = paired(failures, **extra)

    _deliver(_event())

    assert len(relay.event_ids) == len(failures) + 1
    assert len(set(relay.event_ids)) == 1  # the relay dedupes the repeats


@pytest.mark.parametrize("status", [400, 403, 404, 413])
def test_a_definitive_refusal_is_not_sent_again(paired, status):
    relay = paired([status, status])

    _deliver(_event())

    assert len(relay.event_ids) == 1
    assert "relay_rejected_at" not in client.load_state()


def test_no_connection_is_retried_like_a_relay_failure(paired, monkeypatch):
    relay = paired([])
    real_request = client._request
    failures = [urllib.error.URLError(ConnectionRefusedError(61, "refused")), TimeoutError("timed out")]

    def flaky(url, **kwargs):
        if failures:
            raise failures.pop(0)
        return real_request(url, **kwargs)

    monkeypatch.setattr(client, "_request", flaky)
    _deliver(_event())

    assert len(relay.event_ids) == 1 and not failures


def test_retries_give_up_after_the_budget(paired, monkeypatch):
    relay = paired([503] * 10)
    monkeypatch.setattr(client, "RETRY_DELAYS", (1.0, 2.0, 4.0))
    monkeypatch.setattr(client, "RETRY_BUDGET", 2.5)
    now = [0.0]

    assert client._deliver(_event(), sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                           clock=lambda: now[0]) is False

    # 1 s and then 2 s would go past 2.5 s: the first send and one retry.
    assert len(relay.event_ids) == 2


def test_a_relay_401_is_recorded_for_status_and_keeps_the_pairing(paired, capsys):
    relay = paired([401], redact_content=True, redact_key="k" * 64)

    _deliver(_event())

    assert len(relay.event_ids) == 1  # a 401 is not retried
    state = client.load_state()
    assert state["relay_rejected_at"]
    assert state["credential"] == "inst.gw.secret" and state["redact_key"] == "k" * 64
    assert api.e2e_status()["relay_rejected"] is True
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    assert "relay refused this pairing's credential" in capsys.readouterr().out

    # The relay knows the gateway again (restored data): the mark goes once
    # the refusal's pause has run out.
    client._refusals[CREDENTIAL]["until"] = 0.0
    _deliver(_event())

    assert "relay_rejected_at" not in client.load_state()
    assert api.e2e_status()["relay_rejected"] is False
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    assert "relay refused" not in capsys.readouterr().out


def test_a_401_does_not_mark_a_pairing_made_meanwhile(paired, monkeypatch):
    paired([401])
    real_request = client._request

    def re_paired_during_the_send(url, **kwargs):
        # The phone pairs this profile again while the old credential's
        # delivery is on its way and gets refused.
        state = client.load_state()
        client.save_state({**state, "credential": "inst.gw2.new", "gateway_id": "gw2"})
        return real_request(url, **kwargs)

    monkeypatch.setattr(client, "_request", re_paired_during_the_send)
    _deliver(_event())

    state = client.load_state()
    assert state["credential"] == "inst.gw2.new"
    assert "relay_rejected_at" not in state


def test_a_profile_unpaired_between_retries_stops_retrying(paired, monkeypatch):
    relay = paired([503, 503])
    real_request = client._request

    def unpair_after_first(url, **kwargs):
        try:
            return real_request(url, **kwargs)
        finally:
            client.remove_state()

    monkeypatch.setattr(client, "_request", unpair_after_first)
    _deliver(_event())

    assert len(relay.event_ids) == 1


@pytest.mark.parametrize("status", [401, 403, BARE_403])
def test_a_refusal_parks_notifications_until_the_pause_runs_out(paired, status):
    relay = paired([status, status])
    now = [0.0]
    clock = lambda: now[0]

    assert client._deliver(_event(), clock=clock) is False
    # Refused the same way: the next events don't reach the relay at all.
    assert client._deliver(_event(), clock=clock) is False
    now[0] += client.REFUSED_FIRST_S - 1
    assert client._deliver(_event(), clock=clock) is False
    assert len(relay.event_ids) == 1

    # The first event after the pause is the probe; refused again, the pause doubles.
    now[0] += 1
    assert client._deliver(_event(), clock=clock) is False
    assert len(relay.event_ids) == 2
    now[0] += client.REFUSED_FIRST_S
    assert client._deliver(_event(), clock=clock) is False
    assert len(relay.event_ids) == 2

    # Accepted: the pause is over, the next event goes straight out.
    now[0] += client.REFUSED_FIRST_S
    assert client._deliver(_event(), clock=clock) is True
    assert client._deliver(_event(), clock=clock) is True
    assert len(relay.event_ids) == 4


def test_the_refusal_pause_doubles_up_to_its_cap(paired):
    relay = paired([401] * 20)
    now = [0.0]
    pauses = []
    for _ in range(10):
        client._deliver(_event(), clock=lambda: now[0])
        pauses.append(client._refusals[CREDENTIAL]["paused_for"])
        now[0] = client._refusals[CREDENTIAL]["until"]
    assert pauses[:3] == [client.REFUSED_FIRST_S, client.REFUSED_FIRST_S * 2, client.REFUSED_FIRST_S * 4]
    assert max(pauses) == pauses[-1] == client.REFUSED_MAX_S
    assert len(relay.event_ids) == 10


def test_pairing_again_is_not_held_by_the_old_pause(paired):
    relay = paired([401])
    client._deliver(_event(), clock=lambda: 0.0)
    state = client.load_state()
    client.save_state({**state, "credential": "inst.gw2.new", "gateway_id": "gw2"})

    assert client._deliver(_event(), clock=lambda: 1.0) is True
    assert len(relay.event_ids) == 2


def test_a_reload_starts_unpaused(paired):
    relay = paired([401])
    client._deliver(_event(), clock=lambda: 0.0)
    client.stop(timeout=0)
    client.start()

    assert client._deliver(_event(), clock=lambda: 1.0) is True
    assert len(relay.event_ids) == 2


def test_a_bare_403_says_the_host_is_blocked_not_to_pair_again(paired, capsys):
    paired([BARE_403])

    _deliver(_event())

    state = client.load_state()
    assert state["relay_blocked_at"] and "relay_rejected_at" not in state
    assert api.e2e_status()["relay_blocked"] is True and api.e2e_status()["relay_rejected"] is False
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    out = capsys.readouterr().out
    assert "empty 403" in out and "Pairing again won't help" in out and "ask whoever runs the relay" in out
    assert "pair again from Conduit" not in out

    client._refusals[CREDENTIAL]["until"] = 0.0
    _deliver(_event())

    assert "relay_blocked_at" not in client.load_state()
    assert api.e2e_status()["relay_blocked"] is False
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    assert "empty 403" not in capsys.readouterr().out


def test_the_relays_own_403_is_not_a_block(paired):
    paired([403])

    _deliver(_event())

    state = client.load_state()
    assert "relay_blocked_at" not in state and "relay_rejected_at" not in state


def test_the_test_command_names_a_block_in_front_of_the_relay(paired, capsys, monkeypatch):
    paired([BARE_403])
    monkeypatch.setattr(cli, "send_now", client.send_now)

    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="test", corrupt=False)) == 1

    out = capsys.readouterr().out
    assert "in front of the relay" in out and "ask whoever runs the relay" in out


def test_each_pairing_keeps_its_own_pause(paired):
    relay = paired([401, 401, 401])
    first = client.load_state()
    second = {**first, "credential": "inst.gw2.other", "gateway_id": "gw2"}

    # Two profiles in one process are refused in turn: neither evicts the
    # other's pause, and each one's pause doubles on its own.
    client._deliver(_event(), clock=lambda: 0.0)
    client.save_state(second)
    client._deliver(_event(), clock=lambda: 1.0)
    client.save_state(first)
    assert client._deliver(_event(), clock=lambda: 2.0) is False
    assert len(relay.event_ids) == 2
    client._deliver(_event(), clock=lambda: client.REFUSED_FIRST_S)
    assert client._refusals[CREDENTIAL]["paused_for"] == client.REFUSED_FIRST_S * 2
    assert client._refusals["inst.gw2.other"]["paused_for"] == client.REFUSED_FIRST_S

    # An accepted delivery for one ends only its own pause.
    client._clear_refusal("inst.gw2.other")
    assert CREDENTIAL in client._refusals and "inst.gw2.other" not in client._refusals


def test_a_refusal_in_flight_across_a_reload_does_not_pause_it(paired, monkeypatch):
    relay = paired([401])
    real_request = client._request

    def reload_during_the_send(url, **kwargs):
        client.stop(timeout=0)
        client.start()
        return real_request(url, **kwargs)

    monkeypatch.setattr(client, "_request", reload_during_the_send)
    client._deliver(_event(), clock=lambda: 0.0)
    monkeypatch.setattr(client, "_request", real_request)

    assert client._refusals == {}
    assert client._deliver(_event(), clock=lambda: 1.0) is True
    assert len(relay.event_ids) == 2


@pytest.mark.parametrize("body, blocked", [(b"", True), (b"<html>Forbidden</html>", True),
                                            (b'{"error": "forb', False), (b'{"error":"token_proof_required"}', False)])
def test_only_an_empty_or_non_json_403_is_a_block(monkeypatch, body, blocked):
    import io

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(client._opener, "open", refuse)
    with pytest.raises(client.RelayRejected) as caught:
        client.request_json("https://relay.example/v1/events", method="POST", payload={})
    assert caught.value.edge_blocked is blocked


def test_the_test_command_records_what_it_saw(paired, monkeypatch):
    paired([401, 200])
    monkeypatch.setattr(cli, "send_now", client.send_now)

    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="test", corrupt=False)) == 1
    assert client.load_state()["relay_rejected_at"]
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="test", corrupt=False)) == 0
    assert "relay_rejected_at" not in client.load_state()


def test_a_bom_before_the_relays_json_is_not_a_block():
    assert client.looks_like_json_object(b'\xef\xbb\xbf{"error":"forbidden"}')
    assert api._looks_like_json_object(b' \xef\xbb\xbf{"error"')
    assert not client.looks_like_json_object(b"") and not api._looks_like_json_object(b"<html>")


def test_the_newest_kind_of_refusal_replaces_the_other(paired, capsys):
    paired([401, BARE_403, 401])

    client._deliver(_event(), clock=lambda: 0.0)
    assert client.load_state()["relay_rejected_at"]
    client._deliver(_event(), clock=lambda: 10_000.0)
    state = client.load_state()
    assert state["relay_blocked_at"] and "relay_rejected_at" not in state
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    assert "pair again from Conduit" not in capsys.readouterr().out
    client._deliver(_event(), clock=lambda: 20_000.0)
    state = client.load_state()
    assert state["relay_rejected_at"] and "relay_blocked_at" not in state


def test_cli_pair_names_a_block_in_front_of_the_relay(monkeypatch, capsys):
    def blocked(code, relay_url, name):
        raise client.RelayRejected(403, "request_rejected", from_relay=False)

    monkeypatch.setattr(cli, "claim_pairing", blocked)
    args = types.SimpleNamespace(conduit_push_action="pair", code="ABCD", relay_url=client.DEFAULT_RELAY_URL, name="")
    assert cli.dispatch(args) == 1
    assert "support@milim.dev" in capsys.readouterr().out


def test_one_write_swaps_the_refusal_marks(paired):
    paired([])
    state = client.load_state()
    client.save_state({**state, "relay_rejected_at": "2026-10-21T00:00:00Z"})
    client.record_relay_refusal(client.load_state(), client.RelayRejected(403, "", from_relay=False))
    state = client.load_state()
    assert state["relay_blocked_at"] and "relay_rejected_at" not in state


def test_enqueue_reports_a_paused_pairing_as_dropped(paired, monkeypatch):
    relay = paired([401])
    _deliver(_event())

    # The clarify middleware falls back at once instead of polling a decision
    # the relay will never park.
    assert client.enqueue(_event()) is False
    assert len(relay.event_ids) == 1
    client._refusals[CREDENTIAL]["until"] = 0.0
    assert client.enqueue(_event()) is True
    client._events.join()
    assert len(relay.event_ids) == 2


def test_an_accepted_test_ends_the_pause(paired):
    relay = paired([BARE_403])
    _deliver(_event())
    assert client._refusals

    client.send_now(_event())

    assert client._refusals == {}
    assert len(relay.event_ids) == 2


def test_an_edge_block_says_so_in_its_message():
    assert "in front of the Conduit relay" in str(client.RelayRejected(403, "", from_relay=False))
    assert "relay rejected the request: nope (403)" in str(client.RelayRejected(403, "nope"))


def test_a_bare_401_is_not_called_a_refused_credential(paired):
    paired([])
    client.record_relay_refusal(client.load_state(), client.RelayRejected(401, "", from_relay=False))
    state = client.load_state()
    assert "relay_rejected_at" not in state and "relay_blocked_at" not in state


def test_notifications_paused_follows_this_profiles_pairing(paired):
    paired([401])
    assert client.notifications_paused() is False
    _deliver(_event())
    assert client.notifications_paused() is True
    client._refusals[CREDENTIAL]["until"] = 0.0
    assert client.notifications_paused() is False
