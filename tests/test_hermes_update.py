"""Update Hermes from Conduit: `hermes update`, spawned by Hermes' dashboard,
runs in a systemd scope of its own, so stopping the dashboard's unit can't
kill it (a real host, 2026-11-20). Every other command, and a host where
systemd can't make the scope, runs as Hermes starts it."""

import importlib.util
import pathlib
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_hermes_update", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
update = api.hermes_update
HERMES = ["/opt/hermes/python", "-c", "bootstrap"]


@pytest.fixture
def launchers(monkeypatch):
    """Hermes' launcher module, as 0.22 and 0.23 lay it out."""
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    module = types.ModuleType("hermes_cli._launchers")

    def runtime_command(repo_root, args=(), *, module="hermes_cli.main", code=None, python=None, home=None):
        return [*HERMES, *args]

    module.runtime_command = runtime_command
    package._launchers = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli._launchers", module)
    yield module
    update.uninstall()


@pytest.fixture
def systemd(monkeypatch, tmp_path):
    """A Linux dashboard in a systemd service, as root, whose systemd makes scopes."""
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/hermes-dashboard.service\n")
    real = update._in_service_cgroup
    monkeypatch.setattr(update, "_in_service_cgroup", lambda: real(str(cgroup)))
    monkeypatch.setattr(update.sys, "platform", "linux")
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    monkeypatch.setattr(update, "_probed", set())
    probes = []

    def run(argv, **kwargs):
        probes.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(update.subprocess, "run", run)
    return types.SimpleNamespace(cgroup=cgroup, probes=probes)


def _spawn_hermes_action(subcommand):
    # Hermes' spawner, by name: the wrapper scopes only what it starts.
    from hermes_cli._launchers import runtime_command
    return runtime_command("/opt/hermes", subcommand)


def test_hermes_update_from_the_dashboard_runs_in_its_own_scope(launchers, systemd):
    assert update.install() is True
    command = _spawn_hermes_action(["update"])
    assert command[:4] == ["/usr/bin/systemd-run", "--scope", "--quiet", "--collect"]
    assert command[4].startswith("--unit=hermes-update-") and command[5] == "--"
    assert command[6:] == [*HERMES, "update"]
    # The probe asked for the same kind of scope first.
    assert systemd.probes == [["/usr/bin/systemd-run", "--scope", "--quiet", "--collect", "--", "true"]]
    # A manager that made one scope isn't asked again, and each update gets a unit of its own.
    again = _spawn_hermes_action(["update"])
    assert len(systemd.probes) == 1 and again[4] != command[4]


def test_a_refused_probe_is_asked_again_next_time(launchers, systemd, monkeypatch):
    update.install()
    answers = [1, 0]
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, answers.pop(0)))
    assert _spawn_hermes_action(["update"]) == [*HERMES, "update"]
    assert _spawn_hermes_action(["update"])[0] == "/usr/bin/systemd-run"


def test_other_actions_and_callers_run_as_hermes_starts_them(launchers, systemd):
    update.install()
    assert _spawn_hermes_action(["gateway", "restart"]) == [*HERMES, "gateway", "restart"]
    assert launchers.runtime_command("/opt/hermes", ["update"]) == [*HERMES, "update"]
    assert systemd.probes == []


def test_a_dashboard_outside_a_service_or_a_refusing_systemd_starts_it_as_before(launchers, systemd, monkeypatch):
    update.install()
    systemd.cgroup.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    assert _spawn_hermes_action(["update"]) == [*HERMES, "update"]
    systemd.cgroup.write_text("0::/system.slice/hermes-dashboard.service\n")
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1))
    assert _spawn_hermes_action(["update"]) == [*HERMES, "update"]


def test_a_dashboard_user_without_root_uses_its_own_manager_or_none(launchers, systemd, monkeypatch, tmp_path):
    update.install()
    monkeypatch.setattr(update.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(update.os, "getuid", lambda: 1000)
    monkeypatch.setattr(update.os.path, "exists", lambda path: path == "/run/user/1000/bus")
    command = _spawn_hermes_action(["update"])
    assert command[:5] == ["env", "XDG_RUNTIME_DIR=/run/user/1000", "/usr/bin/systemd-run", "--user", "--scope"]
    monkeypatch.setattr(update.os.path, "exists", lambda path: False)
    assert _spawn_hermes_action(["update"]) == [*HERMES, "update"]


def test_installing_twice_wraps_hermes_once_and_unloading_puts_it_back(launchers, systemd):
    own = launchers.runtime_command
    assert update.install() and update.install()
    assert launchers.runtime_command.__wrapped__ is own
    update.uninstall()
    assert launchers.runtime_command is own


def test_the_cgroup_check_reads_this_process_by_pid(monkeypatch):
    """With no path, it reads this process's own cgroup file, built from its pid."""
    opened = []

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return "0::/system.slice/hermes-dashboard.service\n"

    def fake_open(path, *args, **kwargs):
        opened.append(path)
        return Handle()

    monkeypatch.setattr(update.os, "getpid", lambda: 4242)
    monkeypatch.setattr("builtins.open", fake_open)
    assert update._in_service_cgroup() is True
    assert opened == [update.os.path.join("/proc", "4242", "cgroup")]
