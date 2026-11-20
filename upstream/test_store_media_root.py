"""A delete names the media root of the store's own profile (notifier#945).

On Hermes' storage interface a session delete takes the profile's retained
media root, and its hosted-document fence retires only the files under it.
Hermes' resolver (``gateway.session_ingress_media.native_inputs_root``) reads
the ambient home, while the plugin deletes in every agent's home from one
process (a room's cleanup), so ``hermes_store`` builds the root from the
store's home. This checks that it is the path Hermes' own resolver gives with
that profile's home in effect, for the launch profile and a named one, and
that a delete in the named profile's store runs with it.

Run in a child with a Hermes home of its own, since Hermes reads its home from
the environment as its modules load.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

CHILD = r"""
import importlib, json, sys, types
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
package = types.ModuleType("conduit_push")
package.__path__ = [str(root)]
sys.modules["conduit_push"] = package
store = importlib.import_module("conduit_push.hermes_store")
if not store.has_storage_interface():
    json.dump({"interface": False}, sys.stdout)
    raise SystemExit(0)

import hermes_constants
from gateway.session_ingress_media import native_inputs_root

member = home / "profiles" / "b"
member.mkdir(parents=True)
found = {"interface": True, "ambient": str(native_inputs_root()), "plugin": {}, "hermes": {}}
for name, profile in (("launch", home), ("member", member)):
    found["plugin"][name] = store._media_root(profile)
    token = hermes_constants.set_hermes_home_override(profile)
    try:
        found["hermes"][name] = str(native_inputs_root())
    finally:
        hermes_constants.reset_hermes_home_override(token)

sid = "20261120_090000_aaaaaa"
with store.open_store(member) as db:
    db.ensure_session(sid, "api_server")
    found["deleted"] = db.delete_sessions([sid])
    found["left"] = db.get_session(sid)
json.dump(found, sys.stdout)
"""


def test_a_delete_names_its_own_profiles_media_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(home)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    if not found["interface"]:
        pytest.skip("this Hermes deletes through SessionDB, which resolves its own media root")
    assert found["plugin"] == found["hermes"]
    # The member's root is its own, not the launch profile's the ambient resolver gives.
    assert found["plugin"]["launch"] == found["ambient"] != found["plugin"]["member"]
    assert (found["deleted"], found["left"]) == (1, None)
