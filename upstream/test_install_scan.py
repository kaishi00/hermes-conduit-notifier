"""Hermes' own install scan passes the plugin as it ships (hermes-conduit#1500).

Hermes scans the whole checkout before it installs or updates a plugin, docs
and relay included. One high finding makes the verdict "caution", and Conduit's
one-tap update can't confirm one, so every host stays on the old version: two
README curl examples that read the crash reports token from a shell variable
did that. The scan's rules
change from release to release, so this runs Hermes' scanner rather than a
copy of its patterns, over the files git tracks (what a clone gets).

Run in a child process with a scratch HERMES_HOME, like test_imports.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import json, sys
from pathlib import Path
import importlib.util
if importlib.util.find_spec("tools.plugin_guard") is None:
    print(json.dumps(None))
    raise SystemExit
from tools.plugin_guard import scan_plugin, should_allow_plugin_install
result = scan_plugin(Path(sys.argv[1]), source="https://github.com/kaishi00/hermes-conduit-notifier.git")
allowed, reason = should_allow_plugin_install(result)
held = [f"{str(f.severity).upper()} {f.pattern_id} {f.file}:{f.line} {f.match}"
        for f in result.findings if str(f.severity).lower() in ("high", "critical")]
print(json.dumps({"allowed": allowed, "reason": reason, "held": held}))
"""


def _shipped_copy(dest: Path) -> Path:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    for name in listed.decode("utf-8").split("\0"):
        source = ROOT / name
        if name and source.is_file() and not source.is_symlink():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / name)
    return dest


def _scan(plugin: Path, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, str(plugin)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_hermes_install_scan_lets_the_update_through(tmp_path):
    scan = _scan(_shipped_copy(tmp_path / "conduit_push"), tmp_path)
    if scan is None:
        pytest.skip("this Hermes has no tools.plugin_guard, so it installs without a scan")
    # The update passes no --force, so only an outright allow lets it through.
    assert scan["allowed"] is True, (f"Hermes' install scan: {scan['reason']}; reword or move these:\n"
                                     + "\n".join(scan["held"]))


def test_the_scan_holds_the_readme_that_broke_the_update(tmp_path):
    """The check above can only pass for the right reason if the scan still holds #1500's README."""
    plugin = tmp_path / "planted"
    plugin.mkdir()
    shutil.copy2(ROOT / "plugin.yaml", plugin / "plugin.yaml")
    variable = "$CRASH_REPORTS_" + "TOKEN"
    (plugin / "README.md").write_text(
        "```shell\n" f'curl -H "Authorization: Bearer {variable}" "$PUBLIC_URL/v1/crash-reports"\n' "```\n")
    scan = _scan(plugin, tmp_path)
    if scan is None:
        pytest.skip("this Hermes has no tools.plugin_guard, so it installs without a scan")
    assert scan["allowed"] is not True and scan["held"], scan
