"""End-to-end protocol provenance: the real plugin middleware vs a real relay.

The unit tests mock the relay's poll responses, so they can drift from what
plugin 0.3 + relay 0.3 actually put on the wire (that is exactly how the
scalar push bug survived: the mock answered the OLD scalar shape while the
plugin was really pushing a batch). These tests spawn the actual Node relay
(``relay/src/server.mjs``, APNS_MODE=accept) and drive the full boundary:

    legacy scalar args -> scalar decision -> scalar answer -> scalar result
    questions[] args   -> batch decision  -> per-qid answers -> batch result

The pushed event originates from ``clarify_loop.middleware`` itself (the same
``push_event``/``clarify_decision``/``sanitize_decision`` chain production
runs), is delivered by the real ``client.send_now``, and the gateway-side
poll is the real ``client.poll_decision``.
"""

import importlib
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELAY_DIR = ROOT / "relay"

# Same hermetic Hermes stubs as test_clarify_loop.py (idempotent when that
# module already ran in this process).
if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "tools" not in sys.modules:
    _tools = types.ModuleType("tools")
    _tools.__path__ = []
    sys.modules["tools"] = _tools
if "tools.clarify_tool" not in sys.modules:
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool

# The repo directory name is hyphenated, so import the plugin modules under a
# synthetic package (the same trick Hermes's plugin loader performs).
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg
if "conduit_push.clarify_loop" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("conduit_push.clarify_loop", ROOT / "clarify_loop.py")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["conduit_push.clarify_loop"] = _module
    _spec.loader.exec_module(_module)

