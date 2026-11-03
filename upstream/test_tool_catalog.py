"""Every tool the plugin registers reads whole in Tool Search's catalog.

Hermes defers plugin tools behind its Tool Search bridge. The model then sees
one catalog line per tool, its description's first sentence cut at a fixed
length (tools/tool_search_catalog.py), and decides from that line alone
whether the tool can do what it's asked. A line cut short, or a first
sentence that doesn't name what the tool does, and the model says it can't
(hermes-conduit#177: "I can't place a phone call from here", with
conduit_call_user in the catalog). So each tool's first sentence leads with
what it does and fits Hermes' cap, which this checks with Hermes' own code.

Run in a child process with a scratch HERMES_HOME holding a copy of the
plugin, so loading it touches no real profile.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from conftest import COPY_IGNORE, PLUGIN, ROOT

# The toolsets the plugin registers its tools in.
TOOLSETS = ["conduit", "conduit_rooms"]

PROBE = r"""
import json, sys
from hermes_cli.plugins import discover_plugins
discover_plugins(force=True)
from tools.registry import registry
from tools.tool_search_catalog import _short_desc

ours = []
for entry in registry.get_all_entries():
    if entry.toolset not in json.loads(sys.argv[1]):
        continue
    ours.append({"name": entry.name, "line": _short_desc(entry.schema.get("description") or "")})
print(json.dumps(sorted(ours, key=lambda tool: tool["name"])))
"""


def _catalog(tmp_path: Path) -> list:
    home = tmp_path / "home"
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN]}}))
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, json.dumps(TOOLSETS)], env=env, capture_output=True, text=True,
                         timeout=180, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_every_tool_reads_whole_in_the_tool_search_catalog(tmp_path: Path) -> None:
    tools = _catalog(tmp_path)
    names = {tool["name"] for tool in tools}
    # The plugin loaded and registered its tools (a broken load lists none).
    assert {"conduit_call_user", "conduit_call_watch", "conduit_calendar", "conduit_phone"} <= names, names
    cut = [f"{tool['name']}: {tool['line']}" for tool in tools if tool["line"].endswith("…")]
    assert not cut, ("These tools' first sentences are cut in Tool Search's catalog; lead each with what the tool "
                     "does, within Hermes' cap:\n" + "\n".join(cut))
