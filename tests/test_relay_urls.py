"""Relay URL rules shared by both pairing paths, and the CLI client's transport."""

import http.server
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _private_copy(name, path):
    # A private copy: other test files replace functions on the shared modules.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


urls = _private_copy("conduit_push._relay_urls_under_test", ROOT / "relay_urls.py")
client = _private_copy("conduit_push._client_transport_under_test", ROOT / "client.py")
cli = _private_copy("conduit_push._cli_under_test", ROOT / "cli.py")


@pytest.mark.parametrize("url", ["https://relay.example", "https://relay.example:8443/base", "http://127.0.0.1:8787",
                                 "http://localhost:8787", "http://[::1]:8787"])
def test_request_urls_fit_for_a_credential(url):
    assert urls.check_request_url(url) == url


@pytest.mark.parametrize("url", ["http://relay.example", "http://192.168.1.10:8787", "http://100.64.0.1",
                                 "https://relay.example/v1/events?x=1", "https://relay.example/?",
                                 "https://u:p@relay.example", "https://relay.example/#x", "ftp://relay.example",
                                 "https://relay.example/a b", "https://relay.example\\@evil", "https://", None,
                                 "https://relay.example:99999"])
def test_request_urls_refused(url):
    with pytest.raises(urls.RelayURLError):
        urls.check_request_url(url)


@pytest.mark.parametrize("url", ["https://relay.example/", "http://10.0.0.2:8787", "http://172.16.0.9",
                                 "http://192.168.1.10:8787/", "http://100.101.102.103:8787", "http://box.tail1.ts.net",
                                 "http://nas.local:8787", "http://[fd7a:115c:a1e0::1]:8787", "http://169.254.1.1"])
def test_claim_bases(url):
    assert urls.check_claim_base(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://relay.example", "http://8.8.8.8", "http://100.128.0.1",
                                 "http://169.254.169.254", "http://100.100.100.200", "http://[fd00:ec2::254]",
                                 "http://169.254.170.2", "http://[::ffff:169.254.169.254]",
                                 "http://metadata.google.internal", "http://METADATA.google.internal.",
                                 "https://relay.example?x=1", "https://relay.example/?", "https://u@relay.example"])
def test_claim_bases_refused(url):
    with pytest.raises(urls.RelayURLError):
        urls.check_claim_base(url)


@pytest.mark.parametrize("returned,claimed,saved", [
    ("https://relay.example/base/", "https://relay.example", "https://relay.example/base"),
    ("https://relay.example:443", "https://RELAY.example", "https://relay.example:443"),
    ("https://other.example", "https://relay.example", "https://relay.example"),
    ("https://relay.example:8443", "https://relay.example", "https://relay.example"),
    ("http://relay.example", "https://relay.example", "https://relay.example"),
    ("https://relay.example?x=1", "https://relay.example", "https://relay.example"),
    (None, "https://relay.example", "https://relay.example"),
    ("", "https://relay.example", "https://relay.example"),
    ("https://push.home.example", "http://100.101.102.103:8787", "https://push.home.example"),
    ("https://push.home.example", "https://box.tail1.ts.net", "https://push.home.example"),
    ("http://127.0.0.1:9000", "http://127.0.0.1:8787", "http://127.0.0.1:9000"),
    (None, "http://127.0.0.1:8787", "http://127.0.0.1:8787"),
])
def test_paired_relay_url(returned, claimed, saved):
    assert urls.paired_relay_url(returned, claimed) == saved


def test_a_lan_claim_without_an_https_answer_is_refused():
    with pytest.raises(urls.RelayURLError) as raised:
        urls.paired_relay_url("http://192.168.1.10:8787", "http://192.168.1.10:8787")
    assert "PUBLIC_URL" in str(raised.value)


def test_errors_never_echo_the_url():
    with pytest.raises(urls.RelayURLError) as raised:
        urls.check_request_url("https://user:hunter2@relay.example")
    assert "hunter2" not in str(raised.value)


class _Server:
    """A loopback HTTP server answering every request with `answer(handler)`."""

    def __init__(self, answer):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
                answer(self)

            do_GET = do_POST = do_DELETE = _any

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _json(handler, status, body):
    raw = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


