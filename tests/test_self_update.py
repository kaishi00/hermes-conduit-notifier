import importlib.util
import json
import pathlib
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants

spec = importlib.util.spec_from_file_location("conduit_plugin_api_self_update", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


@pytest.fixture(autouse=True)
def _unix_backend(monkeypatch):
    monkeypatch.setattr(api.self_update.sys, "platform", "linux")
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_CHILD_PID", raising=False)
    monkeypatch.setattr(api.self_update, "_self_restart_scheduled", False)
    monkeypatch.setattr(api.self_update, "_self_update_thread", None)
    monkeypatch.setattr(api.self_update, "_self_update_limiter", api.core._MintLimiter(3, 600.0))


def _restarted(*_):
    return 0


def _run(tmp_path, *, pull=None, gateway=0, version="9.9.9", markers=None, monkeypatch=None, gateway_work=lambda: 0):
    """Runs one job in this thread; returns (job file, scheduled restarts, notifications)."""
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    scheduled, notified = [], []
    monkeypatch.setattr(api.capabilities, "_plugin_version", lambda: version)
    job = {"id": "j1", "step": "downloading", "from_version": api.capabilities.LOADED_VERSION, "boot_id": api.capabilities.BOOT_ID,
           "profile": "coder"}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        (tmp_path / "desktop" / "interrupted_turns.json").write_text("{}")

    if markers is not None:
        (tmp_path / "desktop").mkdir(parents=True, exist_ok=True)
        (tmp_path / "desktop" / "interrupted_turns.json").write_text(json.dumps(markers))

    def restart_gateway(draining):
        if callable(gateway):
            return gateway(draining)
        if isinstance(gateway, Exception):
            raise gateway
        return gateway

    api.self_update._run_self_update(job, path, pull or (lambda: {"ok": True}), restart_gateway,
                         lambda delay, fn: scheduled.append((delay, fn)), lambda: [tmp_path],
                         notified.append, sleep=sleep, gateway_work=gateway_work)
    return json.loads(path.read_text()), scheduled, notified, sleeps


def test_an_update_restarts_the_gateway_then_the_dashboard(monkeypatch, tmp_path):
    job, scheduled, notified, _ = _run(tmp_path, monkeypatch=monkeypatch)
    assert job["step"] == "restarting_dashboard" and job["version"] == "9.9.9"
    assert [delay for delay, _ in scheduled] == [api.self_update.SELF_RESTART_DELAY_S]
    # The restarted dashboard notifies, not this one.
    assert notified == []


def test_running_chats_are_waited_for_on_the_host(monkeypatch, tmp_path):
    job, scheduled, _, sleeps = _run(tmp_path, monkeypatch=monkeypatch,
                                     markers={"s1": {"writer_pid": api.self_update.os.getpid()}})
    assert sleeps == [api.self_update.SELF_UPDATE_CHATS_RECHECK_S]
    assert job["step"] == "restarting_dashboard" and len(scheduled) == 1


def test_a_turn_that_never_ends_doesnt_hold_the_update_forever(monkeypatch, tmp_path):
    monkeypatch.setattr(api.self_update, "SELF_UPDATE_CHATS_LIMIT_S", 3 * api.self_update.SELF_UPDATE_CHATS_RECHECK_S)
    monkeypatch.setattr(api.self_update, "_running_turns", lambda *a: 1)
    job, scheduled, notified, sleeps = _run(tmp_path, monkeypatch=monkeypatch, markers={"s1": {}})
    assert job["step"] == "failed" and job["code"] == "restart_needed" and scheduled == []
    assert len(sleeps) == 3 and [n["step"] for n in notified] == ["failed"]


def test_an_unreadable_timestamp_reads_as_stale(tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": "old", "updated_at": "x"}))
    assert api.self_update.self_update_status(path)["step"] == "failed"


def test_nothing_new_on_disk_finishes_without_a_restart(monkeypatch, tmp_path):
    job, scheduled, notified, _ = _run(tmp_path, monkeypatch=monkeypatch, version=api.capabilities.LOADED_VERSION)
    assert job["step"] == "done" and scheduled == []
    assert [n["step"] for n in notified] == ["done"]


def test_a_refused_pull_fails_with_hermes_words(monkeypatch, tmp_path):
    job, scheduled, notified, _ = _run(tmp_path, monkeypatch=monkeypatch, pull=lambda: {
        "ok": False, "consent_required": True, "error": "Plugin 'conduit_push' wants new capabilities"})
    assert job["step"] == "failed" and job["code"] == "consent_required" and job["failed_step"] == "downloading"
    assert "new capabilities" in job["detail"] and scheduled == []
    assert [n["step"] for n in notified] == ["failed"]


def test_a_gateway_that_doesnt_come_back_fails(monkeypatch, tmp_path):
    job, scheduled, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway=api.self_update.subprocess.TimeoutExpired("hermes", 1))
    assert job["step"] == "failed" and "gateway" in job["detail"] and scheduled == []
    assert job["failed_step"] == "restarting_gateway"
    job, _, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway=1)
    assert job["step"] == "failed"


