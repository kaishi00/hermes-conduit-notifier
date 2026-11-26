"""Terminals on the Hermes host (terminal_host.py, dashboard/api/terminals.py; desktop issue 450)."""

import base64
import importlib.util
import json
import os
import re
import pathlib
import sys
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals")

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["CONDUIT_TESTS_NO_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


th = _load("conduit_terminal_host_test", ROOT / "terminal_host.py")
wf = _load("conduit_workspace_files_terminal_test", ROOT / "workspace_files.py")
api = _load("conduit_plugin_api_terminals", ROOT / "dashboard" / "plugin_api.py")

SH = ["/bin/sh"]


def until(check, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def host():
    made = th.TerminalHost(argv=SH)
    yield made
    made.close_all()


def output(terminal, since=0):
    return terminal.since(since)[0].decode("utf-8", errors="replace")


# --- The host --------------------------------------------------------------------


def test_a_terminal_runs_in_its_folder_and_keeps_what_it_wrote(host, tmp_path):
    terminal = host.create("", str(tmp_path), cols=100, rows=30)
    assert terminal.title == tmp_path.name and terminal.row()["cols"] == 100
    terminal.write(b"pwd; echo TERM=$TERM\n")
    until(lambda: "TERM=xterm-256color" in output(terminal))
    assert str(tmp_path) in output(terminal)
    end = terminal.end
    terminal.write(b"echo later\n")
    until(lambda: "later" in output(terminal, end))
    assert "TERM=" not in output(terminal, end)


def test_ctrl_c_stops_the_running_command_not_the_shell(host, tmp_path):
    terminal = host.create("", str(tmp_path))
    terminal.write(b"sleep 30; echo slept\n")

    def interrupted():
        # Pressed until the shell answers: on a busy machine the first press
        # can land before sleep has started.
        terminal.write(b"\x03")
        terminal.write(b"echo back-$((2+3))\n")
        time.sleep(0.3)
        return "back-5" in output(terminal)

    until(interrupted)
    # The typed line echoes "echo slept"; only a line of its own would mean sleep finished.
    assert not re.search(r"(^|\n)slept\r?\n", output(terminal))
    assert terminal.exit_code is None


def test_a_viewer_gets_what_it_missed_then_live_output_then_the_exit(host, tmp_path):
    terminal = host.create("", str(tmp_path))
    terminal.write(b"echo before\n")
    until(lambda: "before" in output(terminal))
    seen, ended = [], []
    key, missed, start, end = terminal.watch(lambda chunk, at: (seen if chunk else ended).append((chunk, at)), 0)
    assert b"before" in missed and start == 0 and end == terminal.end
    terminal.write(b"echo after; exit 3\n")
    until(lambda: ended)
    assert b"after" in b"".join(chunk for chunk, _ in seen)
    assert terminal.exit_code == 3 and terminal.row()["exited"] is True
    with pytest.raises(th.TerminalError) as exc:
        terminal.write(b"x")
    assert exc.value.status == 409
    terminal.unwatch(key)


def test_scrollback_keeps_the_last_part(host, tmp_path, monkeypatch):
    monkeypatch.setattr(th, "SCROLLBACK_BYTES", 1000)
    terminal = host.create("", str(tmp_path))
    terminal.write(b"i=0; while [ $i -lt 300 ]; do echo line$i; i=$((i+1)); done\n")
    until(lambda: "line299" in output(terminal))
    data, start, end = terminal.since(0)
    assert start > 0 and end - start == len(data) <= 1000


def test_limits_owners_and_closing(host, tmp_path, monkeypatch):
    monkeypatch.setattr(th, "MAX_PER_PROFILE", 2)
    first = host.create("work", str(tmp_path), title="  dev server  ")
    host.create("work", str(tmp_path))
    with pytest.raises(th.TerminalError) as exc:
        host.create("work", str(tmp_path))
    assert exc.value.status == 429
    other = host.create("", str(tmp_path))
    assert first.title == "dev server"
    assert [row["id"] for row in host.list("")] == [other.id]
    assert len(host.list("work", str(tmp_path))) == 2 and host.list("work", "/elsewhere") == []
    with pytest.raises(th.TerminalError) as exc:
        host.get("", first.id)
    assert exc.value.status == 404
    host.rename("work", first.id, "astro")
    assert host.get("work", first.id).title == "astro"
    host.close("work", first.id)
    until(lambda: first.exit_code is not None)
    assert len(host.list("work")) == 1


def test_an_unwatched_terminal_ends_after_a_week(tmp_path):
    now = [1000.0]
    made = th.TerminalHost(clock=lambda: now[0], argv=SH)
    try:
        terminal = made.create("", str(tmp_path))
        now[0] += th.IDLE_UNWATCHED_S + 1
        assert made.list("") == []
        until(lambda: terminal.exit_code is not None)
    finally:
        made.close_all()


def test_typing_into_a_shell_that_isnt_reading_gives_up(host, tmp_path, monkeypatch):
    terminal = host.create("me", str(tmp_path), 80, 24)
    monkeypatch.setattr(th, "WRITE_TIMEOUT_S", 0.2)

    def full(fd, data):
        raise OSError(th.errno.EAGAIN, "full")

    monkeypatch.setattr(th.os, "write", full)
    started = time.monotonic()
    with pytest.raises(th.TerminalError) as exc:
        terminal.write(b"x")
    assert exc.value.status == 409 and time.monotonic() - started < 2


def test_sizes_are_kept_in_range(host, tmp_path):
    terminal = host.create("", str(tmp_path), cols="wide", rows=10_000)
    assert (terminal.cols, terminal.rows) == (80, 24) or terminal.rows == th.MAX_ROWS
    terminal.resize(1, 99999)
    assert (terminal.cols, terminal.rows) == (th.MIN_SIZE, th.MAX_ROWS)


# --- Routes ----------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path, host):
    async def authorized(ws):
        return True

    monkeypatch.setattr(api.workspace, "_workspace_files", lambda: wf)
    monkeypatch.setattr(api.workspace, "_workspace_module", wf)
    monkeypatch.setattr(api.workspace, "_workspace", lambda profile: wf.LocalWorkspace())
    monkeypatch.setattr(api.workspace, "_managed_boundary", lambda: None)
    monkeypatch.setattr(api.grok_live, "_ws_authorized", authorized)
    th._host = host
    monkeypatch.setattr(api.terminals, "_host_module", lambda: th)
    monkeypatch.setattr(api.terminals, "_settings_path", lambda: str(tmp_path / "conduit-terminals.json"))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_routes_open_list_rename_and_close(client, tmp_path):
    root = str(tmp_path)
    made = client.post(f"{BASE}/terminals", json={"root": root, "title": "dev", "cols": 120, "rows": 40}).json()
    terminal = made["terminal"]
    assert terminal["title"] == "dev" and terminal["root"] == os.path.realpath(root)
    listed = client.get(f"{BASE}/terminals", params={"root": root}).json()
    assert listed["available"] is True and [t["id"] for t in listed["terminals"]] == [terminal["id"]]
    renamed = client.post(f"{BASE}/terminals/rename", json={"id": terminal["id"], "title": "astro"}).json()
    assert renamed["terminal"]["title"] == "astro"
    assert client.delete(f"{BASE}/terminals", params={"id": terminal["id"]}).json()["ok"] is True
    assert client.delete(f"{BASE}/terminals", params={"id": terminal["id"]}).status_code == 404


def test_the_socket_replays_streams_takes_input_and_says_when_it_ends(client, tmp_path):
    terminal = client.post(f"{BASE}/terminals", json={"root": str(tmp_path)}).json()["terminal"]
    with client.websocket_connect(f"{BASE}/terminals/socket?id={terminal['id']}&since=0") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["terminal"]["id"] == terminal["id"]
        ws.send_text(json.dumps({"type": "resize", "cols": 90, "rows": 20}))
        ws.send_text(json.dumps({"type": "input", "data": "echo hi-$((1+1))\n"}))
        seen = b""
        while b"hi-2" not in seen:
            frame = ws.receive_json()
            assert frame["type"] == "output" and frame["end"] > 0
            seen += base64.b64decode(frame["b64"])
        ws.send_text(json.dumps({"type": "input", "data": "exit 0\n"}))
        while True:
            frame = ws.receive_json()
            if frame["type"] == "exit":
                assert frame["code"] == 0
                break
    with client.websocket_connect(f"{BASE}/terminals/socket?id={terminal['id']}&since=0") as ws:
        assert ws.receive_json()["terminal"]["exited"] is True
        assert ws.receive_json()["type"] == "output"
        assert ws.receive_json()["type"] == "exit"


def test_the_socket_refuses_a_closed_terminal(client):
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect(f"{BASE}/terminals/socket?id=nope") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == api.terminals.CLOSE_GONE


def test_turning_terminals_off_ends_them_and_refuses_new_ones(client, tmp_path, host):
    terminal = client.post(f"{BASE}/terminals", json={"root": str(tmp_path)}).json()["terminal"]
    assert client.get(f"{BASE}/terminals/settings").json()["enabled"] is True
    assert client.post(f"{BASE}/terminals/settings", json={"enabled": False}).json()["enabled"] is False
    assert host.list("") == []
    refused = client.post(f"{BASE}/terminals", json={"root": str(tmp_path)})
    assert refused.status_code == 403 and refused.json()["detail"] == "Terminals are turned off on this Hermes"
    listed = client.get(f"{BASE}/terminals").json()
    assert listed["available"] is False and listed["enabled"] is False
    assert terminal["id"]
    client.post(f"{BASE}/terminals/settings", json={"enabled": True})
    assert client.post(f"{BASE}/terminals", json={"root": str(tmp_path)}).status_code == 200
    assert client.post(f"{BASE}/terminals/settings", json={"enabled": "no"}).status_code == 400


def test_hosted_and_ssh_workspaces_get_no_terminals(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api.workspace, "_managed_boundary", lambda: str(tmp_path))
    hosted = client.post(f"{BASE}/terminals", json={"root": str(tmp_path)})
    assert hosted.status_code == 403 and "hosted" in hosted.json()["detail"]
    monkeypatch.setattr(api.workspace, "_managed_boundary", lambda: None)

    class Ssh:
        _fs = object()

    monkeypatch.setattr(api.workspace, "_workspace", lambda profile: Ssh())
    ssh = client.post(f"{BASE}/terminals", json={"root": "/srv"})
    assert ssh.status_code == 501 and "SSH" in ssh.json()["detail"]


def test_a_missing_folder_or_body_is_refused(client, tmp_path):
    assert client.post(f"{BASE}/terminals", json={}).status_code == 400
    gone = client.post(f"{BASE}/terminals", json={"root": str(tmp_path / "gone")})
    assert gone.status_code == 404


def test_the_plugin_says_it_serves_terminals():
    assert "terminals" in api.capabilities.ROUTE_CAPABILITIES
