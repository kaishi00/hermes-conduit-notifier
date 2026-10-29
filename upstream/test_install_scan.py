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

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import json, sys
from pathlib import Path
try:
    from tools.plugin_guard import scan_plugin
except ImportError:
    print(json.dumps(None))
    raise SystemExit
result = scan_plugin(Path(sys.argv[1]), source="https://github.com/kaishi00/hermes-conduit-notifier.git")
blocking = [f"{f.severity.upper()} {f.pattern_id} {f.file}:{f.line} {f.match}"
            for f in result.findings if f.severity in ("high", "critical")]
print(json.dumps({"verdict": result.verdict, "blocking": blocking}))
"""


def _shipped_copy(dest: Path) -> Path:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    for name in listed.decode("utf-8").split("\0"):
        source = ROOT / name
        if name and source.is_file() and not source.is_symlink():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / name)
    return dest


def test_hermes_install_scan_lets_the_update_through(tmp_path):
    plugin = _shipped_copy(tmp_path / "conduit_push")
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, str(plugin)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    scan = json.loads(out.stdout.strip().splitlines()[-1])
    if scan is None:
        return  # A Hermes without the plugin scan installs anything.
    assert not scan["blocking"], (f"Hermes' install scan says {scan['verdict']}, which holds the update; "
                                  "reword or move these:\n" + "\n".join(scan["blocking"]))
