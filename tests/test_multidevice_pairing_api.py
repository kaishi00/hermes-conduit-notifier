"""Dashboard pairing selects the requesting installation, including key rotation."""
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
if "hermes_constants" not in sys.modules:
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = constants
spec = importlib.util.spec_from_file_location("conduit_plugin_api_multidevice", ROOT / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
CODE = "ABCDE-FGH23"
KID = "01" * 16
SECRET = "Ag" * 21 + "A"


def _claimed(installation="phone", gateway="gateway", credential=None):
    return {"installation_id": installation, "gateway_id": gateway,
            "credential": credential or f"{installation}.{gateway}.secret", "relay_url": "https://relay.example"}


def _pair(path, installation="phone", gateway="gateway", multi=True, credential=None):
    return api.pairing.pair_profile({"pairing_code": CODE, "multi_device": multi}, path=path,
        claim=lambda *_: _claimed(installation, gateway, credential))


def _sidecar(primary, installation):
    return primary.parent / "conduit-push-devices" / (hashlib.sha256(installation.encode()).hexdigest() + ".json")


@pytest.fixture
def state(tmp_path, monkeypatch):
    primary = tmp_path / "conduit-push.json"
    monkeypatch.setattr(api.pairing, "_e2e_limiter", api.core._MintLimiter(10, 60.0))
    monkeypatch.setattr(api.core, "_pairing_state_path", lambda: primary)
    monkeypatch.setattr(api.pairing, "_pair_gateway_name", lambda *_: "Hermes")
    revoked = []
    monkeypatch.setattr(api.pairing, "_revoke_replaced_pairing", lambda *args: revoked.append(args))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return primary, TestClient(app), revoked


def test_opted_in_pairing_adds_a_device_and_refreshes_only_its_own_slot(state):
    primary, http, revoked = state
    _pair(primary)
    phone = json.loads(primary.read_text())
    phone.update(redact_content=True, redact_key="privacy", e2e={"kid": KID, "secret": SECRET})
    primary.write_text(json.dumps(phone))
    _pair(primary, "tablet")
    tablet = _sidecar(primary, "tablet")
    second = json.loads(tablet.read_text())
    assert json.loads(primary.read_text()) == phone
    assert second["require_e2e"] is True and "e2e" not in second
    assert second["redact_content"] is True and second["redact_key"] == "privacy"
    assert revoked == []
    assert tablet.stat().st_mode & 0o777 == 0o600
    assert tablet.parent.stat().st_mode & 0o777 == 0o700
    _pair(primary, "tablet", "replacement")
    assert json.loads(tablet.read_text())["gateway_id"] == "replacement"
    assert len(revoked) == 1 and revoked[0][1] == "tablet.gateway.secret"
    assert json.loads(primary.read_text()) == phone
    assert len(list(tablet.parent.glob("*.json"))) == 1


def test_legacy_pairing_still_replaces_the_primary_and_preserves_other_devices(state):
    primary, _, revoked = state
    _pair(primary)
    _pair(primary, "tablet")
    tablet = _sidecar(primary, "tablet").read_text()
    _pair(primary, "legacy", multi=False)
    assert json.loads(primary.read_text())["installation_id"] == "legacy"
    assert _sidecar(primary, "tablet").read_text() == tablet
    assert len(revoked) == 1 and revoked[0][1] == "phone.gateway.secret"


def test_status_and_provisioning_select_only_the_requested_installation(state):
    primary, http, _ = state
    _pair(primary)
    _pair(primary, "tablet")
    body = {"installation_id": "tablet", "gateway_id": "gateway", "kid": KID, "secret": SECRET}
    response = http.post(f"{BASE}/e2e", json=body)
    assert response.status_code == 200, response.text
    assert "e2e" not in json.loads(primary.read_text())
    assert json.loads(_sidecar(primary, "tablet").read_text())["e2e"]["kid"] == KID
    status = http.get(f"{BASE}/e2e", params={"installation_id": "tablet"})
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json()["installation_id"] == "tablet" and status.json()["e2e"]["kid"] == KID
    assert SECRET not in status.text and "credential" not in status.text
    unknown = http.get(f"{BASE}/e2e", params={"installation_id": "unknown"})
    assert unknown.json()["paired"] is False and "installation_id" not in unknown.json()
    assert http.post(f"{BASE}/e2e", json={**body, "gateway_id": "wrong"}).status_code == 409
    assert http.post(f"{BASE}/e2e", json={**body, "installation_id": "unknown"}).status_code == 409
    primary.unlink()
    assert http.get(f"{BASE}/e2e", params={"installation_id": "tablet"}).json()["paired"] is True


@pytest.mark.parametrize("installation", ["", " ", "x" * 257, "line\nbreak"])
def test_status_rejects_invalid_installation_identifiers(state, installation):
    primary, http, _ = state
    _pair(primary)
    response = http.get(f"{BASE}/e2e", params={"installation_id": installation})
    assert response.status_code == 400


@pytest.mark.parametrize("multi", ["true", 1, None, [], {}])
def test_pairing_refuses_non_boolean_opt_in_before_spending_the_code(state, multi):
    primary, _, _ = state
    calls = []
    with pytest.raises(api.core.TokenError) as caught:
        api.pairing.pair_profile({"pairing_code": CODE, "multi_device": multi}, path=primary,
            claim=lambda *_: calls.append(True) or _claimed())
    assert caught.value.status == 400 and calls == []


def test_sidecar_key_rotation_never_changes_the_primary_key(state):
    primary, http, _ = state
    _pair(primary)
    _pair(primary, "tablet")
    first_body = {"installation_id": "phone", "gateway_id": "gateway", "kid": "02" * 16, "secret": SECRET}
    assert http.post(f"{BASE}/e2e", json=first_body).status_code == 200
    second_body = {**first_body, "installation_id": "tablet", "kid": KID}
    assert http.post(f"{BASE}/e2e", json=second_body).status_code == 200
    before = primary.read_text()
    module = api.pairing._e2e_module()
    old_secret = module.unb64u(SECRET)
    keys = module.derive_keys(KID, old_secret)
    request, pending = module.start_rotation(keys, installation_id="tablet", gateway_id="gateway",
                                            suite=module.SUITE_CLASSICAL, kid_new="03" * 16)
    response = http.post(f"{BASE}/e2e/rotate", json=request)
    assert response.status_code == 200, response.text
    new_secret = module.finish_rotation(response.json()["rotation"], keys, old_secret, pending,
                                        installation_id="tablet", gateway_id="gateway")
    tablet = json.loads(_sidecar(primary, "tablet").read_text())
    assert module.unb64u(tablet["e2e"]["secret"]) == new_secret
    assert tablet["e2e_previous"]["kid"] == KID
    assert primary.read_text() == before
    refused = http.post(f"{BASE}/e2e/rotate", json={**request, "installation_id": "phone"})
    assert refused.status_code == 409 and primary.read_text() == before


def test_a_sidecar_refresh_reuses_its_path_after_the_primary_is_removed(state):
    primary, http, revoked = state
    _pair(primary)
    _pair(primary, "tablet")
    body = {"installation_id": "tablet", "gateway_id": "gateway", "kid": KID, "secret": SECRET}
    assert http.post(f"{BASE}/e2e", json=body).status_code == 200
    primary.unlink()
    _pair(primary, "tablet", credential="fresh-credential")
    assert not primary.exists()
    tablet = json.loads(_sidecar(primary, "tablet").read_text())
    assert tablet["credential"] == "fresh-credential" and tablet["e2e"]["kid"] == KID
    assert len(revoked) == 1 and revoked[0][1] == "tablet.gateway.secret"


def test_e2e_does_not_trust_a_sidecar_with_another_installation(state):
    primary, http, _ = state
    _pair(primary)
    _pair(primary, "tablet")
    tablet_path = _sidecar(primary, "tablet")
    stored = json.loads(tablet_path.read_text())
    tablet_path.write_text(json.dumps({**stored, "installation_id": "another"}))
    before = tablet_path.read_text()
    assert http.get(f"{BASE}/e2e", params={"installation_id": "tablet"}).json()["paired"] is False
    body = {"installation_id": "tablet", "gateway_id": "gateway", "kid": KID, "secret": SECRET}
    assert http.post(f"{BASE}/e2e", json=body).status_code == 409
    assert tablet_path.read_text() == before


def test_pairing_refuses_a_symlinked_device_directory_without_writing_outside_the_profile(state, tmp_path):
    primary, _, revoked = state
    _pair(primary)
    before = primary.read_text()
    external = tmp_path / "outside"
    external.mkdir()
    (primary.parent / "conduit-push-devices").symlink_to(external, target_is_directory=True)
    with pytest.raises(api.core.TokenError) as caught:
        _pair(primary, "tablet")
    assert caught.value.status == 409
    assert list(external.iterdir()) == [] and primary.read_text() == before and revoked == []


def _concurrent_pair(primary, installation, ready, errors):
    try:
        def claim(*_):
            ready.wait(timeout=10)
            return _claimed(installation)
        api.pairing.pair_profile({"pairing_code": CODE, "multi_device": True}, path=primary, claim=claim)
    except Exception as exc:
        errors.put(repr(exc))
        raise


def test_concurrent_first_pairings_from_separate_processes_keep_both_devices(state):
    import multiprocessing
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process regression uses fork to inherit the isolated dashboard test setup")
    primary, _, _ = state
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Barrier(2)
    errors = ctx.Queue()
    processes = [ctx.Process(target=_concurrent_pair, args=(primary, installation, ready, errors))
                 for installation in ("phone", "tablet")]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
        assert all(process.exitcode == 0 for process in processes)
        states = [json.loads(primary.read_text())]
        states.extend(json.loads(path.read_text()) for path in (primary.parent / "conduit-push-devices").glob("*.json"))
        assert {record["installation_id"] for record in states} == {"phone", "tablet"}
        assert all(record["require_e2e"] is True for record in states)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        errors.close()


def test_legacy_refresh_of_an_existing_sidecar_cannot_leave_a_hidden_old_pairing(state):
    primary, _, revoked = state
    _pair(primary)
    _pair(primary, "tablet")
    before = primary.read_text()
    _pair(primary, "tablet", "refreshed", multi=False)
    assert primary.read_text() == before
    assert json.loads(_sidecar(primary, "tablet").read_text())["gateway_id"] == "refreshed"
    assert len(revoked) == 1 and revoked[0][1] == "tablet.gateway.secret"


def test_multi_device_pairing_refuses_capacity_before_spending_another_code(state):
    primary, _, _ = state
    for number in range(16):
        _pair(primary, f"device-{number}")
    calls = []
    with pytest.raises(api.core.TokenError) as caught:
        api.pairing.pair_profile({"pairing_code": CODE, "multi_device": True}, path=primary,
                                 claim=lambda *_: calls.append(True) or _claimed("overflow"))
    assert caught.value.status == 409 and calls == []


def test_known_installation_can_refresh_at_capacity(state):
    primary, _, revoked = state
    for number in range(16):
        _pair(primary, f"device-{number}")
    result = api.pairing.pair_profile({"pairing_code": CODE, "multi_device": True,
                                       "installation_id": "device-15"}, path=primary,
        claim=lambda *_: _claimed("device-15", "refreshed"))
    assert result["gateway_id"] == "refreshed"
    assert len(list(primary.parent.joinpath("conduit-push-devices").glob("*.json"))) == 15
    assert len(revoked) == 1 and revoked[0][1] == "device-15.gateway.secret"


def test_pairing_claim_must_match_the_requested_installation(state):
    primary, _, revoked = state
    _pair(primary)
    before = primary.read_text()
    with pytest.raises(api.core.TokenError) as caught:
        api.pairing.pair_profile({"pairing_code": CODE, "multi_device": True,
                                  "installation_id": "phone"}, path=primary,
            claim=lambda *_: _claimed("another"))
    assert caught.value.status == 409 and primary.read_text() == before
    assert len(revoked) == 1 and revoked[0][1] == "another.gateway.secret"


def test_capacity_is_rechecked_after_a_concurrent_pairing_and_unused_claim_is_revoked(state):
    primary, _, revoked = state
    for number in range(15):
        _pair(primary, f"device-{number}")

    def claim(*_):
        _pair(primary, "concurrent")
        return _claimed("overflow")

    with pytest.raises(api.core.TokenError) as caught:
        api.pairing.pair_profile({"pairing_code": CODE, "multi_device": True}, path=primary, claim=claim)
    assert caught.value.status == 409
    assert not _sidecar(primary, "overflow").exists()
    assert _sidecar(primary, "concurrent").exists()
    assert len(revoked) == 1 and revoked[0][1] == "overflow.gateway.secret"


@pytest.mark.parametrize("installation", ["", " ", 1, "x" * 257, "line\nbreak"])
def test_pairing_validates_an_explicit_installation_before_claim(state, installation):
    primary, _, _ = state
    calls = []
    with pytest.raises(api.core.TokenError) as caught:
        api.pairing.pair_profile({"pairing_code": CODE, "multi_device": True,
                                  "installation_id": installation}, path=primary,
            claim=lambda *_: calls.append(True) or _claimed())
    assert caught.value.status == 400 and calls == []


@pytest.mark.parametrize("installation", ["phone", "tablet"])
def test_delete_pairing_revokes_and_removes_only_the_selected_device(state, monkeypatch, installation):
    primary, http, _ = state
    _pair(primary)
    _pair(primary, "tablet")
    requests = []
    def request(*args):
        requests.append(args)
        return 204, {}
    monkeypatch.setattr(api.watch_tools, "_relay_request", request)
    response = http.delete(f"{BASE}/pair", params={"installation_id": installation})
    assert response.status_code == 200, response.text
    assert response.json()["paired"] is False
    assert requests == [("https://push.milim.dev/v1/gateways/current", "DELETE",
                         f"{installation}.gateway.secret", None, api.pairing.PAIR_TIMEOUT_S)]
    assert primary.exists() is (installation != "phone")
    assert _sidecar(primary, "tablet").exists() is (installation != "tablet")
    other = "tablet" if installation == "phone" else "phone"
    assert http.get(f"{BASE}/e2e", params={"installation_id": other}).json()["paired"] is True


@pytest.mark.parametrize("status", [401, 503])
def test_delete_keeps_local_state_when_relay_refuses_but_accepts_already_revoked(state, monkeypatch, status):
    primary, http, _ = state
    _pair(primary)
    before = primary.read_text()
    monkeypatch.setattr(api.watch_tools, "_relay_request", lambda *_: (status, {}))
    response = http.delete(f"{BASE}/pair", params={"installation_id": "phone"})
    assert response.status_code == (200 if status == 401 else 502)
    if status == 503:
        assert primary.read_text() == before
    else:
        assert not primary.exists()


def test_delete_does_not_remove_replacement_credentials_written_during_the_relay_call(state, monkeypatch):
    primary, http, _ = state
    _pair(primary)
    _pair(primary, "tablet")
    def request(*_):
        _pair(primary, "tablet", "replacement")
        return 204, {}
    monkeypatch.setattr(api.watch_tools, "_relay_request", request)
    response = http.delete(f"{BASE}/pair", params={"installation_id": "tablet"})
    assert response.status_code == 409
    assert json.loads(_sidecar(primary, "tablet").read_text())["gateway_id"] == "replacement"
    assert json.loads(primary.read_text())["installation_id"] == "phone"


def test_delete_unknown_installation_never_revokes_another_device(state, monkeypatch):
    primary, http, _ = state
    _pair(primary)
    before = primary.read_text()
    calls = []
    monkeypatch.setattr(api.watch_tools, "_relay_request", lambda *_: calls.append(True) or (204, {}))
    assert http.delete(f"{BASE}/pair", params={"installation_id": "unknown"}).status_code == 409
    assert calls == [] and primary.read_text() == before


def test_delete_retains_the_pairing_after_a_network_failure(state, monkeypatch):
    import urllib.error
    primary, http, _ = state
    _pair(primary)
    before = primary.read_text()
    def request(*_):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(api.watch_tools, "_relay_request", request)
    response = http.delete(f"{BASE}/pair", params={"installation_id": "phone"})
    assert response.status_code == 502 and primary.read_text() == before


@pytest.mark.parametrize("installation", ["", " ", "x" * 257, "line\nbreak"])
def test_delete_validates_installation_before_contacting_the_relay(state, monkeypatch, installation):
    _, http, _ = state
    calls = []
    monkeypatch.setattr(api.watch_tools, "_relay_request", lambda *_: calls.append(True) or (204, {}))
    response = http.delete(f"{BASE}/pair", params={"installation_id": installation})
    assert response.status_code == 400 and calls == []
