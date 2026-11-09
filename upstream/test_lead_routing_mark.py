"""A lead's routing turn sends no push (hermes-conduit#2064): the plugin
knows the turn by Hermes' routing prompt (gateway/hosted_room_lead.py
routing_prompt), its header and its instruction, so a reworded prompt would
bring back the "response ready" push of the lead's raw JSON pick. This plans
a lead-routed room's turn through Hermes' own Discussion policy and holds
room_rules.is_lead_routing_prompt to the prompt it writes, and an ordinary
member's turn, or the lead's own when a message names it, quoting the
instruction, to not being one.

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
for name in ("room_choices", "room_rules"):
    spec = importlib.util.spec_from_file_location(f"conduit_push.{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
room_rules = sys.modules["conduit_push.room_rules"]

from gateway import hosted_room_context as context
from gateway import hosted_room_discussion as discussion
from gateway import hosted_rooms

members = [{"member_id": "member-ada", "profile": "default", "handle": "ada"},
           {"member_id": "member-linus", "profile": "linus", "handle": "linus"}]
db = hosted_rooms.default_db_path()
gateway = hosted_rooms.local_authority_gateway_id()


def first_turn(room_id, text, **settings):
    hosted_rooms.create_room(db, room_id=room_id, name="Zoo trip", members=members, authority_gateway_id=gateway, now=1)
    if settings:
        context.set_room_context(db, room_id=room_id, event_id="ctx", purpose="", notes="", **settings)
    seq = hosted_rooms.append_event(
        db, room_id=room_id, event_id="u1", kind="message.user", actor={"kind": "user", "id": "desktop"},
        payload={"text": text, "thread_id": "t1"}, authority_gateway_id=gateway, authority_epoch=1)["seq"]
    room = hosted_rooms.room_state(db, room_id=room_id)
    room["members"] = members
    events = hosted_rooms.read_events(db, room_id=room_id, limit=hosted_rooms.MAX_LOG_LIMIT)["events"]
    decision = discussion.plan_next_task(room, events, local_profiles=("default", "linus"),
                                         room_context=context.room_context(db, room_id=room_id, before_seq=seq))
    return decision.reason, decision.task.payload.get("prompt") if decision.task else None


lead_reason, lead_prompt = first_turn("room-lead", "Plan the day", routing="lead", lead_member_id="member-ada")
quote = "What does this mean?\n" + room_rules.LEAD_ROUTING_MARK + ", so you decide who takes it."
member_reason, member_prompt = first_turn("room-all", quote)
# Named directly, the lead answers as a participant, even quoting the instruction.
named_reason, named_prompt = first_turn("room-named", "@ada " + quote, routing="lead", lead_member_id="member-ada")
print(json.dumps({
    "lead": [lead_reason, lead_prompt, room_rules.is_lead_routing_prompt(lead_prompt)],
    "member": [member_reason, member_prompt, room_rules.is_lead_routing_prompt(member_prompt)],
    "named": [named_reason, named_prompt, room_rules.is_lead_routing_prompt(named_prompt)],
}))
"""


def test_the_plugin_knows_a_leads_routing_turn_by_hermes_own_prompt(tmp_path: Path) -> None:
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT)], env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    reason, prompt, routing = seen["lead"]
    assert reason == "lead_routing" and prompt, seen
    assert routing, prompt
    reason, prompt, routing = seen["member"]
    assert prompt and not routing, prompt
    reason, prompt, routing = seen["named"]
    assert reason != "lead_routing" and prompt and not routing, seen["named"]
