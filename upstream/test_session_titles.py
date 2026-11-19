"""The Work tools' Bot Chat check reads the chat's title by its session id
(session_titles.read_or_raise), notifier 881: a Bot Chat whose row is hidden,
with no gateway key equal to its id, must still be found, on the old
SessionDB and on Hermes' storage interface alike, and an unknown id reads as
untitled.

Run in a child with a Hermes home of its own, since Hermes reads its home
from the environment as its modules load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT

CHILD = r"""
import importlib.util, json, sys, types
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
package = types.ModuleType("conduit_push")
package.__path__ = [str(root)]
sys.modules["conduit_push"] = package
for name in ("hermes_store", "session_titles"):
    spec = importlib.util.spec_from_file_location(f"conduit_push.{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
hermes_store = sys.modules["conduit_push.hermes_store"]
session_titles = sys.modules["conduit_push.session_titles"]

store = hermes_store.open_store(home)
try:
    store.ensure_session("20261118_bot_chat_a1b2", source="tui")
    store.set_session_title("20261118_bot_chat_a1b2", "Bot Chat")
    store.set_session_hidden("20261118_bot_chat_a1b2", True)
    store.ensure_session("20261118_other_c3d4", source="tui")
    store.set_session_title("20261118_other_c3d4", "Trip plans")
finally:
    store.close()
print(json.dumps({
    "interface": hermes_store.has_storage_interface(),
    "bot_chat": session_titles.read_or_raise(home, "20261118_bot_chat_a1b2"),
    "other": session_titles.read_or_raise(home, "20261118_other_c3d4"),
    "unknown": session_titles.read_or_raise(home, "20261118_nobody"),
}))
"""


def test_a_hidden_bot_chat_is_found_by_its_session_id(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    env = dict(os.environ, HERMES_HOME=str(home))
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(home)], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen["bot_chat"] == "Bot Chat", seen
    assert seen["other"] == "Trip plans", seen
    assert seen["unknown"] == "", seen
