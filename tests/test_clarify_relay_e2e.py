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
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
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
    internal_base = f"http://127.0.0.1:{port}"
    key_path = tmpdir / "ephemeral-key.pem"
    key_path.write_text(_ephemeral_key_pem(node), encoding="utf-8")

    # The Node relay is normally behind a TLS reverse proxy. Exercise that
    # topology locally with a throwaway trusted certificate, not an HTTP
    # exception in the production client or disabled certificate validation.
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([
                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .sign(tls_key, hashes.SHA256()))
    cert_path, tls_key_path = tmpdir / "tls.pem", tmpdir / "tls.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    tls_key_path.write_bytes(tls_key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    tls_key_path.chmod(0o600)

    class Proxy(BaseHTTPRequestHandler):
        def forward(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in ("host", "connection", "transfer-encoding")}
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                connection.request(self.command, self.path, body=data, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                connection.close()

        do_GET = do_POST = do_PUT = do_DELETE = forward

        def log_message(self, format, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, tls_key_path)
    proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
    base = f"https://127.0.0.1:{proxy.server_port}"
    trust = pytest.MonkeyPatch()
    trust.setenv("SSL_CERT_FILE", str(cert_path))
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    env = {
        **os.environ,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "PUBLIC_URL": base,
        "DATA_PATH": str(tmpdir / "relay-data.json"),
        "APNS_KEY_PATH": str(key_path),
        "APNS_KEY_ID": "AAAAAAAAAA",
        "APNS_TEAM_ID": "BBBBBBBBBB",
        "APNS_TOPIC": "com.milim.relay",
        "APNS_MODE": "accept",
        "APNS_CAPTURE_PATH": str(tmpdir / "apns-capture.jsonl"),
    }
    process = subprocess.Popen(
        [node, "src/server.mjs"], cwd=str(RELAY_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_healthy(internal_base, process)
        yield {"base": base, "capture": tmpdir / "apns-capture.jsonl"}
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=5)
        trust.undo()
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(tmpdir, ignore_errors=True)


_REAL_CLIENT_FUNCTIONS = ("load_state", "poll_decision", "enqueue", "cancel_decision", "send_now", "save_state", "state_path", "claim_pairing")


@pytest.fixture()
def restore_real_client():
    """Pin the real transport onto the SHARED client module for this test.

    Must run per-test, not at import time: pytest imports every test module
    during collection but runs them later, so test_clarify_loop's tests may
    have installed their hermetic fakes (load_state/poll_decision/enqueue/
    cancel_decision) onto this same module object before these tests run.
    Deliberately NOT importlib.reload: that would also wipe the fakes for
    tests that run AFTER this file, and pytest makes no cross-file ordering
    guarantee. Saving and restoring only this file's functions keeps both
    orderings hermetic.
    """
    saved = {name: getattr(loop.client, name) for name in _REAL_CLIENT_FUNCTIONS}
    importlib.reload(loop.client)
    yield
    for name, function in saved.items():
        setattr(loop.client, name, function)


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
    # These protocol/E2E tests explicitly opt into full card content; the
    # fresh-pairing default-redaction boundary is covered by client security tests.
    state["redact_content"] = False
    loop.client.save_state(state)
    return {"base": base, "device_credential": device_credential, "state": state}


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
        # Deliver FIRST, signal second: the relay parks the pending decision
        # before replying to /v1/events, so a returned send_now guarantees
        # the decision is findable by the test's /respond request. Appending
        # in finally keeps the event visible for diagnosis if the send raises
        # (the holder error then fails the test loudly). Returns True — this
        # stands in for client.enqueue, whose boolean the middleware now
        # reads to detect a local drop.
        try:
            real_send_now(event)
        finally:
            events.append(event)
        return True

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
    while not events and "error" not in holder and time.time() < deadline:
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


def _parked(relay, request_id):
    data = json.loads((relay["capture"].parent / "relay-data.json").read_text(encoding="utf-8"))
    matches = [entry for entry in data["pendingDecisions"].values() if entry["id"] == request_id]
    assert len(matches) == 1
    return matches[0]


def _captured(relay):
    path = relay["capture"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_private_pairing_first_push_and_repair_with_real_relay(relay, restore_real_client, monkeypatch, tmp_path):
    monkeypatch.setattr(loop.client, "state_path", lambda: tmp_path / "conduit-push.json")
    status, registered = _request(relay["base"], "/v1/installations", method="POST", body={
        "bundle_id": "com.milim.relay", "device_token": "b" * 64, "environment": "production",
    })
    assert status == 201, registered
    installation_id = registered["installation"]["id"]
    transport = []
    real_request = loop.client.request_json

    def capture_request(url, **kwargs):
        result = real_request(url, **kwargs)
        if url.endswith("/v1/events"):
            transport.append({"url": url, "event": kwargs["payload"], "response": result})
        return result

    monkeypatch.setattr(loop.client, "request_json", capture_request)
    records = []
    for index, (label, choice, expected) in enumerate([
        ("fresh pairing", None, True),
        ("explicit redaction on survives re-pairing", True, True),
        ("explicit redaction off survives re-pairing", False, False),
        ("missing choice defaults private on re-pairing", None, True),
        ("non-boolean choice defaults private on re-pairing", "false", True),
    ]):
        previous = loop.client.load_state()
        if previous:
            previous.pop("redact_content", None)
            if choice is not None:
                previous["redact_content"] = choice
            loop.client.save_state(previous)
        status, pairing = _request(
            relay["base"], f"/v1/installations/{installation_id}/pairings",
            method="POST", credential=registered["credential"],
        )
        assert status == 201, pairing
        state = loop.client.claim_pairing(
            pairing["pairing_code"], relay_url=relay["base"], gateway_name="privacy evidence",
        )
        assert state["redact_content"] is expected
        assert loop.client.load_state() == state
        assert loop.client.state_path().stat().st_mode & 0o777 == 0o600
        if previous:
            assert state["redact_key"] == previous["redact_key"]
        event = loop.push_event(
            "response.ready", identifier=f"response:privacy-evidence-{index}",
            session_id="synthetic-session", profile="synthetic-profile",
            title="SYNTHETIC PRIVATE TITLE", body="SYNTHETIC PRIVATE CHAT SNIPPET",
        )
        before = len(_captured(relay))
        response = loop.client.send_now(event)
        assert response["accepted"] is True and response["delivered"] is True
        delivered = _captured(relay)[before:]
        assert len(delivered) == 1
        outgoing = transport[-1]["event"]
        assert ("SYNTHETIC PRIVATE CHAT SNIPPET" in json.dumps(outgoing)) is (not expected)
        assert ("SYNTHETIC PRIVATE TITLE" in json.dumps(outgoing)) is (not expected)
        if expected:
            assert "SYNTHETIC PRIVATE" not in json.dumps(delivered)
        assert (outgoing["event_id"] != event["event_id"]) is expected
        assert state["redact_key"] not in json.dumps(outgoing)
        assert state["credential"] not in json.dumps(outgoing)
        records.append({
            "scenario": label,
            "persisted_state": {
                "relay_url": state["relay_url"], "redact_content": state["redact_content"],
                "redact_key_bytes": len(bytes.fromhex(state["redact_key"])),
                "file_mode": "0600", "encryption_provisioned": "e2e" in state,
            },
            "relay_request": transport[-1],
            "apns_payload": delivered[0]["notification"]["payload"],
        })
    evidence_dir = os.environ.get("CONDUIT_TEST_EVIDENCE_DIR")
    if evidence_dir:
        destination = pathlib.Path(evidence_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "private-pairing-real-relay.json").write_text(
            json.dumps({"transport": "trusted loopback HTTPS; APNs captured, not sent", "scenarios": records}, indent=2),
            encoding="utf-8",
        )


def test_encrypted_batch_clarify_round_trips_without_the_relay_seeing_content(relay, gateway, fast_poll, monkeypatch):
    e2e = sys.modules["conduit_push.e2e"]
    kid = "0123456789abcdef0123456789abcdef"
    secret = bytes(range(32))
    state = dict(gateway["state"])
    state["e2e"] = {"kid": kid, "secret": e2e.b64u(secret)}
    loop.client.save_state(state)
    keys = e2e.derive_keys(kid, secret)
    context = {"installation_id": state["installation_id"], "gateway_id": state["gateway_id"]}
    before = len(_captured(relay))
    released = []
    real_cancel = loop.client.cancel_decision
    monkeypatch.setattr(loop.client, "cancel_decision", lambda request_id: released.append(request_id) or real_cancel(request_id))

    worker, holder, events, release = _run_clarify(monkeypatch, {
        "questions": [
            {"question": "Which environment?", "choices": ["staging", "prod"]},
            {"question": "Ship the migration too?", "choices": ["yes", "no"]},
        ],
    })
    try:
        assert events, "the middleware never pushed the input.needed event"
        # What the APNs layer received: the generic alert, the envelope, and
        # no question text, choices or session id anywhere.
        delivered = _captured(relay)[before:]
        assert len(delivered) == 1
        payload = delivered[0]["notification"]["payload"]
        wire = json.dumps(payload)
        for text in ("Which environment?", "staging", "Ship the migration", "sess-e2e"):
            assert text not in wire
        assert payload["aps"]["mutable-content"] == 1
        assert payload["aps"]["alert"] == {"title": "Input needed", "body": "Hermes needs your response before it can continue."}
        assert payload["conduit"]["e2e"] == 1
        envelope = payload["conduit_e2e"]
        # The relay data file parked question ids only.
        assert "Which environment?" not in json.dumps(_parked(relay, envelope["req"]))

        # The phone opens the envelope exactly as delivered.
        inner = e2e.open_event({"type": "input.needed", "e2e": envelope}, keys, **context)
        decision = inner["decision"]
        request_id = decision["request_id"]
        assert envelope["req"] == request_id
        assert [q["question"] for q in decision["questions"]] == ["Which environment?", "Ship the migration too?"]

        # A plaintext answer from anywhere but the phone never answers: the
        # plugin releases the decision and Hermes' own clarify path decides.
        forged = _request(gateway["base"], f"/v1/decisions/{request_id}/respond", method="POST",
                          body={"answer": "prod", "question_id": "q0"}, credential=gateway["device_credential"])
        assert forged[0] == 200
        deadline = time.time() + 5
        while request_id not in released and time.time() < deadline:
            time.sleep(0.01)
        assert request_id in released, "the forged answer should have released the decision"
        assert worker.is_alive(), "the forged answer must not become the tool result"
        release.set()
        worker.join(timeout=10)
        assert holder.get("result") == "native answer"
    finally:
        release.set()


def test_sealed_answers_complete_an_encrypted_batch_clarify(relay, gateway, fast_poll, monkeypatch):
    e2e = sys.modules["conduit_push.e2e"]
    kid = "fedcba9876543210fedcba9876543210"
    secret = bytes(range(1, 33))
    state = dict(gateway["state"])
    state["e2e"] = {"kid": kid, "secret": e2e.b64u(secret)}
    loop.client.save_state(state)
    keys = e2e.derive_keys(kid, secret)
    context = {"installation_id": state["installation_id"], "gateway_id": state["gateway_id"]}
    before = len(_captured(relay))

    worker, holder, events, release = _run_clarify(monkeypatch, {
        "questions": [
            {"question": "Which environment?", "choices": ["staging", "prod"]},
            {"question": "Notify the team?", "choices": ["yes", "no"]},
        ],
    })
    try:
        envelope = _captured(relay)[before:][0]["notification"]["payload"]["conduit_e2e"]
        request_id = e2e.open_event({"type": "input.needed", "e2e": envelope}, keys, **context)["decision"]["request_id"]
        for qid, answer in (("q0", "staging"), ("q1", "yes")):
            sealed = e2e.seal_answer(answer, keys, request_id=request_id, question_id=qid, **context)
            status, payload = _request(gateway["base"], f"/v1/decisions/{request_id}/respond", method="POST",
                                       body={"answer": sealed, "question_id": qid}, credential=gateway["device_credential"])
            assert status == 200, payload
        parked = _parked(relay, request_id)
        assert [q["qid"] for q in parked["questions"]] == ["q0", "q1"]
        assert "staging" not in json.dumps(parked)
        worker.join(timeout=10)
        assert not worker.is_alive(), "middleware did not observe the sealed answers"
        assert "error" not in holder, holder.get("error")
        responses = json.loads(holder["result"])["responses"]
        assert [row["user_response"] for row in responses] == ["staging", "yes"]
    finally:
        release.set()
