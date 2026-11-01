"""Bot workstreams on a real Hermes (work_tool, work_broker;
designs/bot-mode-coordinator, acceptance tests 1, 4, 15 and 21).

Its own dashboard, because the coordinator needs a model: each profile gets a
fake OpenAI-compatible server (Hermes' own tests/fakes/fake_llm_provider.py)
whose model is named after the profile. The launch profile and a secondary
profile each have Bot workstreams on, and in each Bot Chat the coordinator
starts a workstream. The test checks Hermes' state, not the replies: the
registry row, the worker's session in that profile's own state.db, on that
profile's model, hidden, with none of its events on the app's socket, and the
work update that came back into the Bot Chat as a hidden turn. A second chat
on the profile, and a terminal call in the Bot Chat, are refused. The launch
profile's bot asks the secondary one (acceptance test 17): the ask arrives in
its Bot Chat as a hidden turn, it answers with work_reply, and the answer
comes back to the asking Bot Chat. Under ``plugins.isolation: host`` the tool
answers with the reason. After a restart, Hermes' own re-run of a crash-killed
workstream turn is retired (acceptance test 6), and a live sibling's marker kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from conftest import COPY_IGNORE, PLUGIN, ROOT, TOKEN, _free_port, _hermes_bin

PROFILES = ("launch", "second")
DONE_TIMEOUT_S = 180


def _hermes_dir() -> Path:
    return Path(_hermes_bin()).resolve().parents[2]


def _fake_llm() -> Any:
    source = _hermes_dir() / "tests" / "fakes" / "fake_llm_provider.py"
    if not source.is_file():
        pytest.skip(f"this Hermes has no {source.relative_to(_hermes_dir())}")
    spec = importlib.util.spec_from_file_location("hermes_fake_llm_provider", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _work_store() -> Any:
    if "conduit_work_store" in sys.modules:
        return sys.modules["conduit_work_store"]
    spec = importlib.util.spec_from_file_location("conduit_work_store", ROOT / "work_store.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _text_of(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _call(fake: Any, tools: List[str], name: str, arguments: Dict[str, Any]) -> Any:
    # Hermes defers plugin tools behind its Tool Search bridge.
    if name in tools:
        return fake.ToolCall(name, arguments)
    return fake.ToolCall("tool_call", {"name": name, "arguments": arguments})


def _responder(fake: Any, profile: str):
    def respond(record: Dict[str, Any]) -> Any:
        body = record["body"]
        messages = body.get("messages") or []
        tools = [tool.get("function", {}).get("name") for tool in body.get("tools") or []]
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            return fake.Text(f"COORDINATOR-SAW on {profile}: {_text_of(last)[:800]}")
        user = next((_text_of(m) for m in reversed(messages) if m.get("role") == "user"), "")
        if "Work update from" in user and "answered your ask" in user:
            return fake.Text(f"ANSWER-SEEN on {profile}")
        asked = re.search(r'work_reply \{"ref": "(A\d+)"', user)
        if "Work update from" in user and "asks you" in user and asked:
            return _call(fake, tools, "work_reply", {"ref": asked.group(1), "text": f"ANSWER-FROM-{profile}"})
        if "Work update from your workstreams" in user:
            return fake.Text(f"WAKE-SEEN on {profile}")
        if "PLEASE ASK" in user:
            return _call(fake, tools, "work_ask", {"bot": "second", "text": "ASK-TASK: what's two and two?"})
        if "WORKER-TASK" in user:
            return fake.Text(f"WORKER-OK from the {profile} model")
        if "PLEASE START" in user:
            return _call(fake, tools, "work_start", {"title": f"Probe on {profile}",
                                                     "instructions": "WORKER-TASK: answer briefly"})
        if "PLEASE RUN" in user:
            return fake.ToolCall("terminal", {"command": "echo coordinator-ran-this"})
        return fake.Text(f"plain answer on {profile}")
    return respond


def _write_profile(home: Path, base_url: str, profile: str, isolation: str) -> None:
    path = home / "config.yaml"
    config = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    config["model"] = {"provider": "custom", "base_url": base_url, "default": f"fake-{profile}",
                       "context_length": 128000}
    config.setdefault("agent", {})["api_max_retries"] = 1
    try:
        from hermes_cli.observability.shared_metrics_consent import OFFER_VERSION

        config["telemetry"] = {"shared_metrics": {"enabled": False, "send": False, "offer_version": OFFER_VERSION}}
    except ImportError:
        pass
    plugins = config.setdefault("plugins", {})
    plugins["enabled"] = [name for name in (plugins.get("enabled") or []) if name != PLUGIN] + [PLUGIN]
    if isolation:
        plugins["isolation"] = isolation
    path.write_text(yaml.safe_dump(config))
    env = home / ".env"
    lines = [line for line in (env.read_text().splitlines() if env.exists() else [])
             if not line.startswith("OPENAI_API_KEY=")]
    env.write_text("\n".join(lines + ["OPENAI_API_KEY=sk-fake-upstream"]) + "\n")
    (home / "plugins").mkdir(exist_ok=True)
    _work_store().Store(home).set_policy("strict")


class Run:
    def __init__(self, home: Path, servers: Dict[str, Any]) -> None:
        self.home = home
        self.servers = servers
        self.frames: List[Dict[str, Any]] = []
        self.chats: Dict[str, str] = {}
        self.stored: Dict[str, str] = {}
        self.others: Dict[str, str] = {}
        self.warm: List[str] = []

    def profile_home(self, profile: str) -> Path:
        return self.home if profile == "launch" else self.home / "profiles" / profile

    def completions(self, runtime_id: str) -> List[str]:
        return [((f.get("params") or {}).get("payload") or {}).get("text", "") for f in self.frames
                if f.get("method") == "event" and (f.get("params") or {}).get("type") == "message.complete"
                and (f.get("params") or {}).get("session_id") == runtime_id]

    def sessions(self, profile: str) -> List[Dict[str, Any]]:
        db = self.profile_home(profile) / "state.db"
        con = sqlite3.connect(db)
        try:
            columns = [row[1] for row in con.execute("PRAGMA table_info(sessions)")]
            wanted = [c for c in ("id", "title", "hidden", "model") if c in columns]
            return [dict(zip(wanted, row)) for row in con.execute(f"SELECT {', '.join(wanted)} FROM sessions")]
        finally:
            con.close()

    def messages(self, profile: str, session_id: str) -> List[Dict[str, Any]]:
        con = sqlite3.connect(self.profile_home(profile) / "state.db")
        try:
            columns = [row[1] for row in con.execute("PRAGMA table_info(messages)")]
            wanted = [c for c in ("role", "content", "display_kind") if c in columns]
            return [dict(zip(wanted, row)) for row in con.execute(
                f"SELECT {', '.join(wanted)} FROM messages WHERE session_id = ? ORDER BY id", (session_id,))]
        finally:
            con.close()


async def _drive(port: int, run: Run, script: List[tuple]) -> None:
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{port}/api/ws?token={TOKEN}", open_timeout=30, max_size=None) as ws:
        pending: Dict[str, asyncio.Future] = {}
        counter = 0

        async def reader() -> None:
            async for raw in ws:
                frame = json.loads(raw)
                run.frames.append(frame)
                if "id" in frame and "method" not in frame and frame["id"] in pending:
                    pending.pop(frame["id"]).set_result(frame)

        task = asyncio.create_task(reader())

        async def call(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal counter
            counter += 1
            key = f"test-{counter}"
            future = asyncio.get_running_loop().create_future()
            pending[key] = future
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": key, "method": method, "params": params}))
            return await asyncio.wait_for(future, 60)

        for profile in PROFILES[1:]:
            # The first session Hermes builds on a secondary profile after the
            # dashboard starts comes up without this plugin's work tools (seen
            # on the 2026-10-31 canary; the next one has them). A throwaway
            # session takes that first build, so the ones under test are
            # ordinary.
            warm = await call("session.create", {"cols": 96, "source": "desktop", "title": "Warm-up", "profile": profile})
            run.warm.append((warm.get("result") or {}).get("session_id", ""))
            await asyncio.sleep(2)
        for profile in PROFILES:
            for title, into in (("Bot Chat", run.chats), (f"Another chat {profile}", run.others)):
                params: Dict[str, Any] = {"cols": 96, "source": "desktop", "title": title}
                if profile != "launch":
                    params["profile"] = profile
                created = (await call("session.create", params)).get("result") or {}
                into[profile] = created.get("session_id", "")
                if title == "Bot Chat":
                    run.stored[profile] = created.get("stored_session_id", "")
        for profile, which, text, done in script:
            target = (run.chats if which == "chat" else run.others)[profile]
            before = len(run.completions(target))
            await call("prompt.submit", {"session_id": target, "text": text})
            deadline = time.monotonic() + DONE_TIMEOUT_S
            while time.monotonic() < deadline:
                if any(t.startswith(done) for t in run.completions(target)[before:]):
                    break
                await asyncio.sleep(0.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _run_dashboard(tmp: Path, isolation: str, script: List[tuple]) -> Run:
    fake = _fake_llm()
    hermes = _hermes_bin()
    home = tmp / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DASHBOARD_SESSION_TOKEN": TOKEN, "NO_COLOR": "1"}
    for profile in PROFILES[1:]:
        subprocess.run([hermes, "profile", "create", profile], env=env, check=True, capture_output=True, timeout=180)
    shutil.copytree(ROOT, home / "plugins" / PLUGIN, ignore=COPY_IGNORE)
    for profile in PROFILES[1:]:
        # A profile's turns load plugins from its own folder: the link the
        # plugin's own sweep (profile_copies) makes on a host that ran it once.
        (home / "profiles" / profile / "plugins").mkdir(exist_ok=True)
        (home / "profiles" / profile / "plugins" / PLUGIN).symlink_to(home / "plugins" / PLUGIN,
                                                                      target_is_directory=True)
    servers = {}
    for profile in PROFILES:
        server = fake.FakeLLMServer(_responder(fake, profile), default_text=f"default {profile}")
        server.start()
        servers[profile] = server
        _write_profile(home if profile == "launch" else home / "profiles" / profile, server.base_url, profile,
                       isolation)
    run = Run(home, servers)
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
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                                     headers={"X-Hermes-Session-Token": TOKEN})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(0.5)
            asyncio.run(_drive(port, run, script))
            time.sleep(1)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(20)
            for server in servers.values():
                server.stop()
            if os.environ.get("UPSTREAM_CONTRACT_LOG"):
                print(log_path.read_text()[-8000:])
    return run


def _requests(run: Run, profile: str) -> List[Dict[str, Any]]:
    return [record["body"] for record in run.servers[profile].requests]


@pytest.fixture(scope="module")
def coordinated(tmp_path_factory: pytest.TempPathFactory) -> Run:
    script = []
    for profile in PROFILES:
        script += [(profile, "chat", "PLEASE START a workstream", "WAKE-SEEN"),
                   (profile, "chat", "PLEASE RUN a command yourself", "COORDINATOR-SAW"),
                   (profile, "other", "PLEASE START from another chat", "COORDINATOR-SAW")]
    script.append(("launch", "chat", "PLEASE ASK the second bot", "ANSWER-SEEN"))
    return _run_dashboard(tmp_path_factory.mktemp("bot-work"), "", script)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_bot_chat_starts_a_workstream_on_its_own_profile(coordinated: Run, profile: str) -> None:
    rows = [row for row in _work_store().Store(coordinated.profile_home(profile)).list(include_closed=True)
            if row.kind == "workstream"]
    assert [(row.ref, row.kind) for row in rows] == [("W1", "workstream")], rows
    row = rows[0]
    assert row.coordinator == coordinated.stored[profile]
    assert row.status == "done" and row.last_result == f"WORKER-OK from the {profile} model", row
    sessions = {s["id"]: s for s in coordinated.sessions(profile)}
    worker = sessions.get(row.session_id)
    assert worker is not None, f"{row.session_id} isn't in {profile}'s state.db: {sorted(sessions)}"
    assert worker.get("hidden") in (1, True) and worker.get("title") == f"Probe on {profile}"
    if "model" in worker:
        assert worker["model"] == f"fake-{profile}"
    other = "second" if profile == "launch" else "launch"
    assert row.session_id not in {s["id"] for s in coordinated.sessions(other)}


@pytest.mark.parametrize("profile", PROFILES)
def test_the_work_update_comes_back_as_a_hidden_turn(coordinated: Run, profile: str) -> None:
    assert any(t.startswith("WAKE-SEEN") for t in coordinated.completions(coordinated.chats[profile]))
    rows = coordinated.messages(profile, coordinated.stored[profile])
    wakes = [m for m in rows if m.get("role") == "user" and "Work update from your workstreams" in (m.get("content") or "")]
    assert wakes, rows
    assert all(m.get("display_kind") == "hidden" for m in wakes if "display_kind" in m)
    assert f"WORKER-OK from the {profile} model" in wakes[0]["content"]


def test_no_workstream_events_reach_the_apps_socket(coordinated: Run) -> None:
    mine = set(coordinated.chats.values()) | set(coordinated.others.values()) | set(coordinated.warm)
    foreign = {(f.get("params") or {}).get("session_id") for f in coordinated.frames
               if f.get("method") == "event" and (f.get("params") or {}).get("session_id") not in mine
               and (f.get("params") or {}).get("session_id")}
    assert foreign == set()


@pytest.mark.parametrize("profile", PROFILES)
def test_the_coordinator_keeps_to_its_policy(coordinated: Run, profile: str) -> None:
    seen = [t for t in coordinated.completions(coordinated.chats[profile]) if "PLEASE" not in t]
    refused = [t for t in seen if "don't run terminal yourself" in t]
    assert refused, seen
    assert not any("coordinator-ran-this" in t for t in seen)


@pytest.mark.parametrize("profile", PROFILES)
def test_another_chat_on_the_profile_cant_start_work(coordinated: Run, profile: str) -> None:
    answers = coordinated.completions(coordinated.others[profile])
    assert any("only work in this bot's Bot Chat" in t for t in answers), answers
    rows = _work_store().Store(coordinated.profile_home(profile)).list(include_closed=True)
    assert len([row for row in rows if row.kind == "workstream"]) == 1


def test_one_bot_asks_another_and_the_answer_comes_back(coordinated: Run) -> None:
    asked = [row for row in _work_store().Store(coordinated.profile_home("launch")).list(include_closed=True)
             if row.kind == "ask_out"]
    took = [row for row in _work_store().Store(coordinated.profile_home("second")).list(include_closed=True)
            if row.kind == "ask_in"]
    assert len(asked) == 1 and len(took) == 1, (asked, took)
    assert asked[0].ask_id == took[0].ask_id
    assert took[0].status == "done" and asked[0].status == "done"
    assert asked[0].last_result == "ANSWER-FROM-second", asked[0]
    # The ask reached the second bot's Bot Chat as a hidden turn, and the
    # answer came back into the asking one, also hidden.
    theirs = coordinated.messages("second", coordinated.stored["second"])
    asks = [m for m in theirs if m.get("role") == "user" and "asks you" in (m.get("content") or "")]
    assert asks and all(m.get("display_kind") == "hidden" for m in asks if "display_kind" in m), theirs
    assert any(t.startswith("ANSWER-SEEN") for t in coordinated.completions(coordinated.chats["launch"]))
    mine = coordinated.messages("launch", coordinated.stored["launch"])
    answers = [m for m in mine if m.get("role") == "user" and "answered your ask" in (m.get("content") or "")]
    assert answers and "ANSWER-FROM-second" in answers[0]["content"]


@pytest.fixture(scope="module")
def isolated(tmp_path_factory: pytest.TempPathFactory) -> Run:
    script = [(profile, "chat", "PLEASE START a workstream", "COORDINATOR-SAW") for profile in PROFILES]
    return _run_dashboard(tmp_path_factory.mktemp("bot-work-isolated"), "host", script)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_plugin_host_answers_with_the_reason(isolated: Run, profile: str) -> None:
    answers = isolated.completions(isolated.chats[profile])
    assert any("plugins.isolation: host" in t for t in answers), answers
    rows = _work_store().Store(isolated.profile_home(profile)).list(include_closed=True)
    assert rows == []


RETIRE_PROBE = r"""
import importlib.util, json, os, sys
from pathlib import Path

