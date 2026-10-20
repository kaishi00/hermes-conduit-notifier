import json
import secrets

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_health
from test_health import BASE, DEVICE, ROOT, Phone, _batch, api, health

VECTOR = json.loads((ROOT / "tests" / "health_vector.json").read_text())["voice"]


class VoicePhone(Phone):
    """Conduit's side of a voice read, as the Swift code does it."""

    def accept(self, answer):
        super().accept(answer)
        host_public = health.unb64u(answer["host_public_key"])
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(host_public))
        self.request_key = health.derive_key(shared, self.device_id, self.public, host_public,
                                             health.INFO_VOICE_REQUEST)
        self.answer_key = health.derive_key(shared, self.device_id, self.public, host_public,
                                            health.INFO_VOICE_ANSWER)

    def ask(self, args, call_id=None, tool="conduit_health"):
        call_id = call_id or secrets.token_urlsafe(12)
        body = json.dumps({"call_id": call_id, "args": args, "provider": "gemini-live"}).encode()
        nonce, ct = health.seal(self.request_key, body, health.voice_aad("request", tool, self.kid, self.device_id, call_id))
        return {"v": 1, "tool": tool, "device_id": self.device_id, "kid": self.kid, "call_id": call_id,
                "nonce": nonce, "ct": ct}

    def read(self, envelope, answer):
        plain = health.open_sealed(self.answer_key, answer["nonce"], answer["ct"],
                                   health.voice_aad("answer", envelope["tool"], self.kid, self.device_id,
                                                   envelope["call_id"]))
        opened = json.loads(plain)
        assert opened["call_id"] == envelope["call_id"]
        return opened["result"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_voice_tools_limiter", api._MintLimiter(1000, 60))
    monkeypatch.setattr(api, "_health_ingest_limiter", api._MintLimiter(1000, 60))
    monkeypatch.setattr(api, "_health_key_limiter", api._MintLimiter(1000, 60))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _share(client, phone, batch=None):
    phone.accept(client.post(f"{BASE}/health/key", json=phone.key_request()).json())
    if batch is not None:
        assert client.post(f"{BASE}/health/ingest", json=phone.seal(batch)).status_code == 200


def _call(client, phone, args, **kw):
    envelope = phone.ask(args, **kw)
    response = client.post(f"{BASE}/voice-tools/call", json=envelope)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return phone.read(envelope, response.json())


def test_shared_voice_vector_matches_conduit():
    # ConduitTests/HealthSyncCryptoTests.swift checks the same values.
    host_private, device_private = bytes(range(32)), bytes(range(32, 64))
    host_public = X25519PrivateKey.from_private_bytes(host_private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    device_public = X25519PrivateKey.from_private_bytes(device_private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kid = health.key_id(device_public, host_public)
    shared = health.agree(host_private, device_public)
    request_key = health.derive_key(shared, "device-vector-1", device_public, host_public, health.INFO_VOICE_REQUEST)
    answer_key = health.derive_key(shared, "device-vector-1", device_public, host_public, health.INFO_VOICE_ANSWER)
    assert request_key.hex() == VECTOR["request_key"] and answer_key.hex() == VECTOR["answer_key"]
    request_aad = health.voice_aad("request", "conduit_health", kid, "device-vector-1", VECTOR["call_id"])
    answer_aad = health.voice_aad("answer", "conduit_health", kid, "device-vector-1", VECTOR["call_id"])
    assert request_aad.decode() == VECTOR["request_aad"] and answer_aad.decode() == VECTOR["answer_aad"]
    assert health.seal(request_key, VECTOR["request"].encode(), request_aad, nonce=bytes(12))[1] == VECTOR["request_ct"]
    assert health.seal(answer_key, VECTOR["answer"].encode(), answer_aad, nonce=bytes(12))[1] == VECTOR["answer_ct"]
    # Ingest's key is a different one.
    assert request_key != health.derive_key(shared, "device-vector-1", device_public, host_public)


def test_the_list_offers_health_only_once_data_arrived(client):
    response = client.get(f"{BASE}/voice-tools")
    assert response.status_code == 200 and response.json() == {"ok": True, "tools": []}
    _share(client, VoicePhone(), _batch())
    tools = client.get(f"{BASE}/voice-tools").json()["tools"]
    assert [tool["name"] for tool in tools] == ["conduit_health"]
    assert tools[0]["key"] == "health"
    assert tools[0]["description"].startswith(test_health.tool.SCHEMA["description"][:80])
    assert "voice call" in tools[0]["description"]
    assert tools[0]["parameters"]["required"] == ["action"]
    assert "device" not in tools[0]["parameters"]["properties"], "a voice read always covers the asking phone"
    assert "action" in tools[0]["parameters"]["properties"]


def test_a_voice_read_comes_back_sealed_for_the_asking_phone(client):
    phone = VoicePhone()
    _share(client, phone, _batch())
    envelope = phone.ask({"action": "daily", "metrics": ["steps"], "from": "2026-10-11", "to": "2026-10-12"})
    raw = client.post(f"{BASE}/voice-tools/call", json=envelope).json()
    assert set(raw) == {"ok", "v", "nonce", "ct"}
    assert "1200" not in json.dumps(raw)
    answer = phone.read(envelope, raw)
    assert answer["ok"] is True
    assert answer["days"] == [{"date": "2026-10-11", "steps": 1200.0}, {"date": "2026-10-12", "steps": 800.0}]
    overview = _call(client, phone, {"action": "overview"})
    assert [device["id"] for device in overview["devices"]] == [DEVICE]


def test_another_device_never_reads_this_ones_data(client):
    owner = VoicePhone()
    owner.key_request = lambda: {**VoicePhone.key_request(owner), "device_name": "ericsiphone1"}
    _share(client, owner, _batch())
    # A key registered with dashboard access alone, under an id that matches
    # the owner's device name: it has no data of its own and must get none.
    intruder = VoicePhone(device_id="ERICSIPHONE1")
    store = health.HealthStore(api._calls_home())
    with store._read() as db:
        assert store.latest_device(db, "ERICSIPHONE1")[0] == DEVICE, "a name lookup would pick the owner"
        assert store.latest_device(db, "ERICSIPHONE1", exact=True) is None
    _share(client, intruder)
    assert _call(client, intruder, {"action": "daily", "metrics": ["steps"]})["error"] == "nothing_shared"
    assert _call(client, intruder, {"action": "overview"})["error"] == "nothing_shared"
    # Asking for the owner's device by id or name changes nothing.
    assert _call(client, intruder, {"action": "sleep", "device": DEVICE})["error"] == "nothing_shared"


def test_a_request_without_the_device_key_opens_nothing(client):
    phone = VoicePhone()
    _share(client, phone, _batch())
    envelope = phone.ask({"action": "overview"})
    response = client.post(f"{BASE}/voice-tools/call", json={**envelope, "call_id": "call-other-1"})
    assert response.status_code == 409 and response.json()["code"] == "bad_seal"
    response = client.post(f"{BASE}/voice-tools/call", json={**envelope, "kid": "f" * 32})
    assert response.status_code == 409 and response.json()["code"] == "unknown_key"
    stranger = VoicePhone()
    stranger.kid, stranger.request_key = phone.kid, secrets.token_bytes(32)
    assert client.post(f"{BASE}/voice-tools/call", json=stranger.ask({"action": "overview"})).status_code == 409
    assert client.post(f"{BASE}/voice-tools/call", json={"v": 1}).status_code == 404
    assert client.post(f"{BASE}/voice-tools/call", json=phone.ask({}, tool="terminal")).status_code == 404
    assert client.post(f"{BASE}/voice-tools/call", json={**envelope, "v": 2}).status_code == 400


def test_a_bad_action_is_an_answer_not_an_error(client):
    phone = VoicePhone()
    _share(client, phone, _batch())
    assert _call(client, phone, {"action": "wipe"})["error"] == "invalid"
    assert _call(client, phone, {"action": "daily", "metrics": ["password"]})["error"] == "unknown_metric"


def test_voice_reads_are_rate_limited(client, monkeypatch):
    monkeypatch.setattr(api, "_voice_tools_limiter", api._MintLimiter(1, 60))
    phone = VoicePhone()
    _share(client, phone, _batch())
    _call(client, phone, {"action": "overview"})
    assert client.post(f"{BASE}/voice-tools/call", json=phone.ask({"action": "overview"})).status_code == 429


def test_a_new_key_for_the_same_device_id_reads_none_of_the_old_data(client):
    owner = VoicePhone()
    _share(client, owner, _batch())
    # Dashboard access alone can re-key the owner's device id; that drops what
    # the old key sent instead of handing it to the new key.
    intruder = VoicePhone(device_id=DEVICE)
    _share(client, intruder)
    assert _call(client, intruder, {"action": "daily", "metrics": ["steps"]})["error"] == "nothing_shared"
    status = client.get(f"{BASE}/health/status", params={"device_id": DEVICE}).json()["device"]
    assert status["rows"] == {}
    assert not status.get("categories"), "what the old key said it shared goes with its data"
    # The real phone gets 409 on its next sync, sets up a key and sends it all again.
    assert client.post(f"{BASE}/health/ingest", json=owner.seal(_batch("batch-0002"))).status_code == 409


def test_the_same_device_key_again_keeps_the_data(client):
    phone = VoicePhone()
    _share(client, phone, _batch())
    # Re-registering the same device key (only that phone can use what it
    # agrees) refreshes the host key and keeps what was sent.
    _share(client, phone)
    assert _call(client, phone, {"action": "daily", "metrics": ["steps"]})["ok"] is True


def test_a_failed_read_still_comes_back_sealed(client, monkeypatch):
    phone = VoicePhone()
    _share(client, phone, _batch())

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(api._plugin_package_module("health_tool"), "_handle", broken)
    assert _call(client, phone, {"action": "daily", "metrics": ["steps"]})["error"] == "unavailable"


def test_the_tool_name_is_sealed_into_the_request(client):
    phone = VoicePhone()
    _share(client, phone, _batch())
    envelope = phone.ask({"action": "overview"})
    store = health.HealthStore(api._calls_home())
    with pytest.raises(health.HealthError) as error:
        store.open_voice_request({**envelope, "tool": "conduit_location"})
    assert error.value.code == "bad_seal"
    with pytest.raises(health.HealthError):
        store.open_voice_request({**envelope, "tool": "Conduit Health\n"})


def test_the_tool_list_shares_the_voice_rate_limit(client, monkeypatch):
    monkeypatch.setattr(api, "_voice_tools_limiter", api._MintLimiter(1, 60))
    assert client.get(f"{BASE}/voice-tools").status_code == 200
    assert client.get(f"{BASE}/voice-tools").status_code == 429


@pytest.mark.parametrize("action", ["x\nforged", ["overview"], {"a": 1}])
def test_the_log_line_carries_only_known_words(client, caplog, action):
    phone = VoicePhone()
    _share(client, phone, _batch())
    call_id = "call-log-0001"
    body = json.dumps({"call_id": call_id, "args": {"action": action}, "provider": "Evil\nline"}).encode()
    nonce, ct = health.seal(phone.request_key, body,
                            health.voice_aad("request", "conduit_health", phone.kid, phone.device_id, call_id))
    envelope = {"v": 1, "tool": "conduit_health", "device_id": phone.device_id, "kid": phone.kid,
                "call_id": call_id, "nonce": nonce, "ct": ct}
    with caplog.at_level("INFO"):
        assert client.post(f"{BASE}/voice-tools/call", json=envelope).status_code == 200
    lines = [record.getMessage() for record in caplog.records if "voice read" in record.getMessage()]
    assert lines == ["Conduit voice read: conduit_health other for voice"]
