"""Pages from the host (dashboard/api/workspace_preview.py; desktop issue 451)."""

import importlib.util
import json
import os
import pathlib
import socket
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["CONDUIT_TESTS_NO_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load("conduit_plugin_api_preview", ROOT / "dashboard" / "plugin_api.py")
seen = []


class DevServer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
        if self.path.startswith("/moved"):
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/sneaky"):
            self.send_response(200)
            self.send_header("X-Conduit-Page-Status", "401")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"nope")
            return
        out = json.dumps({"path": self.path, "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "a=1; Path=/")
        self.send_header("Set-Cookie", "b=2; HttpOnly")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)

    do_GET = do_POST = do_HEAD = do_DELETE = _answer


@pytest.fixture
def dev_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DevServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seen.clear()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def state(monkeypatch):
    flags = {"enabled": True, "boundary": None}
    monkeypatch.setattr(api.terminals, "terminals_enabled", lambda: flags["enabled"])
    monkeypatch.setattr(api.workspace, "_managed_boundary", lambda: flags["boundary"])
    return flags


@pytest.fixture
def client(state):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def wrapped(pairs):
    return {"X-Conduit-Page-Headers": json.dumps(pairs)}


def test_a_page_reaches_the_dev_server_with_its_own_headers_only(client, dev_port):
    got = client.get(f"{BASE}/workspace/preview/{dev_port}/src/a%20b.ts?x=1&profile=dev",
                     headers={**wrapped([["Accept", "text/html"], ["Cookie", "page=1"], ["Accept-Encoding", "gzip"]]),
                              "Cookie": "hermes_session=secret", "X-Hermes-Session-Token": "secret"})
    assert got.status_code == 200
    assert got.headers["x-conduit-page-status"] == "200"
    assert got.headers["cache-control"] == "no-store"
    assert json.loads(got.content)["path"] == "/src/a%20b.ts?x=1&profile=dev"
    request = seen[-1]
    assert request["headers"]["Host"] == f"localhost:{dev_port}"
    assert request["headers"]["Cookie"] == "page=1"
    assert request["headers"]["Accept-Encoding"] == "identity"
    assert "X-Hermes-Session-Token" not in request["headers"]
    pairs = json.loads(got.headers["x-conduit-page-headers"])
    assert ["Set-Cookie", "a=1; Path=/"] in pairs and ["Set-Cookie", "b=2; HttpOnly"] in pairs
    assert "set-cookie" not in got.headers


def test_a_post_carries_its_body_and_the_root_path(client, dev_port):
    got = client.post(f"{BASE}/workspace/preview/{dev_port}/", content=b"hello",
                      headers=wrapped([["Content-Type", "text/plain"]]))
    assert json.loads(got.content) == {"path": "/", "body": "hello"}
    assert seen[-1]["headers"]["Content-Type"] == "text/plain"


def test_redirects_and_errors_come_back_wrapped_not_followed(client, dev_port):
    moved = client.get(f"{BASE}/workspace/preview/{dev_port}/moved", follow_redirects=False)
    assert moved.status_code == 200 and moved.headers["x-conduit-page-status"] == "302"
    assert ["Location", "/elsewhere"] in json.loads(moved.headers["x-conduit-page-headers"])
    missing = client.get(f"{BASE}/workspace/preview/{dev_port}/missing")
    assert missing.status_code == 200 and missing.headers["x-conduit-page-status"] == "404"
    assert missing.content == b"nope"


def test_nothing_listening_is_a_502(client):
    got = client.get(f"{BASE}/workspace/preview/{free_port()}/")
    assert got.status_code == 502 and "answers on port" in got.json()["detail"]


def test_refusals(client, state, dev_port):
    assert client.get(f"{BASE}/workspace/preview/0/").status_code == 400
    assert client.get(f"{BASE}/workspace/preview/070/").status_code == 400
    assert client.get(f"{BASE}/workspace/preview/x/").status_code == 400
    # The test client's server is on port 80: the dashboard's own port is never reached.
    assert client.get(f"{BASE}/workspace/preview/80/").status_code == 403
    bad = client.get(f"{BASE}/workspace/preview/{dev_port}/", headers={"X-Conduit-Page-Headers": "{"})
    assert bad.status_code == 400
    bad = client.get(f"{BASE}/workspace/preview/{dev_port}/", headers=wrapped([["X-A", "1\r\nX-B: 2"]]))
    assert bad.status_code == 400
    state["enabled"] = False
    off = client.get(f"{BASE}/workspace/preview/{dev_port}/")
    assert off.status_code == 403 and "turned off" in off.json()["detail"]
    state["enabled"] = True
    state["boundary"] = "/srv"
    assert client.get(f"{BASE}/workspace/preview/{dev_port}/").status_code == 403
    assert seen == []


def test_hop_headers_from_the_page_are_dropped(client, dev_port):
    client.get(f"{BASE}/workspace/preview/{dev_port}/",
               headers=wrapped([["Connection", "close"], ["Host", "evil"], ["X-Page", "yes"]]))
    headers = seen[-1]["headers"]
    assert headers["Host"] == f"localhost:{dev_port}" and headers["X-Page"] == "yes"


def test_the_capability_is_listed():
    assert "workspace-preview" in api.capabilities.ROUTE_CAPABILITIES


def test_a_header_http_cant_send_is_a_400(client, dev_port):
    got = client.get(f"{BASE}/workspace/preview/{dev_port}/", headers=wrapped([["X-Name", "\U0001f389"]]))
    assert got.status_code == 400 and got.headers["cache-control"] == "no-store"
    assert client.get(f"{BASE}/workspace/preview/{dev_port}/", headers=wrapped([["X:Evil", "1"]])).status_code == 400
    assert client.get(f"{BASE}/workspace/preview/{dev_port}/", headers=wrapped([["X-A", "a\x01b"]])).status_code == 400
    assert seen == []


def test_the_dev_server_cant_set_the_wrappers_headers(client, dev_port):
    got = client.get(f"{BASE}/workspace/preview/{dev_port}/sneaky")
    assert got.headers["x-conduit-page-status"] == "200"
    assert not any(name.lower().startswith("x-conduit-page-") for name, _ in json.loads(got.headers["x-conduit-page-headers"]))


def test_a_listener_that_isnt_http_answered_badly_not_unreachable(client):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def hang_up():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.close()

    thread = threading.Thread(target=hang_up, daemon=True)
    thread.start()
    try:
        got = client.get(f"{BASE}/workspace/preview/{port}/")
    finally:
        listener.close()
    assert got.status_code == 502 and "didn't answer properly" in got.json()["detail"]


def test_requests_past_the_slots_are_turned_away_not_queued(client, dev_port, monkeypatch):
    held = []
    for _ in range(api.workspace_preview.PREVIEW_SLOTS):
        assert api.workspace_preview._preview_slots.acquire(blocking=False)
        held.append(1)
    try:
        got = client.get(f"{BASE}/workspace/preview/{dev_port}/")
        assert got.status_code == 503 and got.headers["cache-control"] == "no-store"
    finally:
        for _ in held:
            api.workspace_preview._preview_slots.release()
    assert client.get(f"{BASE}/workspace/preview/{dev_port}/").status_code == 200


def test_a_connect_that_times_out_is_a_504(client, monkeypatch):
    class Slow:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            raise socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr(api.workspace_preview.http.client, "HTTPConnection", Slow)
    assert client.get(f"{BASE}/workspace/preview/{free_port()}/").status_code == 504
