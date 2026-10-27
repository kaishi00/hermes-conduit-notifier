"""Every Hermes name the plugin imports still exists (contract.yml ``imports``).

Run in a child process with a scratch HERMES_HOME, so importing Hermes
touches no real profile. An optional import is one the plugin guards with a
try; it still fails here, because a missing one quietly turns a feature off.
A name tried from several modules of one package (where Hermes moved it) needs only one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CONTRACT = Path(__file__).resolve().parent / "contract.yml"

PROBE = r"""
import importlib, json, sys

def has(module, name):
    try:
        mod = importlib.import_module(module)
    except Exception:
        return False
    if name == "*" or hasattr(mod, name):
        return True
    try:
        importlib.import_module(f"{module}.{name}")
        return True
    except Exception:
        return False

missing, alternatives = [], {}
for module, kinds in json.loads(sys.argv[1]).items():
    for name in kinds.get("required", []):
        if not has(module, name):
            missing.append(f"{module}.{name}" if name != "*" else module)
    for name in kinds.get("optional", []):
        # Alternatives share a name and a top-level package (tools.approval and
        # tools.approval_context), so unrelated same-named imports stay apart.
        key = f"{name} {module.split('.')[0]}" if name != "*" else f"* {module}"
        alternatives.setdefault(key, []).append(module)
# An optional name the plugin tries from several modules (an old and a new
# home) is there when any of them has it.
for key, modules in alternatives.items():
    name = key.split(" ")[0]
    if not any(has(m, name) for m in modules):
        missing.append(" or ".join(f"{m}.{name}" if name != "*" else m for m in modules) + " (optional)")
print(json.dumps(sorted(missing)))
"""


def _missing(tmp_path: Path) -> list:
    imports = (yaml.safe_load(CONTRACT.read_text()) or {}).get("imports") or {}
    env = {"HERMES_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, json.dumps(imports)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_hermes_names_the_plugin_imports_exist(tmp_path, known_failure):
    missing = _missing(tmp_path)
    known_failure(not missing, "missing " + ", ".join(missing) if missing else "", "\n".join(missing))
