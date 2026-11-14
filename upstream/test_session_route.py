"""A model picked in a TUI chat is the route Hermes rebuilds it on
(dashboard/api/session_route.py).

Hermes' TUI persists a chat's live route at the top of its ``model_config``
(tui_gateway/agent_factory.py ``_runtime_model_config``) and leaves a nested
``gateway_runtime`` an earlier gateway turn wrote, which the reader every
rebuild and resume uses takes first (``SessionDB.session_gateway_runtime``).
A pick saved that way rebuilds on the old provider: Furina's Bot Chat paired
every pick with zai, zai refused it, and the fallback put the chat back on
glm-5.3-flash (2026-11-14).

``test_hermes_rebuilds_a_pick_on_its_own_provider`` asks Hermes' own persist,
without the plugin: a known failure until Hermes keeps both copies together,
when the wrapper gets another look. The other test puts the plugin's wrapper
on the real server and reads the route back the way a resume does. Each runs
in a child with a scratch HERMES_HOME, like test_turn_source.py: the gateway's
server module is imported there.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from conftest import ROOT

PROBE = r"""
import importlib.util, json, sys, types
from tui_gateway import server
from hermes_state import SessionDB

if sys.argv[1] == "plugin":
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_route", sys.argv[3])
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    installed = api.session_route.ensure_installed()
else:
    installed = False
stored = {
    "model": "glm-5.3-flash", "provider": "opencode-go", "base_url": "https://opencode.ai/zen/go/v1",
    "gateway_runtime": {"provider": "zai", "base_url": "https://api.z.ai/api/coding/paas/v4", "fallback_active": False},
}
agent = types.SimpleNamespace(model="deepseek-v4.1-flash", provider="opencode-go", requested_provider="opencode-go",
                              base_url="https://opencode.ai/zen/go/v1", api_mode="chat_completions",
                              reasoning_config=None, service_tier=None)
# The persist calls it through the server's globals, where the wrapper sits.
persist_sees = server._persist_live_session_runtime.__globals__.get("_runtime_model_config")
config = persist_sees(agent, stored)
route = SessionDB.session_gateway_runtime({"model_config": json.dumps(config)})
open(sys.argv[2], "w").write(json.dumps({"installed": installed, "provider": route.get("provider"),
                                         "base_url": route.get("base_url")}))
"""


def _probe(tmp_path: Any, mode: str) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    answer = tmp_path / "answer.json"
    out = subprocess.run([sys.executable, "-c", PROBE, mode, str(answer), str(ROOT / "dashboard" / "plugin_api.py")],
                         env=env, capture_output=True, text=True, timeout=300, cwd=home)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(answer.read_text())


def test_hermes_rebuilds_a_pick_on_its_own_provider(tmp_path: Any, known_failure: Any) -> None:
    found = _probe(tmp_path, "hermes")
    known_failure(found["provider"] == "opencode-go", "stale gateway_runtime wins",
                  f"the pick rebuilds on {found['provider']!r}")


def test_the_plugin_keeps_a_picks_route(tmp_path: Any) -> None:
    found = _probe(tmp_path, "plugin")
    assert found["installed"]
    assert found["provider"] == "opencode-go"
    assert found["base_url"] == "https://opencode.ai/zen/go/v1"
