"""Hermes' memory provider discovery doesn't parse the plugin's code
(memory_scan, hermes-conduit#1931).

Since 2026-10-09 Hermes parses every top-level module of a plugin folder
whose ``__init__.py`` doesn't name the memory provider contract in its first
8 KB, each time it lists the providers: the dashboard does on each plugins hub
build. The plugin's 3.5 MB made a build seconds of CPU, minutes on a phone.
Both tests list the providers in a child process, counting what Hermes
parses. ``test_hermes_lists_memory_providers_without_parsing_the_plugin``
asks Hermes alone, without loading the plugin: a known failure until Hermes
stops parsing a folder whole on every look, when the guard gets another
look. The other loads the plugin as Hermes does and checks none of its
modules is parsed, while another plugin's still is.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import yaml

from conftest import COPY_IGNORE, PLUGIN, ROOT, _hermes_bin

PROBE = r"""
import ast, json, sys
from pathlib import Path
if sys.argv[1] == "load":
    from hermes_cli import plugins as manager
    manager.discover_plugins()
parsed = []
real_parse = ast.parse
def parse(source, *args, **kwargs):
    frame = sys._getframe(1)
    parsed.append(str(frame.f_locals.get("module", "")))
    return real_parse(source, *args, **kwargs)
ast.parse = parse
from plugins.memory import discover_memory_providers
names = [name for name, _description, _available in discover_memory_providers()]
print(json.dumps({"parsed": parsed, "names": names}))
"""


def _list(tmp_path: Path, load: bool) -> Dict[str, Any]:
    home = tmp_path / "home"
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    # A plain plugin beside it, whose __init__ doesn't name the contract, is parsed.
    other = home / "plugins" / "plain_plugin"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("def register(ctx):\n    pass\n")
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN]}}))
    python = Path(_hermes_bin()).resolve().parent / "python"
    env = {**os.environ, "HERMES_HOME": str(home), "NO_COLOR": "1"}
    out = subprocess.run([str(python), "-c", PROBE, "load" if load else "alone"], env=env, capture_output=True,
                         text=True, timeout=180, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-4000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    result["ours"] = [p for p in result["parsed"] if p.startswith(str(home / "plugins" / PLUGIN))]
    result["other"] = [p for p in result["parsed"] if p.startswith(str(other))]
    return result


def test_hermes_lists_memory_providers_without_parsing_the_plugin(tmp_path: Path, known_failure: Any) -> None:
    result = _list(tmp_path, load=False)
    assert PLUGIN not in result["names"]
    known_failure(result["ours"] == [], "plugin parsed", f"Hermes parsed {len(result['ours'])} of its modules")


def test_listing_memory_providers_never_parses_the_plugin(tmp_path: Path) -> None:
    result = _list(tmp_path, load=True)
    assert result["ours"] == [], f"Hermes parsed {len(result['ours'])} of the plugin's modules"
    assert result["other"], result["parsed"]
    assert PLUGIN not in result["names"]
