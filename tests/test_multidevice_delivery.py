"""Independent recipients for one profile, without sharing credentials or keys."""

import hashlib
import json
import threading

import pytest

from test_delivery import ROOT, _private_copy

client = _private_copy("conduit_push._client_multidevice_under_test", ROOT / "client.py")


@pytest.fixture
def devices(tmp_path, monkeypatch):
    primary = tmp_path / "conduit-push.json"
    monkeypatch.setattr(client, "state_path", lambda: primary)
    client.start()

    def add(installation, *, first=False, **fields):
        path = primary if first else tmp_path / "conduit-push-devices" / f"{hashlib.sha256(installation.encode()).hexdigest()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"installation_id": installation, "gateway_id": f"gw-{installation}",
                 "credential": f"credential-{installation}", "relay_url": "https://relay.example",
                 "require_e2e": True, **fields}
        path.write_text(json.dumps(state))
        return path, state

    yield add
    client.stop()


def event(**fields):
    return {"event_id": "response:same-turn", "type": "response.ready", "session_id": "chat-1",
            "profile": "example-profile", "body": "Done", **fields}


def test_every_device_receives_its_own_sealed_copy(devices, monkeypatch):
    states = {}
    for index, name in enumerate(("device-a", "device-b", "device-c", "device-d")):
        _, state = devices(name, first=index == 0, e2e={"kid": f"{index + 1:032x}",
                                                      "secret": client.e2e.b64u(bytes([index + 1]) * 32)})
        states[name] = state
    sent = {}
    done = threading.Event()

    def send(payload, state, timeout):
        outgoing = client.build_outgoing(payload, state)
        keys = client.e2e.keys_from_state(state)
        opened = client.e2e.open_event(outgoing, keys, installation_id=state["installation_id"],
                                       gateway_id=state["gateway_id"])
        sent[state["installation_id"]] = (state["credential"], opened)
        if len(sent) == 4:
            done.set()
        return {"accepted": True}

    monkeypatch.setattr(client, "_send", send)
    assert client.enqueue(event())
    assert done.wait(3), sent
    assert set(sent) == set(states)
    assert {value[0] for value in sent.values()} == {state["credential"] for state in states.values()}
    assert all(value[1]["body"] == "Done" for value in sent.values())


def test_an_offline_device_does_not_hold_up_other_devices(devices, monkeypatch):
    devices("offline", first=True)
    devices("device-c")
    blocked, release, delivered = threading.Event(), threading.Event(), threading.Event()

    def send(payload, state, timeout):
        if state["installation_id"] == "offline":
            blocked.set()
            release.wait(3)
            raise ConnectionError("offline")
        delivered.set()
        return {"accepted": True}

    monkeypatch.setattr(client, "_send", send)
    try:
        assert client.enqueue(event())
        assert blocked.wait(1)
        assert delivered.wait(1), "An online recipient must not wait for an offline recipient's retry budget"
    finally:
        release.set()


def test_a_new_pairing_never_sends_content_before_key_handover(devices):
    _, state = devices("device-c", first=True)
    outgoing = client.build_outgoing(event(), state)
    assert "body" not in outgoing
    assert "session_id" not in outgoing


def test_remaining_devices_still_receive_after_primary_removed(devices, monkeypatch):
    primary, _ = devices("device-a", first=True)
    devices("device-c")
    primary.unlink()
    delivered = threading.Event()
    monkeypatch.setattr(client, "_send", lambda *args, **kwargs: delivered.set() or {"accepted": True})
    assert client.enqueue(event())
    assert delivered.wait(2)


def test_call_requests_keep_the_primary_route(devices, monkeypatch):
    devices("device-a", first=True)
    devices("device-c")
    sent = []
    monkeypatch.setattr(client, "_send", lambda payload, state, timeout: sent.append(state["installation_id"]) or {})
    assert client.enqueue(event(type="call.requested"))
    client._events.join()
    assert sent == ["device-a"]


def test_poll_accepts_answer_from_secondary_device_and_releases_every_copy(devices, monkeypatch):
    devices("device-a", first=True, require_e2e=False)
    devices("device-c", require_e2e=False)
    cancelled = []

    def request(url, *, method, credential, **kwargs):
        if method == "DELETE":
            cancelled.append(credential)
            return {}
        return {"status": "answered", "answer": "Yes"} if credential == "credential-device-c" else {"status": "pending"}

    monkeypatch.setattr(client, "request_json", request)
    assert client.poll_decision("req-1") == {"status": "answered", "answer": "Yes"}
    assert client.cancel_decision("req-1")
    assert set(cancelled) == {"credential-device-a", "credential-device-c"}


def test_batch_answers_on_different_devices_are_merged_once_per_question(devices, monkeypatch):
    devices("device-a", first=True, require_e2e=False)
    devices("device-c", require_e2e=False)
    statuses = {"credential-device-a": {"status": "pending", "answers": {"q1": "A"}, "remaining": ["q2"]},
                "credential-device-c": {"status": "pending", "answers": {"q2": "B"}, "remaining": ["q1"]}}
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: statuses[kwargs["credential"]])
    answer = client.poll_decision("req-batch")
    assert answer["status"] == "answered"
    assert answer["answers"] == {"q1": "A", "q2": "B"}
    assert answer["remaining"] == []


