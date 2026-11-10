"""Turns in sessions that aren't conversations send no reply push
(notifier#660): a script's ``hermes chat --source tool``, a one-shot
``chat -q``/``-z`` and a Kanban worker.

The plugin asks Hermes which session a turn runs in
(``agent.session_source.session_source_for``) and whether the gateway runs it
as a one-shot (``gateway.session_finite.finite_turn_required``). This loads
the plugin in a child with Hermes' own modules and the variables each kind of
run sets, and checks its answer, so a Hermes that renames them shows up here
rather than as pushes on the reporter's phone.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import ROOT, _hermes_bin

PROBE = r"""
import importlib.util, json, os, sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("conduit_push", root / "__init__.py",
                                              submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules["conduit_push"] = package
spec.loader.exec_module(package)
from gateway.session_finite import finite_turn_scope

answers = {"env": package._automation_turn("cli")}
with finite_turn_scope(True):
    answers["gateway one-shot"] = package._automation_turn("local")
with finite_turn_scope(False):
    answers["gateway chat"] = package._automation_turn("local")
print(json.dumps(answers))
"""

# What each run sets (hermes_cli/main.py's --source, cli_single_query.py, gateway/session_kanban.py), and
# whether its turn is quiet run in process (platform cli) and as a gateway turn that isn't a one-shot
# (platform local), where only an explicit source counts: the one-shot fact is the gateway's to say.
RUNS = {
    "terminal chat": ({}, False, False),
    "tool": ({"HERMES_SESSION_SOURCE": "tool", "HERMES_SESSION_SOURCE_EXPLICIT": "1",
              "HERMES_SINGLE_QUERY_SESSION": "1"}, True, True),
    "one-shot": ({"HERMES_SINGLE_QUERY_SESSION": "1"}, True, False),
    "kanban": ({"HERMES_SESSION_SOURCE": "kanban"}, True, True),
    # A one-shot from inside a TUI chat inherits its label but is still a one-shot.
    "one-shot from the TUI": ({"HERMES_SESSION_SOURCE": "tui", "HERMES_SINGLE_QUERY_SESSION": "1"}, True, False),
    "TUI chat": ({"HERMES_SESSION_SOURCE": "tui"}, False, False),
}


def test_hermes_says_which_turns_are_automation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    python = Path(_hermes_bin()).with_name("python")
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(("HERMES_SESSION_", "HERMES_SINGLE_QUERY"))}
    for name, (env, quiet, quiet_in_gateway) in RUNS.items():
        done = subprocess.run([str(python), "-c", PROBE, str(ROOT)], capture_output=True, text=True, timeout=120,
                              env={**base, **env, "HERMES_HOME": str(home)}, cwd=str(tmp_path))
        assert done.returncode == 0, done.stderr[-2000:]
        answers = json.loads(done.stdout.strip().splitlines()[-1])
        assert answers["env"] is quiet, name
        assert answers["gateway one-shot"] is True, name
        assert answers["gateway chat"] is quiet_in_gateway, name
