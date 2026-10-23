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
    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_CHILD_PID", raising=False)
    monkeypatch.setattr(api, "_self_restart_scheduled", False)
    monkeypatch.setattr(api, "_self_update_thread", None)
    monkeypatch.setattr(api, "_self_update_limiter", api._MintLimiter(3, 600.0))


def _run(tmp_path, *, pull=None, gateway=0, version="9.9.9", markers=None, monkeypatch=None):
    """Runs one job in this thread; returns (job file, scheduled restarts, notifications)."""
    path = tmp_path / api.SELF_UPDATE_FILE
    scheduled, notified = [], []
    monkeypatch.setattr(api, "_plugin_version", lambda: version)
    job = {"id": "j1", "step": "downloading", "from_version": api.LOADED_VERSION, "boot_id": api.BOOT_ID,
           "profile": "coder"}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        (tmp_path / "desktop" / "interrupted_turns.json").write_text("{}")

    if markers:
        (tmp_path / "desktop").mkdir(parents=True, exist_ok=True)
        (tmp_path / "desktop" / "interrupted_turns.json").write_text(json.dumps(markers))

    def restart_gateway():
        if isinstance(gateway, Exception):
            raise gateway
        return gateway

    api._run_self_update(job, path, pull or (lambda: {"ok": True}), restart_gateway,
                         lambda delay, fn: scheduled.append((delay, fn)), lambda: [tmp_path],
                         notified.append, sleep=sleep)
    return json.loads(path.read_text()), scheduled, notified, sleeps


def test_an_update_restarts_the_gateway_then_the_dashboard(monkeypatch, tmp_path):
    job, scheduled, notified, _ = _run(tmp_path, monkeypatch=monkeypatch)
    assert job["step"] == "restarting_dashboard" and job["version"] == "9.9.9"
    assert [delay for delay, _ in scheduled] == [api.SELF_RESTART_DELAY_S]
    # The restarted dashboard notifies, not this one.
    assert notified == []


def test_running_chats_are_waited_for_on_the_host(monkeypatch, tmp_path):
    job, scheduled, _, sleeps = _run(tmp_path, monkeypatch=monkeypatch,
                                     markers={"s1": {"writer_pid": api.os.getpid()}})
    assert sleeps == [api.SELF_UPDATE_CHATS_RECHECK_S]
    assert job["step"] == "restarting_dashboard" and len(scheduled) == 1


def test_a_turn_that_never_ends_doesnt_hold_the_update_forever(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "SELF_UPDATE_CHATS_LIMIT_S", 3 * api.SELF_UPDATE_CHATS_RECHECK_S)
    monkeypatch.setattr(api, "_running_turns", lambda *a: 1)
    job, scheduled, notified, sleeps = _run(tmp_path, monkeypatch=monkeypatch, markers={"s1": {}})
    assert job["step"] == "failed" and job["code"] == "restart_needed" and scheduled == []
    assert len(sleeps) == 3 and [n["step"] for n in notified] == ["failed"]


def test_an_unreadable_timestamp_reads_as_stale(tmp_path):
    path = tmp_path / api.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": "old", "updated_at": "x"}))
    assert api.self_update_status(path)["step"] == "failed"


def test_nothing_new_on_disk_finishes_without_a_restart(monkeypatch, tmp_path):
    job, scheduled, notified, _ = _run(tmp_path, monkeypatch=monkeypatch, version=api.LOADED_VERSION)
    assert job["step"] == "done" and scheduled == []
    assert [n["step"] for n in notified] == ["done"]


def test_a_refused_pull_fails_with_hermes_words(monkeypatch, tmp_path):
    job, scheduled, notified, _ = _run(tmp_path, monkeypatch=monkeypatch, pull=lambda: {
        "ok": False, "consent_required": True, "error": "Plugin 'conduit_push' wants new capabilities"})
    assert job["step"] == "failed" and job["code"] == "consent_required" and job["failed_step"] == "downloading"
    assert "new capabilities" in job["detail"] and scheduled == []
    assert [n["step"] for n in notified] == ["failed"]


def test_a_gateway_that_doesnt_come_back_fails(monkeypatch, tmp_path):
    job, scheduled, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway=api.subprocess.TimeoutExpired("hermes", 1))
    assert job["step"] == "failed" and "gateway" in job["detail"] and scheduled == []
    assert job["failed_step"] == "restarting_gateway"
    job, _, _, _ = _run(tmp_path, monkeypatch=monkeypatch, gateway=1)
    assert job["step"] == "failed"