def test_the_gateways_running_work_is_waited_for_before_its_restart(monkeypatch, tmp_path):
    # Issue 546: a restart sent during a turn makes the gateway refuse new work until it ends.
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    work, seen = [2, 1, 0], []

    def gateway_work():
        return work.pop(0)

    def restart_gateway(draining):
        seen.append(("restart", json.loads(path.read_text())))
        return 0

    def sleep(seconds):
        job = json.loads(path.read_text())
        seen.append((job.get("gateway_stage"), job.get("running_chats")))

    monkeypatch.setattr(api.capabilities, "_plugin_version", lambda: "9.9.9")
    job = {"id": "j1", "step": "downloading", "boot_id": api.capabilities.BOOT_ID}
    api.self_update._run_self_update(job, path, lambda: {"ok": True}, restart_gateway, lambda *a: None,
                                     lambda: [tmp_path], lambda job: None, sleep=sleep, point_profiles=lambda: None,
                                     gateway_work=gateway_work)
    assert seen[:2] == [("waiting", 2), ("waiting", 1)]
    # Restarting, no longer waiting.
    assert seen[2][0] == "restart" and seen[2][1]["step"] == "restarting_gateway" and "gateway_stage" not in seen[2][1]
    final = json.loads(path.read_text())
    assert final["step"] == "restarting_dashboard" and "gateway_stage" not in final and final["running_chats"] == 0


def test_work_that_never_ends_doesnt_hold_the_gateway_restart_forever(monkeypatch, tmp_path):
    monkeypatch.setattr(api.self_update, "SELF_UPDATE_GATEWAY_WAIT_S", 3 * api.self_update.SELF_UPDATE_CHATS_RECHECK_S)
    job, scheduled, _, sleeps = _run(tmp_path, monkeypatch=monkeypatch, gateway_work=lambda: 1, markers={})
    assert len(sleeps) == 3 and job["step"] == "restarting_dashboard" and len(scheduled) == 1


def test_a_job_that_fails_while_waiting_drops_the_gateway_stage(monkeypatch, tmp_path):
    work = [1]

    def gateway_work():
        if not work:
            raise RuntimeError("socket")
        return work.pop()

    job, _, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway_work=gateway_work, markers={})
    assert job["step"] == "failed" and job["failed_step"] == "restarting_gateway"
    assert "gateway_stage" not in job and "running_chats" not in job


def test_a_draining_gateway_shows_as_draining_and_isnt_a_failure(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    stages = []

    def gateway(draining):
        draining()
        stages.append(json.loads(path.read_text()).get("gateway_stage"))
        return 0

    job, scheduled, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway=gateway)
    assert stages == ["draining"] and job["step"] == "restarting_dashboard" and "gateway_stage" not in job


class _SlowRestart:
    def __init__(self, timeouts):
        self.timeouts, self.killed, self.waits = timeouts, False, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.killed:
            return -9
        if self.timeouts:
            self.timeouts -= 1
            raise api.self_update.subprocess.TimeoutExpired("hermes", timeout)
        return 0

    def kill(self):
        self.killed = True


def test_a_restart_past_the_limit_is_waited_on_while_the_gateway_drains(monkeypatch):
    proc, drained = _SlowRestart(2), []
    _fake_hermes(monkeypatch, lambda profile=None, *, interrupt=False: (proc, False))
    monkeypatch.setattr(api.self_update, "_gateway_draining", lambda: True)
    _, restart_gateway = api.self_update._hermes_update_steps()
    assert restart_gateway(lambda: drained.append(1)) == 0
    assert drained == [1, 1] and not proc.killed


def test_a_restart_past_the_limit_that_isnt_draining_fails_and_is_reaped(monkeypatch):
    proc = _SlowRestart(1)
    _fake_hermes(monkeypatch, lambda profile=None, *, interrupt=False: (proc, False))
    monkeypatch.setattr(api.self_update, "_gateway_draining", lambda: False)
    _, restart_gateway = api.self_update._hermes_update_steps()
    with pytest.raises(api.self_update.subprocess.TimeoutExpired):
        restart_gateway()
    # Killed, then waited on so it doesn't stay a zombie of the dashboard.
    assert proc.killed and len(proc.waits) == 2


