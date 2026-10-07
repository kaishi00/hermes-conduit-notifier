"""Watch tool grants end to end: the real plugin code against a real relay.

A grant is opened through open_watch_grant, the plugin's own poller answers,
and a stand-in Watch seals its call the way Conduit does (the grant's call
key, ChaCha20-Poly1305) and posts it to the relay with the grant's relay key.
"""

import base64
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the real relay")


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_watch_e2e", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(base, path, *, method="GET", body=None, credential=""):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}


@pytest.fixture(scope="module")
def relay():
    node = shutil.which("node")
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="conduit-watch-e2e-"))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    key = subprocess.run(
        [node, "-e",
         "const {generateKeyPairSync}=require('node:crypto');"
         "const {privateKey}=generateKeyPairSync('ec',{namedCurve:'P-256'});"
         "process.stdout.write(privateKey.export({type:'sec1',format:'pem'}));"],
        capture_output=True, text=True, check=True,
    ).stdout
    (tmpdir / "key.pem").write_text(key, encoding="utf-8")
    process = subprocess.Popen(
        [node, "src/server.mjs"],
        cwd=ROOT / "relay",
        env={
            **os.environ,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PUBLIC_URL": f"https://relay-{port}.example",
            "DATA_PATH": str(tmpdir / "relay.json"),
            "APNS_KEY_PATH": str(tmpdir / "key.pem"),
            "APNS_KEY_ID": "AAAAAAAAAA",
            "APNS_TEAM_ID": "BBBBBBBBBB",
            "APNS_TOPIC": "com.milim.relay",
            "APNS_MODE": "accept",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1):
                    break
            except Exception:
                if time.time() > deadline or process.poll() is not None:
                    raise RuntimeError("relay did not start")
                time.sleep(0.1)
        yield base
    finally:
        process.kill()
        process.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def paired(relay, tmp_path, monkeypatch):
    status, registered = _request(relay, "/v1/installations", method="POST", body={
        "bundle_id": "com.milim.relay", "device_token": "b" * 64, "environment": "production"})
    assert status == 201
    installation_id = registered["installation"]["id"]
    status, pairing = _request(relay, f"/v1/installations/{installation_id}/pairings", method="POST",
                               credential=registered["credential"])
    assert status == 201
    status, claimed = _request(relay, "/v1/pairings/claim", method="POST",
                               body={"pairing_code": pairing["pairing_code"], "gateway_name": "watch e2e"})
    assert status == 200
    path = tmp_path / "conduit-push.json"
    path.write_text(json.dumps({
        "credential": claimed["credential"],
        "installation_id": claimed["installation_id"],
        "gateway_id": claimed["gateway_id"],
        "relay_url": relay,
    }), encoding="utf-8")
    monkeypatch.setattr(api, "_WATCH_RELAY_SCHEMES", ("https://", "http://127.0.0.1"))
    monkeypatch.setattr(api, "_watch_grants", api._WatchGrants())
    return path


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def watch_call(relay, grant, rid, payload):
    keys = api.watch_tool_keys(base64.urlsafe_b64decode(grant["key"] + "="))
    sealed = api.seal_watch_tool(keys["call"], "call", grant["grant_id"], rid, payload)
    status, body = _request(relay, f"/v1/watch-tools/grants/{grant['grant_id']}/calls", method="POST",
                            body={"rid": rid, **sealed}, credential=grant["watch_key"])
    if status != 200:
        return status, body
    return status, api.open_watch_tool(keys["result"], "result", grant["grant_id"], rid, body, max_bytes=64 * 1024)


def test_a_watch_lookup_goes_through_the_relay_and_ends_with_the_grant(relay, paired, monkeypatch):
    searches = []

    def search(query, limit):
        searches.append((query, limit))
        return json.dumps({"success": True, "data": {"web": [
            {"title": "Tokyo weather", "url": "https://example.com/tokyo", "description": "Sunny, 21°C"}]}})

    monkeypatch.setattr(api, "_hermes_web_search", search)
    grant = api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=paired)

    status, answer = watch_call(relay, grant, b64u(os.urandom(16)), {"tool": "web_search", "args": {"query": "weather in Tokyo", "limit": 3}})
    assert status == 200
    assert answer == {"ok": True, "query": "weather in Tokyo",
                      "results": [{"title": "Tokyo weather", "url": "https://example.com/tokyo", "snippet": "Sunny, 21°C"}]}
    assert searches == [("weather in Tokyo", 3)]

    status, answer = watch_call(relay, grant, b64u(os.urandom(16)), {"tool": "start_job", "args": {"instructions": "x"}})
    assert status == 200 and answer["status"] == 403

    # A call sealed with another key never reaches a tool; the Watch's
    # request just times out at the relay, so only check nothing ran.
    assert api.revoke_watch_grant({"grant_id": grant["grant_id"]}, profile=None) == {"revoked": True}
    status, _ = watch_call(relay, grant, b64u(os.urandom(16)), {"tool": "web_search", "args": {"query": "again"}})
    assert status == 401
    assert len(searches) == 1


def test_the_watch_closing_its_grant_stops_the_host_poller(relay, paired):
    grant = api.open_watch_grant({"tools": ["web_search"]}, profile=None, path=paired)
    [live] = api._watch_grants.all()
    status, _ = _request(relay, f"/v1/watch-tools/grants/{grant['grant_id']}", method="DELETE", credential=grant["watch_key"])
    assert status == 204
    assert live.closed.wait(10)
    assert api._watch_grants.all() == []
