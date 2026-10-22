"""Re-pairing revokes the credential it replaced at the relay, and a phone
whose relay gateway slots are full is told how to get out (#250)."""

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
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


def _private_copy(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _private_copy("conduit_push._client_repair_under_test", ROOT / "client.py")
cli = _private_copy("conduit_push._cli_repair_under_test", ROOT / "cli.py")
api = _private_copy("conduit_plugin_api_repair", ROOT / "dashboard" / "plugin_api.py")

CODE = "ABCDE-FGH23"
NEW = {"installation_id": "inst-1", "gateway_id": "gw-2", "credential": "inst-1.gw-2.new",
       "relay_url": "https://relay.example"}


@pytest.fixture
def relay_calls(monkeypatch):
    calls = []

    def relay_request(url, method, credential, payload, timeout):
        calls.append((url, method, credential))
        return 204, {}

    monkeypatch.setattr(api, "_relay_request", relay_request)
    # Runs the revoke inline, so it is checked without waiting.
    monkeypatch.setattr(api, "_start_revoke_thread", lambda target: target())
    return calls


def _pair(path, **body):
    return api.pair_profile({"pairing_code": CODE, **body}, path=path, claim=lambda relay, code, name: dict(NEW))


def test_re_pairing_revokes_the_replaced_credential_at_its_relay(tmp_path, relay_calls):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "inst-1.gw-1.old", "relay_url": "https://relay.example/"}))
    _pair(path)
    assert relay_calls == [("https://relay.example/v1/gateways/current", "DELETE", "inst-1.gw-1.old")]
    assert json.loads(path.read_text())["credential"] == "inst-1.gw-2.new"


def test_the_revoke_runs_on_its_own_daemon_thread(tmp_path, monkeypatch):
    # The real seam, with only Thread stubbed: a broken helper can't hide behind a patched one.
    calls, threads = [], []

    class Thread:
        def __init__(self, target, name, daemon):
            threads.append((name, daemon))
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(api, "_relay_request", lambda url, method, credential, payload, timeout:
                        calls.append((method, credential)) or (204, {}))
    monkeypatch.setattr(api.threading, "Thread", Thread)
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "inst-1.gw-1.old", "relay_url": "https://relay.example"}))
    _pair(path)
    assert threads == [("conduit-pair-revoke", True)]
    assert calls == [("DELETE", "inst-1.gw-1.old")]


def test_a_first_pairing_revokes_nothing(tmp_path, relay_calls):
    _pair(tmp_path / "s.json")
    assert relay_calls == []


def test_a_replaced_credential_on_plain_http_is_not_sent(tmp_path, relay_calls):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "inst-1.gw-1.old", "relay_url": "http://relay.example"}))
    _pair(path, relay_url="https://relay.example")
    assert relay_calls == []


def test_a_failed_revoke_leaves_the_new_pairing_working(tmp_path, monkeypatch):
    def relay_request(*args):
        raise OSError("down")

    monkeypatch.setattr(api, "_relay_request", relay_request)
    monkeypatch.setattr(api, "_start_revoke_thread", lambda target: target())
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "inst-1.gw-1.old", "relay_url": "https://relay.example"}))
    assert _pair(path)["paired"] is True
    assert json.loads(path.read_text())["credential"] == "inst-1.gw-2.new"


def test_no_thread_to_revoke_with_leaves_the_pairing_saved(tmp_path, monkeypatch):
    def no_thread(target):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(api, "_start_revoke_thread", no_thread)
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "inst-1.gw-1.old", "relay_url": "https://relay.example"}))
    assert _pair(path)["paired"] is True
    assert json.loads(path.read_text())["credential"] == "inst-1.gw-2.new"


