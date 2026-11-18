"""A profile paired with several devices (hermes-conduit#2758): each keeps its
own pairing, notifications go to every one, a question resolves once, and a
device is removed or made the main one on its own."""

import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
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


client = _private_copy("conduit_push._client_devices_under_test", ROOT / "client.py")
api = _private_copy("conduit_plugin_api_devices", ROOT / "dashboard" / "plugin_api.py")

CODE = "ABCDE-FGH23"
PHONE = "11111111-1111-4111-8111-111111111111"
IPAD = "22222222-2222-4222-8222-222222222222"
MAC = "33333333-3333-4333-8333-333333333333"
RELAY = "https://relay.example"


def _pairing(installation, gateway, **extra):
    return {"credential": f"{installation}.{gateway}.secret", "installation_id": installation, "gateway_id": gateway,
            "relay_url": RELAY, **extra}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_pairing_state_path", lambda: tmp_path / "conduit-push.json")
    monkeypatch.setattr(client, "get_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def revoked(monkeypatch):
    calls = []

    def relay_request(url, method, credential, payload, timeout):
        calls.append((url, method, credential))
        return 204, {}

    monkeypatch.setattr(api.watch_tools, "_relay_request", relay_request)
    monkeypatch.setattr(api.pairing, "_start_revoke_thread", lambda target: target())
    return calls


def _pair(home, installation, gateway, **body):
    claimed = _pairing(installation, gateway)
    return api.pairing.pair_profile({"pairing_code": CODE, "relay_url": RELAY, **body}, path=home / "conduit-push.json",
                                    claim=lambda relay, code, name: dict(claimed))


def _read(path):
    return json.loads(path.read_text())


def _device_file(home, installation):
    return home / "conduit-push-devices" / f"{installation}.json"


# --- Pairing from Conduit ------------------------------------------------------


def test_another_device_pairs_besides_the_main_one(home, revoked):
    assert _pair(home, PHONE, "gw-1", device_name="iPhone")["main"] is True
    result = _pair(home, IPAD, "gw-2", also=True, device_name="iPad\nPro")
    assert result["main"] is False
    assert _read(home / "conduit-push.json")["installation_id"] == PHONE
    ipad = _read(_device_file(home, IPAD))
    assert ipad["gateway_id"] == "gw-2" and ipad["device_name"] == "iPad Pro" and ipad["paired_at"]
    assert oct(_device_file(home, IPAD).stat().st_mode & 0o777) == "0o600"
    assert revoked == []  # nobody's pairing was replaced
    listed = api.pairing.paired_devices()
    assert [(entry["installation_id"], entry["main"], entry["device_name"]) for entry in listed] == [
        (PHONE, True, "iPhone"), (IPAD, False, "iPad Pro")]
    assert all("credential" not in entry for entry in listed)


def test_the_first_device_is_the_main_one_even_when_it_asks_to_be_another(home, revoked):
    assert _pair(home, PHONE, "gw-1", also=True)["main"] is True
    assert not (home / "conduit-push-devices").exists()


def test_pairing_the_same_device_again_replaces_only_its_own_pairing(home, revoked):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True)
    _pair(home, IPAD, "gw-3", also=True)
    assert _read(_device_file(home, IPAD))["gateway_id"] == "gw-3"
    assert _read(home / "conduit-push.json")["gateway_id"] == "gw-1"
    assert revoked == [(f"{RELAY}/v1/gateways/current", "DELETE", f"{IPAD}.gw-2.secret")]


def test_an_older_conduit_still_moves_the_main_pairing(home, revoked):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True)
    result = _pair(home, MAC, "gw-3")  # no "also": the main pairing moves, as before
    assert result["main"] is True
    assert _read(home / "conduit-push.json")["installation_id"] == MAC
    assert _read(_device_file(home, IPAD))["gateway_id"] == "gw-2"  # the others keep theirs
    assert revoked == [(f"{RELAY}/v1/gateways/current?replaced_by={MAC}", "DELETE", f"{PHONE}.gw-1.secret")]