@pytest.fixture
def servers():
    made = []

    def make(answer):
        made.append(_Server(answer))
        return made[-1]
    yield make
    for server in made:
        server.close()


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_redirects_are_refused_and_the_credential_goes_nowhere_else(servers, method):
    sink = servers(lambda h: _json(h, 200, {"ok": True}))

    def redirect(handler):
        handler.send_response(307)
        handler.send_header("Location", f"{sink.url}/stolen")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    source = servers(redirect)
    with pytest.raises(client.RelayRejected) as raised:
        client.request_json(f"{source.url}/v1/events", method=method, credential="secret-bearer",
                            payload={} if method == "POST" else None)
    assert raised.value.status == 307
    assert source.requests[0][2]["Authorization"] == "Bearer secret-bearer"
    assert sink.requests == []


def test_request_json_refuses_plain_http_off_this_machine_before_sending(monkeypatch):
    monkeypatch.setattr(client._opener, "open", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(ValueError):
        client.request_json("http://192.168.1.10:8787/v1/events", method="POST", credential="c", payload={})


def _paired_client(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "state_path", lambda: tmp_path / "conduit-push.json")


def test_cli_claim_saves_the_relays_https_url_after_a_lan_claim(monkeypatch, tmp_path):
    _paired_client(monkeypatch, tmp_path)
    seen = []

    def claim(url, **kwargs):
        seen.append(url)
        return {"credential": "c", "installation_id": "i", "gateway_id": "g", "relay_url": "https://push.home.example"}
    monkeypatch.setattr(client, "_request", claim)
    state = client.claim_pairing("CODE", "http://100.101.102.103:8787/", "box")
    assert seen == ["http://100.101.102.103:8787/v1/pairings/claim"]
    assert state["relay_url"] == "https://push.home.example"
    assert client.load_state()["relay_url"] == "https://push.home.example"


def test_cli_claim_refuses_a_bad_relay_url_before_spending_the_code(monkeypatch, tmp_path):
    _paired_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_request", lambda *a, **k: pytest.fail("claimed"))
    for url in ("http://relay.example", "https://relay.example?x=1", "https://u:p@relay.example"):
        with pytest.raises(ValueError):
            client.claim_pairing("CODE", url, "box")
    assert client.load_state() is None


def test_cli_claim_over_https_keeps_its_origin(monkeypatch, tmp_path):
    _paired_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_request", lambda url, **k: {"credential": "c", "installation_id": "i",
                                                              "relay_url": "https://elsewhere.example"})
    assert client.claim_pairing("CODE", "https://relay.example", "box")["relay_url"] == "https://relay.example"


def test_pair_prints_one_line_instead_of_a_traceback(monkeypatch, capsys):
    args = types.SimpleNamespace(conduit_push_action="pair", code="CODE", relay_url="http://relay.example", name="")
    assert cli.dispatch(args) == 1
    out = capsys.readouterr().out
    assert out.startswith("Pairing failed: ") and "https://" in out and "Traceback" not in out


def test_plain_http_skips_any_proxy_and_answers_are_capped(servers, monkeypatch):
    # A proxy that would see the bearer in clear: never used for plain HTTP.
    proxy = servers(lambda h: _json(h, 200, {"via": "proxy"}))
    monkeypatch.setenv("http_proxy", proxy.url)
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    fresh = _private_copy("conduit_push._client_proxy_under_test", ROOT / "client.py")
    relay = servers(lambda h: _json(h, 200, {"ok": True}))
    assert fresh.request_json(f"{relay.url}/v1/events", method="POST", credential="c", payload={}) == {"ok": True}
    assert proxy.requests == []

    huge = servers(lambda h: _json(h, 200, {"pad": "x" * 64}))
    monkeypatch.setattr(fresh, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(fresh.RelayRejected) as raised:
        fresh.request_json(f"{huge.url}/v1/events", method="POST", credential="c", payload={})
    assert raised.value.detail == "response_too_large"


def test_unpair_with_a_legacy_http_url_removes_the_local_pairing_and_says_so(monkeypatch, tmp_path, capsys):
    _paired_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "request_json", lambda *a, **k: pytest.fail("sent over plain http"))
    client.save_state({"credential": "c", "relay_url": "http://192.168.1.10:8787"})
    with pytest.raises(client.UnpairedLocally) as raised:
        client.unpair()
    assert "Remove this gateway in Conduit" in str(raised.value)
    assert client.load_state() is None