def test_a_crash_fails_the_job(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("disk")

    job, _, notified, _ = _run(tmp_path, monkeypatch=monkeypatch, pull=boom)
    assert job["step"] == "failed" and "RuntimeError" in job["detail"] and len(notified) == 1


def test_the_restarted_dashboard_finishes_and_notifies(monkeypatch, tmp_path):
    path = tmp_path / api.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": "old",
                                "version": api.LOADED_VERSION, "profile": "coder"}))
    notified = []
    job = api.finish_self_update_after_restart(path, notified.append, delay=0)
    assert job["step"] == "done" and job["loaded_version"] == api.LOADED_VERSION
    api.time.sleep(0.2)
    assert [n["step"] for n in notified] == ["done"]
    assert json.loads(path.read_text())["step"] == "done"
    # Once only.
    assert api.finish_self_update_after_restart(path, notified.append, delay=0) is None


def test_a_restart_mid_download_reads_as_failed(monkeypatch, tmp_path):
    path = tmp_path / api.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "downloading", "boot_id": "old"}))
    job = api.finish_self_update_after_restart(path, lambda job: None, delay=0)
    assert job["step"] == "failed"


def test_another_restart_after_the_pull_still_finishes_the_job(tmp_path):
    path = tmp_path / api.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": "old",
                                "version": api.LOADED_VERSION}))
    job = api.finish_self_update_after_restart(path, lambda job: None, delay=0)
    assert job["step"] == "done"


def test_status_fails_a_job_nobody_runs_and_says_so(tmp_path):
    path = tmp_path / api.SELF_UPDATE_FILE
    notified = []
    path.write_text(json.dumps({"id": "j1", "step": "restarting_gateway", "boot_id": api.BOOT_ID,
                                "updated_at": api.time.time()}))
    assert api.self_update_status(path, notify=notified.append)["step"] == "failed"
    path.write_text(json.dumps({"id": "j2", "step": "restarting_dashboard", "boot_id": "old", "updated_at": 0}))
    assert api.self_update_status(path, notify=notified.append)["step"] == "failed"
    path.write_text(json.dumps({"id": "j3", "step": "restarting_dashboard", "boot_id": "old",
                                "updated_at": api.time.time()}))
    assert api.self_update_status(path, notify=notified.append)["step"] == "restarting_dashboard"
    api.time.sleep(0.2)
    assert [n["id"] for n in notified] == ["j1", "j2"]


def test_a_pending_restart_isnt_stale(monkeypatch, tmp_path):
    path = tmp_path / api.SELF_UPDATE_FILE
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": api.BOOT_ID, "updated_at": 0}))
    monkeypatch.setattr(api, "_self_restart_scheduled", True)
    assert api.self_update_status(path, notify=lambda job: None)["step"] == "restarting_dashboard"


def test_another_process_loading_the_file_leaves_a_live_job_alone(tmp_path):
    import subprocess as sp
    other = sp.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        path = tmp_path / api.SELF_UPDATE_FILE
        path.write_text(json.dumps({"id": "j1", "step": "downloading", "boot_id": "old", "pid": other.pid}))
        assert api.finish_self_update_after_restart(path, lambda job: None, delay=0) is None
        assert json.loads(path.read_text())["step"] == "downloading"
    finally:
        other.kill()
        other.wait()
    # Once that process is gone, the job is over.
    assert api.finish_self_update_after_restart(path, lambda job: None, delay=0)["step"] == "failed"


def test_a_dashboard_that_cant_restart_itself_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(api.sys, "platform", "win32")
    with pytest.raises(api.TokenError) as raised:
        api.start_self_update(None, lambda *a: None, path=tmp_path / "job.json", steps=(dict, int))
    assert raised.value.status == 501


def test_a_hermes_without_the_steps_refuses():
    with pytest.raises(api.TokenError) as raised:
        api._hermes_update_steps()
    assert raised.value.status == 501


def test_a_hermes_with_the_modules_but_not_the_calls_refuses(monkeypatch):
    # "cannot import name ..." names the module it looked in, so it reads as missing too.
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", types.ModuleType("hermes_cli.plugins_cmd"))
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", types.ModuleType("hermes_cli.web_server"))
    with pytest.raises(api.TokenError) as raised:
        api._hermes_update_steps()
    assert raised.value.status == 501