def test_a_loopback_revoke_skips_the_proxy(monkeypatch):
    used = []

    class Opener:
        def __init__(self, name):
            self.name = name

        def open(self, request, timeout):
            used.append(self.name)
            raise api.urllib.error.HTTPError(request.full_url, 401, "u", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(api, "_relay_opener", Opener("proxied"))
    monkeypatch.setattr(api, "_direct_relay_opener", Opener("direct"))
    api._relay_request("http://127.0.0.1:8787/v1/gateways/current", "DELETE", "c", None, 1)
    api._relay_request("https://relay.example/v1/gateways/current", "DELETE", "c", None, 1)
    assert used == ["direct", "proxied"]


def test_a_full_gateway_list_is_not_called_rate_limiting(monkeypatch):
    def fake_open(request, timeout):
        raise api.urllib.error.HTTPError(request.full_url, 429, "Too Many", {},
                                         io.BytesIO(b'{"error":"gateway_limit_reached"}'))

    monkeypatch.setattr(api._relay_opener, "open", fake_open)
    with pytest.raises(api.TokenError) as raised:
        api._claim_pairing("https://relay.example", CODE, "n")
    assert raised.value.status == 409
    assert "too many pairings" in str(raised.value) and "Notifications" in str(raised.value)


def test_a_rate_limited_claim_still_says_try_again(monkeypatch):
    def fake_open(request, timeout):
        raise api.urllib.error.HTTPError(request.full_url, 429, "Too Many", {},
                                         io.BytesIO(b'{"error":"rate_limited"}'))

    monkeypatch.setattr(api._relay_opener, "open", fake_open)
    with pytest.raises(api.TokenError) as raised:
        api._claim_pairing("https://relay.example", CODE, "n")
    assert raised.value.status == 429


def _client_requests(monkeypatch, tmp_path, previous, revoke_error=None):
    path = tmp_path / "conduit-push.json"
    if previous is not None:
        path.write_text(json.dumps(previous))
    monkeypatch.setattr(client, "state_path", lambda: path)
    calls = []

    def request(url, *, method, payload=None, credential="", timeout):
        calls.append((url, method, credential))
        if method == "POST":
            return dict(NEW)
        if revoke_error is not None:
            raise revoke_error
        return {}

    monkeypatch.setattr(client, "_request", request)
    return path, calls


def test_the_cli_pairing_revokes_the_replaced_credential(monkeypatch, tmp_path):
    path, calls = _client_requests(monkeypatch, tmp_path,
                                   {"credential": "inst-1.gw-1.old", "relay_url": "https://relay.example"})
    client.claim_pairing(CODE, "https://relay.example", "box")
    assert calls[1:] == [("https://relay.example/v1/gateways/current", "DELETE", "inst-1.gw-1.old")]
    assert json.loads(path.read_text())["credential"] == "inst-1.gw-2.new"


def test_the_cli_pairing_survives_a_refused_revoke(monkeypatch, tmp_path):
    path, calls = _client_requests(monkeypatch, tmp_path,
                                   {"credential": "inst-1.gw-1.old", "relay_url": "https://relay.example"},
                                   revoke_error=client.RelayRejected(401, "unauthorized"))
    assert client.claim_pairing(CODE, "https://relay.example", "box")["credential"] == "inst-1.gw-2.new"
    assert json.loads(path.read_text())["credential"] == "inst-1.gw-2.new"


def test_the_cli_first_pairing_revokes_nothing(monkeypatch, tmp_path):
    _path, calls = _client_requests(monkeypatch, tmp_path, None)
    client.claim_pairing(CODE, "https://relay.example", "box")
    assert [method for _url, method, _credential in calls] == ["POST"]


def test_the_cli_names_the_way_out_of_a_full_gateway_list(monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise client.RelayRejected(429, "gateway_limit_reached")

    monkeypatch.setattr(cli, "claim_pairing", refuse)
    args = types.SimpleNamespace(conduit_push_action="pair", code=CODE, relay_url="https://relay.example", name="")
    assert cli.dispatch(args) == 1
    out = capsys.readouterr().out
    assert "gateway_limit_reached" in out and "turn notifications off and on" in out.lower()
