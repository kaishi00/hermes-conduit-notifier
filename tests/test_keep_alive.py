"""The relay connection is kept between requests (#562): one TLS handshake
for a burst, a connection closed while idle is replaced without failing the
request, and a connection is only reused once its answer was read whole."""

import os
import datetime
import http.server
import importlib.util
import ipaddress
import json
import pathlib
import ssl
import sys
import threading
import time
import types
import urllib.error
import urllib.request

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _private_copy(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keep_alive = _private_copy("conduit_push._keep_alive_under_test", ROOT / "keep_alive.py")
client = _private_copy("conduit_push._client_keep_alive_under_test", ROOT / "client.py")


def _certificate(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _Relay(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, context):
        self.connections = 0
        self.requests = []
        self.close_after = False  # drop the connection after answering, without saying so
        super().__init__(("127.0.0.1", 0), _Handler)
        self.context = context

    def get_request(self):
        sock, address = super().get_request()
        self.connections += 1
        return self.context.wrap_socket(sock, server_side=True), address


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append((self.command, self.path, body, self.headers.get("Connection")))
        if self.path == "/big-error":
            payload = json.dumps({"error": "nope", "pad": "x" * 64 * 1024}).encode()
            status = 400
        else:
            payload, status = json.dumps({"ok": True, "n": len(self.server.requests)}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        if self.server.close_after:
            self.close_connection = True

    do_GET = do_POST = _answer


@pytest.fixture()
def relay(tmp_path):
    cert_path, key_path = _certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server = _Relay(server_context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(cert_path))
    handler = keep_alive.KeepAliveHTTPSHandler(context=client_context)
    opener = urllib.request.build_opener(client._RefuseRedirects, handler, urllib.request.ProxyHandler({}))
    yield server, handler, opener, f"https://127.0.0.1:{server.server_address[1]}"
    handler.close_idle()
    server.shutdown()
    server.server_close()


def _get(opener, url, timeout=5.0):
    with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
        return json.loads(response.read())


def test_a_burst_shares_one_connection(relay):
    server, handler, opener, base = relay
    for _ in range(5):
        assert _get(opener, f"{base}/v1/meta")["ok"] is True
    assert server.connections == 1
    assert handler.connects == 1
    # urllib's own handler asks to close; this one doesn't.
    assert all(connection != "close" for *_rest, connection in server.requests)


def test_request_json_goes_through_the_kept_connection(relay, monkeypatch):
    server, handler, opener, base = relay
    monkeypatch.setattr(client, "_opener", opener)
    for index in range(3):
        answer = client.request_json(f"{base}/v1/events", method="POST", payload={"i": index}, credential="c" * 32)
        assert answer["ok"] is True
    assert server.connections == 1
    assert [json.loads(body)["i"] for _m, _p, body, _c in server.requests] == [0, 1, 2]


def test_a_connection_closed_while_idle_is_replaced_and_the_request_sent_once(relay):
    server, handler, opener, base = relay
    server.close_after = True
    assert _get(opener, f"{base}/one")["n"] == 1
    # The relay closed it after answering; the pooled copy is dead.
    assert _get(opener, f"{base}/two")["n"] == 2
    assert [path for _m, path, _b, _c in server.requests] == ["/one", "/two"]
    assert server.connections == 2


def test_a_post_on_a_connection_the_relay_closed_while_idle_goes_out_on_a_new_one(relay, monkeypatch):
    server, handler, opener, base = relay
    monkeypatch.setattr(client, "_opener", opener)
    server.close_after = True
    assert _get(opener, f"{base}/one")["n"] == 1
    # The close has arrived: the dead connection is dropped before the
    # POST is written, so it isn't refused for fear of a replay.
    deadline = time.monotonic() + 2
    while handler._idle and time.monotonic() < deadline:
        if keep_alive._closed_by_peer(handler._idle[next(iter(handler._idle))][0][0]):
            break
        time.sleep(0.01)
    answer = client.request_json(f"{base}/v1/pairings/claim", method="POST", payload={"code": "x"})
    assert answer["ok"] is True
    assert server.connections == 2


def test_an_error_read_part_way_is_not_reused(relay, monkeypatch):
    server, handler, opener, base = relay
    monkeypatch.setattr(client, "_opener", opener)
    with pytest.raises(client.RelayRejected) as raised:
        client.request_json(f"{base}/big-error", method="GET")
    assert raised.value.status == 400
    assert _get(opener, f"{base}/after")["ok"] is True
    # The error's body was read only in part, so the next request opened anew.
    assert server.connections == 2


def test_a_new_connection_that_fails_is_a_url_error(relay):
    server, handler, opener, base = relay
    port = server.server_address[1]
    handler.close_idle()
    server.shutdown()
    server.server_close()
    with pytest.raises(urllib.error.URLError):
        _get(opener, f"https://127.0.0.1:{port}/gone", timeout=2.0)


def test_a_fork_starts_with_no_connections(relay, monkeypatch):
    server, handler, opener, base = relay
    _get(opener, f"{base}/one")
    monkeypatch.setattr(keep_alive.os, "getpid", lambda: -1)
    _get(opener, f"{base}/two")
    assert server.connections == 2


def test_a_connection_in_flight_across_a_fork_is_not_pooled(relay, monkeypatch):
    server, handler, opener, base = relay
    with opener.open(urllib.request.Request(f"{base}/one"), timeout=5.0) as response:
        # The process forks while the answer is read: neither side pools it.
        monkeypatch.setattr(keep_alive.os, "getpid", lambda: -1)
        response.read()
    assert handler._idle == {} or all(not idle for idle in handler._idle.values())



class _DroppedAfterWriting:
    """A kept connection the relay dropped after taking the request."""
    sock = types.SimpleNamespace(settimeout=lambda timeout: None)

    def __init__(self):
        self.timeout = None
        self.sent = []

    def request(self, method, selector, body, headers, encode_chunked=False):
        self.sent.append(method)

    def getresponse(self):
        raise keep_alive.http.client.RemoteDisconnected("closed")

    def close(self):
        pass


@pytest.mark.parametrize("method, resent", [("POST", False), ("GET", True), ("DELETE", True)])
def test_only_a_request_asked_twice_safely_is_sent_again(monkeypatch, method, resent):
    handler = keep_alive.KeepAliveHTTPSHandler()
    dropped = _DroppedAfterWriting()
    fresh = []
    monkeypatch.setattr(handler, "_take", lambda origin: dropped)

    class Refused(_DroppedAfterWriting):
        def request(self, *args, **kwargs):
            raise ConnectionRefusedError("the test stops here")

    def connect(origin, timeout):
        fresh.append(origin)
        return Refused()

    monkeypatch.setattr(handler, "_connect", connect)
    request = urllib.request.Request("https://relay.example/v1/pairings/claim",
                                     data=b"{}" if method == "POST" else None, method=method)
    request.timeout = 5.0
    with pytest.raises(urllib.error.URLError):
        handler.https_open(request)
    assert dropped.sent == [method]
    assert bool(fresh) is resent
