"""A linked room a separate `hermes dashboard` started is moved into the gateway's store when the
gateway starts (gateway/hosted_rooms_gateway_import.py), and Linked Rooms follows it there
(feature map: "Linked Rooms"): room_start.Starter.moved names the gateway's store and the room's
owner as the move recorded them, and the room's log there goes on under the same authority at a
later epoch, which a guest's copy takes and its agents' fresh grants are minted for.

Run in a child with a Hermes home of its own, since Hermes reads its home from the environment as
its modules load.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import ROOT

CHILD = r"""
import importlib.util, json, sqlite3, sys, types
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
package = types.ModuleType("conduit_push")
package.__path__ = [str(root)]
sys.modules["conduit_push"] = package
for name in ("room_identity", "room_envelope", "room_mailbox", "room_links", "room_bridge", "room_cutoff",
             "room_start"):
    spec = importlib.util.spec_from_file_location(f"conduit_push.{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
room_start = sys.modules["conduit_push.room_start"]

from gateway import hosted_rooms
from gateway.hosted_rooms_gateway_import import import_standalone_rooms

members = [{"member_id": "m-furina", "profile": "default", "handle": "Furina"},
           {"member_id": "m-nahida", "profile": "nahida", "handle": "Nahida"}]
source, target = home / "shared-state.db", home / "state.db"
local = hosted_rooms.local_authority_gateway_id()
hosted_rooms.create_room(source, room_id="room-linked", name="Zoo trip", members=members,
                         authority_gateway_id=local, now=1)
before = hosted_rooms.read_events(source, room_id="room-linked", limit=100)
starter = room_start.Starter(home)
not_yet = starter.moved("room-linked")
with sqlite3.connect(target) as conn:  # the gateway's store, as its runtime makes it
    conn.execute("CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT)")
copied = import_standalone_rooms(target)
moved = starter.moved("room-linked")
after = hosted_rooms.read_events(target, room_id="room-linked", limit=100)
frozen = hosted_rooms.read_events(source, room_id="room-linked", limit=100)
print(json.dumps({"local": local, "not_yet": not_yet, "copied": copied, "moved": moved,
                  "before": before["authority"], "after": after["authority"], "frozen": frozen["authority"],
                  "before_seqs": [e["seq"] for e in before["events"]],
                  "after_seqs": [e["seq"] for e in after["events"]]}, default=str))
"""


def test_a_room_hermes_moves_into_the_gateway_is_followed_there_at_a_later_epoch(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    env = dict(os.environ, HERMES_HOME=str(home))
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(home)], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen["not_yet"] is None and seen["copied"] == 1, seen
    # The draft goes to the root gateway's store, owned as the move recorded it.
    assert seen["moved"]["kind"] == "canonical" and seen["moved"]["home"] == str(home), seen
    with sqlite3.connect(home / "state.db") as conn:
        owner, = conn.execute("SELECT value FROM state_meta WHERE key=?",
                              ("gateway.hosted.owner.v1:room-linked",)).fetchone()
    assert seen["moved"]["subject"] == owner, seen
    # The guest's copy checks the authority's gateway id (room_copies.on_page): it stays this
    # installation's, at a later epoch, and the log there carries on from the one the copy holds.
    assert seen["after"]["gateway_id"] == seen["local"] == seen["before"]["gateway_id"], seen
    assert seen["after"]["epoch"] > seen["before"]["epoch"], seen
    assert seen["after_seqs"][:len(seen["before_seqs"])] == seen["before_seqs"], seen
    # And the copy left behind is frozen under another authority: the one the plugin read before.
    assert seen["frozen"]["gateway_id"] != seen["local"], seen