def test_a_drain_past_its_own_limit_fails(monkeypatch):
    monkeypatch.setattr(api.self_update, "SELF_UPDATE_DRAIN_LIMIT_S", 2 * api.self_update.SELF_UPDATE_GATEWAY_LIMIT_S)
    proc = _SlowRestart(5)
    _fake_hermes(monkeypatch, lambda profile=None, *, interrupt=False: (proc, False))
    monkeypatch.setattr(api.self_update, "_gateway_draining", lambda: True)
    _, restart_gateway = api.self_update._hermes_update_steps()
    with pytest.raises(api.self_update.subprocess.TimeoutExpired):
        restart_gateway()
    assert proc.killed


def _fake_guards(monkeypatch, tmp_path, answer):
    guards = types.ModuleType("hermes_cli.gateway_lifecycle_guards")
    guards._candidate_homes = lambda: [tmp_path]
    guards.live_work = lambda home: answer
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_lifecycle_guards", guards)


def test_the_gateways_work_leaves_out_what_its_restart_doesnt_wait_for(monkeypatch, tmp_path):
    _fake_guards(monkeypatch, tmp_path, {"active_agents": 3, "active_work": [
        {"kind": "chat"}, {"kind": "cron", "wedged": True}, {"kind": "cron", "restart_safe": True}]})
    assert api.self_update._gateway_work() == 1


def test_an_older_gateway_counts_its_agents(monkeypatch, tmp_path):
    _fake_guards(monkeypatch, tmp_path, {"active_agents": 2, "active_work": []})
    assert api.self_update._gateway_work() == 2


def test_no_gateway_or_no_guard_holds_nothing(monkeypatch, tmp_path):
    _fake_guards(monkeypatch, tmp_path, None)
    assert api.self_update._gateway_work() == 0
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_lifecycle_guards", None)
    assert api.self_update._gateway_work() == 0


def test_a_draining_gateway_reads_from_its_state_file(monkeypatch, tmp_path):
    status = types.ModuleType("gateway.status")
    records = {"gateway_state": "draining"}
    status.read_runtime_status = lambda path: records if path == tmp_path / "gateway_state.json" else None
    status.runtime_status_is_stale = lambda record: False
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.status", status)
    monkeypatch.setattr(api.self_update, "_gateway_homes", lambda: [tmp_path])
    assert api.self_update._gateway_draining()
    records["gateway_state"] = "running"
    assert not api.self_update._gateway_draining()
    status.runtime_status_is_stale = lambda record: True
    records["gateway_state"] = "draining"
    assert not api.self_update._gateway_draining()


def test_a_gateway_check_that_cant_find_hermes_reads_as_no_gateway(monkeypatch):
    # Review on #554: a raise here escaped restart_gateway before the kill and reap.
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_lifecycle_guards", None)

    def no_root():
        raise RuntimeError("no Hermes home")

    monkeypatch.setattr(api.self_update, "_hermes_homes", no_root)
    assert api.self_update._gateway_homes() == []
    assert api.self_update._gateway_draining() is False and api.self_update._gateway_work() == 0
    proc = _SlowRestart(1)
    _fake_hermes(monkeypatch, lambda profile=None, *, interrupt=False: (proc, False))
    _, restart_gateway = api.self_update._hermes_update_steps()
    with pytest.raises(api.self_update.subprocess.TimeoutExpired):
        restart_gateway()
    assert proc.killed and len(proc.waits) == 2


def test_a_crash_fails_the_job(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("disk")

    job, _, notified, _ = _run(tmp_path, monkeypatch=monkeypatch, pull=boom)
    assert job["step"] == "failed" and "RuntimeError" in job["detail"] and len(notified) == 1


def test_the_restarted_dashboard_finishes_and_notifies(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": "old",
                                "version": api.capabilities.LOADED_VERSION, "profile": "coder"}))
    notified = []
    job = api.self_update.finish_self_update_after_restart(path, notified.append, delay=0)
    assert job["step"] == "done" and job["loaded_version"] == api.capabilities.LOADED_VERSION
    api.self_update.time.sleep(0.2)
    assert [n["step"] for n in notified] == ["done"]
    assert json.loads(path.read_text())["step"] == "done"
    # Once only.
    assert api.self_update.finish_self_update_after_restart(path, notified.append, delay=0) is None


def test_a_restart_mid_download_reads_as_failed(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "downloading", "boot_id": "old"}))
    job = api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0)
    assert job["step"] == "failed"


