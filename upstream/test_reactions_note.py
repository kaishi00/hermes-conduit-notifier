"""The user's reactions reach the bot's next turn through Hermes' real store (issue 3439).

``reactions.note`` reads ``take_unseen_reactions`` through hermes_store.py's
storage-interface mapping; a renamed field on Hermes' entries would make it
fail every turn behind a debug log. This writes a chat with a reply, reacts to
it as the user would (``message.react`` stores it the same way), and reads the
note twice: once with the reaction, then nothing, since Hermes stamps it seen.

Run in a child with a Hermes home of its own, since Hermes reads its home from
the environment as its modules load.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

CHILD = r"""
import importlib, json, sys, types
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
package = types.ModuleType("conduit_push")
package.__path__ = [str(root)]
sys.modules["conduit_push"] = package
store = importlib.import_module("conduit_push.hermes_store")
if not store.has_storage_interface():
    json.dump({"skip": True}, sys.stdout)
    raise SystemExit(0)
reactions = importlib.import_module("conduit_push.reactions")

import persistence_assembly as assembly
from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import NewSession

sid = "20261127_230000_aaaaaa"
handle = assembly.open_persistence(assembly.profile_scope(home=home))
try:
    handle.sessions.create(NewSession(session_id=sid, source="api_server"))
    handle.messages.append_batch(sid, [NewMessage.from_live({"role": "user", "content": "Show me two designs"}),
                                       NewMessage.from_live({"role": "assistant", "content": "Here is the second design."})])
    row = handle.messages.latest_row_id(sid, role="assistant")
    handle.messages.set_reaction(sid, row, "❤️", author="user")
finally:
    handle.close()

first = reactions.take(sid)
json.dump({"first": first, "formatted": reactions.format_note(first), "again": reactions.note(sid)}, sys.stdout)
"""


def test_a_reaction_reaches_the_next_turn_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(home)], env=env, capture_output=True,
                         text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    if found.get("skip"):
        pytest.skip("a release before Hermes' storage interface keeps SessionDB's own take_unseen_reactions")
    [entry] = found["first"]
    assert (entry["role"], entry["emoji"], entry["text"]) == ("assistant", "❤️", "Here is the second design.")
    assert isinstance(entry["row_id"], int)
    assert found["formatted"].endswith('- ❤️ on your message: "Here is the second design."')
    # Stamped seen as it was read: the next turn hears nothing.
    assert found["again"] is None