root, home = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("conduit_push", root / "__init__.py",
                                              submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules["conduit_push"] = package
spec.loader.exec_module(package)
from conduit_push import work_tool
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

record_turn_start(home, "crashed", "do the work")
record_turn_start(home, "sibling", "a turn another process is running")
path = home / "desktop" / "interrupted_turns.json"
markers = json.loads(path.read_text())
markers["crashed"]["writer_pid"] = 2 ** 22 + 12345  # a process that's gone
markers["sibling"]["writer_pid"] = os.getppid()  # one that's alive
markers["sibling"].pop("writer_start_time", None)
path.write_text(json.dumps(markers))
for session_key in ("crashed", "sibling"):
    work_tool._retire_auto_continue(home, session_key)
print(json.dumps({key: read_turn_marker(home, key) is not None for key in ("crashed", "sibling")}))
"""


def test_a_restart_retires_hermes_own_rerun_of_a_workstream_turn(tmp_path: Path) -> None:
    """Recovery after a restart (C.7) decides from the journal, so Hermes'
    auto-continue marker for a crash-killed workstream turn goes; a marker
    a live sibling process wrote stays, as Hermes' own resume leaves it."""
    home = tmp_path / "home"
    home.mkdir()
    python = Path(_hermes_bin()).with_name("python")
    done = subprocess.run([str(python), "-c", RETIRE_PROBE, str(ROOT), str(home)], capture_output=True, text=True,
                          timeout=120, env={**os.environ, "HERMES_HOME": str(home)}, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"crashed": False, "sibling": True}