def test_another_restart_after_the_pull_still_finishes_the_job(tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old",
                                "version": api.capabilities.LOADED_VERSION}))
    job = api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0)
    assert job["step"] == "done"


def test_a_dashboard_restarted_before_the_gateway_restarts_it(monkeypatch, tmp_path):
    # Review on #554: during the wait for the gateway's work, a dashboard restart loads the new
    # plugin while the gateway still runs the old one. The new dashboard restarts the gateway.
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old", "gateway_pending": True,
                                "gateway_stage": "waiting", "running_chats": 1, "version": api.capabilities.LOADED_VERSION}))
    restarts, notified = [], []
    monkeypatch.setattr(api.capabilities, "_plugin_version", lambda: api.capabilities.LOADED_VERSION)
    monkeypatch.setattr(api.self_update, "_hermes_update_steps",
                        lambda: (lambda: {"ok": True}, lambda draining: restarts.append(1) or 0))
    monkeypatch.setattr(api.self_update, "_gateway_work", lambda: 0)
    monkeypatch.setattr(api.self_update, "_point_profiles", lambda: [])
    api.self_update.finish_self_update_after_restart(path, notified.append, delay=0)
    api.self_update._self_update_thread.join(5)
    job = json.loads(path.read_text())
    assert restarts == [1] and job["step"] == "done" and job["boot_id"] == api.capabilities.BOOT_ID
    assert "gateway_pending" not in job and "gateway_stage" not in job
    assert [n["step"] for n in notified] == ["done"]


def test_a_plugin_host_leaves_a_pending_gateway_restart_to_the_dashboard(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    pending = {"id": "j1", "step": "restarting_gateway", "boot_id": "old", "gateway_pending": True,
               "version": api.capabilities.LOADED_VERSION}
    path.write_text(json.dumps(pending))
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    monkeypatch.setattr(api.self_update, "_hermes_update_steps", lambda: pytest.fail("a plugin host restarted Hermes"))
    assert api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0) is None
    assert json.loads(path.read_text()) == pending


def test_a_resumed_job_whose_pull_fails_reads_as_a_pull_failure(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old", "gateway_pending": True,
                                "version": api.capabilities.LOADED_VERSION}))
    monkeypatch.setattr(api.self_update, "_hermes_update_steps",
                        lambda: (lambda: {"ok": False, "error": "offline"}, lambda draining: 0))
    api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0)
    api.self_update._self_update_thread.join(5)
    job = json.loads(path.read_text())
    assert job["step"] == "failed" and job["failed_step"] == "downloading"


def test_a_dashboard_restarted_before_the_gateway_says_so_where_it_cant_restart_it(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old", "gateway_pending": True,
                                "version": api.capabilities.LOADED_VERSION}))

    for error in (api.core.TokenError(501, "no"), ImportError("broken inside Hermes")):
        path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old", "gateway_pending": True,
                                    "version": api.capabilities.LOADED_VERSION}))

        def unsupported():
            raise error

        monkeypatch.setattr(api.self_update, "_hermes_update_steps", unsupported)
        job = api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0)
        assert job["step"] == "failed" and "hermes gateway restart" in job["detail"]


def test_a_resume_that_cant_write_its_start_ends_the_job_at_once(monkeypatch, tmp_path):
    # Review on #554: the resume's first write failing used to vanish at import, leaving the job
    # "waiting" for the stale hour. It ends at once with the manual-restart instruction.
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old", "gateway_pending": True,
                                "gateway_stage": "waiting", "version": api.capabilities.LOADED_VERSION}))
    monkeypatch.setattr(api.self_update, "_hermes_update_steps",
                        lambda: (lambda: pytest.fail("the job ran"), lambda draining: 0))
    write, writes = api.self_update._write_self_update, []

    def first_write_fails(target, job):
        writes.append(job["step"])
        if len(writes) == 1:
            raise OSError("read-only")
        write(target, job)

    monkeypatch.setattr(api.self_update, "_write_self_update", first_write_fails)
    notified = []
    job = api.self_update.finish_self_update_after_restart(path, notified.append, delay=0)
    on_disk = json.loads(path.read_text())
    assert job["step"] == on_disk["step"] == "failed" and "hermes gateway restart" in on_disk["detail"]
    assert "gateway_stage" not in on_disk


