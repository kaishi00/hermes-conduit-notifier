"""A workstream started from a group room reports back into that room
(room_work, Bot workstreams): the post goes through Hermes' own room
delivery for scheduled jobs (cron.scheduler_delivery_room.deliver_to_room),
lands in the room's log as an automated post, never as the user's, and its
@mention gives the bot, and only that bot, the next turn.

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

root = Path(sys.argv[1])
package = types.ModuleType("conduit_push")
package.__path__ = [str(root)]
sys.modules["conduit_push"] = package
for name in ("room_choices", "room_rules", "room_work"):
    spec = importlib.util.spec_from_file_location(f"conduit_push.{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
room_work = sys.modules["conduit_push.room_work"]

from gateway import hosted_rooms
from gateway import hosted_room_discussion as discussion

members = [{"member_id": "m-furina", "profile": "default", "handle": "Furina"},
           {"member_id": "m-nahida", "profile": "nahida", "handle": "Nahida"}]
db = hosted_rooms.default_db_path()
hosted_rooms.create_room(db, room_id="room-work", name="Stage", members=members,
                         authority_gateway_id=hosted_rooms.local_authority_gateway_id(), now=1)
reply_to = {"to": "room", "room": "room-work", "handle": "Furina", "session": "s"}
lines = ["- W1 “Wallpaper” finished. In short: WALLPAPER-READY"]
first = room_work.post(reply_to, bot="Furina", key="default-W1-abc", lines=lines, profiles=["default"])
again = room_work.post(reply_to, bot="Furina", key="default-W1-abc", lines=lines, profiles=["default"])
events = hosted_rooms.read_events(db, room_id="room-work", limit=hosted_rooms.MAX_LOG_LIMIT)["events"]
posts = [e for e in events if e["kind"] == "message.user"]
room = hosted_rooms.room_state(db, room_id="room-work")
room["members"] = members
decision = discussion.plan_next_task(room, events, local_profiles=("default", "nahida"))
task = decision.task
print(json.dumps({"first": first, "again": again, "posts": posts, "status": decision.status,
                  "profile": task.member.profile if task else None,
                  "prompt": task.payload.get("prompt") if task else None}, default=str))
"""


def test_a_room_works_report_is_posted_and_wakes_only_its_bot(tmp_path: Path) -> None:
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT)], env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen["first"] is None and seen["again"] is None, seen
    post, = seen["posts"]  # the same key twice is one post
    assert post["actor"]["kind"] == "user" and post["actor"]["id"].startswith("cron:"), post
    assert post["actor"]["display_name"] == "Furina's work"
    assert post["payload"]["text"].startswith("@Furina ") and "WALLPAPER-READY" in post["payload"]["text"]
    assert seen["status"] == "task" and seen["profile"] == "default", seen
    assert "WALLPAPER-READY" in seen["prompt"] and "not the user" in seen["prompt"], seen["prompt"]
