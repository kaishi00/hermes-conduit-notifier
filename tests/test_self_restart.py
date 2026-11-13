import os
import importlib.util
import json
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants

spec = importlib.util.spec_from_file_location("conduit_plugin_api_self_restart", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


@pytest.fixture(autouse=True)
def _unix_backend(monkeypatch):
    monkeypatch.setattr(api.self_update.sys, "platform", "linux")
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_CHILD_PID", raising=False)
    monkeypatch.setattr(api.self_update, "_self_restart_scheduled", False)


def _markers(home, entries):
    (home / "desktop").mkdir(parents=True, exist_ok=True)
    (home / "desktop" / "interrupted_turns.json").write_text(json.dumps(entries))


def test_an_idle_dashboard_schedules_one_re_exec(tmp_path):
    scheduled = []
    result = api.self_update.self_restart(lambda delay, fn: scheduled.append((delay, fn)), homes=lambda: [tmp_path])
    assert result["restarting"] is True and result["boot_id"] == api.capabilities.BOOT_ID
    assert [delay for delay, _ in scheduled] == [api.self_update.SELF_RESTART_DELAY_S]
    api.self_update.self_restart(lambda delay, fn: scheduled.append((delay, fn)), homes=lambda: [tmp_path])
    assert len(scheduled) == 1


def test_a_turn_started_before_the_exec_postpones_it(monkeypatch, tmp_path):
    execs, scheduled = [], []
    monkeypatch.setattr(api.self_update, "_reexec", lambda: execs.append(True))
    api.self_update.self_restart(lambda delay, fn: scheduled.append((delay, fn)), homes=lambda: [tmp_path])
    _markers(tmp_path, {"s1": {"writer_pid": api.self_update.os.getpid()}})
    scheduled.pop(0)[1]()
    assert execs == [] and [delay for delay, _ in scheduled] == [api.self_update.SELF_RESTART_RECHECK_S]
    _markers(tmp_path, {})
    scheduled.pop(0)[1]()
    assert execs == [True]


def test_a_failed_exec_can_be_asked_again(monkeypatch, tmp_path):
    def fail():
        raise OSError("exec format error")

    monkeypatch.setattr(api.self_update, "_reexec", fail)
    scheduled = []
    api.self_update.self_restart(lambda delay, fn: scheduled.append(fn), homes=lambda: [tmp_path])
    scheduled.pop()()
    assert api.self_update._self_restart_scheduled is False
    api.self_update.self_restart(lambda delay, fn: scheduled.append(fn), homes=lambda: [tmp_path])
    assert len(scheduled) == 1


def test_a_turn_running_in_this_process_refuses(tmp_path):
    other = tmp_path / "profiles" / "coder"
    _markers(tmp_path, {"s1": {"writer_pid": api.self_update.os.getpid() + 1}})
    _markers(other, {"s2": {"writer_pid": api.self_update.os.getpid()}, "s3": {"writer_pid": api.self_update.os.getpid()}})
    scheduled = []
    with pytest.raises(api.core.TokenError) as raised:
        api.self_update.self_restart(lambda *a: scheduled.append(a), homes=lambda: [tmp_path, other])
    assert raised.value.status == 409 and "2 chat turns" in str(raised.value)
    assert scheduled == []


def test_turns_of_other_processes_and_unreadable_markers_do_not_block(tmp_path):
    _markers(tmp_path, {"s1": {"writer_pid": api.self_update.os.getpid() + 1}, "s2": "junk"})
    (tmp_path / "x" / "desktop").mkdir(parents=True)
    (tmp_path / "x" / "desktop" / "interrupted_turns.json").write_text("{not json")
    assert api.self_update._running_turns(api.self_update.os.getpid(), [tmp_path, tmp_path / "x", tmp_path / "missing"]) == 0


def test_a_string_pid_counts_and_an_earlier_process_with_this_pid_does_not(tmp_path):
    pid = api.self_update.os.getpid()
    _markers(tmp_path, {"s1": {"writer_pid": str(pid)},
                        "s2": {"writer_pid": pid, "writer_start_time": 1000.0},
                        "s3": {"writer_pid": pid, "writer_start_time": 5000.4}})
    assert api.self_update._running_turns(pid, [tmp_path]) == 3
    assert api.self_update._running_turns(pid, [tmp_path], start_time=5000.0) == 2


def test_a_turn_in_a_child_process_counts(monkeypatch, tmp_path):
    _markers(tmp_path, {"s1": {"writer_pid": 4242}, "s2": {"writer_pid": 4343}})
    monkeypatch.setattr(api.self_update, "_descends_from", lambda pid, ancestor: pid == 4242 and ancestor == api.self_update.os.getpid())
    assert api.self_update._running_turns(api.self_update.os.getpid(), [tmp_path]) == 1


def test_markers_are_read_where_hermes_writes_them(monkeypatch, tmp_path):
    marker = tmp_path / "elsewhere.json"
    marker.write_text(json.dumps({"s1": {"writer_pid": api.self_update.os.getpid()}}))
    package, module = types.ModuleType("tui_gateway"), types.ModuleType("tui_gateway.turn_marker")
    module._marker_path = lambda home: marker
    package.turn_marker = module
    monkeypatch.setitem(sys.modules, "tui_gateway", package)
    monkeypatch.setitem(sys.modules, "tui_gateway.turn_marker", module)
    assert api.self_update._running_turns(api.self_update.os.getpid(), [tmp_path]) == 1
    del module._marker_path
    with pytest.raises(AttributeError):
        api.self_update._running_turns(api.self_update.os.getpid(), [tmp_path])


@pytest.mark.parametrize("setup", ["plugin-host", "desktop", "hermes-desktop", "windows-desktop"])
def test_backends_that_cannot_re_exec_refuse(monkeypatch, tmp_path, setup):
    if setup == "windows-desktop":
        # Hermes Desktop on Windows is still Desktop's to restart.
        monkeypatch.setattr(api.self_update.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "hermes_cli.anon_auth", None)
        monkeypatch.setenv("HERMES_DESKTOP_CHILD_PID", "123")
    elif setup == "plugin-host":
        monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    elif setup == "desktop":
        # A Hermes without its own answer: the variable Desktop sets (also
        # inside a real Hermes install, notifier #322).
        monkeypatch.setitem(sys.modules, "hermes_cli.anon_auth", None)
        monkeypatch.setenv("HERMES_DESKTOP_CHILD_PID", "123")
    else:
        anon_auth = types.ModuleType("hermes_cli.anon_auth")
        anon_auth.on_local_desktop_backend = lambda: True
        monkeypatch.setitem(sys.modules, "hermes_cli.anon_auth", anon_auth)
    with pytest.raises(api.core.TokenError) as raised:
        api.self_update.self_restart(lambda *a: pytest.fail("scheduled"), homes=lambda: [tmp_path])
    assert raised.value.status == 501


def test_the_re_exec_keeps_the_original_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(api.self_update.sys, "orig_argv", ["python3", "-m", "hermes_cli.main", "dashboard", "--port", "9119"])
    monkeypatch.setattr(api.self_update.os, "execv", lambda exe, argv: seen.append((exe, argv)))
    api.self_update._reexec()
    assert seen == [(api.self_update.sys.executable, [api.self_update.sys.executable, "-m", "hermes_cli.main", "dashboard", "--port", "9119"])]


def test_a_windows_dashboard_restarts_itself(monkeypatch, tmp_path):
    # Issue notifier 774: Windows used to answer 501, so an update left the dashboard on the old plugin.
    monkeypatch.setattr(api.self_update.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "hermes_cli.anon_auth", None)
    scheduled = []
    result = api.self_update.self_restart(lambda delay, fn: scheduled.append(fn), homes=lambda: [tmp_path])
    assert result["restarting"] is True and len(scheduled) == 1


def test_on_windows_the_restart_hands_the_command_line_to_the_relauncher_and_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(api.self_update.sys, "platform", "win32")
    monkeypatch.setattr(api.self_update.sys, "orig_argv", ["python.exe", "C:\\hermes\\Scripts\\hermes.exe", "dashboard"])
    spawned, exits = [], []
    monkeypatch.setattr(api.self_update.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)))
    monkeypatch.setattr(api.self_update.os, "execv", lambda *a: pytest.fail("exec on Windows"))
    monkeypatch.setattr(api.self_update.os, "_exit", lambda code: exits.append(code))
    api.self_update._reexec()
    assert exits == [0]
    argv, kw = spawned[0]
    assert argv[1:3] == ["-I", str(ROOT / "windows_relaunch.py")]
    assert argv[3] == str(api.self_update.os.getpid())
    assert json.loads(argv[4]) == [api.self_update.sys.executable, "C:\\hermes\\Scripts\\hermes.exe", "dashboard"]
    assert argv[5] == str(tmp_path / "logs" / "dashboard-restart.log")
    assert kw["creationflags"] & 0x01000000 and kw["stdin"] == api.self_update.subprocess.DEVNULL