def test_a_failed_write_before_the_restart_isnt_read_as_taken_down(monkeypatch, tmp_path):
    # Review on #554: the write that drops gateway_pending failing left a failed job the next
    # dashboard read as taken down by a restart that was never sent, and called done.
    write, failed = api.self_update._write_self_update, []

    def drop_fails_once(target, job):
        if job["step"] == "restarting_gateway" and "gateway_pending" not in job and not failed:
            failed.append(1)
            raise OSError("disk full")
        write(target, job)

    monkeypatch.setattr(api.self_update, "_write_self_update", drop_fails_once)
    job, _, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway=lambda draining: pytest.fail("restart sent"))
    assert failed and job["step"] == "failed" and job["gateway_pending"]
    assert not api.self_update._taken_down_by_gateway_restart(job)


def test_a_failure_before_the_gateway_restart_isnt_read_as_taken_down(tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "failed", "failed_step": "restarting_gateway", "boot_id": "old",
                                "gateway_pending": True, "version": api.capabilities.LOADED_VERSION,
                                "finished_at": api.self_update.time.time() - 5}))
    assert api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0) is None


def test_a_gateway_restart_that_took_the_dashboard_down_finishes_the_job(tmp_path):
    # A service manager restarted the dashboard with the gateway: the restart
    # command died with it and the job read as failed (Eric, 2026-10-24).
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    failed = {"id": "j1", "step": "failed", "failed_step": "restarting_gateway", "boot_id": "old",
              "version": api.capabilities.LOADED_VERSION, "finished_at": api.self_update.time.time() - 5,
              "detail": "The plugin is updated, but the gateway didn't restart. Run hermes gateway status on the host."}
    path.write_text(json.dumps(failed))
    notified = []
    job = api.self_update.finish_self_update_after_restart(path, notified.append, delay=0)
    assert job["step"] == "done" and job["loaded_version"] == api.capabilities.LOADED_VERSION
    assert "detail" not in job and "failed_step" not in job
    api.self_update.time.sleep(0.2)
    assert [n["step"] for n in notified] == ["done"]


def test_an_old_or_other_failure_stays_failed(tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    base = {"id": "j1", "step": "failed", "failed_step": "restarting_gateway", "boot_id": "old",
            "version": api.capabilities.LOADED_VERSION, "finished_at": api.self_update.time.time() - 5}
    for changed in ({"finished_at": api.self_update.time.time() - api.self_update.SELF_UPDATE_TAKEN_DOWN_S - 60},
                    {"failed_step": "downloading"},
                    {"code": "restart_needed"},
                    {"version": "0.0.1"},
                    {"boot_id": api.capabilities.BOOT_ID}):
        path.write_text(json.dumps({**base, **changed}))
        assert api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0) is None
        assert json.loads(path.read_text())["step"] == "failed"


def test_status_fails_a_job_nobody_runs_and_says_so(tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    notified = []
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": api.capabilities.BOOT_ID,
                                "updated_at": api.self_update.time.time()}))
    assert api.self_update.self_update_status(path, notify=notified.append)["step"] == "failed"
    path.write_text(json.dumps({"id": "j2", "step": "restarting_dashboard", "boot_id": "old", "updated_at": 0}))
    assert api.self_update.self_update_status(path, notify=notified.append)["step"] == "failed"
    path.write_text(json.dumps({"id": "j3", "step": "restarting_dashboard", "boot_id": "old",
                                "updated_at": api.self_update.time.time()}))
    assert api.self_update.self_update_status(path, notify=notified.append)["step"] == "restarting_dashboard"
    api.self_update.time.sleep(0.2)
    assert [n["id"] for n in notified] == ["j1", "j2"]


def test_a_pending_restart_isnt_stale(monkeypatch, tmp_path):
    path = tmp_path / api.self_update.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": api.capabilities.BOOT_ID, "updated_at": 0}))
    monkeypatch.setattr(api.self_update, "_self_restart_scheduled", True)
    assert api.self_update.self_update_status(path, notify=lambda job: None)["step"] == "restarting_dashboard"


def test_another_process_loading_the_file_leaves_a_live_job_alone(tmp_path):
    import subprocess as sp
    other = sp.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        path = tmp_path / api.self_update.SELF_UPDATE_FILE
        path.write_text(json.dumps({"id": "j1", "step": "downloading", "boot_id": "old", "pid": other.pid}))
        assert api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0) is None
        assert json.loads(path.read_text())["step"] == "downloading"
    finally:
        other.kill()
        other.wait()
    # Once that process is gone, the job is over.
    assert api.self_update.finish_self_update_after_restart(path, lambda job: None, delay=0)["step"] == "failed"


def test_a_dashboard_that_cant_restart_itself_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(api.self_update.sys, "platform", "win32")
    with pytest.raises(api.core.TokenError) as raised:
        api.self_update.start_self_update(None, lambda *a: None, path=tmp_path / "job.json", steps=(dict, _restarted))
    assert raised.value.status == 501


