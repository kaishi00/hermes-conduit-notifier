import importlib.util
import json
import os
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
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


e2e = _load("conduit_push.e2e", ROOT / "e2e.py")
client = _load("conduit_push.client", ROOT / "client.py")
api = _load("conduit_plugin_api_e2e", ROOT / "dashboard" / "plugin_api.py")

KID = "0123456789abcdef0123456789abcdef"
SECRET = bytes(range(32))
INSTALLATION = "11111111-2222-3333-4444-555555555555"
GATEWAY = "gw-1"


def _keys():
    return e2e.derive_keys(KID, SECRET)


def _state(**extra):
    state = {
        "relay_url": "https://relay",
        "credential": "c",
        "installation_id": INSTALLATION,
        "gateway_id": GATEWAY,
        "e2e": {"kid": KID, "secret": e2e.b64u(SECRET)},
    }
    state.update(extra)
    return state


def _clarify_event(question="Deploy to prod?", questions=None):
    decision = {"kind": "clarify", "request_id": "conduit-push-abc123def456", "question": question, "choices": ["Yes", "No"]}
    if questions is not None:
        decision["questions"] = questions
    return {
        "event_id": "input:0123456789abcdef",
        "type": "input.needed",
        "plugin_version": "0.5.0",
        "plugin_capabilities": ["e2e-v1"],
        "session_id": "sess-1",
        "profile": "default",
        "body": question,
        "decision": decision,
    }


# --- Keys ------------------------------------------------------------------


def test_keys_are_separate_per_direction_and_purpose():
    keys = _keys()
    assert len({keys.push, keys.answer, keys.meta, SECRET}) == 4


def test_known_answer_vector_matches_the_app():
    # Conduit's E2ETests.swift checks the same derivation with CryptoKit.
    keys = _keys()
    assert keys.push.hex() == _hkdf_hex(b"conduit-e2e-v1 push gateway-to-device")
    assert e2e.thread_token(keys, "sess-1") == e2e.thread_token(e2e.derive_keys(KID, SECRET), "sess-1")