def test_a_device_among_the_others_that_takes_the_main_pairing_leaves_the_others(home, revoked):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True)
    _pair(home, IPAD, "gw-3")  # without "also": the main pairing
    assert _read(home / "conduit-push.json")["gateway_id"] == "gw-3"
    assert not _device_file(home, IPAD).exists()
    assert sorted(credential for _url, _method, credential in revoked) == [f"{PHONE}.gw-1.secret", f"{IPAD}.gw-2.secret"]


def test_status_and_keys_are_each_devices_own(home, revoked):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True)
    status = api.pairing.e2e_status(installation_id=IPAD)
    assert status["installation_id"] == IPAD and status["gateway_id"] == "gw-2" and status["main"] is False
    assert len(status["devices"]) == 2
    # An older Conduit asks without its id: the main pairing, as before.
    assert api.pairing.e2e_status()["installation_id"] == PHONE
    # A device the profile isn't paired with gets the main pairing's status.
    assert api.pairing.e2e_status(installation_id=MAC)["installation_id"] == PHONE
    kid, secret = "a" * 32, "A" * 43
    api.pairing.provision_e2e({"installation_id": IPAD, "gateway_id": "gw-2", "kid": kid, "secret": secret})
    assert _read(_device_file(home, IPAD))["e2e"]["kid"] == kid
    assert "e2e" not in _read(home / "conduit-push.json")
    with pytest.raises(api.core.TokenError) as refused:
        api.pairing.provision_e2e({"installation_id": MAC, "gateway_id": "gw-9", "kid": kid, "secret": secret})
    assert refused.value.status == 409


def test_removing_another_device_leaves_the_main_one(home, revoked):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True)
    result = api.pairing.remove_paired_device({"installation_id": IPAD})
    assert [entry["installation_id"] for entry in result["devices"]] == [PHONE]
    assert not _device_file(home, IPAD).exists()
    # The main device rings for calls: the Watch's ring may follow it there.
    assert revoked == [(f"{RELAY}/v1/gateways/current?replaced_by={PHONE}", "DELETE", f"{IPAD}.gw-2.secret")]
    with pytest.raises(api.core.TokenError) as missing:
        api.pairing.remove_paired_device({"installation_id": IPAD})
    assert missing.value.status == 404


def test_removing_the_main_device_hands_its_place_to_the_first_other(home, revoked):
    (home / "conduit-push.json").write_text(json.dumps(_pairing(PHONE, "gw-1", redact_content=True, redact_key="k" * 64)))
    _pair(home, IPAD, "gw-2", also=True)
    _pair(home, MAC, "gw-3", also=True)
    result = api.pairing.remove_paired_device({"installation_id": PHONE})
    main = _read(home / "conduit-push.json")
    assert main["installation_id"] == IPAD and main["redact_content"] is True and main["redact_key"] == "k" * 64
    assert not _device_file(home, IPAD).exists() and _device_file(home, MAC).exists()
    assert [(entry["installation_id"], entry["main"]) for entry in result["devices"]] == [(IPAD, True), (MAC, False)]
    assert revoked == [(f"{RELAY}/v1/gateways/current?replaced_by={IPAD}", "DELETE", f"{PHONE}.gw-1.secret")]


def test_removing_the_only_device_unpairs_the_profile(home, revoked):
    _pair(home, PHONE, "gw-1")
    assert api.pairing.remove_paired_device({"installation_id": PHONE})["devices"] == []
    assert not (home / "conduit-push.json").exists()


def test_another_device_becomes_the_main_one_without_pairing_again(home, revoked):
    (home / "conduit-push.json").write_text(json.dumps(_pairing(PHONE, "gw-1", redact_content=True, redact_key="k" * 64)))
    _pair(home, IPAD, "gw-2", also=True)
    result = api.pairing.make_main_device({"installation_id": IPAD})
    main, phone = _read(home / "conduit-push.json"), _read(_device_file(home, PHONE))
    assert main["installation_id"] == IPAD and main["redact_content"] is True
    assert phone["gateway_id"] == "gw-1" and "redact_content" not in phone
    assert not _device_file(home, IPAD).exists()
    assert [(entry["installation_id"], entry["main"]) for entry in result["devices"]] == [(IPAD, True), (PHONE, False)]
    assert revoked == []


