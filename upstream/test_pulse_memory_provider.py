"""Pulse's own turns never reach the memory provider (room_memory, notifier #633).

Pulse sends hidden asks into its chat a few times a day, each with tens of
thousands of characters of context, and Hermes handed every one to the
memory provider as the user's: a retain per turn, a recall keyed on the
whole ask, and the chat's transcript at its end and before compressing.
A provider that reprocesses a session on each append (Hindsight) spent
about $2 a day on it.

The probe loads the plugin as Hermes does, gives Hermes' own MemoryManager
a provider that records what reaches it, and runs a Pulse turn and a turn
of the user's through Hermes' own turn code (the prefetch before the model
and the sync after it), then the session's end and its pre-compress
summary. Only the user's turn may arrive.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import yaml

from conftest import COPY_IGNORE, PLUGIN, ROOT, _hermes_bin

PROBE = r"""
import json, sys, types
from hermes_cli import plugins as manager
manager.discover_plugins()
delivery = next(module for name, module in sys.modules.items() if name.endswith("conduit_push.pulse_delivery"))
pulse_store = next(module for name, module in sys.modules.items() if name.endswith("conduit_push.pulse_store"))
from hermes_constants import get_hermes_home
from pathlib import Path
store = pulse_store.PulseStore(Path(get_hermes_home()) / pulse_store.DB_NAME)
class Sent:
    ok, status, detail = True, "streaming", ""
attempt = delivery.submit(store, lambda chat_id, body: Sent(), kind="report", chat_id="sess_pulse",
                          text=lambda tag: f"Pulse report {tag}.\n" + "Context Pulse gathered.\n" * 400)
ask = attempt["text"]

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
seen = []
class Recording(MemoryProvider):
    name = "recording"
    def is_available(self): return True
    def initialize(self, session_id, **kwargs): pass
    def get_tool_schemas(self): return []
    def prefetch(self, query, *, session_id=""):
        seen.append(["prefetch", query[:20]]); return ""
    def queue_prefetch(self, query, *, session_id=""): seen.append(["queue_prefetch", query[:20]])
    def sync_turn(self, user_content, assistant_content, *, session_id="", **kwargs):
        seen.append(["sync_turn", user_content[:20], [m.get("content", "")[:20] for m in kwargs.get("messages") or []]])
    def on_turn_start(self, turn_number, message, **kwargs): seen.append(["on_turn_start", message[:20]])
    def on_session_end(self, messages): seen.append(["on_session_end", [m["content"][:20] for m in messages]])
    def on_pre_compress(self, messages):
        seen.append(["on_pre_compress", [m["content"][:20] for m in messages]]); return ""

memory = MemoryManager()
memory.add_provider(Recording())
memory.initialize_all(session_id="sess_pulse", hermes_home=str(get_hermes_home()), platform="cli")

from agent import turn_context
from run_agent import AIAgent
agent = types.SimpleNamespace(_memory_manager=memory, session_id="sess_pulse", _user_turn_count=1, _turn_author=None,
                              _emit_status=lambda *a, **k: None)
user = "why is the dentist on Thursday?"
chat = [{"role": "user", "content": ask}, {"role": "assistant", "content": "Three cards."},
        {"role": "user", "content": user}, {"role": "assistant", "content": "It moved."}]
for text, reply, messages in ((ask, "Three cards.", chat[:2]), (user, "It moved.", chat)):
    turn_context._memory_turn_start_and_prefetch(agent, text)
    AIAgent._sync_external_memory_for_turn(agent, original_user_message=text, final_response=reply,
                                           interrupted=False, messages=messages)
    memory.flush_pending(timeout=30)
memory.on_pre_compress(chat)
memory.on_session_end(chat)
print(json.dumps(seen))
"""


def _run(tmp_path: Path) -> Any:
    home = tmp_path / "home"
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN]}}))
    python = Path(_hermes_bin()).resolve().parent / "python"
    env: Dict[str, str] = {**os.environ, "HERMES_HOME": str(home), "NO_COLOR": "1"}
    out = subprocess.run([str(python), "-c", PROBE], env=env, capture_output=True, text=True, timeout=180,
                         cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_pulse_turn_never_reaches_the_memory_provider(tmp_path: Path) -> None:
    user = "why is the dentist o"
    assert _run(tmp_path) == [
        ["on_turn_start", user],
        ["prefetch", user],
        ["sync_turn", user, [user, "It moved."]],
        ["queue_prefetch", user],
        ["on_pre_compress", [user, "It moved."]],
        ["on_session_end", [user, "It moved."]],
    ]
