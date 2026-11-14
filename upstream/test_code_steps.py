"""execute_code's script in a Bot Chat, on a real Hermes (notifier#784).

Hermes calls the plugin's pre_tool_call for each tool execute_code's script
calls with no session id, only the task of the execute_code call it runs in.
The plugin takes that call's chat from its task: on Quick Things Here
(lookups) the script's calls are the Bot Chat's steps, so the turn's budget
holds them, while a call from an agent with no session isn't held at all.

Run in a child process with a scratch HERMES_HOME holding a copy of the
plugin, so loading it touches no real profile.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from conftest import COPY_IGNORE, PLUGIN, ROOT

BOT_CHAT = "bot-chat-784"
# More steps than a turn's budget (work_store.QUICK_STEPS, 5), counting the
# execute_code call itself.
STEPS = 6

PROBE = r"""
import json, sys
from pathlib import Path
from hermes_cli.plugins import discover_plugins
discover_plugins(force=True)
from hermes_state import SessionDB
from model_tools import handle_function_call

home = Path(sys.argv[1])
db = SessionDB(db_path=home / "state.db")
db.create_session(sys.argv[2], "cli")
db.set_session_title(sys.argv[2], "Bot Chat")
db.close()
code = (
    "from hermes_tools import terminal\n"
    f"for n in range({sys.argv[3]}):\n"
    "    print(n, terminal(f'echo code-step-{n}'))\n"
)
script = handle_function_call("execute_code", {"code": code}, task_id="task-784", session_id=sys.argv[2],
                              turn_id="turn-784")
alone = handle_function_call("terminal", {"command": "echo no-session-784"}, task_id="other-task", session_id="")
print(json.dumps({"script": script, "alone": alone}))
"""


def _work_store():
    if "conduit_work_store" in sys.modules:
        return sys.modules["conduit_work_store"]
    spec = importlib.util.spec_from_file_location("conduit_work_store", ROOT / "work_store.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN]}}))
    # Quick Things Here, saved, as Edit Bot > Work saves it.
    _work_store().Store(home).set_policy("lookups")
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, str(home), BOT_CHAT, str(STEPS)], env=env,
                         capture_output=True, text=True, timeout=180, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_execute_codes_script_takes_the_bot_chats_steps(tmp_path: Path) -> None:
    result = _run(tmp_path)
    script = json.loads(result["script"])["output"]
    # execute_code was the turn's first step, so four of its script's ran.
    ran = [n for n in range(STEPS) if f"'output': 'code-step-{n}'" in script]
    assert ran == [0, 1, 2, 3], script
    assert "terminal didn't run" in script, script
    # A call from an agent with no session isn't the coordinator's.
    assert "no-session-784" in result["alone"], result["alone"]
