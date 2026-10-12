"""Synthetic, loopback-only transport and pairing security regressions."""
import importlib.util
import ipaddress
import json
import ssl
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
if "conduit_push" not in sys.modules:
    package = types.ModuleType("conduit_push")
    package.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = package


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


client = _load("conduit_push.client", ROOT / "client.py")


def test_client_loader_reuses_registered_module():
    assert _load("conduit_push.client", ROOT / "missing-client.py") is client


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "conduit-push.json"
    monkeypatch.setattr(client, "state_path", lambda: path)
    return path


@pytest.fixture
def tls_servers(tmp_path, monkeypatch):
    # Throwaway trusted localhost certificate, never a production key.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([
                       x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
                   ]), critical=False).sign(key, hashes.SHA256()))
    cert_path = tmp_path / "localhost.pem"
    key_path = tmp_path / "localhost.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    key_path.chmod(0o600)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    servers = []

    def start(responder):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                 "method": self.command, "body": body})
                status, headers, payload = responder(self.path)
                self.send_response(status)
                for header, value in headers.items():
                    self.send_header(header, value)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            do_POST = do_GET
            do_DELETE = do_GET

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"https://127.0.0.1:{server.server_port}", requests

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("same_origin", [False, True])
def test_redirect_never_reaches_sink(tls_servers, status, method, same_origin):
    sink_url, sink_requests = tls_servers(lambda path: (200, {}, {"ok": True}))
    source_url, source_requests = tls_servers(
        lambda path: (status, {"Location": "/sink" if same_origin else sink_url + "/sink"}, {}))
    rejected = None
    try:
        client.request_json(source_url + "/start", method=method,
                            credential="synthetic-test-bearer", payload={} if method == "POST" else None)
    except client.RelayRejected as error:
        rejected = error
    assert sink_requests == [], "Redirect target received a request (and potentially the bearer)"
    assert len(source_requests) == 1, "Same-origin redirect must not be followed either"
    assert source_requests[0]["authorization"] == "Bearer synthetic-test-bearer"
    assert rejected is not None
    assert rejected.status == status


def test_trusted_https_request_still_works(tls_servers):
    url, requests = tls_servers(lambda path: (200, {}, {"ok": True}))
    assert client.request_json(url + "/ok", method="POST", payload={"test": True},
                               credential="synthetic-test-bearer") == {"ok": True}
    assert json.loads(requests[0]["body"]) == {"test": True}


UNSAFE_URLS = [
    "http://127.0.0.1:1", "ftp://relay.example", "//relay.example", "https:///missing",
    "https://user:synthetic-password@relay.example", "https://@relay.example",
    "https://relay.example/#fragment", "https://relay.example/#",
    "https://relay.example:invalid", "https://relay.example:65536",
    " https://relay.example", "https://relay.example\n", "https://relay.example\\path",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
@pytest.mark.parametrize("entry", ["request", "pairing"])
def test_unsafe_url_rejected_before_transport(url, entry, monkeypatch, state_file):
    transport = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"{}"

    def open_request(*args, **kwargs):
        transport.append(args)
        return Response()

    monkeypatch.setattr(client.urllib.request, "build_opener",
                        lambda *args: types.SimpleNamespace(open=open_request))
    if entry == "pairing":
        def claim_response(*args, **kwargs):
            transport.append(args)
            return {"credential": "synthetic-new-credential", "installation_id": "test-installation"}
        monkeypatch.setattr(client, "request_json", claim_response)
    with pytest.raises(ValueError, match="HTTPS"):
        if entry == "request":
            client.request_json(url, method="GET", credential="synthetic-test-bearer")
        else:
            client.claim_pairing("SYNTHETIC-CODE", relay_url=url)
    assert transport == []
    assert not state_file.exists()


@pytest.mark.parametrize("replacement", UNSAFE_URLS + [
    "https://other.example", "https://relay.example:8443", "https://relay.example.evil.example",
    "", None, False, [], {},
])
@pytest.mark.parametrize("existing", [False, True])
def test_pairing_rejects_unsafe_replacement_before_persisting(replacement, existing, state_file, monkeypatch):
    if existing:
        client.save_state({"credential": "synthetic-old-credential", "relay_url": "https://relay.example"})
    before = state_file.read_bytes() if existing else None
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: {
        "credential": "synthetic-new-credential", "installation_id": "test-installation",
        "relay_url": replacement,
    })
    with pytest.raises(ValueError):
        client.claim_pairing("SYNTHETIC-CODE", relay_url="https://relay.example")
    assert (state_file.read_bytes() if state_file.exists() else None) == before


