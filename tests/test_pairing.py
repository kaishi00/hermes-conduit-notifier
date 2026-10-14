import importlib.util
import json
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants

spec = importlib.util.spec_from_file_location("conduit_plugin_api_pairing", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

CODE = "ABCDE-FGH23"


def _claimed(**extra):
    body = {"installation_id": "inst-1", "gateway_id": "gw-1", "credential": "inst-1.gw-1.secret",
            "relay_url": "https://relay.example"}
    body.update(extra)
    return body


def _recording_claim(calls, answer=None):
    def claim(relay_url, code, name):
        calls.append((relay_url, code, name))
        return answer or _claimed()
    return claim


@pytest.fixture(autouse=True)
def _short_hostname(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "box")


def test_pairing_claims_the_code_and_saves_the_credential(tmp_path):
    path = tmp_path / "conduit-push.json"
    calls = []
    result = api.pair_profile({"pairing_code": " abcde-fgh23 ", "relay_url": "https://relay.example/"},
                              path=path, claim=_recording_claim(calls))
    assert calls == [("https://relay.example", CODE, calls[0][2])]
    assert calls[0][2].endswith("Hermes")
    state = json.loads(path.read_text())
    assert state["credential"] == "inst-1.gw-1.secret"
    assert state["installation_id"] == "inst-1" and state["gateway_id"] == "gw-1"
    assert state["relay_url"] == "https://relay.example"
    assert "credential" not in result
    assert result == {"paired": True, "installation_id": "inst-1", "gateway_id": "gw-1",
                      "gateway_name": calls[0][2], "relay_url": "https://relay.example"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_control_characters_leave_the_gateway_name(tmp_path):
    calls = []
    api.pair_profile({"pairing_code": CODE, "gateway_name": "box\r\nX: y"}, path=tmp_path / "s.json",
                     claim=_recording_claim(calls))
    assert calls[0][2] == "boxX: y"


def test_a_named_profile_is_in_the_gateway_name(tmp_path):
    calls = []
    api.pair_profile({"pairing_code": CODE}, profile="coder", path=tmp_path / "s.json", claim=_recording_claim(calls))
    assert calls[0][2].endswith("Hermes (coder)")


def test_without_a_relay_url_the_current_then_default_relay_is_used(tmp_path):
    path = tmp_path / "s.json"
    calls = []
    api.pair_profile({"pairing_code": CODE}, path=path, claim=_recording_claim(calls, _claimed(relay_url=None)))
    assert calls[-1][0] == api.DEFAULT_RELAY_URL
    path.write_text(json.dumps({"credential": "old", "relay_url": "https://own.relay"}))
    api.pair_profile({"pairing_code": CODE}, path=path, claim=_recording_claim(calls, _claimed(relay_url=None)))
    assert calls[-1][0] == "https://own.relay"
    assert json.loads(path.read_text())["relay_url"] == "https://own.relay"


def test_re_pairing_keeps_the_privacy_choice_and_drops_the_old_key(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "old", "redact_content": True, "redact_key": "k",
                                "e2e": {"kid": "x", "secret": "y"}}))
    api.pair_profile({"pairing_code": CODE}, path=path, claim=_recording_claim([]))
    state = json.loads(path.read_text())
    assert state["redact_content"] is True and state["redact_key"] == "k"
    assert "e2e" not in state and state["credential"] == "inst-1.gw-1.secret"


def test_re_pairing_the_same_phone_and_gateway_keeps_the_key(tmp_path):
    path = tmp_path / "s.json"
    key = {"kid": "x", "secret": "y"}
    path.write_text(json.dumps({"credential": "old", "installation_id": "inst-1", "gateway_id": "gw-1", "e2e": key}))
    api.pair_profile({"pairing_code": CODE}, path=path, claim=_recording_claim([]))
    assert json.loads(path.read_text())["e2e"] == key


def test_a_stored_relay_that_fails_the_check_is_not_used(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"credential": "old", "relay_url": "file:///etc"}))
    calls = []
    api.pair_profile({"pairing_code": CODE}, path=path, claim=_recording_claim(calls, _claimed(relay_url="http://[")))
    assert calls[0][0] == api.DEFAULT_RELAY_URL
    assert json.loads(path.read_text())["relay_url"] == api.DEFAULT_RELAY_URL


def test_a_relay_named_url_with_extras_is_not_stored(tmp_path):
    path = tmp_path / "s.json"
    api.pair_profile({"pairing_code": CODE, "relay_url": "https://relay.example"}, path=path,
                     claim=_recording_claim([], _claimed(relay_url="https://u:p@evil?x=1")))
    assert json.loads(path.read_text())["relay_url"] == "https://relay.example"


@pytest.mark.parametrize("body", [
    {}, {"pairing_code": "ABCDE"}, {"pairing_code": "ABCDE-FGHI1"}, {"pairing_code": 12},
    {"pairing_code": CODE, "relay_url": "ftp://relay"},
    {"pairing_code": CODE, "relay_url": "https://user:pw@relay"},
    {"pairing_code": CODE, "relay_url": "https://relay?x=1"},
    {"pairing_code": CODE, "relay_url": 5},
    {"pairing_code": CODE, "relay_url": "https://relay.example/a\r\nb"},
    {"pairing_code": CODE, "relay_url": "https://relay.example/\u00fc"},
    {"pairing_code": CODE, "relay_url": "https://[::1"},
])
def test_malformed_requests_are_refused_without_claiming(tmp_path, body):
    calls = []
    with pytest.raises(api.TokenError) as raised:
        api.pair_profile(body, path=tmp_path / "s.json", claim=_recording_claim(calls),
                         limit=lambda: calls.append("limited"))
    assert raised.value.status == 400
    assert calls == []
    assert not (tmp_path / "s.json").exists()


