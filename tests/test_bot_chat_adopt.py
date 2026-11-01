"""bot_chat_adopt: an old Bot Chat moves under its gateway, so other bots can reach it."""

import importlib.util
import json
import pathlib
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("conduit_push_test_bot_chat_adopt", ROOT / "bot_chat_adopt.py")
adopt_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adopt_mod)


@pytest.fixture(autouse=True)
def _clean():
    adopt_mod._backoff.clear()
    yield
    adopt_mod._backoff.clear()


def _store(home, *, source="tui", chat_id=None, keys=(), title="Bot Chat", sid="20260909_203606_5a2e4f"):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, chat_id TEXT, "
                   "session_key TEXT, origin_json TEXT)")
        db.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO sessions (id, title, source, chat_id) VALUES (?, ?, ?, ?)", (sid, title, source, chat_id))
        db.executemany("INSERT INTO state_meta VALUES (?, '{}')", [(key,) for key in keys])
    return sid


def _bind(home, sid):
    with sqlite3.connect(home / "state.db") as db:
        db.execute("INSERT INTO state_meta VALUES (?, '{}')", (adopt_mod.POLICY_PREFIX + sid,))


def test_an_unmoved_bot_chat_is_found(tmp_path):
    sid = _store(tmp_path)
    assert adopt_mod.legacy_bot_chat(tmp_path) == sid


@pytest.mark.parametrize("kwargs", [
    {"keys": ("gateway.local_policy.v1:20260909_203606_5a2e4f",)},
    {"keys": ("gateway.local_legacy.v1:20260909_203606_5a2e4f",)},
    {"chat_id": "local-abc"},
    {"source": "api_server"},
    {"title": "Some chat"},
])
def test_a_moved_or_other_chat_is_left_alone(tmp_path, kwargs):
    _store(tmp_path, **kwargs)
    assert adopt_mod.legacy_bot_chat(tmp_path) is None


def test_no_store_is_nothing_to_move(tmp_path):
    assert adopt_mod.legacy_bot_chat(tmp_path) is None


def _registry(home, entries):
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "active_sessions.json").write_text(json.dumps({"entries": entries}))


def test_a_live_lease_holds_it_and_a_dead_one_doesnt(tmp_path):
    sid = _store(tmp_path)
    _registry(tmp_path, [{"session_id": sid, "pid": 4021861}])
    assert adopt_mod.held_by_live_process(tmp_path, sid, alive=lambda pid: True)
    assert not adopt_mod.held_by_live_process(tmp_path, sid, alive=lambda pid: False)
    assert not adopt_mod.held_by_live_process(tmp_path, "another", alive=lambda pid: True)


def test_the_sweep_moves_each_profiles_bot_chat_and_skips_a_held_one(tmp_path, monkeypatch):
    root = tmp_path
    _store(root, sid="root_chat", keys=(adopt_mod.POLICY_PREFIX + "root_chat",))
    zhongli = _store(root / "profiles" / "zhongli")
    rei = _store(root / "profiles" / "rei", sid="20260815_110900_2abc45")
    _registry(root / "profiles" / "rei", [{"session_id": rei, "pid": 4242}])
    monkeypatch.setattr(adopt_mod, "_pid_alive", lambda pid: True)
    calls = []

    def moved(home, sid):
        calls.append((home.name, sid))
        _bind(home, sid)
        return "adopted"

    tried = adopt_mod.sweep(root, now=1000.0, adopt_one=moved)
    assert calls == [("zhongli", zhongli)]
    assert tried == {str(root / "profiles" / "zhongli"): "adopted"}
    assert adopt_mod.sweep(root, now=2000.0, adopt_one=moved) == {}  # moved: nothing more to do


def test_a_refused_move_waits_longer_each_time(tmp_path):
    _store(tmp_path)
    refused = lambda home, sid: "refused: runtime_coordination_required"  # noqa: E731
    assert adopt_mod.sweep(tmp_path, now=0.0, adopt_one=refused)
    assert adopt_mod.sweep(tmp_path, now=adopt_mod.SWEEP_S - 1, adopt_one=refused) == {}
    assert adopt_mod.sweep(tmp_path, now=adopt_mod.SWEEP_S, adopt_one=refused)
    assert adopt_mod.sweep(tmp_path, now=adopt_mod.SWEEP_S * 2, adopt_one=refused) == {}  # waits 2x now
    assert adopt_mod.sweep(tmp_path, now=adopt_mod.SWEEP_S * 3, adopt_one=refused)


def test_adopt_runs_the_child_with_the_profiles_home_and_checks_the_store(tmp_path):
    sid = _store(tmp_path)
    seen = {}

    def run(argv, env, **kwargs):
        seen.update(argv=argv, home=env["HERMES_HOME"], timeout=kwargs["timeout"])
        _bind(tmp_path, sid)
        return SimpleNamespace(stdout='{"status": "resumed"}\n')

    assert adopt_mod.adopt(tmp_path, sid, run=run) == "adopted"
    assert seen["home"] == str(tmp_path) and seen["argv"][-2:] == [str(tmp_path), sid]


def test_an_answer_lost_after_the_move_still_counts_as_moved(tmp_path):
    sid = _store(tmp_path)

    def run(argv, env, **kwargs):
        _bind(tmp_path, sid)  # the gateway moved it, then the answer outgrew the frame
        return SimpleNamespace(stdout='{"status": "refused", "detail": "ConnectionClosedError"}\n')

    assert adopt_mod.adopt(tmp_path, sid, run=run) == "adopted"


def test_a_refusal_is_reported(tmp_path):
    sid = _store(tmp_path)
    run = lambda argv, env, **kwargs: SimpleNamespace(  # noqa: E731
        stdout='{"status": "refused", "detail": "runtime_coordination_required"}\n')
    assert adopt_mod.adopt(tmp_path, sid, run=run) == "refused: runtime_coordination_required"
    assert adopt_mod.adopt(tmp_path, sid, run=lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("x", 1))).startswith("failed")


def test_the_child_script_compiles():
    compile(adopt_mod.CHILD, "<bot_chat_adopt child>", "exec")
