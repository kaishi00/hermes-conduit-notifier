"""Hermes Desktop loads the plugin's Desktop half (desktop/plugin.js), which
draws the boards conduit_show keeps for Desktop chats.

Hermes lints a package's Desktop surface for constructs a plugin must not use
(eval, a second-stage import, reaching into the app's DOM), and its rules
change between releases, so this runs Hermes' own lint over the files the
plugin ships, as test_install_scan.py runs its scanner. Run in a child process
with a scratch HERMES_HOME, like test_imports.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_install_scan import _shipped_copy

PROBE = r"""
import json, sys
from pathlib import Path
import importlib.util
if importlib.util.find_spec("hermes_cli.plugin_validate_desktop") is None:
    print(json.dumps(None))
    raise SystemExit
from hermes_cli.plugin_validate_desktop import desktop_surface_hits
print(json.dumps(desktop_surface_hits(Path(sys.argv[1]))))
"""


def test_hermes_desktop_lint_passes_the_desktop_half(tmp_path):
    plugin = _shipped_copy(tmp_path / "conduit_push")
    assert (plugin / "desktop" / "plugin.js").is_file()
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, str(plugin)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    hits = json.loads(out.stdout.strip().splitlines()[-1])
    if hits is None:
        pytest.skip("this Hermes has no Desktop plugin lint")
    assert hits == [], "Hermes' Desktop lint holds the Desktop half:\n" + "\n".join(hits)