def test_a_removed_device_isnt_paired_again_by_itself(home, revoked):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True, auto=True)
    api.pairing.remove_paired_device({"installation_id": IPAD})
    with pytest.raises(api.core.TokenError) as refused:
        _pair(home, IPAD, "gw-3", also=True, auto=True, installation_id=IPAD)
    assert refused.value.status == 409
    assert not _device_file(home, IPAD).exists()
    # Conduit that doesn't name itself: the claim tells, and its gateway goes.
    revoked.clear()
    with pytest.raises(api.core.TokenError):
        _pair(home, IPAD, "gw-4", also=True, auto=True)
    assert revoked == [(f"{RELAY}/v1/gateways/current", "DELETE", f"{IPAD}.gw-4.secret")]
    # Paired by hand, it's back, and pairs by itself again from then on.
    _pair(home, IPAD, "gw-5", also=True)
    assert _read(_device_file(home, IPAD))["gateway_id"] == "gw-5"
    _pair(home, IPAD, "gw-6", also=True, auto=True, installation_id=IPAD)
    assert _read(_device_file(home, IPAD))["gateway_id"] == "gw-6"


def test_device_routes_refuse_what_isnt_an_installation(home, revoked):
    _pair(home, PHONE, "gw-1")
    for call in (api.pairing.remove_paired_device, api.pairing.make_main_device):
        with pytest.raises(api.core.TokenError) as refused:
            call({"installation_id": "../conduit-push"})
        assert refused.value.status == 400


def test_the_waiting_list_names_each_devices_gateway(home, revoked, monkeypatch):
    _pair(home, PHONE, "gw-1")
    _pair(home, IPAD, "gw-2", also=True)
    monkeypatch.setattr(api.core, "_calls_home", lambda: home)
    listed = api.waiting.waiting_list()
    assert listed["gateway_id"] == "gw-1"
    assert listed["gateways"] == {PHONE: "gw-1", IPAD: "gw-2"}


# --- Sending from the plugin ---------------------------------------------------


@pytest.fixture
def queued(home, monkeypatch):
    import queue

    monkeypatch.setattr(client, "_events", queue.PriorityQueue(maxsize=16))
    monkeypatch.setattr(client, "_worker_started", True)  # nothing delivers
    client._clear_refusal()
    return client._events


def _write_pairings(home, *, others=(IPAD,), main_extra=None):
    (home / "conduit-push.json").write_text(json.dumps(_pairing(PHONE, "gw-1", **(main_extra or {}))))
    for number, installation in enumerate(others, start=2):
        path = _device_file(home, installation)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(_pairing(installation, f"gw-{number}", paired_at=f"2026-11-18T00:00:0{number}Z")))


def _drain(events):
    items = []
    while not events.empty():
        items.append(events.get_nowait())
    return items


def test_a_notification_goes_to_every_device(home, queued):
    _write_pairings(home, others=(IPAD, MAC))
    assert client.enqueue({"type": "response.ready"}) is True
    items = _drain(queued)
    states = [item[2].run(client.load_state) for item in items]
    assert [state["gateway_id"] for state in states] == ["gw-1", "gw-2", "gw-3"]
    assert [item[4][2] for item in items] == ["gw-1", "gw-2", "gw-3"]


def test_a_phone_change_stays_with_the_main_device(home, queued):
    _write_pairings(home)
    assert client.enqueue({"type": "phone.changes"}) is True
    assert [item[4][2] for item in _drain(queued)] == ["gw-1"]


def test_a_paused_device_doesnt_hold_up_the_others(home, queued):
    _write_pairings(home)
    client._refusals[f"{PHONE}.gw-1.secret"] = {"paused_for": 60, "until": 1e18}
    try:
        assert client.notifications_paused() is False
        assert client.enqueue({"type": "response.ready"}) is True
        assert [item[4][2] for item in _drain(queued)] == ["gw-2"]
        client._refusals[f"{IPAD}.gw-2.secret"] = {"paused_for": 60, "until": 1e18}
        assert client.notifications_paused() is True
        assert client.enqueue({"type": "response.ready"}) is False
    finally:
        client._clear_refusal()


def test_another_devices_pairing_lasts_only_with_the_main_one(home, queued):
    _write_pairings(home)
    (home / "conduit-push.json").unlink()
    assert client.paired_devices() == []
    with client.for_device(IPAD):
        assert client.load_state() is None
    assert client.enqueue({"type": "response.ready"}) is False


