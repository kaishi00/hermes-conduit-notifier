"""CONDUIT_PUSH_PROXY (push_proxy.py): this plugin's HTTPS traffic through a
proxy of its own, without touching a host that doesn't set it (notifier #746,
proposed by aminrpg, whose CONNECT and credential-isolation tests these
follow)."""

import http.server
import importlib.util
import logging
import os
import pathlib
import sys
import threading
import types
import urllib.error
import urllib.request

import pytest

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
    sys.modules[name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
push_proxy = _private_copy("conduit_push.push_proxy", ROOT / "push_proxy.py")
client = _private_copy("conduit_push._client_push_proxy_under_test", ROOT / "client.py")
room_mailbox = _private_copy("conduit_push._room_mailbox_push_proxy_under_test", ROOT / "room_mailbox.py")
maps_host = _private_copy("conduit_push._maps_host_push_proxy_under_test", ROOT / "maps_host.py")
cli = _private_copy("conduit_push._cli_push_proxy_under_test", ROOT / "cli.py")


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for name in ("CONDUIT_PUSH_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                 "no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)


class _Proxy:
    """A loopback proxy that refuses every CONNECT with 407 and records it."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self):
                outer.requests.append((self.command, self.path, dict(self.headers)))
                self.send_error(407, "proxy authentication required")

            do_CONNECT = do_GET = do_POST = _any

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def proxy():
    made = _Proxy()
    yield made
    made.close()


# --- Unset: nothing changes ----------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_empty_hands_every_caller_its_own_opener(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("CONDUIT_PUSH_PROXY", value)
    default = object()
    assert push_proxy.opener(default, lambda proxy: pytest.fail("built a proxied opener")) is default
    assert push_proxy.proxy_url() is None and push_proxy.status() is None
    assert push_proxy.websocket_kwargs("wss://relay.example/v1/watch", lambda url, proxy=None: None) == {}
    assert not push_proxy.proxies("https", "relay.example")
    # Each module's HTTPS request uses the very opener it always did.
    assert api.watch_tools._https_relay_opener() is api.watch_tools._relay_opener
    assert api.gemini_qwen_live._provider_opener() is api.gemini_qwen_live._opener
    assert api.core._proxy_opener(api.local_live_agent._local_live_front_opener, api.gemini_qwen_live._proxied_opener) \
        is api.local_live_agent._local_live_front_opener
    assert api.core._proxy_websocket_kwargs("wss://api.x.ai/v1/realtime", lambda url, proxy=None: None) == {}
    assert room_mailbox._https_opener() is room_mailbox._opener
    assert client.push_proxy.opener(client._opener, client._proxied_opener) is client._opener


def test_unset_still_follows_the_hosts_own_https_proxy(monkeypatch, proxy):
    # What a host with a process-wide HTTPS_PROXY had before this setting, it keeps.
    monkeypatch.setenv("https_proxy", proxy.url)
    monkeypatch.setenv("HTTPS_PROXY", proxy.url)
    fresh = _private_copy("conduit_push._client_env_proxy_under_test", ROOT / "client.py")
    with pytest.raises(urllib.error.URLError, match="Tunnel connection failed: 407"):
        fresh.request_json("https://relay.example/v1/events", method="POST", credential="secret", payload={})
    assert proxy.requests[0][:2] == ("CONNECT", "relay.example:443")


# --- Set: HTTPS goes through it, plain HTTP and NO_PROXY hosts don't ------------------


def test_relay_https_goes_through_the_proxy_without_the_credential(monkeypatch, proxy):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", proxy.url)
    with pytest.raises(urllib.error.URLError, match="Tunnel connection failed: 407"):
        client.request_json("https://relay.example/v1/events", method="POST", credential="secret", payload={})
    assert len(proxy.requests) == 1
    method, target, headers = proxy.requests[0]
    assert (method, target) == ("CONNECT", "relay.example:443")
    assert "Authorization" not in headers and "secret" not in repr(headers)


def test_plain_http_never_goes_through_it(monkeypatch, proxy):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", proxy.url)
    relay = _Proxy()  # answers 407 to anything: enough to see who got the request
    try:
        with pytest.raises(client.RelayRejected):
            client.request_json(f"{relay.url}/v1/events", method="POST", credential="c", payload={})
        assert proxy.requests == [] and relay.requests[0][0] == "POST"
    finally:
        relay.close()


def test_a_host_no_proxy_names_is_reached_directly(monkeypatch, proxy):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", proxy.url)
    monkeypatch.setenv("no_proxy", "relay.example")
    monkeypatch.setenv("NO_PROXY", "relay.example")
    assert not push_proxy.proxies("https", "relay.example")
    assert push_proxy.proxies("https", "api.x.ai")
    assert push_proxy.websocket_kwargs("wss://relay.example/x", lambda url, proxy=None: None) == {}


def test_every_https_opener_in_the_plugin_uses_it(monkeypatch, proxy):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", proxy.url)
    request = lambda url: urllib.request.Request(url, data=b"{}", method="POST")  # noqa: E731
    openers = {
        "watch relay": api.watch_tools._https_relay_opener(),
        "voice providers": api.gemini_qwen_live._provider_opener(),
        "local live front": api.core._proxy_opener(api.local_live_agent._local_live_front_opener,
                                                   api.gemini_qwen_live._proxied_opener),
        "rooms mailbox": room_mailbox._https_opener(),
        "maps": maps_host.push_proxy.opener(maps_host._OPENER, maps_host._proxied_opener),
    }
    for name, opener in openers.items():
        with pytest.raises(urllib.error.URLError, match="Tunnel connection failed: 407"):
            opener.open(request(f"https://{name.replace(' ', '-')}.example/x"), timeout=5)
    assert [r[1] for r in proxy.requests] == [f"{n.replace(' ', '-')}.example:443" for n in openers]
    # Kept while the setting stays the same.
    assert api.watch_tools._https_relay_opener() is openers["watch relay"]
    assert room_mailbox._proxied("https", "mailbox.example")


def test_websockets_get_the_proxy_for_wss_only(monkeypatch):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(push_proxy, "_ws_proxy_support", None)

    def connect(url, proxy=True, **kwargs):
        return None

    assert push_proxy.websocket_kwargs("wss://relay.example/v1/watch", connect) == {"proxy": "http://proxy.example:3128"}
    assert push_proxy.websocket_kwargs("ws://192.168.1.5:8000/speech", connect) == {}


def test_an_older_websockets_without_proxy_support_connects_as_before(monkeypatch, caplog):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(push_proxy, "_ws_proxy_support", None)
    with caplog.at_level(logging.WARNING, logger="hermes.plugins.conduit_push"):
        assert push_proxy.websocket_kwargs("wss://relay.example/x", lambda url, extra_headers=None: None) == {}
    assert "older than 15" in caplog.text


# --- A bad value is ignored, said once, never echoed ---------------------------------


@pytest.mark.parametrize("value", ["socks5://user:hunter2@proxy.example:1080", "proxy.example:3128",
                                   "http://user:hunter2@proxy.example:99999", "http://", "http://proxy.example/path"])
def test_a_bad_value_is_ignored_and_logged_once_without_the_password(monkeypatch, caplog, value):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", value)
    monkeypatch.setattr(push_proxy, "_parsed", None)
    default = object()
    with caplog.at_level(logging.WARNING, logger="hermes.plugins.conduit_push"):
        assert push_proxy.opener(default, lambda proxy: pytest.fail("used a bad proxy")) is default
        assert push_proxy.opener(default, lambda proxy: pytest.fail("used a bad proxy")) is default
    warnings = [r for r in caplog.records if "CONDUIT_PUSH_PROXY is ignored" in r.getMessage()]
    assert len(warnings) == 1
    assert "hunter2" not in caplog.text
    assert push_proxy.status().startswith("ignored (") and "hunter2" not in push_proxy.status()


def test_status_names_the_proxy_without_its_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CONDUIT_PUSH_PROXY", "http://user:hunter2@proxy.example:3128")
    assert push_proxy.status() == "http://proxy.example:3128 (HTTPS requests only)"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "load_state", lambda: {"gateway_name": "box", "relay_url": "https://relay.example"})
    cli.dispatch(types.SimpleNamespace(conduit_push_action="status"))
    out = capsys.readouterr().out
    assert "Proxy (CONDUIT_PUSH_PROXY): http://proxy.example:3128 (HTTPS requests only)" in out
    assert "hunter2" not in out


def test_status_says_nothing_when_unset(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "load_state", lambda: {"gateway_name": "box", "relay_url": "https://relay.example"})
    cli.dispatch(types.SimpleNamespace(conduit_push_action="status"))
    assert "Proxy" not in capsys.readouterr().out
