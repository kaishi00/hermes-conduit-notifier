"""Phone receipts through real plugin routes; native auth has a separate probe."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from test_work_grants import (home, on, http, recipient_sessions, scoped, preview, act, BASE, HEADERS,
                              bots, hermes, work, store_mod, TASK, SCOPE)

PHONE = "/api/plugins/conduit_push/phone-consent"


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def public(key):
    return b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


@pytest.fixture
def phone(http, bots):
    path = bots["hermes"] / "conduit-push.json"
    path.write_text(json.dumps({"installation_id": "phone-installation", "gateway_id": "gateway-1",
                                "credential": "private-relay-credential", "relay_url": "https://relay.example"}))
    return http


def challenge(http, body, headers=HEADERS):
    return http.post(PHONE + "/challenge", headers=headers, json=body)


def receipt(http, key, reply, headers=HEADERS):
    body = reply.json()
    return http.post(PHONE + "/receipt", headers=headers,
                     json={"challenge": body["challenge"], "signature": b64(key.sign(body["message"].encode()))})


def enroll(http, key=None):
    key = key or Ed25519PrivateKey.generate()
    issued = challenge(http, {"action": "enroll", "public_key": public(key),
                              "installation_id": "phone-installation", "gateway_id": "gateway-1"})
    assert issued.status_code == 200, issued.text
    response = receipt(http, key, issued)
    assert response.status_code == 200, response.text
    return key, response.json()["device"]


def pending(http, device, grant=None, action="grant_approve"):
    grant = grant or preview(http)
    return challenge(http, {"action": action, "device": device, "grant": grant["id"], "digest": grant["digest"]})


def test_cross_language_wire_vector():
    from pathlib import Path
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    vector = json.loads((Path(__file__).parent / "fixtures/phone-consent-v1.json").read_text())
    seed = base64.b64decode(vector["seed"])
    key = Ed25519PrivateKey.from_private_bytes(seed)
    assert base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode() == vector["public_key"]
    message = vector["message"].encode()
    assert base64.b64encode(key.sign(message)).decode() == vector["signature"]
    Ed25519PublicKey.from_public_bytes(base64.b64decode(vector["public_key"])).verify(base64.b64decode(vector["signature"]), message)
    assert work.work_grants.canonical(json.loads(vector["message"])) == vector["message"]
    assert work.work_grants.digest(json.loads(vector["snapshot_text"])) == vector["digest"]


def test_explicit_enrollment_and_phone_receipt_admit_exact_recipient_once(phone, bots, hermes):
    scoped()
    key, device = enroll(phone)
    read = phone.get(PHONE, headers=HEADERS)
    assert read.status_code == 200, read.text
    data = read.json()
    assert data["protocol"] == "conduit-phone-consent/1"
    grant = data["grants"][0]
    assert grant["snapshot"]["task"] == TASK and grant["snapshot"]["scope"] == SCOPE
    issued = pending(phone, device, grant)
    assert issued.status_code == 200, issued.text
    assert not hermes.named("session.create")
    assert receipt(phone, key, issued).status_code == 200
    assert receipt(phone, key, issued).status_code == 409
    worker = store_mod.Store(bots["nahida"]).get("W1")
    assert worker.goal == TASK and worker.requested_by["grant"] == grant["id"]
    assert len(hermes.named("session.create")) == 1
    actor = work.work_grants.get(bots["hermes"], grant["id"])["actor"]
    assert actor["source"] == "phone_consent" and actor["device"] == device
    assert not hermes.named("approval.respond") and not hermes.named("config.set")


@pytest.mark.parametrize("action", ["grant_decline", "grant_revoke"])
def test_phone_decline_revoke_terminal(phone, hermes, action):
    scoped()
    key, device = enroll(phone)
    issued = pending(phone, device, action=action)
    assert receipt(phone, key, issued).status_code == 200
    assert receipt(phone, key, pending(phone, device)).status_code == 409
    assert not hermes.named("session.create")


@pytest.mark.parametrize("headers", [{}, {**HEADERS, "x-test-identity": "automation"},
    {**HEADERS, "x-test-identity": "native"}, {**HEADERS, "x-test-identity": "local"},
    {**HEADERS, "x-test-identity": "session-token"}, {**HEADERS, "x-test-identity": "server-internal"},
    {**HEADERS, "origin": "https://evil.example"}, {**HEADERS, "sec-fetch-site": "cross-site"},
    {k: v for k, v in HEADERS.items() if k != "origin"}])
def test_non_owner_cannot_enroll_or_issue_or_submit(phone, hermes, headers):
    key, device = enroll(phone)
    scoped()
    issued = pending(phone, device)
    assert challenge(phone, {"action": "enroll", "public_key": public(key),
                            "installation_id": "phone-installation", "gateway_id": "gateway-1"}, headers).status_code in (401, 403)
    assert receipt(phone, key, issued, headers).status_code in (401, 403)
    expected_read = (200,) if "origin" not in headers and headers.get("cookie") else (401, 403)
    assert phone.get(PHONE, headers=headers).status_code in expected_read
    assert not hermes.named("session.create")


@pytest.mark.parametrize("field,value", [("device", "unknown"), ("grant", "Gwrong"), ("digest", "0" * 64),
    ("task", "changed"), ("scope", "all"), ("profile", "other"), ("snapshot", {}), ("action", "approve")])
def test_wrong_device_task_snapshot_and_unknown_fields_fail(phone, hermes, field, value):
    scoped()
    _, device = enroll(phone)
    grant = preview(phone)
    body = {"action": "grant_approve", "device": device, "grant": grant["id"], "digest": grant["digest"], field: value}
    assert challenge(phone, body).status_code in (400, 409)
    assert not hermes.named("session.create")


def test_wrong_key_and_tampered_message_cannot_authorize(phone, hermes):
    scoped()
    key, device = enroll(phone)
    issued = pending(phone, device)
    assert receipt(phone, Ed25519PrivateKey.generate(), issued).status_code == 403
    # A forged signature doesn't consume another device's valid receipt.
    assert receipt(phone, key, issued).status_code == 200
    assert len(hermes.named("session.create")) == 1


def test_stale_challenge_and_racing_replay(phone, hermes, monkeypatch):
    scoped()
    key, device = enroll(phone)
    issued = pending(phone, device)
    module = work.phone_consent
    now = module.time.time()
    with monkeypatch.context() as mp:
        mp.setattr(module.time, "time", lambda: now + 120)
        assert receipt(phone, key, issued).status_code == 409
    issued = pending(phone, device)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: receipt(phone, key, issued), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(hermes.named("session.create")) == 1


@pytest.mark.parametrize("change", ["repair", "unpair", "revoke", "replace"])
def test_device_changes_revoke_authority_and_interrupt(phone, bots, hermes, change):
    scoped()
    key, device = enroll(phone)
    assert receipt(phone, key, pending(phone, device)).status_code == 200
    stale = pending(phone, device, action="grant_revoke")
    path = bots["hermes"] / "conduit-push.json"
    original = path.read_text()
    if change in ("repair", "unpair"):
        if change == "repair":
            state = json.loads(original)
            state["credential"] = "new-credential"
            path.write_text(json.dumps(state))
        else:
            path.unlink()
        work.phone_consent.pairing_changed(bots["hermes"], work)
        path.write_text(original)  # restoring an old pairing must not resurrect authority
    elif change == "replace":
        enroll(phone)
    else:
        response = phone.post(PHONE + "/revoke-device", headers=HEADERS, json={"device": device})
        assert response.status_code == 200, response.text
    with work.work_grants.registry(bots["hermes"])._db(write=False) as db:
        record = db.execute("SELECT active, record FROM consent_devices WHERE id = ?", (device,)).fetchone()
    audit = json.loads(record["record"])
    assert record["active"] == 0 and audit["owner"] == "owner" and audit["enrolled_at"] > 0
    assert audit["revoked_at"] >= audit["enrolled_at"] and audit["revoked_by"]["source"]
    assert receipt(phone, key, stale).status_code == 409
    assert not work.work_grants.worker_valid(work, bots["nahida"], store_mod.Store(bots["nahida"]).get("W1"))
    assert work.work_grants.cards(bots["hermes"])["grants"][0]["status"] == "revoked"
    assert hermes.named("session.interrupt")


def test_stop_after_challenge_never_starts_recipient(phone, bots, hermes):
    scoped()
    key, device = enroll(phone)
    issued = pending(phone, device)
    store_mod.Store(bots["hermes"]).update("A1", status="stopped")
    assert receipt(phone, key, issued).status_code == 409
    assert not hermes.named("session.create")


def test_profile_and_owner_change_cannot_submit_saved_challenge(phone, bots, hermes, monkeypatch):
    import sys
    import types
    scoped()
    key, device = enroll(phone)
    issued = pending(phone, device)
    sessions = sys.modules["hermes_cli.web_server_sessions"]
    owners = sys.modules["gateway.owner_identity"]
    monkeypatch.setattr(owners, "host_owner_subject", lambda: "other-owner")
    monkeypatch.setattr(sessions, "_session_mutation_context", lambda request, profile:
        (None, types.SimpleNamespace(subject="other-owner", profile_id=str(bots["hermes"].resolve()), transport_id="http")))
    assert receipt(phone, key, issued).status_code == 409
    monkeypatch.setattr(owners, "host_owner_subject", lambda: "owner")
    monkeypatch.setattr(phone.grant_api.core, "_calls_home", lambda: bots["nahida"])
    monkeypatch.setattr(sessions, "_session_mutation_context", lambda request, profile:
        (None, types.SimpleNamespace(subject="owner", profile_id=str(bots["nahida"].resolve()), transport_id="http")))
    path = bots["nahida"] / "conduit-push.json"
    path.write_bytes((bots["hermes"] / "conduit-push.json").read_bytes())
    assert receipt(phone, key, issued).status_code == 409
    assert not hermes.named("session.create")


def test_device_revoke_during_native_create_blocks_prompt(phone, bots, hermes):
    scoped()
    key, device = enroll(phone)
    original = hermes.session_create
    def create(params):
        answer = original(params)
        response = phone.post(PHONE + "/revoke-device", headers=HEADERS, json={"device": device})
        assert response.status_code == 200 and response.json()["interrupted"] is False
        return answer
    hermes.session_create = create
    assert receipt(phone, key, pending(phone, device)).status_code == 502
    assert not hermes.named("prompt.submit")
    assert hermes.named("session.interrupt") or hermes.named("session.close")
    assert work.work_grants.cards(bots["hermes"])["grants"][0]["status"] == "revoked"


@pytest.mark.parametrize("operation", ["remove", "update", "save"])
def test_native_pairing_writers_retire_device_without_a_phone_read(phone, bots, hermes, monkeypatch, operation):
    import importlib
    scoped()
    key, device = enroll(phone)
    assert receipt(phone, key, pending(phone, device)).status_code == 200
    client = importlib.import_module(work.__package__ + ".client")
    path = bots["hermes"] / "conduit-push.json"
    monkeypatch.setattr(client, "state_path", lambda: path)
    if operation == "remove":
        client.remove_state()
    elif operation == "update":
        client.update_state(lambda state: state.update(credential="new-pairing"))
    else:
        state = json.loads(path.read_text())
        client.save_state({**state, "gateway_id": "new-gateway"})
    assert work.work_grants.cards(bots["hermes"])["grants"][0]["status"] == "revoked"
    assert hermes.named("session.interrupt")


def test_enrollment_is_one_use_and_old_pending_replacement_cannot_roll_back(phone):
    first = Ed25519PrivateKey.generate()
    second = Ed25519PrivateKey.generate()
    body = {"action": "enroll", "installation_id": "phone-installation", "gateway_id": "gateway-1"}
    a = challenge(phone, {**body, "public_key": public(first)})
    b = challenge(phone, {**body, "public_key": public(second)})
    assert receipt(phone, first, a).status_code == 200
    assert receipt(phone, first, a).status_code == 409
    assert receipt(phone, second, b).status_code == 409


@pytest.mark.parametrize("already_enrolled,transition", [(True, "revoke"), (True, "repair"), (True, "unpair"),
                                                        (False, "repair"), (False, "unpair")])
def test_pending_initial_enrollment_cannot_survive_enroll_then_retire(phone, bots, transition, already_enrolled):
    old_key = Ed25519PrivateKey.generate()
    stale = challenge(phone, {"action": "enroll", "public_key": public(old_key),
        "installation_id": "phone-installation", "gateway_id": "gateway-1"})
    current = enroll(phone)[1] if already_enrolled or transition == "revoke" else None
    if transition == "revoke":
        assert phone.post(PHONE + "/revoke-device", headers=HEADERS, json={"device": current}).status_code == 200
    else:
        path = bots["hermes"] / "conduit-push.json"
        original = path.read_text()
        if transition == "unpair":
            path.unlink()
        else:
            path.write_text(json.dumps({**json.loads(original), "credential": "replacement"}))
        work.phone_consent.pairing_changed(bots["hermes"], work)
        path.write_text(original)
    assert receipt(phone, old_key, stale).status_code == 409
    assert phone.get(PHONE, headers=HEADERS).json()["device"] is None


@pytest.mark.parametrize("value", ["a", "!" * 43, [], None, "A" * 42 + "B"])
def test_malformed_public_key_is_a_refusal(phone, value):
    assert challenge(phone, {"action": "enroll", "installation_id": "phone-installation", "gateway_id": "gateway-1",
                             "public_key": value}).status_code == 400


def test_two_distinct_phone_challenges_share_the_one_use_grant(phone, hermes):
    scoped()
    key, device = enroll(phone)
    first, second = pending(phone, device), pending(phone, device)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda issued: receipt(phone, key, issued), [first, second]))
    assert sorted(r.status_code for r in replies) == [200, 409]
    assert len(hermes.named("session.create")) == 1


def test_uncertain_phone_admission_never_replays_on_reconnect(phone, hermes):
    scoped()
    key, device = enroll(phone)
    hermes.refuse["prompt.submit"] = {"code": -32000, "message": "uncertain"}
    issued = pending(phone, device)
    assert receipt(phone, key, issued).status_code >= 400
    assert phone.get(PHONE, headers=HEADERS).json()["grants"][0]["status"] == "consumed"
    assert receipt(phone, key, issued).status_code == 409
    assert receipt(phone, key, pending(phone, device)).status_code == 409
    assert len(hermes.named("session.create")) == 1


def test_device_revoke_uses_durable_session_after_registration_loss(phone, bots, hermes):
    scoped()
    key, device = enroll(phone)
    assert receipt(phone, key, pending(phone, device)).status_code == 200
    recipient = store_mod.Store(bots["nahida"])
    worker = recipient.get("W1")
    with recipient._db() as db:
        db.execute("DELETE FROM work WHERE ref = 'W1'")
    response = phone.post(PHONE + "/revoke-device", headers=HEADERS, json={"device": device})
    assert response.status_code == 200 and response.json()["interrupted"] is True
    assert hermes.named("session.interrupt")
    assert not hermes._sessions["rt-" + worker.session_id]["running"]


def test_phone_grant_revoke_preserves_native_interrupt(phone, bots, hermes):
    scoped()
    key, device = enroll(phone)
    assert receipt(phone, key, pending(phone, device)).status_code == 200
    assert receipt(phone, key, pending(phone, device, action="grant_revoke")).status_code == 200
    assert store_mod.Store(bots["nahida"]).get("W1").status == "stopped"
    assert hermes.named("session.interrupt")


def test_pairing_not_encryption_or_ids_is_not_consent(phone, bots, hermes):
    scoped()
    grant = preview(phone)
    # Neither existing pairing state nor any generic chat/card/proposal mints a device.
    assert challenge(phone, {"action": "grant_approve", "device": "gateway-1", "grant": grant["id"], "digest": grant["digest"]}).status_code == 409
    assert not hermes.named("session.create")
    assert act(phone, grant).status_code == 200  # dashboard still has its own consent boundary