def test_a_hermes_without_the_steps_refuses():
    with pytest.raises(api.core.TokenError) as raised:
        api.self_update._hermes_update_steps()
    assert raised.value.status == 501


def test_a_hermes_with_the_modules_but_not_the_calls_refuses(monkeypatch):
    # "cannot import name ..." names the module it looked in, so it reads as missing too.
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", types.ModuleType("hermes_cli.plugins_cmd"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", types.ModuleType("hermes_cli.web_server"))
    with pytest.raises(api.core.TokenError) as raised:
        api.self_update._hermes_update_steps()
    assert raised.value.status == 501


def test_start_runs_one_job_at_a_time(monkeypatch, tmp_path):
    import threading
    release = threading.Event()

    def pull():
        release.wait(5)
        return {"ok": False, "error": "stop"}

    path = tmp_path / "job.json"
    first = api.self_update.start_self_update("coder", lambda *a: None, path=path, steps=(pull, _restarted),
                                  homes=lambda: [tmp_path], notify=lambda job: None)
    second = api.self_update.start_self_update("coder", lambda *a: None, path=path, steps=(pull, _restarted),
                                   homes=lambda: [tmp_path], notify=lambda job: None)
    assert first["id"] == second["id"] and first["step"] == "downloading" and first["profile"] == "coder"
    release.set()
    api.self_update._self_update_thread.join(5)
    assert json.loads(path.read_text())["step"] == "failed"


def test_the_notification_goes_to_the_profile_that_asked(monkeypatch):
    sent = []
    client = types.SimpleNamespace(__package__="conduit_push_dashboard_package",
                                   load_state=lambda: {"credential": "c"}, send_now=sent.append)
    monkeypatch.setattr(api.self_update, "_plugin_client", lambda: client)
    monkeypatch.setitem(sys.modules, "conduit_push_dashboard_package.events", _events())
    api.self_update._notify_self_update({"id": "j1", "step": "done", "loaded_version": "0.63.0", "profile": ""})
    assert sent and sent[0]["type"] == "background_task.finished"
    assert sent[0]["title"] == "Conduit plugin updated" and "0.63.0" in sent[0]["body"]
    assert sent[0]["profile"] == "default"


def test_the_plugin_client_imports_with_its_siblings():
    client = api.self_update._plugin_client()
    assert client.__name__ == "conduit_push_dashboard_package.client"
    assert callable(client.send_now)


def _events():
    spec = importlib.util.spec_from_file_location("conduit_self_update_events", ROOT / "events.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_job_waiting_for_its_dashboard_restart_is_returned_not_replaced(monkeypatch, tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": api.capabilities.BOOT_ID,
                                "updated_at": api.self_update.time.time()}))
    monkeypatch.setattr(api.self_update, "_self_restart_scheduled", True)
    job = api.self_update.start_self_update(None, lambda *a: None, path=path, steps=(dict, _restarted), homes=lambda: [tmp_path],
                                notify=lambda job: None)
    assert job["id"] == "j1" and api.self_update._self_update_thread is None


def test_a_failed_handover_lets_a_later_restart_through(monkeypatch, tmp_path):
    def unwritable(path, job):
        if job.get("step") == "restarting_dashboard":
            raise OSError("disk full")
        path.write_text(json.dumps(job))

    monkeypatch.setattr(api.self_update, "_write_self_update", unwritable)
    monkeypatch.setattr(api.capabilities, "_plugin_version", lambda: "9.9.9")
    job = {"id": "j1", "step": "downloading", "boot_id": api.capabilities.BOOT_ID}
    notified = []
    api.self_update._run_self_update(job, tmp_path / "job.json", lambda: {"ok": True}, _restarted, lambda *a: None,
                         lambda: [tmp_path], notified.append, sleep=lambda s: None, gateway_work=lambda: 0)
    assert job["step"] == "failed" and api.self_update._self_restart_scheduled is False and len(notified) == 1
    # Failed at the handover itself, not earlier.
    assert job["failed_step"] == "restarting_dashboard" and "OSError" in job["detail"]


def test_starts_are_rate_limited(monkeypatch, tmp_path):
    def run(i):
        path = tmp_path / f"job{i}.json"
        api.self_update.start_self_update(None, lambda *a: None, path=path, steps=(lambda: {"ok": False, "error": "x"}, _restarted),
                              homes=lambda: [tmp_path], notify=lambda job: None)
        api.self_update._self_update_thread.join(5)

    for i in range(3):
        run(i)
    with pytest.raises(api.core.TokenError) as raised:
        run(3)
    assert raised.value.status == 429