import conduit_push.clarify_loop as loop  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the real relay")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ephemeral_key_pem(node):
    # The relay's ApnsClient parses the key eagerly; an ephemeral P-256 key
    # satisfies it and is never used (APNS_MODE=accept never sends).
    result = subprocess.run(
        [node, "-e",
         "const {generateKeyPairSync}=require('node:crypto');"
         "const {privateKey}=generateKeyPairSync('ec',{namedCurve:'P-256'});"
         "process.stdout.write(privateKey.export({type:'sec1',format:'pem'}));"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _wait_healthy(base, process):
    deadline = time.time() + 15
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("relay process exited during startup")
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("relay did not become healthy")


@pytest.fixture(scope="module")
def relay():
    node = shutil.which("node")
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="conduit-clarify-e2e-"))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    key_path = tmpdir / "ephemeral-key.pem"
    key_path.write_text(_ephemeral_key_pem(node), encoding="utf-8")
    env = {
        **os.environ,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "PUBLIC_URL": f"https://relay-{port}.example",
        "DATA_PATH": str(tmpdir / "relay-data.json"),
        "APNS_KEY_PATH": str(key_path),
        "APNS_KEY_ID": "AAAAAAAAAA",
        "APNS_TEAM_ID": "BBBBBBBBBB",
        "APNS_TOPIC": "com.milim.relay",
        # Every APNs send succeeds without touching the network, so parked
        # decisions stay deliverable and the middleware's poll loop runs to
        # the answer (a rejected/thrown send would flip it undeliverable and
        # the middleware would fall back to the native path instead).
        "APNS_MODE": "accept",
    }
    process = subprocess.Popen(
        [node, "src/server.mjs"],
        cwd=str(RELAY_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_healthy(base, process)
        yield {"base": base}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def restore_real_client():
    """Restore the real transport on the SHARED client module.

    Must run per-test, not at import time: pytest imports every test module
    during collection but runs them later, so test_clarify_loop's tests
    install their hermetic fakes (load_state/poll_decision/enqueue) onto this
    same module object AFTER this file was imported. Reloading here puts the
    genuine functions back immediately before these tests run.
    """
    importlib.reload(loop.client)


@pytest.fixture()
def gateway(relay, restore_real_client, monkeypatch, tmp_path):
    """One registered device + claimed gateway, with the plugin state the
    real client functions read saved under a test-private path."""
    base = relay["base"]
    monkeypatch.setattr(loop.client, "state_path", lambda: tmp_path / "conduit-push.json")
    registered = _request(base, "/v1/installations", method="POST", body={
        "bundle_id": "com.milim.relay",
        "device_token": "a" * 64,
        "environment": "production",
    })
    assert registered[0] == 201, registered
    device_credential = registered[1]["credential"]
    installation_id = registered[1]["installation"]["id"]
    pairing = _request(base, f"/v1/installations/{installation_id}/pairings", method="POST", credential=device_credential)
    assert pairing[0] == 201, pairing
    # The REAL pairing entry point — this saves the state that send_now and
    # poll_decision later read.
    state = loop.client.claim_pairing(pairing[1]["pairing_code"], relay_url=base, gateway_name="clarify e2e")
    # PUBLIC_URL is a stand-in in tests; keep the saved state pointed at the
    # local process so the plugin's requests reach the relay under test.
    state["relay_url"] = base
    loop.client.save_state(state)
    return {"base": base, "device_credential": device_credential}


@pytest.fixture()
def fast_poll():
    previous = loop.POLL_INTERVAL_SECONDS
    loop.POLL_INTERVAL_SECONDS = 0.05
    yield
    loop.POLL_INTERVAL_SECONDS = previous


def _request(base, path, *, method="GET", body=None, credential=""):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _run_clarify(monkeypatch, args):
    """Run loop.middleware against the real relay in a worker thread.

    ``enqueue`` is intercepted only to make delivery synchronous and capture
    the pushed event; the delivery itself is the real ``send_now``. The
    original (native gateway) path blocks on ``release`` so the relay answer
    wins the race, exactly like a desktop client that never answers.
    """
    events = []
    real_send_now = loop.client.send_now

    def capture_and_send(event):
        events.append(event)
        real_send_now(event)

    monkeypatch.setattr(loop.client, "enqueue", capture_and_send)
    release = threading.Event()
    holder = {}

    def blocking_original(unused_args):
        release.wait(10)
        return "native answer"

    def run_middleware():
        try:
            holder["result"] = loop.middleware(
                tool_name="clarify",
                args=args,
                next_call=blocking_original,
                session_id="sess-e2e",
            )
        except Exception as exc:  # surfaced by the caller's assertions
            holder["error"] = exc

    worker = threading.Thread(target=run_middleware, daemon=True)
    worker.start()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.01)
    return worker, holder, events, release


def test_legacy_scalar_invocation_round_trips_scalar_through_the_real_relay(relay, gateway, fast_poll, monkeypatch):
    worker, holder, events, release = _run_clarify(monkeypatch, {
        "question": "Which environment?",
        "choices": ["staging", "prod"],
    })
    try:
        assert events, "the middleware never pushed the input.needed event"
        event = events[0]
        decision = event["decision"]
        assert event["type"] == "input.needed"
        assert decision["kind"] == "clarify"
        assert decision["request_id"].startswith("conduit-push-")
        assert decision["question"] == "Which environment?"
        assert decision["choices"] == ["staging", "prod"]
        # THE contract: a legacy scalar invocation pushes the scalar decision,
        # so the relay parks it on the scalar path end to end.
        assert "questions" not in decision, "legacy scalar must not push a batch decision"

        # The device answers through the relay's scalar respond path.
        status, payload = _request(
            gateway["base"],
            f"/v1/decisions/{decision['request_id']}/respond",
            method="POST",
            body={"answer": "staging"},
            credential=gateway["device_credential"],
        )
        assert status == 200, payload
        assert payload == {"status": "answered"}, payload

        worker.join(timeout=10)
        assert not worker.is_alive(), "middleware did not observe the relay answer"
        assert "error" not in holder, holder.get("error")
        assert json.loads(holder["result"]) == {
            "question": "Which environment?",
            "choices_offered": ["staging", "prod"],
            "user_response": "staging",
        }

        # What the gateway's poll sees for a scalar decision is the scalar
        # answer shape — the exact wire contract the mocked unit tests assume.
        assert loop.client.poll_decision(decision["request_id"]) == {
            "status": "answered",
            "answer": "staging",
        }
    finally:
        release.set()


def test_one_entry_questions_invocation_round_trips_batch_through_the_real_relay(relay, gateway, fast_poll, monkeypatch):
    # Guard against "fixing" scalar by collapsing every single-question case:
    # a one-entry questions[] invocation is BATCH protocol on the wire too.
    worker, holder, events, release = _run_clarify(monkeypatch, {
        "questions": [{"question": "Which environment?", "choices": ["staging", "prod"]}],
    })
    try:
        assert events, "the middleware never pushed the input.needed event"
        decision = events[0]["decision"]
        assert decision["request_id"].startswith("conduit-push-")
        # Strict-boolean sanitizer contract: multi_select rides only when
        # true; the relay's store adds the wire-shape false on intake.
        assert decision["questions"] == [
            {"qid": "q0", "question": "Which environment?", "choices": ["staging", "prod"]},
        ]

        status, payload = _request(
            gateway["base"],
            f"/v1/decisions/{decision['request_id']}/respond",
            method="POST",
            body={"answer": "staging", "question_id": "q0"},
            credential=gateway["device_credential"],
        )
        assert status == 200, payload
        assert payload == {"status": "answered", "remaining": []}, payload

        worker.join(timeout=10)
        assert not worker.is_alive(), "middleware did not observe the relay answer"
        assert "error" not in holder, holder.get("error")
        assert json.loads(holder["result"]) == {
            "responses": [
                {"question": "Which environment?", "choices_offered": ["staging", "prod"], "user_response": "staging"},
            ]
        }

        # The gateway's poll sees the per-qid answers map, never a scalar answer.
        poll = loop.client.poll_decision(decision["request_id"])
        assert poll == {"status": "answered", "answers": {"q0": "staging"}, "remaining": []}, poll
    finally:
        release.set()