def test_another_device_sends_under_the_profiles_privacy_choice(home):
    _write_pairings(home, main_extra={"redact_content": True, "redact_key": "k" * 64})
    with client.for_device(IPAD):
        state = client.load_state()
        assert "redact_content" not in state
        outgoing = client.build_outgoing({"type": "approval.needed", "event_id": "approval:abc", "title": "rm -rf /",
                                          "body": "rm -rf /"}, state)
    assert "rm -rf" not in json.dumps(outgoing)


def test_a_question_answered_on_one_device_is_released_on_the_others(home, monkeypatch):
    _write_pairings(home, others=(IPAD, MAC))
    calls = []

    def relay(url, *, method, payload=None, credential="", timeout=15.0):
        calls.append((method, credential))
        if method == "DELETE":
            return {}
        if credential.startswith(IPAD):
            return {"status": "answered", "answer": "Yes"}
        return {"status": "pending"}

    monkeypatch.setattr(client, "request_json", relay)
    monkeypatch.setattr(client.threading, "Thread", lambda target, **kwargs: types.SimpleNamespace(start=target))
    assert client.poll_decision("clarify:1") == {"status": "answered", "answer": "Yes"}
    assert ("DELETE", f"{PHONE}.gw-1.secret") in calls and ("DELETE", f"{MAC}.gw-3.secret") in calls
    assert ("DELETE", f"{IPAD}.gw-2.secret") not in calls


def test_a_question_still_waits_while_one_device_has_it_parked(home, monkeypatch):
    _write_pairings(home)

    def relay(url, *, method, payload=None, credential="", timeout=15.0):
        if credential.startswith(PHONE):
            raise client.RelayRejected(404, "decision_not_found")
        return {"status": "pending", "remaining": ["q1"]}

    monkeypatch.setattr(client, "request_json", relay)
    assert client.poll_decision("clarify:2") == {"status": "pending", "remaining": ["q1"]}


def test_releasing_a_question_releases_it_everywhere(home, monkeypatch):
    _write_pairings(home)
    calls = []
    monkeypatch.setattr(client, "request_json",
                        lambda url, *, method, payload=None, credential="", timeout=15.0: calls.append((method, credential)) or {})
    assert client.cancel_decision("clarify:3") is True
    assert calls == [("DELETE", f"{PHONE}.gw-1.secret"), ("DELETE", f"{IPAD}.gw-2.secret")]


def test_unpairing_from_the_host_unpairs_every_device(home, monkeypatch):
    _write_pairings(home)
    calls = []
    monkeypatch.setattr(client, "_request", lambda url, *, method, payload=None, credential="", timeout=15.0:
                        calls.append((method, credential)) or {})
    monkeypatch.setattr(client, "request_json", lambda url, *, method, payload=None, credential="", timeout=15.0:
                        calls.append((method, credential)) or {})
    assert client.unpair() is True
    assert sorted(credential for _method, credential in calls) == [f"{PHONE}.gw-1.secret", f"{IPAD}.gw-2.secret"]
    assert not (home / "conduit-push.json").exists() and not _device_file(home, IPAD).exists()


def test_a_key_rotation_from_the_host_asks_every_device(home):
    key = {"kid": "a" * 32, "secret": "A" * 43, "created_at": "2026-11-18T00:00:00Z"}
    (home / "conduit-push.json").write_text(json.dumps(_pairing(PHONE, "gw-1", e2e=key)))
    _device_file(home, IPAD).parent.mkdir()
    _device_file(home, IPAD).write_text(json.dumps(_pairing(IPAD, "gw-2", e2e=key)))
    assert client.request_key_rotation() is True
    assert _read(home / "conduit-push.json")["rotate_requested_at"]
    assert _read(_device_file(home, IPAD))["rotate_requested_at"]


def test_a_device_file_can_only_hold_its_own_installation(home):
    _write_pairings(home)
    _device_file(home, MAC).write_text(json.dumps(_pairing(IPAD, "gw-9")))
    assert [installation for installation, _state in client.other_devices()] == [IPAD]
    with pytest.raises(ValueError):
        client.device_state_path("../conduit-push")
