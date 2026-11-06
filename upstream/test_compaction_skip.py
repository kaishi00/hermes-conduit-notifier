"""A hidden turn that Hermes' compaction catches still reaches the model
(compaction_skip).

Pulse's reports and briefs, and a bot's wake, are hidden turns. When Hermes
compacts the chat as one starts, its check for a real ask after the handoff
doesn't count a hidden row (agent/conversation_loop.py,
``_should_skip_model_call_for_reference_handoff``): it skips the model and
writes "Context was compacted. ..." as the reply, which Pulse showed as its
report (2026-11-06).

``test_hermes_counts_a_hidden_ask_after_a_handoff`` asks Hermes' own check,
without the plugin: a known failure until Hermes counts it, when the guard
gets another look. The other test runs a chat on its own dashboard with a
fake model: one long turn of the user's, then a hidden one that has to
compact first, and checks the hidden turn's reply is the model's.
Without the guard Hermes writes its status line there instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import pytest

from conftest import COPY_IGNORE, PLUGIN, ROOT, TOKEN, _free_port, _hermes_bin
from test_bot_work import _fake_llm, _text_of, _write_profile

WAIT_S = 180
# Pulse's chat: earlier hidden turns, then one the provider turns down as too
# long for its window until the chat is compacted, as a real one does with a
# chat near its limit. Hermes compacts and tries again, the path where it
# checks for an ask. Only the last four rows stay whole (protect_last_n), so
# the earlier turns compact.
FILLER = "the quick brown fox jumps over the lazy dog. "
TURNS = [f"BIG-TURN {n}: an earlier report. " + FILLER * 110 for n in range(6)]
ASK = "PULSE-ASK: the report, please."
OVERFLOW = ("This model's maximum context length is 100000 tokens. However, your messages resulted in "
            "140000 tokens. (context_length_exceeded)")


def test_hermes_counts_a_hidden_ask_after_a_handoff(known_failure: Any) -> None:
    from agent import conversation_loop
    from agent.context_compressor import SUMMARY_PREFIX

    handoff = {"role": "user", "content": f"{SUMMARY_PREFIX}\nEarlier: the user planned a trip."}
    hidden = {"role": "user", "content": ASK, "display_kind": "hidden"}
    check = conversation_loop._should_skip_model_call_for_reference_handoff
    # Hermes' verdict for the same ask typed by the user: it goes on.
    assert check([handoff, {"role": "user", "content": ASK}], ASK) is False
    skipped = check([handoff, hidden], ASK)
    known_failure(skipped is False, "hidden ask skipped", f"Hermes' check returned {skipped!r}")


def test_the_guard_is_on_hermes_check() -> None:
    import importlib.util
    import sys

    from agent import conversation_loop

    spec = importlib.util.spec_from_file_location("conduit_compaction_skip", ROOT / "compaction_skip.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    own = conversation_loop._should_skip_model_call_for_reference_handoff
    try:
        assert module.install()
        from agent.context_compressor import SUMMARY_PREFIX

        handoff = {"role": "user", "content": f"{SUMMARY_PREFIX}\nEarlier: the user planned a trip."}
        check = conversation_loop._should_skip_model_call_for_reference_handoff
        assert check([handoff, {"role": "user", "content": ASK, "display_kind": "hidden"}], ASK) is False
        # Hermes' verdict stands otherwise: a handoff and nothing to ask
        # after it still never drives a call.
        assert check([handoff, {"role": "assistant", "content": "Done."}], "") is True
        # The status lines Pulse takes for no reply are this Hermes' own.
        assert module.notice(conversation_loop._HANDOFF_SKIP_FINAL_RESPONSE) == module.SKIPPED_DETAIL
        assert module.notice(conversation_loop._COMPRESSION_TIMEOUT_FINAL_RESPONSE) == module.TIMED_OUT_DETAIL
        assert module.notice(module.SKIPPED) == module.SKIPPED_DETAIL
    finally:
        module.uninstall()
    assert conversation_loop._should_skip_model_call_for_reference_handoff is own


def _responder(fake: Any, compacted: Any, turned_down: List[str]):
    def respond(record: Dict[str, Any]) -> Any:
        messages = record["body"].get("messages") or []
        last_user = next((_text_of(m) for m in reversed(messages) if m.get("role") == "user"), "")
        if last_user.startswith("PULSE-ASK"):
            if not any(compacted(_text_of(m)) for m in messages):
                turned_down.append(last_user)
                return fake.Error(400, OVERFLOW)
            return fake.Text("PULSE-REPLY: nothing came up")
        if last_user.startswith("BIG-TURN"):
            return fake.Text("BIG-OK")
        # Hermes' compression call asks for the summary.
        return fake.Text("## Goal\nThe user wrote notes about a fox.\n## Progress\nRead them.")
    return respond


def _rows(home: Path) -> List[Dict[str, Any]]:
    con = sqlite3.connect(home / "state.db")
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(messages)")]
        wanted = [c for c in ("id", "session_id", "role", "content", "display_kind") if c in columns]
        return [dict(zip(wanted, row)) for row in con.execute(f"SELECT {', '.join(wanted)} FROM messages ORDER BY id")]
    finally:
        con.close()


async def _chat(port: int, home: Path) -> None:
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{port}/api/ws?token={TOKEN}", open_timeout=30, max_size=None) as ws:
        counter = 0

        async def call(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal counter
            counter += 1
            key = f"t{counter}"
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": key, "method": method, "params": params}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), WAIT_S))
                if frame.get("id") == key:
                    return frame.get("result") or {}

        async def turn(params: Dict[str, Any]) -> None:
            await call("prompt.submit", params)
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                frame = json.loads(await asyncio.wait_for(ws.recv(), WAIT_S))
                if frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "message.complete":
                    return

        created = await call("session.create", {"cols": 96, "source": "desktop", "title": "Pulse"})
        sid = created.get("session_id", "")
        # Pulse's chat: every turn in it is a hidden one of Pulse's, so no ask
        # the user typed survives a compaction there.
        for text in TURNS:
            await turn({"session_id": sid, "text": text, "display_kind": "hidden"})
        await turn({"session_id": sid, "text": ASK, "display_kind": "hidden"})


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    fake = _fake_llm()
    hermes = _hermes_bin()
    tmp = tmp_path_factory.mktemp("compaction-skip")
    home = tmp / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DASHBOARD_SESSION_TOKEN": TOKEN, "NO_COLOR": "1"}
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    from agent.context_compressor import ContextCompressor

    turned_down: List[str] = []
    server = fake.FakeLLMServer(_responder(fake, ContextCompressor._is_context_summary_content, turned_down),
                                default_text="default")
    server.start()
    _write_profile(home, server.base_url, "launch", "")
    import yaml

    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["compression"] = {**(config.get("compression") or {}), "protect_first_n": 0, "protect_last_n": 4}
    config_path.write_text(yaml.safe_dump(config))
    port = _free_port()
    log_path = tmp / "dashboard.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen([hermes, "dashboard", "--no-open", "--skip-build", "--port", str(port)],
                                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 150
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"the dashboard exited ({proc.returncode}):\n{log_path.read_text()[-4000:]}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"the dashboard did not start:\n{log_path.read_text()[-4000:]}")
                with contextlib.suppress(OSError):
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                                     headers={"X-Hermes-Session-Token": TOKEN})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        if response.status == 200:
                            break
                time.sleep(0.5)
            asyncio.run(_chat(port, home))
            time.sleep(1)
            return {"rows": _rows(home), "turned_down": turned_down, "log": log_path}
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(20)
            with contextlib.suppress(OSError, ValueError, TypeError):
                os.kill(int(json.loads((home / "gateway.lock").read_text())["pid"]), signal.SIGTERM)
            server.stop()
            if os.environ.get("UPSTREAM_CONTRACT_LOG"):
                print(log_path.read_text()[-8000:])


def test_a_hidden_turn_that_compacts_first_gets_the_models_reply(ran: Dict[str, Any]) -> None:
    rows = ran["rows"]
    asks = [row for row in rows if row["role"] == "user" and row["content"] == ASK]
    assert asks and asks[-1].get("display_kind") == "hidden"
    # The provider turned the hidden turn down once, so Hermes compacted the
    # chat and tried again: it's where the test bites.
    assert ran["turned_down"] == [ASK], "the hidden turn never had to compact, so the test proves nothing"
    after = [row for row in rows if row["id"] > asks[-1]["id"] and row["role"] == "assistant"]
    texts = [row["content"] or "" for row in after]
    assert any(text.startswith("PULSE-REPLY") for text in texts), texts
    assert not any(text.startswith("Context was compacted") for text in texts), texts