def test_unpair_keeps_a_pairing_claimed_while_the_relay_revoked_the_old_one(monkeypatch, tmp_path):
    _paired_client(monkeypatch, tmp_path)
    client.save_state({"credential": "old", "relay_url": "https://relay.example"})
    revoked = []

    def revoke(url, method, credential, **kwargs):
        revoked.append(credential)
        client.save_state({"credential": "new", "relay_url": "https://relay.example"})  # claimed meanwhile
        return {}

    monkeypatch.setattr(client, "request_json", revoke)
    assert client.unpair() is True
    assert revoked == ["old"]
    assert client.load_state()["credential"] == "new"


def test_unpair_when_not_paired_keeps_a_pairing_claimed_meanwhile(monkeypatch, tmp_path):
    _paired_client(monkeypatch, tmp_path)
    client.remove_state()
    loaded = client.load_state

    def load_then_claim():
        value = loaded()
        monkeypatch.setattr(client, "load_state", loaded)
        client.save_state({"credential": "new", "relay_url": "https://relay.example"})  # claimed meanwhile
        return value

    monkeypatch.setattr(client, "load_state", load_then_claim)
    assert client.unpair() is False
    assert client.load_state()["credential"] == "new"


def test_unpair_when_not_paired_still_removes_a_leftover_file(monkeypatch, tmp_path):
    _paired_client(monkeypatch, tmp_path)
    client.state_path().write_text("{}", encoding="utf-8")
    assert client.unpair() is False
    assert not client.state_path().exists()


@pytest.mark.parametrize("answer", [{}, [], {"credential": "", "installation_id": "i"}, {"credential": "c"}])
def test_cli_claim_refuses_a_malformed_answer_without_saving(monkeypatch, tmp_path, answer):
    _paired_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_request", lambda *a, **k: answer)
    with pytest.raises(ValueError):
        client.claim_pairing("CODE", "https://relay.example", "box")
    assert client.load_state() is None


def test_https_is_recognised_in_any_case():
    assert urls.is_https("HTTPS://relay.example") and urls.is_https("https://relay.example")
    assert not urls.is_https("http://127.0.0.1:8787")


def test_status_warns_about_a_saved_plain_http_relay(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_state", lambda: {"credential": "c", "relay_url": "http://192.168.1.10:8787"})
    monkeypatch.setattr(cli, "e2e_status", lambda state: {"enabled": False, "crypto": True})
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    assert "not a usable https://" in capsys.readouterr().out


def test_test_prints_one_line_for_a_broken_saved_pairing(monkeypatch, capsys):
    def broken(event):
        raise KeyError("relay_url")
    monkeypatch.setattr(cli, "send_now", broken)
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="test", corrupt=False)) == 1
    assert "did not accept" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://metadata.google.internal", "https://169.254.169.254",
                                 "https://sub.metadata.google.internal", "http://metadata.google.internal"])
def test_metadata_addresses_are_refused_over_any_scheme(url):
    for check in (urls.check_claim_base, urls.check_request_url):
        with pytest.raises(urls.RelayURLError):
            check(url)


def test_metadata_addresses_still_count_as_private():
    assert urls.is_private("169.254.169.254") and urls.is_private("metadata.google.internal")


def test_status_strips_control_characters(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_state", lambda: {"credential": "c", "relay_url": "https://relay.example",
                                                    "gateway_name": "box\x1b[31m"})
    monkeypatch.setattr(cli, "e2e_status", lambda state: {"enabled": False, "crypto": True})
    assert cli.dispatch(types.SimpleNamespace(conduit_push_action="status")) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "Paired: box[31m" in out and "Warning" not in out