def _fake_hermes(monkeypatch, spawn):
    plugins_cmd = types.ModuleType("hermes_cli.plugins_cmd")
    plugins_cmd.dashboard_update_user_plugin = lambda name: {"ok": True, "name": name}
    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._spawn_gateway_restart = spawn
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", plugins_cmd)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", web_server)


def test_the_gateway_restart_interrupts_on_a_hermes_that_drains(monkeypatch):
    calls = []

    def spawn(profile=None, *, interrupt=False):
        calls.append((profile, interrupt))
        return types.SimpleNamespace(wait=lambda timeout: 0), False

    _fake_hermes(monkeypatch, spawn)
    pull, restart_gateway = api.self_update._hermes_update_steps()
    assert pull() == {"ok": True, "name": "conduit_push"}
    assert restart_gateway() == 0 and calls == [(None, True)]


def test_the_gateway_restart_works_on_a_hermes_before_interrupt(monkeypatch):
    # Hermes before 2026-10-20 took no interrupt: passing it was a TypeError.
    calls = []

    def spawn(profile=None):
        calls.append(profile)
        return types.SimpleNamespace(wait=lambda timeout: 0), False

    _fake_hermes(monkeypatch, spawn)
    _, restart_gateway = api.self_update._hermes_update_steps()
    assert restart_gateway() == 0 and calls == [None]


def test_the_gateway_restart_skips_interrupt_when_it_cannot_be_passed_by_name(monkeypatch):
    calls = []

    def spawn(profile=None, interrupt=False, /):
        calls.append((profile, interrupt))
        return types.SimpleNamespace(wait=lambda timeout: 0), False

    _fake_hermes(monkeypatch, spawn)
    _, restart_gateway = api.self_update._hermes_update_steps()
    assert restart_gateway() == 0 and calls == [(None, False)]


def test_the_corrected_push_isnt_a_duplicate_of_the_failed_one(monkeypatch):
    # The relay drops a repeated event id, so the done push for a job the
    # dying dashboard reported as failed needs an id of its own.
    sent = []
    events = types.ModuleType("fake_conduit_pkg.events")
    events.event_id = lambda prefix, *parts: f"{prefix}:{'|'.join(str(p) for p in parts)}"
    events.push_event = lambda kind, identifier, **fields: {"identifier": identifier, **fields}
    monkeypatch.setitem(sys.modules, "fake_conduit_pkg.events", events)
    client = types.SimpleNamespace(__package__="fake_conduit_pkg", load_state=lambda: True, send_now=sent.append)
    monkeypatch.setattr(api.self_update, "_plugin_client", lambda: client)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: __import__("contextlib").nullcontext())
    api.self_update._notify_self_update({"id": "j1", "step": "failed", "detail": "gateway"})
    api.self_update._notify_self_update({"id": "j1", "step": "done", "loaded_version": "0.69.1"})
    assert len(sent) == 2 and sent[0]["identifier"] != sent[1]["identifier"]
    assert sent[1]["title"] == "Conduit plugin updated"