class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self, _limit):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_claim_posts_only_the_code_and_name(monkeypatch):
    seen = {}

    def fake_open(request, timeout):
        seen.update(url=request.full_url, body=json.loads(request.data), auth=request.get_header("Authorization"))
        return _Response(200, _claimed())

    monkeypatch.setattr(api._relay_opener, "open", fake_open)
    assert api._claim_pairing("https://relay.example", CODE, "box Hermes")["installation_id"] == "inst-1"
    assert seen == {"url": "https://relay.example/v1/pairings/claim",
                    "body": {"pairing_code": CODE, "gateway_name": "box Hermes"}, "auth": None}


def test_any_2xx_claim_answer_is_accepted(monkeypatch):
    monkeypatch.setattr(api._relay_opener, "open", lambda request, timeout: _Response(201, _claimed()))
    assert api._claim_pairing("https://relay.example", CODE, "n")["gateway_id"] == "gw-1"


def test_a_refused_claim_does_not_echo_the_relay_status(monkeypatch):
    monkeypatch.setattr(api._relay_opener, "open", lambda request, timeout: _Response(302, {}))
    with pytest.raises(api.TokenError) as raised:
        api._claim_pairing("https://relay.example", CODE, "n")
    assert raised.value.status == 502 and "302" not in str(raised.value)


def test_a_broken_relay_answer_reads_as_unreachable(monkeypatch):
    def fake_open(request, timeout):
        raise api.http.client.IncompleteRead(b"")

    monkeypatch.setattr(api._relay_opener, "open", fake_open)
    with pytest.raises(api.TokenError) as raised:
        api._claim_pairing("https://relay.example", CODE, "n")
    assert raised.value.status == 502


def test_a_broken_relay_error_body_still_reads_as_refused(monkeypatch):
    class Broken(api.urllib.error.HTTPError):
        def read(self, *_):
            raise api.http.client.IncompleteRead(b"")

    def fake_open(request, timeout):
        raise Broken(request.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(api._relay_opener, "open", fake_open)
    with pytest.raises(api.TokenError) as raised:
        api._claim_pairing("https://relay.example", CODE, "n")
    assert raised.value.status == 502


def test_an_expired_code_reads_as_gone(monkeypatch):
    def fake_open(request, timeout):
        raise api.urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(api._relay_opener, "open", fake_open)
    with pytest.raises(api.TokenError) as raised:
        api._claim_pairing("https://relay.example", CODE, "n")
    assert raised.value.status == 410


def test_the_route_answers_without_the_credential(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_pairing_state_path", lambda: tmp_path / "s.json")
    monkeypatch.setattr(api, "_claim_pairing", lambda relay, code, name: _claimed())
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).post(f"{BASE}/pair", json={"pairing_code": CODE})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["ok"] is True and "credential" not in response.json()
    bad = TestClient(app).post(f"{BASE}/pair", json={"pairing_code": "nope"})
    assert bad.status_code == 400


def test_a_plain_http_relay_on_a_public_address_is_refused_before_claiming(tmp_path):
    calls = []
    with pytest.raises(api.TokenError) as raised:
        api.pair_profile({"pairing_code": CODE, "relay_url": "http://relay.example"}, path=tmp_path / "s.json",
                         claim=_recording_claim(calls))
    assert raised.value.status == 400 and "https://" in str(raised.value)
    assert calls == []


@pytest.mark.parametrize("claim_at", ["http://100.101.102.103:8787", "http://192.168.1.10:8787",
                                      "http://relay.tail1234.ts.net:8787", "https://relay.tail1234.ts.net"])
def test_a_self_hosted_relay_is_claimed_over_the_lan_and_saved_at_its_https_url(tmp_path, claim_at):
    path = tmp_path / "s.json"
    calls = []
    api.pair_profile({"pairing_code": CODE, "relay_url": claim_at}, path=path,
                     claim=_recording_claim(calls, _claimed(relay_url="https://push.home.example")))
    assert calls[0][0] == claim_at
    assert json.loads(path.read_text())["relay_url"] == "https://push.home.example"


def test_a_public_relay_naming_another_origin_keeps_the_claimed_url(tmp_path):
    path = tmp_path / "s.json"
    api.pair_profile({"pairing_code": CODE, "relay_url": "https://relay.example"}, path=path,
                     claim=_recording_claim([], _claimed(relay_url="https://elsewhere.example")))
    assert json.loads(path.read_text())["relay_url"] == "https://relay.example"


def test_a_relay_naming_plain_http_is_not_saved(tmp_path):
    path = tmp_path / "s.json"
    api.pair_profile({"pairing_code": CODE, "relay_url": "https://relay.example"}, path=path,
                     claim=_recording_claim([], _claimed(relay_url="http://relay.example")))
    assert json.loads(path.read_text())["relay_url"] == "https://relay.example"
    # Claimed over the LAN with no HTTPS address to fall back on: nothing is saved.
    path2 = tmp_path / "s2.json"
    with pytest.raises(api.TokenError) as raised:
        api.pair_profile({"pairing_code": CODE, "relay_url": "http://192.168.1.10:8787"}, path=path2,
                         claim=_recording_claim([], _claimed(relay_url="http://192.168.1.10:8787")))
    assert raised.value.status == 502 and "PUBLIC_URL" in str(raised.value)
    assert not path2.exists()
