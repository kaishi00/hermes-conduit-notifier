"""A chat's claim left by a runtime nobody is attached to is let go
(hermes-conduit issue 3125).

The dashboard's takeover route (dashboard/api/chats.py) never drops a claim
this process holds while a client is on it. One whose runtime lost its
client (the phone's own earlier connection, parked on the gateway's
detached-socket sentinel) and runs nothing is let go through Hermes' own
release, so the device that left it can send again
(chat_takeover.release_detached_claims).

These hold Hermes to what that needs: a claim made the way a turn makes it
(_claim_active_session_slot) on a runtime parked on _detached_ws_transport is
released by the takeover and its registry entry is gone; the same runtime
with a live client keeps it. The gateway's helpers are private names on
tui_gateway.server (tui_gateway/*.py is watched).

Run in a child with a Hermes home of its own, since Hermes reads its home
from the environment as its modules load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parents[1]

CHILD = r"""
import importlib.util, json, sys, threading
from tui_gateway import server
from hermes_cli import active_sessions
from hermes_constants import get_hermes_home

spec = importlib.util.spec_from_file_location("conduit_upstream_chat_takeover", sys.argv[1] + "/chat_takeover.py")
takeover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(takeover)


class Socket:  # a client still attached
    _closed = False

    def write(self, *args, **kwargs):
        return True


def runtime(sid, key, transport):
    lease, refusal = server._claim_active_session_slot(key, live_session_id=sid, surface="desktop")
    assert refusal is None, refusal
    record = server._deferred_session_record(key, cols=80, cwd=str(get_hermes_home()), history=[], lease=lease,
                                             source="desktop")
    record["agent_ready"].set()
    record["transport"] = transport
    with server._sessions_lock:
        server._sessions[sid] = record
    return record


def claims(key):
    return [entry for entry in active_sessions.active_session_registry_snapshot(get_hermes_home())
            if entry.get("session_id") == key]


registry, turn_marker = takeover.modules()
seen = {}
detached = runtime("live-a", "chat-a", server._detached_ws_transport)
seen["detached_before"] = len(claims("chat-a"))
seen["detached"] = takeover.take_over_session(["chat-a"], registry=registry, turn_marker=turn_marker,
                                              home=get_hermes_home())
seen["detached_after"] = len(claims("chat-a"))
seen["detached_released"] = detached.get("active_session_lease") is None
attached = runtime("live-b", "chat-b", Socket())
seen["attached"] = takeover.take_over_session(["chat-b"], registry=registry, turn_marker=turn_marker,
                                              home=get_hermes_home())
seen["attached_after"] = len(claims("chat-b"))
with open(sys.argv[2], "w") as out:  # the gateway keeps stdout for its own protocol
    json.dump(seen, out)
"""


def _takeovers(tmp_path: Path) -> dict[str, Any]:
    home = tmp_path / "hermes"
    home.mkdir()
    seen = tmp_path / "seen.json"
    out = subprocess.run([sys.executable, "-c", CHILD, str(PLUGIN), str(seen)],
                         env=dict(os.environ, HERMES_HOME=str(home)), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(seen.read_text())


def test_a_detached_runtimes_claim_is_let_go_and_an_attached_ones_stays(tmp_path: Path) -> None:
    seen = _takeovers(tmp_path)
    assert seen["detached_before"] == 1
    assert seen["detached"] == {"status": "taken_over", "surface": "desktop"}
    assert seen["detached_after"] == 0 and seen["detached_released"]
    assert seen["attached"] == {"status": "same_host", "surface": "desktop"}
    assert seen["attached_after"] == 1