def test_an_update_points_every_profile_at_it_before_the_gateway_restarts(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(api.capabilities, "_plugin_version", lambda: "9.9.9")
    job = {"id": "j1", "step": "downloading", "boot_id": api.capabilities.BOOT_ID}
    api.self_update._run_self_update(job, tmp_path / "job.json", lambda: {"ok": True},
                                     lambda draining: order.append("restart") or 0, lambda *a: None, lambda: [tmp_path],
                                     lambda job: None, sleep=lambda s: None,
                                     point_profiles=lambda: order.append("profiles"))
    assert order == ["profiles", "restart"]


def test_profiles_are_pointed_from_the_main_home(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    shared = root / "plugins" / "conduit_push"
    shared.mkdir(parents=True)
    (shared / "plugin.yaml").write_text("name: conduit_push\nversion: 9.9.9\n")
    home = root / "profiles" / "coder"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [conduit_push]\n")
    monkeypatch.setattr(api.core, "_plugin_root", lambda: str(shared))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_default_hermes_root", lambda: root, raising=False)
    # The sweep reads profile_copies.py from the plugin's folder.
    (shared / "profile_copies.py").write_text((ROOT / "profile_copies.py").read_text())
    assert api.self_update._point_profiles() == ["profile coder: linked to the main install (9.9.9)"]
    assert (home / "plugins" / "conduit_push").resolve() == shared.resolve()


def _module_at(path):
    module = types.ModuleType(f"fake_{path.stem}")
    module.__file__ = str(path)
    return module


def test_hermes_changed_on_disk_reads_the_loaded_files_against_the_start(monkeypatch, tmp_path):
    # issue 1889: a dashboard started on Hermes 0.22.3 kept running after Hermes moved to 0.22.4+.
    package = tmp_path / "hermes" / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    declarations = tmp_path / "hermes" / "pm" / "plugin_declarations.py"
    declarations.parent.mkdir()
    declarations.write_text("")
    hermes_cli = _module_at(package / "__init__.py")
    monkeypatch.setitem(sys.modules, "fake_hermes_declarations", _module_at(declarations))
    started = declarations.stat().st_mtime
    assert not api.self_update._hermes_changed_on_disk(hermes_cli, started)
    os_utime = __import__("os").utime
    os_utime(declarations, (started + 60, started + 60))
    assert api.self_update._hermes_changed_on_disk(hermes_cli, started)
    # A file written as the process started isn't a change: the start time is rough.
    os_utime(declarations, (started + 2, started + 2))
    assert not api.self_update._hermes_changed_on_disk(hermes_cli, started)


def test_hermes_without_a_file_or_a_start_time_reads_as_unchanged():
    assert not api.self_update._hermes_changed_on_disk(types.ModuleType("hermes_cli"), 0.0)


def test_a_changed_hermes_pulls_in_a_fresh_process(monkeypatch):
    _fake_hermes(monkeypatch, lambda profile=None: (None, False))
    sys.modules["hermes_cli.plugins_cmd"].dashboard_update_user_plugin = lambda name: pytest.fail("pulled in the mixed process")
    monkeypatch.setattr(api.self_update, "_hermes_changed_on_disk", lambda hermes_cli: True)
    runs = []

    def run(argv, **kwargs):
        runs.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="Updating abc..def\n"
                                     + api.self_update._FRESH_PULL_MARK + json.dumps({"ok": True, "name": argv[-1]}) + "\n")

    monkeypatch.setattr(api.self_update.subprocess, "run", run)
    pull, _ = api.self_update._hermes_update_steps()
    assert pull() == {"ok": True, "name": "conduit_push"}
    (argv, kwargs), = runs
    assert argv[0] == sys.executable and argv[-1] == "conduit_push" and kwargs["stdin"] is api.self_update.subprocess.DEVNULL


def test_a_fresh_pull_runs_the_hermes_on_disk_for_this_home(monkeypatch, tmp_path):
    package = tmp_path / "hermes" / "hermes_cli"
    package.mkdir(parents=True)
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__file__ = str(package / "__init__.py")
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path / "home"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    runs = []

    def run(argv, **kwargs):
        runs.append(kwargs)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=api.self_update._FRESH_PULL_MARK + '{"ok": true}')

    monkeypatch.setattr(api.self_update.subprocess, "run", run)
    assert api.self_update._pull_in_fresh_process() == {"ok": True}
    kwargs, = runs
    # The checkout's folder first on the path, so `-c` imports the Hermes on disk.
    assert kwargs["cwd"] == str((tmp_path / "hermes").resolve())
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path / "home")


def test_a_re_exec_counts_from_when_it_loaded_hermes_again(monkeypatch):
    # _reexec keeps the pid, so the process's creation time stays the first start's.
    monkeypatch.setattr(api.self_update, "_MODULE_LOADED_AT", 2_000.0)
    psutil = types.ModuleType("psutil")
    psutil.Process = lambda pid: types.SimpleNamespace(create_time=lambda: 1_000.0)
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    assert api.self_update._process_start_time() == 2_000.0


def test_a_fresh_pull_that_gives_no_answer_says_to_restart_the_dashboard(monkeypatch):
    monkeypatch.setattr(api.self_update.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback"))
    result = api.self_update._pull_in_fresh_process()
    assert result["ok"] is False and "Restart the Hermes dashboard" in result["error"]

    def timeout(*a, **k):
        raise api.self_update.subprocess.TimeoutExpired("python", 1)

    monkeypatch.setattr(api.self_update.subprocess, "run", timeout)
    error = api.self_update._pull_in_fresh_process()["error"]
    assert error.startswith("The update didn't finish on the host within 15 minutes.") and "Restart the Hermes dashboard" in error


def test_a_fresh_pull_passes_hermes_refusals_through(monkeypatch):
    refusal = {"ok": False, "scan_blocked": True, "error": "blocked"}
    monkeypatch.setattr(api.self_update.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stderr="", stdout=api.self_update._FRESH_PULL_MARK + json.dumps(refusal)))
    assert api.self_update._pull_in_fresh_process() == refusal
