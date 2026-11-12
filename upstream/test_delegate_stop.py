"""Stopping a waiting workstream stops its delegate agents (app issue 2206).

Hermes stops a chat's delegate agents only with its running turn, and a
workstream that waits for its agents has no running turn. So the plugin stops
them itself: work_tool._stop_agents calls
``tools.delegate_tool_registry.interrupt_subagent`` with the
``child_subagent_id`` that subagent_start reported. This checks both halves
against the real Hermes: the id subagent_start passes is the one the child
is stamped with, delegate_task's registration files the child under it, and
the registry stops the agent it names, and only that one.

Run in a child process with a scratch HERMES_HOME, so importing Hermes
touches no real profile.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROBE = r"""
import ast, inspect, json
from tools import delegate_tool, delegate_tool_registry as registry

# The id subagent_start hands the plugin: in _build_child_agent, the
# expression passed as child_subagent_id must be the one stamped on
# child._subagent_id (compared as code), or child._subagent_id itself.
tree = ast.parse(inspect.getsource(delegate_tool._build_child_agent))
hook_names = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
            and node.args[0].value == "subagent_start":
        for k in node.keywords:
            if k.arg == "child_subagent_id":
                hook_names.add(ast.dump(k.value))
stamped = set()
for node in ast.walk(tree):
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        pairs = (zip(target.elts, node.value.elts)
                 if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple)
                 else [(target, node.value)])
        for t, v in pairs:
            if isinstance(t, ast.Attribute) and t.attr == "_subagent_id":
                # Its value, or the child's own stamp read back.
                stamped |= {ast.dump(v), ast.dump(ast.Attribute(value=t.value, attr=t.attr, ctx=ast.Load()))}

class Agent:
    def __init__(self, sid):
        self._subagent_id, self._parent_subagent_id, self._delegate_depth = sid, None, 1
        self.stops = []
    def interrupt(self, message=None):
        self.stops.append(message)

class Parent:
    session_id = "parent"

# Register the way delegate_task does, by the child's stamped id; then stop by
# that id, as the plugin does with the hook's child_subagent_id.
ours, theirs = Agent("sa-0-ours"), Agent("sa-1-theirs")
for child in (ours, theirs):
    delegate_tool._register_child(child, Parent(), "goal", owner_session_id="s",
                                  owner_transport=object(), owner_session_record=object())
stopped = registry.interrupt_subagent("sa-0-ours")
unknown = registry.interrupt_subagent("sa-gone")
print(json.dumps({
    "stopped": stopped, "unknown": unknown,
    "ours": len(ours.stops), "theirs": len(theirs.stops),
    "hook_id_is_childs_id": bool(hook_names & stamped),
}))
"""

def test_the_registry_stops_the_agent_the_hook_named(tmp_path: Path) -> None:
    env = {"HERMES_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert found["hook_id_is_childs_id"], "subagent_start's child_subagent_id is the id the child registers under"
    assert found["stopped"] is True and found["ours"] == 1
    assert found["theirs"] == 0, "only the agent named"
    assert found["unknown"] is False
