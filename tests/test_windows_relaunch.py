"""windows_relaunch.py: the helper that starts a Windows dashboard again once the old one exits
(issue notifier 774). The waiting itself is Windows-only; the rest runs anywhere."""

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("conduit_windows_relaunch", ROOT / "windows_relaunch.py")
relaunch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relaunch)

COMMAND = ["C:\\hermes\\.venv\\Scripts\\python.exe", "C:\\hermes\\.venv\\Scripts\\hermes.exe", "dashboard", "--port", "9119"]


def test_the_command_starts_detached_with_its_output_in_the_log(tmp_path):
    seen = []

    class Proc:
        pid = 4242

    def popen(command, **kw):
        seen.append((command, kw))
        return Proc()

    log = tmp_path / "dashboard-restart.log"
    assert relaunch.start(COMMAND, str(log), popen=popen) == 4242
    command, kw = seen[0]
    assert command == COMMAND
    assert kw["creationflags"] == relaunch.DETACHED_BREAKAWAY and kw["stdin"] == subprocess.DEVNULL
    assert "dashboard restarted" in log.read_text()


def test_a_job_that_forbids_breakaway_starts_it_without(tmp_path):
    flags = []

    class Proc:
        pid = 7

    def popen(command, **kw):
        flags.append(kw["creationflags"])
        if kw["creationflags"] & relaunch.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return Proc()

    assert relaunch.start(COMMAND, str(tmp_path / "log"), popen=popen) == 7
    assert flags == [relaunch.DETACHED_BREAKAWAY, relaunch.DETACHED]


def test_a_log_that_cant_be_opened_still_starts_the_dashboard(tmp_path):
    started = []

    class Proc:
        pid = 9

    def popen(command, **kw):
        started.append(command)
        return Proc()

    blocked = tmp_path / "not-a-folder"
    blocked.write_text("")
    assert relaunch.start(COMMAND, str(blocked / "dashboard-restart.log"), popen=popen) == 9
    assert started == [COMMAND]


def test_the_same_command_matches_whatever_names_the_interpreter():
    assert relaunch._same_command(["python.exe", *COMMAND[1:]], COMMAND)
    assert not relaunch._same_command(COMMAND[1:], COMMAND)  # the hermes.exe launcher itself
    assert not relaunch._same_command([*COMMAND[:-1], "9120"], COMMAND)


def test_a_dashboard_its_supervisor_started_again_is_found():
    pytest.importorskip("psutil")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "conduit-relaunch-probe"])
    try:
        command = ["another-python", "-c", "import time; time.sleep(30)", "conduit-relaunch-probe"]
        assert relaunch.started_by_someone_else(command)
        assert not relaunch.started_by_someone_else(command, own_pid=proc.pid)
        assert not relaunch.started_by_someone_else([*command[:-1], "something-else"])
    finally:
        proc.kill()
        proc.wait()


def test_it_waits_for_the_old_dashboard_then_starts_the_new_one(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(relaunch, "wait_for_exit", lambda pid: events.append(("waited", pid)) or True)
    monkeypatch.setattr(relaunch, "started_by_someone_else", lambda command: False)
    monkeypatch.setattr(relaunch, "OTHER_START_GRACE_S", 0.0)
    monkeypatch.setattr(relaunch, "start", lambda command, log: events.append(("started", command, log)))
    assert relaunch.main(["windows_relaunch.py", "321", json.dumps(COMMAND), str(tmp_path / "log")]) == 0
    assert events == [("waited", 321), ("started", COMMAND, str(tmp_path / "log"))]


def test_it_leaves_a_dashboard_someone_else_started_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(relaunch, "wait_for_exit", lambda pid: True)
    monkeypatch.setattr(relaunch, "started_by_someone_else", lambda command: True)
    monkeypatch.setattr(relaunch, "start", lambda *a: pytest.fail("started a second dashboard"))
    assert relaunch.main(["windows_relaunch.py", "321", json.dumps(COMMAND), str(tmp_path / "log")]) == 0


def test_an_old_dashboard_that_never_exits_is_not_joined_by_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(relaunch, "wait_for_exit", lambda pid: False)
    monkeypatch.setattr(relaunch, "start", lambda *a: pytest.fail("started while the old one runs"))
    assert relaunch.main(["windows_relaunch.py", "321", json.dumps(COMMAND), str(tmp_path / "log")]) == 1