def test_a_job_that_forbids_breakaway_still_gets_the_relauncher(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    flags = []

    def popen(argv, **kw):
        flags.append(kw["creationflags"])
        if kw["creationflags"] & 0x01000000:
            raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(api.self_update.subprocess, "Popen", popen)
    api.self_update._relaunch_on_windows(["python.exe", "hermes.exe", "dashboard"])
    assert flags == [0x08000200 | 0x01000000, 0x08000200]


def test_a_relauncher_that_cant_start_leaves_the_dashboard_serving(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(api.self_update.sys, "platform", "win32")
    monkeypatch.setattr(api.self_update.sys, "orig_argv", ["python.exe", "hermes.exe", "dashboard"])

    def popen(argv, **kw):
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(api.self_update.subprocess, "Popen", popen)
    monkeypatch.setattr(api.self_update.os, "_exit", lambda code: pytest.fail("exited"))
    with pytest.raises(OSError):
        api.self_update._reexec()


def test_the_route_answers_and_schedules_the_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(api.self_update, "_hermes_homes", lambda: [tmp_path])
    execs = []
    monkeypatch.setattr(api.self_update, "_reexec", lambda: execs.append(True))
    monkeypatch.setattr(api.self_update, "SELF_RESTART_DELAY_S", 0.0)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).post(f"{BASE}/self/restart")
    assert response.status_code == 200, response.text
    assert response.json()["restarting"] is True
    assert response.headers["cache-control"] == "no-store"
