"""Real relay transport for four independently encrypted installations.

APNS_MODE=accept captures the APNs payload; these are protocol tests, not
claims that Apple's network or physical devices delivered a banner.
"""
import hashlib
import json
import time

from test_clarify_relay_e2e import relay, _request, _captured
from test_multidevice_delivery import client, devices, event


def test_four_devices_open_their_own_push_and_release_stale_answers(relay, devices):
    credentials = {}
    states = {}
    before = len(_captured(relay))
    for index, name in enumerate(("device-a", "device-b", "device-c", "device-d")):
        code, registration = _request(relay["base"], "/v1/installations", method="POST", body={
            "bundle_id": "com.milim.relay", "device_token": hashlib.sha256(name.encode()).hexdigest(),
            "environment": "production",
        })
        assert code == 201, registration
        installation = registration["installation"]["id"]
        device_credential = registration["credential"]
        code, pairing = _request(relay["base"], f"/v1/installations/{installation}/pairings",
                                 method="POST", credential=device_credential)
        assert code == 201, pairing
        code, state = _request(relay["base"], "/v1/pairings/claim", method="POST",
                               body={"pairing_code": pairing["pairing_code"], "gateway_name": name})
        assert code == 200, state
        _, saved = devices(installation, first=index == 0, credential=state["credential"],
                           gateway_id=state["gateway_id"], relay_url=relay["base"],
                           e2e={"kid": f"{index + 1:032x}", "secret": client.e2e.b64u(bytes([index + 1]) * 32)})
        credentials[installation] = device_credential
        states[installation] = saved

    request_id = "multidevice-real-request"
    decision = {"kind": "clarify", "request_id": request_id, "question": "Where?",
                "questions": [{"qid": "q0", "question": "Where?", "choices": ["staging", "prod"]},
                              {"qid": "q1", "question": "Notify?", "choices": ["yes", "no"]}]}
    assert client.enqueue(event(type="input.needed", decision=decision))
    for events, _ in client._device_lanes.values():
        events.join()
    payloads = _captured(relay)[before:]
    assert len(payloads) == 4
    opened = []
    for delivery in payloads:
        payload = delivery["notification"]["payload"]
        assert "Where?" not in json.dumps(payload)
        envelope = payload["conduit_e2e"]
        matches = []
        for state in states.values():
            try:
                inner = client.e2e.open_event({"type": "input.needed", "e2e": envelope},
                    client.e2e.keys_from_state(state), installation_id=state["installation_id"], gateway_id=state["gateway_id"])
                matches.append(inner)
            except Exception:
                continue
        assert len(matches) == 1, "each envelope opens only for its intended recipient"
        opened.append(matches[0])
    assert all(inner["profile"] == "example-profile" and inner["session_id"] == "chat-1" for inner in opened)

    for state, qid, answer in zip(list(states.values())[1:3], ("q0", "q1"), ("staging", "yes")):
        sealed = client.e2e.seal_answer(answer, client.e2e.keys_from_state(state), request_id=request_id,
            question_id=qid, installation_id=state["installation_id"], gateway_id=state["gateway_id"])
        status, result = _request(relay["base"], f"/v1/decisions/{request_id}/respond", method="POST",
            body={"answer": sealed, "question_id": qid}, credential=credentials[state["installation_id"]])
        assert status == 200, result
    assert client.poll_decision(request_id) == {"status": "answered", "answers": {"q0": "staging", "q1": "yes"}, "remaining": []}
    assert client.cancel_decision(request_id)
    for state in states.values():
        status, result = _request(relay["base"], f"/v1/decisions/{request_id}/respond", method="POST",
            body={"answer": "late", "question_id": "q0"}, credential=credentials[state["installation_id"]])
        assert status == 410, result
