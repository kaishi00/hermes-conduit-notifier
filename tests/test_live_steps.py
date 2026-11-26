"""Live steps: what each running chat is doing now, for the bots lists (Eric, 2026-11-26)."""
import contextlib
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants
if "tools" not in sys.modules:
    _tools = types.ModuleType("tools")
    _tools.__path__ = []
    sys.modules["tools"] = _tools
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


steps = _load("conduit_push.live_steps", ROOT / "live_steps.py")
live = _load("conduit_push.live_activity", ROOT / "live_activity.py")
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_live_steps", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    steps.forget()
    # A test that registers the plugin leaves its lineage behind; each case sets its own.
    monkeypatch.setattr(steps, "lineage", None)
    yield
    steps.forget()


def test_a_turn_publishes_its_step_and_its_end_takes_it_away(tmp_path):
    steps.started(tmp_path, "s-1", now=10.0)
    assert steps.read(tmp_path) == {"s-1": {"phase": "thinking", "tool": None, "since": 10.0}}
    steps.step("s-1", "tool", "terminal", now=12.0)
    assert steps.read(tmp_path)["s-1"] == {"phase": "tool", "tool": "terminal", "since": 12.0}
    # The same step again keeps when it began.
    steps.step("s-1", "tool", "terminal", now=20.0)
    assert steps.read(tmp_path)["s-1"]["since"] == 12.0
    steps.step("s-1", "approval", "terminal", now=21.0)
    assert steps.read(tmp_path)["s-1"] == {"phase": "approval", "tool": None, "since": 21.0}
    steps.ended("s-1")
    assert not (tmp_path / steps.FILE_NAME).exists()
    assert steps.read(tmp_path) == {}


def test_a_session_with_no_turn_noted_is_left_out(tmp_path):
    # A fork or a delegated child never started a turn here.
    steps.step("child", "tool", "terminal")
    steps.ended("child")
    assert not (tmp_path / steps.FILE_NAME).exists()


def test_each_home_has_its_own_file(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    steps.started(one, "a", now=1.0)
    steps.started(two, "b", now=2.0)
    assert set(steps.read(one)) == {"a"}
    assert set(steps.read(two)) == {"b"}
    # A chat that moves home leaves the old one.
    steps.started(two, "a", now=3.0)
    assert steps.read(one) == {}
    assert set(steps.read(two)) == {"a", "b"}


def test_a_stale_or_broken_file_reads_as_nothing(tmp_path):
    path = tmp_path / steps.FILE_NAME
    path.write_text(json.dumps({"steps": {"s": {"phase": "tool", "tool": "web", "since": 1}}, "at": 1000.0}))
    assert set(steps.read(tmp_path, now=1000.0 + 60)) == {"s"}
    assert steps.read(tmp_path, now=1000.0 + steps.STALE_S + 1) == {}
    assert steps.read(tmp_path, now=900.0) == {}  # ahead of the clock
    path.write_text("{")
    assert steps.read(tmp_path) == {}
    path.write_text(json.dumps({"steps": {"s": {"phase": "shouting"}, "t": {"phase": "input", "tool": "x"}}, "at": 5.0}))
    assert steps.read(tmp_path, now=6.0) == {"t": {"phase": "input", "tool": None, "since": None}}


def test_no_more_than_the_cap_per_home(tmp_path):
    for n in range(steps.MAX_SESSIONS + 3):
        steps.started(tmp_path, f"s-{n}", now=float(n))
    read = steps.read(tmp_path)
    assert len(read) == steps.MAX_SESSIONS
    assert "s-0" not in read and f"s-{steps.MAX_SESSIONS + 2}" in read


def test_the_hooks_note_each_step(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "enqueue", lambda event: True)
    monkeypatch.setattr(plugin.calls, "outcome_note", lambda session_id: None)
    monkeypatch.setattr(plugin.calls, "turn_ended", lambda *args, **kwargs: True)
    plugin._pre_llm_call(session_id="s-1", turn_id="t-1", conversation_history=[])
    assert steps.read(tmp_path)["s-1"]["phase"] == "thinking"
    plugin._pre_tool_call(session_id="s-1", tool_name="terminal")
    assert steps.read(tmp_path)["s-1"]["tool"] == "terminal"
    # The category only, never a tool's own name.
    plugin._pre_tool_call(session_id="s-1", tool_name="my_secret_project_tool")
    assert steps.read(tmp_path)["s-1"]["tool"] == "tool"
    plugin._pre_api_request(session_id="s-1")
    assert steps.read(tmp_path)["s-1"]["phase"] == "thinking"
    plugin._pre_tool_call(session_id="s-1", tool_name="clarify")
    assert steps.read(tmp_path)["s-1"] == {**steps.read(tmp_path)["s-1"], "phase": "input", "tool": None}
    plugin._on_session_end(session_id="s-1", interrupted=True)
    assert steps.read(tmp_path) == {}


@pytest.mark.parametrize("approval_session_id", ["s-1", None])
def test_an_approval_waits_then_thinks_again(monkeypatch, tmp_path, approval_session_id):
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin, "enqueue", lambda event: True)
    monkeypatch.setattr(plugin.calls, "outcome_note", lambda session_id: None)
    monkeypatch.setattr(plugin.calls, "alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin.calls, "cancel_alerts", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "_titled", lambda prefix, session_id: prefix)
    plugin._pre_llm_call(session_id="s-1", turn_id="t-1", conversation_history=[])
    # Hermes names the chat by its session key when it gives no session id.
    payload = {"session_key": "s-1", "description": "Run command?"}
    if approval_session_id is not None:
        payload["session_id"] = approval_session_id
    plugin._pre_approval_request(**payload)
    assert steps.read(tmp_path)["s-1"]["phase"] == "approval"
    plugin._post_approval_response(**payload)
    assert steps.read(tmp_path)["s-1"]["phase"] == "thinking"


def test_a_broken_store_never_breaks_a_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path / "missing")
    monkeypatch.setattr(plugin, "enqueue", lambda event: True)
    monkeypatch.setattr(plugin.calls, "outcome_note", lambda session_id: None)
    plugin._pre_llm_call(session_id="s-2", turn_id="t-2", conversation_history=[])
    plugin._pre_tool_call(session_id="s-2", tool_name="terminal")