def test_batch_keeps_accepted_answers_when_one_device_is_unpaired(devices, monkeypatch):
    primary, _ = devices("device-a", first=True, require_e2e=False)
    devices("device-c", require_e2e=False)
    statuses = {"credential-device-a": {"status": "pending", "answers": {"q1": "A"}, "remaining": ["q2"]},
                "credential-device-c": {"status": "pending", "remaining": ["q1", "q2"]}}
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: statuses[kwargs["credential"]])
    assert client.poll_decision("req-unpair")["answers"] == {"q1": "A"}
    primary.unlink()
    statuses["credential-device-c"] = {"status": "pending", "answers": {"q2": "B"}, "remaining": ["q1"]}
    assert client.poll_decision("req-unpair") == {
        "status": "answered", "answers": {"q1": "A", "q2": "B"}, "remaining": [],
    }


def test_held_chat_quiets_only_the_installation_looking_at_it(devices, monkeypatch):
    from conduit_push import chat_on_screen
    primary, _ = devices("device-a", first=True)
    devices("device-b")
    chat_on_screen.hold(primary.parent, "mac", ["chat-1"], 30, installation_id="device-a")
    delivered = threading.Event()
    sent = []

    def send(payload, state, timeout):
        sent.append(state["installation_id"])
        delivered.set()
        return {}

    monkeypatch.setattr(client, "_send", send)
    import time
    assert client.enqueue(event(_notification_started=time.monotonic()))
    assert delivered.wait(1)
    for events, _ in client._device_lanes.values():
        events.join()
    assert sent == ["device-b"]


def test_repairing_a_device_drops_its_old_queued_event(devices, monkeypatch):
    path, old = devices("device-a", first=True)
    devices("device-c")
    path.write_text(json.dumps({**old, "credential": "new-credential"}))
    sent = []
    monkeypatch.setattr(client, "_send", lambda *args, **kwargs: sent.append(args) or {})
    assert not client._deliver(event(), pairing=client._pairing_of(old))
    assert sent == []


def test_late_delivery_cannot_reopen_a_released_question(devices, monkeypatch):
    devices("device-a", first=True)
    started, release = threading.Event(), threading.Event()
    cancelled = []

    def send(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(client, "_send", send)
    monkeypatch.setattr(client, "request_json", lambda url, **kw: cancelled.append((url, kw["method"])) or {})
    request = event(type="input.needed", decision={"request_id": "late-question"})
    assert client.enqueue(request)
    assert started.wait(1)
    assert client.cancel_decision("late-question")
    release.set()
    for events, _ in client._device_lanes.values():
        events.join()
    assert len(cancelled) == 2, "one release before the send returns, another after it could have parked"
    assert not client._deliver(request)


def test_missing_handover_key_rejects_injected_plaintext_answer(devices):
    _, state = devices("device-a", first=True)
    assert client.opened_answers({"status": "answered", "answer": "Yes"}, state, "req") == {"status": "rejected"}


def test_replaced_installations_do_not_exhaust_delivery_slots(devices, monkeypatch):
    sent = []
    monkeypatch.setattr(client, "_send", lambda payload, state, timeout: sent.append(state["installation_id"]) or {})
    for index in range(18):
        devices(f"replacement-{index}", first=True)
        assert client.enqueue(event(event_id=f"replacement:{index}"))
        for events, _ in list(client._device_lanes.values()):
            events.join()
    assert len(sent) == 18


def test_idle_device_worker_retires_and_can_restart(devices, monkeypatch):
    devices("device-a", first=True)
    monkeypatch.setattr(client, "_DEVICE_IDLE_S", 0.01)
    sent = []
    release = threading.Event()

    def send(payload, state, timeout):
        release.wait(2)
        sent.append(payload)
        return {"accepted": True}

    monkeypatch.setattr(client, "_send", send)
    assert client.enqueue(event())
    events, worker = next(iter(client._device_lanes.values()))
    release.set()
    events.join()
    worker.join(2)
    assert not worker.is_alive()
    assert not client._device_lanes
    assert client.enqueue(event())
    for events, _ in list(client._device_lanes.values()):
        events.join()
    assert len(sent) == 2


def test_a_rejected_partial_form_cannot_be_reported_complete(devices, monkeypatch):
    devices("device-a", first=True, require_e2e=False)
    devices("device-c", require_e2e=False)
    statuses = {"credential-device-a": {"status": "rejected", "answers": {"q1": "A"}},
                "credential-device-c": {"status": "unknown"}}
    monkeypatch.setattr(client, "request_json", lambda url, **kwargs: statuses[kwargs["credential"]])
    assert client.poll_decision("rejected-form") == {"status": "rejected", "answers": {"q1": "A"}}


def test_an_expired_batch_does_not_complete_from_cached_partial_answers(devices, monkeypatch):
    devices("device-a", first=True, require_e2e=False)
    devices("device-c", require_e2e=False)
    status = {"status": "pending", "answers": {"q1": "A"}, "remaining": ["q2"]}
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: status)
    assert client.poll_decision("expired-batch")["status"] == "pending"
    status = {"status": "unknown"}
    assert client.poll_decision("expired-batch")["status"] == "unknown"
