"""The Work tools know a hidden Bot Chat by its session id (notifier#881).

A Bot Chat's row is hidden, and its session id isn't its gateway session key.
After Hermes' storage refactor the plugin's title read missed it: the id lookup
was gone from Hermes' stand-in SessionDB, and the key listing leaves hidden
rows out unless asked, so ``work_list`` in a real Bot Chat answered
``not_bot_chat``. This writes such a chat (and an ordinary one) through
whichever door this Hermes has, then asks the Work tools' own guard
(work_tool.is_bot_chat) and the title read approval pushes use, by id and by
key.

Run in a child with a Hermes home of its own, since Hermes reads its home from
the environment as its modules load.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT

CHILD = r"""
import importlib, json, sys, types
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
package = types.ModuleType("conduit_push")
package.__path__ = [str(root)]
sys.modules["conduit_push"] = package
store = importlib.import_module("conduit_push.hermes_store")
titles = importlib.import_module("conduit_push.session_titles")
work_tool = importlib.import_module("conduit_push.work_tool")

chats = (("20261118_090000_aaaaaa", "agent:main:api_server:dm:bot", "Bot Chat", True),
         ("20261118_090500_bbbbbb", "agent:main:api_server:dm:trip", "Trip", False))
if store.has_storage_interface():
    import persistence_assembly as assembly
    from persistence.conversation.sessions import NewSession
    handle = assembly.open_persistence(assembly.profile_scope(home=home))
    try:
        for sid, key, title, hidden in chats:
            handle.sessions.create(NewSession(session_id=sid, source="api_server", session_key=key))
            handle.sessions.set_title(sid, title)
            handle.sessions.set_hidden(sid, hidden)
    finally:
        handle.close()
else:  # a release before Hermes' storage interface
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        for sid, key, title, hidden in chats:
            db.create_session(sid, "api_server", session_key=key)
            db.set_session_title(sid, title)
            db.set_session_hidden(sid, hidden)
    finally:
        db.close()

asked = [sid for sid, _, _, _ in chats] + [key for _, key, _, _ in chats] + ["20261118_100000_cccccc"]
json.dump({"titles": {k: titles.read_or_raise(home, k) for k in asked},
           "bot_chat": {k: work_tool.is_bot_chat(k, home) for k in asked}}, sys.stdout)
"""


def test_a_hidden_bot_chat_is_known_by_its_id_and_its_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(home)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert found["titles"] == {
        "20261118_090000_aaaaaa": "Bot Chat", "agent:main:api_server:dm:bot": "Bot Chat",
        "20261118_090500_bbbbbb": "Trip", "agent:main:api_server:dm:trip": "Trip",
        "20261118_100000_cccccc": "",
    }
    # The Work tools take a session id: only the Bot Chat's passes.
    assert found["bot_chat"]["20261118_090000_aaaaaa"] is True
    assert found["bot_chat"]["20261118_090500_bbbbbb"] is False
    assert found["bot_chat"]["20261118_100000_cccccc"] is False