def test_the_route_reads_the_profile_s_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    api.live_steps._live_steps_limiter._mints.clear()
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    assert http.get(f"{BASE}/live-steps").json() == {"ok": True, "steps": {}}
    steps.started(tmp_path, "s-1", now=10.0)
    steps.step("s-1", "tool", "web", now=11.0)
    response = http.get(f"{BASE}/live-steps")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"ok": True, "steps": {"s-1": {"phase": "tool", "tool": "web", "since": 11.0}}}


def test_a_compaction_mid_turn_moves_the_step_to_the_new_session_id(tmp_path, monkeypatch):
    asked = []

    def lineage(session_id, running):
        asked.append((session_id, set(running)))
        return "s-1" if session_id == "s-2" else False

    monkeypatch.setattr(steps, "lineage", lineage)
    steps.started(tmp_path, "s-1", now=10.0)
    steps.step("s-2", "tool", "terminal", now=12.0)
    # The live registry lists the compacted session: the step is under its id now.
    assert steps.read(tmp_path) == {"s-2": {"phase": "tool", "tool": "terminal", "since": 12.0}}
    assert asked == [("s-2", {"s-1"})]
    steps.step("s-2", "thinking", now=13.0)
    assert len(asked) == 1  # found directly from then on
    steps.ended("s-2")
    assert not (tmp_path / steps.FILE_NAME).exists()


def test_the_turn_ends_under_its_compacted_id_with_no_step_taken_since(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "lineage", lambda session_id, running: "s-1" if session_id == "s-2" else False)
    steps.started(tmp_path, "s-1", now=10.0)
    steps.ended("s-2")
    assert not (tmp_path / steps.FILE_NAME).exists()


def test_an_id_that_continues_no_turn_is_asked_once(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(steps, "lineage", lambda session_id, running: asked.append(session_id) or False)
    steps.started(tmp_path, "s-1", now=10.0)
    steps.step("other", "tool", "terminal")
    steps.step("other", "thinking")
    assert asked == ["other"]
    assert steps.read(tmp_path)["s-1"]["phase"] == "thinking"


def test_an_id_the_store_cant_place_yet_is_asked_again_later(tmp_path, monkeypatch):
    asked = []
    clock = [100.0]
    monkeypatch.setattr(steps.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(steps, "lineage", lambda session_id, running: asked.append(session_id) or None)
    steps.started(tmp_path, "s-1", now=10.0)
    steps.step("s-2", "tool", "terminal")
    steps.step("s-2", "tool", "terminal")
    assert asked == ["s-2"]
    clock[0] += steps.UNSETTLED_RETRY_S + 1
    steps.step("s-2", "tool", "terminal")
    assert asked == ["s-2", "s-2"]


def test_a_failing_lineage_lookup_leaves_the_step_alone(tmp_path, monkeypatch):
    def broken(session_id, running):
        raise RuntimeError("store locked")

    monkeypatch.setattr(steps, "lineage", broken)
    steps.started(tmp_path, "s-1", now=10.0)
    steps.step("s-2", "tool", "terminal")
    assert steps.read(tmp_path) == {"s-1": {"phase": "thinking", "tool": None, "since": 10.0}}


def test_the_plugin_follows_compactions_with_live_activitys_lineage():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "live_steps.lineage = live_activity.compacted_from" in source
