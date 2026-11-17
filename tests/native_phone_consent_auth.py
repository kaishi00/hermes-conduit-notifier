"""Native password/cookie and provider-verified bearer, real plugin routes.

Run with Hermes' Python/PYTHONPATH in a disposable home. The only fixture
execution is the session RPC server/hook registration, not authentication.
This is not an OAuth login UI, Swift execution or physical-phone test.
"""
import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from native_scoped_grant_auth import main, work, call, BOT_CHAT, store_mod, SCOPE

PHONE = "/api/plugins/conduit_push/phone-consent"
HEADERS = {"origin": "http://testserver", "sec-fetch-site": "same-origin"}


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def cases(home, api, owner, anonymous, fake, homes):
    (home / "conduit-push.json").write_text(json.dumps({"installation_id": "test-phone", "gateway_id": "test-gateway",
        "credential": "disposable-pairing-credential", "relay_url": "https://relay.example"}))
    key = Ed25519PrivateKey.generate()
    enrollment = {"action": "enroll", "public_key": b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
                  "installation_id": "test-phone", "gateway_id": "test-gateway"}
    import re
    import hermes_cli.web_server as web
    page = TestClient(web.app).get("/")
    match = re.search(r'window\.__HERMES_SESSION_TOKEN__=("[^\"]+")', page.text)
    assert match
    public_bootstrap = json.loads(match.group(1))
    denied = anonymous.post(PHONE + "/challenge", headers={**HEADERS, "x-hermes-session-token": public_bootstrap}, json=enrollment)
    assert denied.status_code in (401, 403)
    assert owner.post(PHONE + "/challenge", headers={"origin": "https://other.example"}, json=enrollment).status_code == 403
    issued = owner.post(PHONE + "/challenge", headers=HEADERS, json=enrollment)
    assert issued.status_code == 200, issued.text
    def signed(challenge):
        data = challenge.json()
        return {"challenge": data["challenge"], "signature": b64(key.sign(data["message"].encode()))}
    enrolled = owner.post(PHONE + "/receipt", headers=HEADERS, json=signed(issued))
    assert enrolled.status_code == 200, enrolled.text
    device = enrolled.json()["device"]
    args = {"bot": "nahida", "text": "Read a disposable phone-protocol fixture.", "scope": SCOPE}
    work.tool_block(BOT_CHAT, "work_ask", args, turn_id="phone-turn", tool_call_id="phone-call")
    assert call("work_ask", args)["ok"]
    read = owner.get(PHONE)
    assert read.status_code == 200 and read.headers["cache-control"] == "no-store"
    grant = next(g for g in read.json()["grants"] if g["snapshot"]["task"] == args["text"])
    before = len(fake.named("session.create"))
    def challenge(action):
        response = owner.post(PHONE + "/challenge", headers=HEADERS,
            json={"action": action, "device": device, "grant": grant["id"], "digest": grant["digest"]})
        assert response.status_code == 200, response.text
        return response
    event = signed(challenge("grant_approve"))
    assert anonymous.post(PHONE + "/receipt", headers=HEADERS, json=event).status_code in (401, 403)
    # The unchanged native gate verifies this provider-minted token as a bearer too.
    from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE
    bearer = owner.cookies.get(SESSION_AT_COOKIE)
    assert bearer
    native = TestClient(owner.app)
    native_headers = {**HEADERS, "authorization": "Bearer " + bearer}
    assert native.get(PHONE, headers=native_headers).status_code == 200
    approved = native.post(PHONE + "/receipt", headers=native_headers, json=event)
    assert approved.status_code == 200, approved.text
    assert native.post(PHONE + "/receipt", headers=native_headers, json=event).status_code == 409
    assert len(fake.named("session.create")) == before + 1
    persisted = work.work_grants.get(home, grant["id"])
    assert persisted["actor"]["source"] == "phone_consent" and persisted["actor"]["device"] == device
    revoked = owner.post(PHONE + "/revoke-device", headers=HEADERS, json={"device": device})
    assert revoked.status_code == 200 and revoked.json()["interrupted"] is True
    assert work.work_grants.get(home, grant["id"])["status"] == "revoked"
    worker = store_mod.Store(homes["nahida"]).get(persisted["work_ref"])
    assert worker.status == "stopped" and fake.named("session.interrupt")
    assert not fake.named("approval.respond") and not fake.named("config.set")
    print(json.dumps({"phone_enrollment": 200, "anonymous_enrollment": denied.status_code,
        "native_cookie_preview": 200, "provider_verified_bearer_approve": approved.status_code,
        "receipt_replay": 409, "device_revoke": 200, "worker_status": "stopped",
        "new_phone_workers": len(fake.named("session.create")) - before, "auth": "unchanged native gate",
        "rpc": "fixture", "swift_executed": False, "physical_phone": False}))


if __name__ == "__main__":
    main(cases)
