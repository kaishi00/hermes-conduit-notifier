"""Where a turn came from, read off the connection Hermes binds to it
(reply_sources.turn_source), when the chat is open on more than one client.

Hermes binds the session's transport to the turn's thread
(tui_gateway/prompt_turn.py rebinds ``session["transport"]``), not the
socket that sent the prompt. A chat open on two clients holds a
FanoutTransport of both (tui_gateway/transport.py, attached by
tui_gateway/session_transports.py), so the plugin
reads the clients behind it: Eric's Bot Chat on .52 (2026-11-07) got no
board rule and no Conduit surface note while it was open on two screens.

This builds the session the way Hermes does, with its own WSTransport and
attach, and checks the plugin's reading of what Hermes binds.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import ROOT

ELECTRON = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Hermes/1.0 Chrome/130 Electron/33.0 Safari/537.36"

# Run in a child with a scratch HERMES_HOME, like test_imports.py: the
# gateway's server module is imported for its attach.
PROBE = r"""
import asyncio, importlib.util, json, sys, types
from tui_gateway import server
from tui_gateway.transport import FanoutTransport
from tui_gateway.ws import WSTransport

spec = importlib.util.spec_from_file_location("conduit_reply_sources", sys.argv[1])
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)
loop = asyncio.new_event_loop()
socket = lambda headers: WSTransport(types.SimpleNamespace(headers=headers), loop)
phone = socket({"x-conduit-client": "conduit"})
session = {"session_key": "bot-chat", "transport": phone}
alone = sources.turn_source("tui", session["transport"])
attached = server._attach_session_transport(session, socket(json.loads(sys.argv[2])))
bound = session["transport"]
open(sys.argv[3], "w").write(json.dumps({"alone": alone, "attached": attached, "fanout": isinstance(bound, FanoutTransport),
                  "bound": type(bound).__name__, "source": sources.turn_source("tui", bound)}))
# The gateway's server takes stdout for its protocol: the answer goes to a file.
"""


@pytest.mark.parametrize("second, expected", [
    ({"x-conduit-client": "conduit"}, "conduit"),
    ({"user-agent": ELECTRON}, None),
])
def test_a_chat_open_on_two_clients_reads_as_theirs(tmp_path, second, expected):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    answer = tmp_path / "answer.json"
    out = subprocess.run([sys.executable, "-c", PROBE, str(ROOT / "reply_sources.py"), json.dumps(second), str(answer)],
                         env=env, capture_output=True, text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    found = json.loads(answer.read_text())
    assert found["alone"] == "conduit"
    assert found["attached"] and found["fanout"], f"Hermes bound {found['bound']}, not a fanout"
    assert found["source"] == expected
