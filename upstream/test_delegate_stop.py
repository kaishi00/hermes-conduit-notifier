"""Stopping a waiting workstream stops its delegate agents (app issue 2206).

Hermes stops a chat's delegate agents only with its running turn, and a
workstream that waits for its agents has no running turn. So the plugin stops
them itself: work_tool._stop_agents calls
``tools.delegate_tool_registry.interrupt_subagent`` with the
``child_subagent_id`` that subagent_start reported. This checks both halves
against the real Hermes: the hook names the id, and the registry stops the
agent it names, and only that one.

Run in a child process with a scratch HERMES_HOME, so importing Hermes
touches no real profile.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROBE = r"""
import inspect, json
from tools import delegate_tool, delegate_tool_registry as registry

class Agent:
    def __init__(self):
        self.stops = []
    def interrupt(self, message=None):
        self.stops.append(message)

ours, theirs = Agent(), Agent()
registry._register_subagent({"subagent_id": "sa-ours", "agent": ours})
registry._register_subagent({"subagent_id": "sa-theirs", "agent": theirs})
stopped = registry.interrupt_subagent("sa-ours")
unknown = registry.interrupt_subagent("sa-gone")
print(json.dumps({
    "stopped": stopped, "unknown": unknown,
    "ours": len(ours.stops), "theirs": len(theirs.stops),
    "hook_names_id": "child_subagent_id" in inspect.getsource(delegate_tool),
}))
"""


def test_the_registry_stops_the_agent_the_hook_named(tmp_path: Path) -> None:
    env = {"HERMES_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert found["hook_names_id"], "subagent_start names child_subagent_id"
    assert found["stopped"] is True and found["ours"] == 1
    assert found["theirs"] == 0, "only the agent named"
    assert found["unknown"] is False