@pytest.mark.parametrize("requested,replacement", [
    ("https://relay.example/", None),
    ("https://Relay.Example", "https://relay.example:443/base"),
    ("https://relay.example:8443/base", "https://RELAY.example:8443/new-base"),
    ("https://[::1]:8443", "https://[::1]:8443/base"),
])
def test_custom_https_pairing_accepts_only_same_origin(requested, replacement, state_file, monkeypatch):
    calls = []

    def claim_response(url, **kwargs):
        calls.append(url)
        body = {"credential": "synthetic-new-credential", "installation_id": "test-installation"}
        if replacement is not None:
            body["relay_url"] = replacement
        return body

    monkeypatch.setattr(client, "request_json", claim_response)
    state = client.claim_pairing("SYNTHETIC-CODE", relay_url=requested)
    assert calls == [requested.rstrip("/") + "/v1/pairings/claim"]
    assert state["relay_url"] == (replacement or requested.rstrip("/"))
    assert json.loads(state_file.read_text()) == state


def test_fresh_pairing_redacts_first_delivery(tls_servers, state_file):
    relay, requests = tls_servers(lambda path: (200, {}, {
        "credential": "synthetic-new-credential", "installation_id": "test-installation"
    } if path == "/v1/pairings/claim" else {"ok": True}))
    state = client.claim_pairing("SYNTHETIC-CODE", relay_url=relay)
    assert state.get("redact_content") is True
    key = state.get("redact_key", "")
    assert len(bytes.fromhex(key)) == 32
    assert client.load_state() == state
    event = {"event_id": "response:synthetic-id", "type": "response.ready",
             "title": "synthetic private title", "body": "synthetic private body"}
    assert client.send_now(event) == {"ok": True}
    outgoing = json.loads(requests[1]["body"])
    wire = json.dumps(outgoing)
    assert event["title"] not in wire
    assert event["body"] not in wire
    assert outgoing["event_id"] != event["event_id"]
    assert key not in wire
    assert requests[0]["authorization"] is None
    assert requests[1]["authorization"] == "Bearer synthetic-new-credential"


@pytest.mark.parametrize("choice", [True, False, None])
@pytest.mark.parametrize("have_key", [False, True])
def test_repair_preserves_explicit_privacy_choice(choice, have_key, state_file, monkeypatch):
    previous = {"credential": "synthetic-old-credential", "installation_id": "old-installation"}
    if choice is not None:
        previous["redact_content"] = choice
    if have_key:
        previous["redact_key"] = "ab" * 32
    client.save_state(previous)
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: {
        "credential": "synthetic-new-credential", "installation_id": "new-installation",
    })
    state = client.claim_pairing("SYNTHETIC-CODE")
    expected = choice if choice is not None else True
    assert state["redact_content"] is expected
    assert client.load_state() == state
    if have_key:
        assert state["redact_key"] == previous["redact_key"]
    elif expected:
        assert len(bytes.fromhex(state["redact_key"])) == 32
    else:
        assert "redact_key" not in state
    event = {"type": "response.ready", "event_id": "response:synthetic-id", "body": "private body"}
    outgoing = client.build_outgoing(event, state)
    assert ("private body" in json.dumps(outgoing)) is (not expected)


@pytest.mark.parametrize("invalid_choice", [None, "", 0, "false"])
def test_repair_without_boolean_choice_defaults_private(invalid_choice, state_file, monkeypatch):
    client.save_state({"credential": "synthetic-old-credential", "redact_content": invalid_choice})
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: {
        "credential": "synthetic-new-credential", "installation_id": "test-installation",
    })
    state = client.claim_pairing("SYNTHETIC-CODE")
    assert state["redact_content"] is True
    assert len(bytes.fromhex(state["redact_key"])) == 32


@pytest.mark.parametrize("returned", [False, True])
def test_pairing_rejects_query_base_before_persisting(returned, state_file, monkeypatch):
    transported = []

    def claim_response(url, **kwargs):
        transported.append(url)
        return {"credential": "synthetic-new-credential", "installation_id": "test-installation",
                "relay_url": "https://relay.example?debug=1"}

    monkeypatch.setattr(client, "request_json", claim_response)
    with pytest.raises(ValueError):
        client.claim_pairing("SYNTHETIC-CODE", relay_url=(
            "https://relay.example" if returned else "https://relay.example?debug=1"))
    assert len(transported) == (1 if returned else 0)
    assert not state_file.exists()


def test_pairing_redirect_does_not_replay_code_or_save_state(tls_servers, state_file):
    sink_url, sink_requests = tls_servers(lambda path: (200, {}, {
        "credential": "synthetic-new-credential", "installation_id": "test-installation"}))
    source_url, source_requests = tls_servers(
        lambda path: (302, {"Location": sink_url + "/claim"}, {}))
    with pytest.raises(client.RelayRejected):
        client.claim_pairing("SYNTHETIC-PRIVATE-CODE", relay_url=source_url)
    assert sink_requests == []
    assert len(source_requests) == 1
    assert json.loads(source_requests[0]["body"])["pairing_code"] == "SYNTHETIC-PRIVATE-CODE"
    assert not state_file.exists()
