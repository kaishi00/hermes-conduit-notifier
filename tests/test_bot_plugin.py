"""A bot's Conduit plugin (hermes-conduit#2803): whether a profile has it on, and
turning it on there, as New Bot and Edit Bot's Turn On ask."""

import importlib.util
import os
import pathlib
import subprocess
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


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_bot_plugin", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    shared = root / "plugins" / "conduit_push"
    shared.mkdir(parents=True)
    (shared / "plugin.yaml").write_text("name: conduit_push\nversion: 0.180.0\n")
    # profile_copies.py is read from the install's folder, as on a host.
    (shared / "profile_copies.py").write_text((ROOT / "profile_copies.py").read_text())
    fresh = root / "profiles" / "furina"
    fresh.mkdir(parents=True)
    (fresh / "config.yaml").write_text("plugins:\n  enabled: []\n")
    monkeypatch.setattr(api.core, "_plugin_root", lambda: str(shared))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_default_hermes_root", lambda: root, raising=False)
    monkeypatch.setattr(api.core, "_profile_dir", lambda name: root / "profiles" / name)
    enabled = set()

    async def refused(profile):
        return profile not in enabled

    monkeypatch.setattr(api.core, "_plugin_refused_on", refused)
    runs = []

    def run(command, **kwargs):
        runs.append((command, kwargs))
        enabled.add(command[command.index("-p") + 1])
        return subprocess.CompletedProcess(command, 0, "✓ Plugin conduit_push enabled.\n", "")

    monkeypatch.setattr(api.bot_plugin.subprocess, "run", run)
    return types.SimpleNamespace(root=root, shared=shared, fresh=fresh, enabled=enabled, runs=runs)


def _client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_a_fresh_profile_reads_off_and_turn_on_links_it_and_runs_hermes_enable(host, monkeypatch):
    client = _client()
    state = client.get(f"{BASE}/profiles/plugin", params={"name": "furina"}).json()
    assert state == {"profile": "furina", "on": False, "allowed": False, "linked": False, "can_turn_on": True}

    monkeypatch.setenv("HERMES_HOME", str(host.fresh))
    response = client.post(f"{BASE}/profiles/plugin", json={"name": "furina"})

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "profile": "furina", "on": True, "allowed": True, "linked": True,
                               "can_turn_on": True}
    assert response.headers["cache-control"] == "no-store"
    link = host.fresh / "plugins" / "conduit_push"
    assert link.resolve() == host.shared.resolve()
    (command, kwargs), = host.runs
    assert command[1:] == ["-m", "hermes_cli.main", "-p", "furina", "plugins", "enable", "conduit_push"]
    # -p names the profile under the root, whatever home the dashboard runs in.
    assert kwargs["env"]["HERMES_HOME"] == str(host.root)
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_hermes_refusing_the_enable_says_why(host, monkeypatch):
    def refuse(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "Error: Plugin 'conduit_push' is recalled.\n")

    monkeypatch.setattr(api.bot_plugin.subprocess, "run", refuse)
    response = _client().post(f"{BASE}/profiles/plugin", json={"name": "furina"})
    assert response.status_code == 502
    assert response.json()["detail"] == "Hermes refused to turn the plugin on: Error: Plugin 'conduit_push' is recalled."


@pytest.mark.parametrize("name, status", [("", 400), ("default", 400), ("../etc", 400), ("-p", 400),
                                          ("nobody", 404)])
def test_only_an_existing_bot_profile_is_named(host, name, status):
    client = _client()
    assert client.post(f"{BASE}/profiles/plugin", json={"name": name}).status_code == status
    assert client.get(f"{BASE}/profiles/plugin", params={"name": name}).status_code == status
    assert host.runs == []


def test_a_profiles_own_copy_of_the_plugin_cant_share_itself(host, monkeypatch, tmp_path):
    own = tmp_path / "elsewhere" / "conduit_push"
    own.mkdir(parents=True)
    (own / "plugin.yaml").write_text("name: conduit_push\nversion: 0.180.0\n")
    (own / "profile_copies.py").write_text((ROOT / "profile_copies.py").read_text())
    monkeypatch.setattr(api.core, "_plugin_root", lambda: str(own))
    client = _client()
    # The bot's own link to the main install still counts as its copy.
    (host.fresh / "plugins").mkdir()
    (host.fresh / "plugins" / "conduit_push").symlink_to(host.shared, target_is_directory=True)
    state = client.get(f"{BASE}/profiles/plugin", params={"name": "furina"}).json()
    assert state["can_turn_on"] is False and state["linked"] is True
    assert client.post(f"{BASE}/profiles/plugin", json={"name": "furina"}).status_code == 501
    assert host.runs == []


def test_a_desktop_ticket_turns_it_on_for_its_own_profile_only(host, monkeypatch):
    monkeypatch.setattr(api.core, "_native_ticket_home", lambda request: str(host.root / "profiles" / "nahida"))
    client = _client()
    assert client.post(f"{BASE}/profiles/plugin", json={"name": "furina"}).status_code == 403
    assert client.get(f"{BASE}/profiles/plugin", params={"name": "furina"}).status_code == 403
    # Checked before the profile is looked up, so it tells nothing about other profiles.
    assert client.get(f"{BASE}/profiles/plugin", params={"name": "nobody"}).status_code == 403
    assert host.runs == []


def test_hermes_failing_to_check_the_profile_is_an_error_not_off(host, monkeypatch):
    async def unavailable(profile):
        raise api.bot_plugin.HTTPException(status_code=503, detail="Hermes couldn't check this profile's plugins")

    monkeypatch.setattr(api.core, "_plugin_refused_on", unavailable)
    response = _client().get(f"{BASE}/profiles/plugin", params={"name": "furina"})
    assert response.status_code == 503
