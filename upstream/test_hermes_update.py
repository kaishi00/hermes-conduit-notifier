"""Update Hermes from Conduit: the `hermes update` Hermes' dashboard spawns
runs in a systemd scope of its own (dashboard/api/hermes_update.py), so
stopping the dashboard's unit can't kill it (a real host, 2026-11-20).

This drives Hermes' own spawner (``web_server_gateway._spawn_hermes_action``)
with the plugin loaded in the dashboard's process, and checks the command it
hands the process: the update gets the scope in front, another action
doesn't. The scope itself is a stand-in, so the test needs no systemd.

Run in a child with a Hermes home of its own, since Hermes reads its home from
the environment as its modules load.
"""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import ROOT

CHILD = r"""
import importlib.util, json, subprocess, sys
from pathlib import Path

root = Path(sys.argv[1])
import hermes_cli.web_server  # the dashboard's process
from hermes_cli import web_server_gateway

spec = importlib.util.spec_from_file_location("conduit_plugin_api", root / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
update = api.hermes_update
update.scope_prefix = lambda: ["SCOPE", "--"]

spawned = []

class Proc:
    pid = 4242
    def poll(self):
        return None

def popen(cmd, **kwargs):
    spawned.append(list(cmd))
    return Proc()

subprocess.Popen = popen
web_server_gateway._spawn_hermes_action(["update"], "hermes-update", env_overrides={"HERMES_ACTION_ID": "a" * 32})
web_server_gateway._spawn_hermes_action(["skills", "list"], "hermes-update")
json.dump({"installed": update._wrapped is not None, "spawned": spawned}, sys.stdout)
"""


def test_hermes_update_from_the_dashboard_gets_its_own_scope(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT)], capture_output=True, text=True, timeout=180,
                         cwd=tmp_path, env={"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"})
    assert out.returncode == 0, out.stderr[-4000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert found["installed"], "the plugin didn't wrap Hermes' launcher in the dashboard"
    update, other = found["spawned"]
    assert update[:2] == ["SCOPE", "--"] and update[-1] == "update", update
    assert "SCOPE" not in other and other[-2:] == ["skills", "list"], other