def test_start_runs_one_job_at_a_time(monkeypatch, tmp_path):
    import threading
    release = threading.Event()

    def pull():
        release.wait(5)
        return {"ok": False, "error": "stop"}

    path = tmp_path / "job.json"
    first = api.start_self_update("coder", lambda *a: None, path=path, steps=(pull, int),
                                  homes=lambda: [tmp_path], notify=lambda job: None)
    second = api.start_self_update("coder", lambda *a: None, path=path, steps=(pull, int),
                                   homes=lambda: [tmp_path], notify=lambda job: None)
    assert first["id"] == second["id"] and first["step"] == "downloading" and first["profile"] == "coder"
    release.set()
    api._self_update_thread.join(5)
    assert json.loads(path.read_text())["step"] == "failed"


def test_the_notification_goes_to_the_profile_that_asked(monkeypatch):
    sent = []
    client = types.SimpleNamespace(__package__="conduit_push_dashboard_package",
                                   load_state=lambda: {"credential": "c"}, send_now=sent.append)
    monkeypatch.setattr(api, "_plugin_client", lambda: client)
    monkeypatch.setitem(sys.modules, "conduit_push_dashboard_package.events", _events())
    api._notify_self_update({"id": "j1", "step": "done", "loaded_version": "0.63.0", "profile": ""})
    assert sent and sent[0]["type"] == "background_task.finished"
    assert sent[0]["title"] == "Conduit plugin updated" and "0.63.0" in sent[0]["body"]
    assert sent[0]["profile"] == "default"


def test_the_plugin_client_imports_with_its_siblings():
    client = api._plugin_client()
    assert client.__name__ == "conduit_push_dashboard_package.client"
    assert callable(client.send_now)


def _events():
    spec = importlib.util.spec_from_file_location("conduit_self_update_events", ROOT / "events.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_job_waiting_for_its_dashboard_restart_is_returned_not_replaced(monkeypatch, tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "j1", "step": "restarting_dashboard", "boot_id": api.BOOT_ID,
                                "updated_at": api.time.time()}))
    monkeypatch.setattr(api, "_self_restart_scheduled", True)
    job = api.start_self_update(None, lambda *a: None, path=path, steps=(dict, int), homes=lambda: [tmp_path],
                                notify=lambda job: None)
    assert job["id"] == "j1" and api._self_update_thread is None


def test_a_failed_handover_lets_a_later_restart_through(monkeypatch, tmp_path):
    def unwritable(path, job):
        if job.get("step") == "restarting_dashboard":
            raise OSError("disk full")
        path.write_text(json.dumps(job))

    monkeypatch.setattr(api, "_write_self_update", unwritable)
    monkeypatch.setattr(api, "_plugin_version", lambda: "9.9.9")
    job = {"id": "j1", "step": "downloading", "boot_id": api.BOOT_ID}
    notified = []
    api._run_self_update(job, tmp_path / "job.json", lambda: {"ok": True}, lambda: 0, lambda *a: None,
                         lambda: [tmp_path], notified.append, sleep=lambda s: None)
    assert job["step"] == "failed" and api._self_restart_scheduled is False and len(notified) == 1


def test_starts_are_rate_limited(monkeypatch, tmp_path):
    def run(i):
        path = tmp_path / f"job{i}.json"
        api.start_self_update(None, lambda *a: None, path=path, steps=(lambda: {"ok": False, "error": "x"}, int),
                              homes=lambda: [tmp_path], notify=lambda job: None)
        api._self_update_thread.join(5)

    for i in range(3):
        run(i)
    with pytest.raises(api.TokenError) as raised:
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
    pull, restart_gateway = api._hermes_update_steps()
    assert pull() == {"ok": True, "name": "conduit_push"}
    assert restart_gateway() == 0 and calls == [(None, True)]


def test_the_gateway_restart_works_on_a_hermes_before_interrupt(monkeypatch):
    # Hermes before 2026-10-20 took no interrupt: passing it was a TypeError.
    calls = []

    def spawn(profile=None):
        calls.append(profile)
        return types.SimpleNamespace(wait=lambda timeout: 0), False

    _fake_hermes(monkeypatch, spawn)
    _, restart_gateway = api._hermes_update_steps()
    assert restart_gateway() == 0 and calls == [None]


def test_the_gateway_restart_skips_interrupt_when_it_cannot_be_passed_by_name(monkeypatch):
    calls = []

    def spawn(profile=None, interrupt=False, /):
        calls.append((profile, interrupt))
        return types.SimpleNamespace(wait=lambda timeout: 0), False

    _fake_hermes(monkeypatch, spawn)
    _, restart_gateway = api._hermes_update_steps()
    assert restart_gateway() == 0 and calls == [(None, False)]
