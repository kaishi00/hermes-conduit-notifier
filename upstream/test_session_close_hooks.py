"""A chat's close reaches the plugin (notifier issue 820).

When Hermes closes a chat (tui_gateway's _finalize_session: Desktop letting
it go, /new, exit), it says so twice: ``on_session_end`` with no turn named,
then ``on_session_finalize``. The plugin ends a turn still running there as
stopped. Hermes' hook gate tells concurrent ``on_session_end`` calls apart by
their turn id, so two chats closing at once share one key and the second is
skipped (NousResearch/hermes-agent#104865); the plugin then takes the close
from ``on_session_finalize``, which Hermes runs on the caller's thread,
outside the gate.

These hold Hermes to both halves: the finalize reaches a callback while
another chat's close is still in ``on_session_end`` (what the workaround
needs), and two concurrent closes both reach ``on_session_end`` (the upstream
bug, a known failure until it is fixed; the workaround can then go).

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

CHILD = r"""
import json, threading
from hermes_cli import lifecycle, plugins

manager = plugins.get_plugin_manager()
manager.discover_and_load()
seen = {"end": [], "finalize": [], "first_inside": False}
inside = threading.Event()
release = threading.Event()
running = threading.Event()  # chat-a's close is in its callback


def on_end(**kwargs):
    seen["end"].append(kwargs.get("session_id"))
    if kwargs.get("session_id") == "chat-a":
        running.set()
        inside.set()
        release.wait(10)
        running.clear()


def on_finalize(**kwargs):
    seen["finalize"].append([kwargs.get("session_id"), threading.current_thread() is threading.main_thread(),
                             running.is_set()])


manager._hooks.setdefault("on_session_end", []).append(on_end)
manager._hooks.setdefault("on_session_finalize", []).append(on_finalize)
first = threading.Thread(target=lambda: lifecycle.invoke_hook(
    "on_session_end", session_id="chat-a", completed=False, interrupted=True, platform="tui"))
first.start()
seen["first_inside"] = inside.wait(10)
# chat-b closes while chat-a's close is still in its on_session_end.
lifecycle.invoke_hook("on_session_end", session_id="chat-b", completed=False, interrupted=True, platform="tui")
lifecycle.finalize_session(session_id="chat-b", platform="tui")
release.set()
first.join(10)
print(json.dumps(seen))
"""


def _closes(tmp_path: Path) -> dict[str, Any]:
    home = tmp_path / "hermes"
    home.mkdir()
    out = subprocess.run([sys.executable, "-c", CHILD], env=dict(os.environ, HERMES_HOME=str(home)),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_chats_finalize_reaches_the_plugin_while_another_close_runs(tmp_path: Path) -> None:
    seen = _closes(tmp_path)
    assert seen["first_inside"]
    # On the caller's thread while chat-a's close is still running: no gate,
    # and no wait for the other close, stands between it and the plugin.
    assert seen["finalize"] == [["chat-b", True, True]]


def test_concurrent_chat_closes_all_reach_on_session_end(tmp_path: Path, known_failure: Any) -> None:
    seen = _closes(tmp_path)
    assert seen["first_inside"]
    # Only the gate's skip is the known failure; anything else fails as itself.
    signature = "second close skipped" if seen["end"] == ["chat-a"] else f"on_session_end reached {seen['end']!r}"
    known_failure(sorted(seen["end"]) == ["chat-a", "chat-b"], signature,
                  f"on_session_end reached {seen['end']!r}")