def _hkdf_hex(info):
    import hashlib
    import hmac

    prk = hmac.new(e2e.SALT, SECRET, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest().hex()


def test_malformed_keys_are_refused():
    with pytest.raises(e2e.E2EError):
        e2e.derive_keys("XYZ", SECRET)
    with pytest.raises(e2e.E2EError):
        e2e.derive_keys(KID, b"short")


# --- Push envelopes ----------------------------------------------------------


def test_sealed_event_hides_content_and_round_trips():
    keys = _keys()
    event = _clarify_event()
    sealed = e2e.seal_event(event, keys, installation_id=INSTALLATION, gateway_id=GATEWAY, now=1_760_000_000)
    wire = json.dumps(sealed)
    for secret_text in ("Deploy to prod?", "sess-1", "default", "Yes"):
        assert secret_text not in wire
    assert sealed["type"] == "input.needed"
    assert sealed["event_id"] == sealed["e2e"]["msg"]
    assert sealed["event_id"] != event["event_id"]
    assert sealed["clarify"] == {"request_id": "conduit-push-abc123def456", "qids": [], "card": True}
    assert sealed["plugin_version"] == "0.5.0"
    inner = e2e.open_event(sealed, keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert inner["body"] == "Deploy to prod?"
    assert inner["decision"]["choices"] == ["Yes", "No"]
    assert inner["session_id"] == "sess-1"


def test_a_sealed_call_request_carries_its_call_inside():
    keys = _keys()
    event = {
        "event_id": "call:0123456789abcdef01234567",
        "type": "call.requested",
        "session_id": "rt-1",
        "title": "Hermes wants to talk",
        "body": "Check the server finished.",
        "call": {"id": "0123456789abcdef01234567", "kind": "done", "title": "Check the server", "session_ids": ["rt-1", "st-1"]},
    }
    sealed = e2e.seal_event(event, keys, installation_id=INSTALLATION, gateway_id=GATEWAY, now=1_760_000_000)
    assert "Check the server" not in json.dumps(sealed)
    assert "call" not in sealed
    inner = e2e.open_event(sealed, keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert inner["call"]["title"] == "Check the server"
    assert inner["call"]["session_ids"] == ["rt-1", "st-1"]


@pytest.mark.parametrize("tamper", [
    lambda s: s.__setitem__("type", "approval.needed"),
    lambda s: s["e2e"].__setitem__("tok", "0" * 16),
    lambda s: s["e2e"].__setitem__("iat", s["e2e"]["iat"] + 1),
    lambda s: s["e2e"].__setitem__("msg", "input:" + "0" * 32),
    lambda s: s["e2e"].__setitem__("req", "conduit-push-000000000000"),
])
def test_relay_visible_fields_are_bound_to_the_ciphertext(tamper):
    keys = _keys()
    sealed = e2e.seal_event(_clarify_event(), keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    tamper(sealed)
    with pytest.raises(e2e.E2EError):
        e2e.open_event(sealed, keys, installation_id=INSTALLATION, gateway_id=GATEWAY)


def test_an_envelope_cannot_move_to_another_pairing():
    keys = _keys()
    sealed = e2e.seal_event(_clarify_event(), keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    with pytest.raises(e2e.E2EError):
        e2e.open_event(sealed, keys, installation_id=INSTALLATION, gateway_id="gw-2")


def test_an_oversized_card_is_dropped_and_marked_undeliverable():
    keys = _keys()
    questions = [
        {"qid": f"q{i}", "question": os.urandom(200).hex(), "choices": [os.urandom(30).hex() for _ in range(8)], "multi_select": False}
        for i in range(8)
    ]
    sealed = e2e.seal_event(_clarify_event(questions=questions), keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert len(sealed["e2e"]["ct"]) <= e2e.MAX_CT_CHARS
    assert sealed["clarify"]["card"] is False
    assert sealed["clarify"]["qids"] == [f"q{i}" for i in range(8)]
    inner = e2e.open_event(sealed, keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert "decision" not in inner
    assert inner["session_id"] == "sess-1"


def test_compressible_content_is_compressed():
    keys = _keys()
    event = _clarify_event(question="all work and no play " * 20)
    sealed = e2e.seal_event(event, keys, installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert sealed["e2e"]["z"] == 1
    assert e2e.open_event(sealed, keys, installation_id=INSTALLATION, gateway_id=GATEWAY)["body"] == event["body"]


# --- Client egress -----------------------------------------------------------


def _capture(monkeypatch, state):
    sent = []
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: sent.append(kwargs["payload"]) or {})
    monkeypatch.setattr(client, "load_state", lambda: state)
    return sent


def test_unprovisioned_pairings_keep_plaintext(monkeypatch):
    state = _state()
    del state["e2e"]
    sent = _capture(monkeypatch, state)
    client.send_now(_clarify_event())
    assert sent[0]["body"] == "Deploy to prod?"
    assert "e2e" not in sent[0]


def test_provisioned_pairings_always_encrypt(monkeypatch):
    sent = _capture(monkeypatch, _state())
    client.send_now(_clarify_event())
    assert "Deploy to prod?" not in json.dumps(sent[0])
    assert "e2e" in sent[0]


def test_redaction_still_applies_before_encryption(monkeypatch):
    sent = _capture(monkeypatch, _state(redact_content=True, redact_key="k"))
    client.send_now(_clarify_event())
    inner = e2e.open_event(sent[0], _keys(), installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert "body" not in inner
    assert inner["decision"]["question"] != "Deploy to prod?"


def test_a_broken_key_never_falls_back_to_plaintext(monkeypatch):
    sent = _capture(monkeypatch, _state(e2e={"kid": KID, "secret": "not-base64!"}))
    saved = []
    monkeypatch.setattr(client, "save_state", saved.append)
    client.send_now(_clarify_event())
    assert len(saved) == 1  # the local re-keying key, minted once
    assert set(sent[0]) == {"event_id", "type", "plugin_version", "plugin_capabilities"}
    assert "Deploy" not in json.dumps(sent[0])


def test_missing_crypto_never_falls_back_to_plaintext(monkeypatch):
    state = _state()
    sent = _capture(monkeypatch, state)
    saved = []
    monkeypatch.setattr(client, "save_state", saved.append)

    def unavailable(*args, **kwargs):
        raise e2e.E2EError("the cryptography package is not available")

    monkeypatch.setattr(e2e, "keys_from_state", unavailable)
    event = _clarify_event()
    client.send_now(event)
    assert set(sent[0]) == {"event_id", "type", "plugin_version", "plugin_capabilities"}
    # The plain digest never reaches the relay: re-keyed with the local key.
    assert sent[0]["event_id"] != event["event_id"]
    assert sent[0]["event_id"].startswith("input:")
    assert state["redact_key"] and saved == [state]


@pytest.mark.parametrize("record", [{}, [], ""])
def test_an_empty_key_record_fails_closed(monkeypatch, record):
    sent = _capture(monkeypatch, _state(e2e=record, redact_key="k"))
    client.send_now(_clarify_event())
    assert set(sent[0]) == {"event_id", "type", "plugin_version", "plugin_capabilities"}
    assert "Deploy" not in json.dumps(sent[0])
    status = {"status": "answered", "answer": "Yes"}
    assert client.opened_answers(status, _state(e2e=record), "conduit-push-abc123def456") == {"status": "rejected"}


def test_a_stale_state_write_keeps_the_provisioned_key(monkeypatch, tmp_path):
    path = tmp_path / "conduit-push.json"
    monkeypatch.setattr(client, "state_path", lambda: path)
    stale = _state()
    del stale["e2e"]
    path.write_text(json.dumps(_state()))
    # A hook that loaded the state before the key arrived writes it back.
    client.save_state(dict(stale, redact_content=True))
    stored = json.loads(path.read_text())
    assert stored["e2e"] == _state()["e2e"] and stored["redact_content"] is True
    # A new pairing doesn't inherit the old pairing's key.
    client.save_state(dict(stale, installation_id="other"))
    assert "e2e" not in json.loads(path.read_text())


def test_ciphertext_cap_matches_the_relay():
    # relay/src/server.mjs E2E_MAX_CT_CHARS: the relay refuses anything larger.
    source = (ROOT / "relay" / "src" / "server.mjs").read_text()
    assert f"E2E_MAX_CT_CHARS = {e2e.MAX_CT_CHARS:_}" in source or f"E2E_MAX_CT_CHARS = {e2e.MAX_CT_CHARS}" in source


def test_client_and_dashboard_agree_on_the_kid_shape():
    assert api._E2E_KID.pattern == e2e.KID_PATTERN.pattern


def test_a_stale_write_never_replaces_a_newer_key(monkeypatch, tmp_path):
    path = tmp_path / "conduit-push.json"
    monkeypatch.setattr(client, "state_path", lambda: path)
    newer = _state(e2e={"kid": "b" * 32, "secret": e2e.b64u(SECRET), "created_at": "2026-10-06T21:00:00Z"})
    path.write_text(json.dumps(newer))
    stale = _state(e2e={"kid": KID, "secret": e2e.b64u(SECRET), "created_at": "2026-10-06T20:00:00Z"})
    client.save_state(dict(stale, redact_content=True))
    stored = json.loads(path.read_text())
    assert stored["e2e"]["kid"] == "b" * 32 and stored["redact_content"] is True


def test_client_and_dashboard_lock_the_same_file(tmp_path):
    path = tmp_path / "conduit-push.json"
    assert client.state_lock_path(path) == api._pairing_state_lock_path(path)


def test_an_answer_that_is_not_text_is_an_e2e_error():
    keys = _keys()
    aad = e2e.answer_aad(kid=KID, installation_id=INSTALLATION, gateway_id=GATEWAY,
                         request_id="conduit-push-abc123def456", question_id="")
    nonce = bytes(12)
    sealed = e2e._aead(keys.answer).encrypt(nonce, b"\xff\xfe", aad)
    answer = f"e2e1.{KID}.{e2e.b64u(nonce)}.{e2e.b64u(sealed)}"
    with pytest.raises(e2e.E2EError):
        e2e.open_answer(answer, keys, installation_id=INSTALLATION, gateway_id=GATEWAY,
                        request_id="conduit-push-abc123def456", question_id="")


@pytest.mark.parametrize("data,z", [(b"\xff\xfe", 0), (b"[1]", 0), (b"\x00\x01garbage", 1)])
def test_unpack_reports_bad_payloads_as_e2e_errors(data, z):
    with pytest.raises(e2e.E2EError):
        e2e.unpack(data, z)


def test_unpack_rejects_a_truncated_deflate_stream():
    import zlib

    compressor = zlib.compressobj(wbits=-15)
    packed = compressor.compress(json.dumps({"title": "x" * 200}).encode()) + compressor.flush()
    with pytest.raises(e2e.E2EError):
        e2e.unpack(packed[: len(packed) // 2], 1)


def test_plugin_hello_stays_plain(monkeypatch):
    sent = _capture(monkeypatch, _state())
    hello = {"event_id": "plugin.hello:0.5.0", "type": "plugin.hello", "plugin_version": "0.5.0"}
    client.send_now(hello)
    assert sent[0] == hello


# --- Clarify answers ---------------------------------------------------------


def _answer(text, question_id="", request_id="conduit-push-abc123def456", keys=None):
    return e2e.seal_answer(text, keys or _keys(), installation_id=INSTALLATION, gateway_id=GATEWAY,
                           request_id=request_id, question_id=question_id)


def test_sealed_answers_open_for_their_request():
    status = {"status": "answered", "answer": _answer("Yes")}
    assert client.opened_answers(status, _state(), "conduit-push-abc123def456") == {"status": "answered", "answer": "Yes"}


def test_sealed_batch_answers_open_per_question():
    status = {"status": "answered", "answers": {"q0": _answer("Red", "q0"), "q1": _answer("Blue", "q1")}, "remaining": []}
    opened = client.opened_answers(status, _state(), "conduit-push-abc123def456")
    assert opened["answers"] == {"q0": "Red", "q1": "Blue"}


@pytest.mark.parametrize("answer", [
    "Yes",  # plaintext from a relay or an old client
    _answer("Yes", request_id="conduit-push-000000000000"),  # replayed from another request
    _answer("Yes", question_id="q1"),  # moved to another question
    _answer("Yes", keys=e2e.derive_keys("f" * 32, bytes(32))),  # another key
])
def test_answers_not_sealed_for_this_request_are_rejected(answer):
    status = {"status": "answered", "answer": answer}
    assert client.opened_answers(status, _state(), "conduit-push-abc123def456") == {"status": "rejected"}


def test_answers_pass_through_for_unprovisioned_pairings():
    state = _state()
    del state["e2e"]
    status = {"status": "answered", "answer": "Yes"}
    assert client.opened_answers(status, state, "conduit-push-abc123def456") == status


def test_pending_polls_are_untouched():
    status = {"status": "pending", "remaining": ["q0"]}
    assert client.opened_answers(status, _state(), "conduit-push-abc123def456") == status


# --- Dashboard provisioning ----------------------------------------------------


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "conduit-push.json"
    path.write_text(json.dumps({"credential": "c", "installation_id": INSTALLATION, "gateway_id": GATEWAY, "relay_url": "https://relay"}))
    monkeypatch.setattr(api, "_pairing_state_path", lambda: path)
    monkeypatch.setattr(api, "_profile_scope", lambda profile: __import__("contextlib").nullcontext())
    api._e2e_limiter._mints.clear()
    return path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def _body(**extra):
    body = {"installation_id": INSTALLATION, "gateway_id": GATEWAY, "kid": KID, "secret": e2e.b64u(SECRET)}
    body.update(extra)
    return body


def test_status_reports_the_pairing_without_a_key(state_file):
    response = _http().get(f"{BASE}/e2e")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "paired": True, "installation_id": INSTALLATION, "gateway_id": GATEWAY, "e2e": None, "crypto": True}


def test_provisioning_stores_the_key_owner_only_and_never_returns_it(state_file):
    response = _http().post(f"{BASE}/e2e", json=_body())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "kid": KID}
    stored = json.loads(state_file.read_text())
    assert stored["e2e"]["kid"] == KID
    assert stored["e2e"]["secret"] == e2e.b64u(SECRET)
    assert stored["credential"] == "c"
    assert oct(state_file.stat().st_mode & 0o777) == "0o600"
    status = _http().get(f"{BASE}/e2e").json()
    assert status["e2e"] == {"kid": KID}
    assert e2e.b64u(SECRET) not in json.dumps(status)


def test_status_reports_an_unusable_key_as_none(state_file):
    state = json.loads(state_file.read_text())
    state["e2e"] = {"kid": KID, "secret": "broken"}
    state_file.write_text(json.dumps(state))
    assert _http().get(f"{BASE}/e2e").json()["e2e"] is None


@pytest.mark.parametrize("extra,code", [
    ({"installation_id": "other"}, 409),
    ({"gateway_id": "gw-2"}, 409),
    ({"kid": "ABC"}, 400),
    ({"secret": e2e.b64u(bytes(16))}, 400),
    ({"secret": None}, 400),
])
def test_provisioning_refuses_other_pairings_and_bad_keys(state_file, extra, code):
    response = _http().post(f"{BASE}/e2e", json=_body(**extra))
    assert response.status_code == code
    assert "e2e" not in json.loads(state_file.read_text())


def test_provisioning_needs_a_pairing(state_file):
    state_file.unlink()
    assert _http().post(f"{BASE}/e2e", json=_body()).status_code == 409
    assert _http().get(f"{BASE}/e2e").json() == {"ok": True, "paired": False, "crypto": True}


def test_provisioning_is_rate_limited(state_file):
    http = _http()
    codes = [http.post(f"{BASE}/e2e", json=_body()).status_code for _ in range(api.E2E_LIMIT + 1)]
    assert codes[-1] == 429


@pytest.mark.parametrize("outgoing", [{}, {"e2e": None}, {"e2e": "x"}, {"e2e": []},
    {"e2e": {"msg": "m", "iat": 1, "tok": "t", "z": 0, "n": "not base64!", "ct": "AA"}}, {"e2e": {"msg": "m"}}, {"e2e": {"msg": "m", "iat": "x", "tok": "t", "z": 0, "n": "AA", "ct": "AA"}}])
def test_open_event_reports_malformed_envelopes_as_e2e_errors(outgoing):
    with pytest.raises(e2e.E2EError):
        e2e.open_event(outgoing, _keys(), installation_id=INSTALLATION, gateway_id=GATEWAY)
