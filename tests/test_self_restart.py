import importlib.util
import json
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants

spec = importlib.util.spec_from_file_location("conduit_plugin_api_self_restart", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


@pytest.fixture(autouse=True)
def _unix_backend(monkeypatch):
    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_CHILD_PID", raising=False)
    monkeypatch.setattr(api, "_self_restart_scheduled", False)


def _markers(home, entries):
    (home / "desktop").mkdir(parents=True, exist_ok=True)
    (home / "desktop" / "interrupted_turns.json").write_text(json.dumps(entries))


def test_an_idle_dashboard_schedules_one_re_exec(tmp_path):
    scheduled = []
    result = api.self_restart(lambda delay, fn: scheduled.append((delay, fn)), homes=lambda: [tmp_path])
    assert result["restarting"] is True and result["boot_id"] == api.BOOT_ID
    assert scheduled == [(api.SELF_RESTART_DELAY_S, api._reexec)]
    api.self_restart(lambda delay, fn: scheduled.append((delay, fn)), homes=lambda: [tmp_path])
    assert len(scheduled) == 1


def test_a_turn_running_in_this_process_refuses(tmp_path):
    other = tmp_path / "profiles" / "coder"
    _markers(tmp_path, {"s1": {"writer_pid": api.os.getpid() + 1}})
    _markers(other, {"s2": {"writer_pid": api.os.getpid()}, "s3": {"writer_pid": api.os.getpid()}})
    scheduled = []
    with pytest.raises(api.TokenError) as raised:
        api.self_restart(lambda *a: scheduled.append(a), homes=lambda: [tmp_path, other])
    assert raised.value.status == 409 and "2 chat turns" in str(raised.value)
    assert scheduled == []


def test_turns_of_other_processes_and_unreadable_markers_do_not_block(tmp_path):
    _markers(tmp_path, {"s1": {"writer_pid": api.os.getpid() + 1}, "s2": "junk"})
    (tmp_path / "x" / "desktop").mkdir(parents=True)
    (tmp_path / "x" / "desktop" / "interrupted_turns.json").write_text("{not json")
    assert api._running_turns(api.os.getpid(), [tmp_path, tmp_path / "x", tmp_path / "missing"]) == 0


@pytest.mark.parametrize("setup", ["windows", "plugin-host", "desktop"])
def test_backends_that_cannot_re_exec_refuse(monkeypatch, tmp_path, setup):
    if setup == "windows":
        monkeypatch.setattr(api.sys, "platform", "win32")
    elif setup == "plugin-host":
        monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    else:
        monkeypatch.setenv("HERMES_DESKTOP_CHILD_PID", "123")
    with pytest.raises(api.TokenError) as raised:
        api.self_restart(lambda *a: pytest.fail("scheduled"), homes=lambda: [tmp_path])
    assert raised.value.status == 501


def test_the_re_exec_keeps_the_original_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(api.sys, "orig_argv", ["python3", "-m", "hermes_cli.main", "dashboard", "--port", "9119"])
    monkeypatch.setattr(api.os, "execv", lambda exe, argv: seen.append((exe, argv)))
    api._reexec()
    assert seen == [(api.sys.executable, [api.sys.executable, "-m", "hermes_cli.main", "dashboard", "--port", "9119"])]


def test_the_route_answers_and_schedules_the_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_hermes_homes", lambda: [tmp_path])
    execs = []
    monkeypatch.setattr(api, "_reexec", lambda: execs.append(True))
    monkeypatch.setattr(api, "SELF_RESTART_DELAY_S", 0.0)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).post(f"{BASE}/self/restart")
    assert response.status_code == 200, response.text
    assert response.json()["restarting"] is True
    assert response.headers["cache-control"] == "no-store"
