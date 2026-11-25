"""A chat's Kanban tasks (hermes-conduit-desktop#553): the cards Hermes stamped
with any of the chat's sessions, on every board, for the line above the
composer."""

import importlib.util
import os
import pathlib
import sqlite3
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
SCHEMA = (ROOT / "tests" / "hermes_state_schema.sql").read_text(encoding="utf-8")
TASKS = ("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
         "started_at INTEGER, completed_at INTEGER, last_failure_error TEXT, consecutive_failures INTEGER{extra})")

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ["HERMES_HOME"])
    sys.modules["hermes_constants"] = _hermes_constants


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_chat_tasks",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_dashboard", None)
    state = sqlite3.connect(tmp_path / "state.db")
    state.executescript(SCHEMA)
    boards = {}

    def add_board(slug, *, stamped=True, archived=False, name=None):
        path = tmp_path / f"{slug}.db"
        conn = sqlite3.connect(path)
        conn.execute(TASKS.format(extra=", session_id TEXT" if stamped else ""))
        conn.commit()
        boards[slug] = {"slug": slug, "name": name or slug.title(), "archived": archived, "db_path": str(path)}
        return conn

    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db.list_boards = lambda include_archived=True: [
        meta for meta in boards.values() if include_archived or not meta["archived"]]
    kanban_db.kanban_db_path = lambda board=None: pathlib.Path(boards[board]["db_path"])
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.kanban_db = kanban_db
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kanban_db)
    yield state, add_board
    state.close()


def _session(conn, sid, *, parent=None, started=1.0, ended=None, end_reason=None):
    conn.execute("INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason) "
                 "VALUES (?, 'conduit', ?, ?, ?, ?)", (sid, parent, started, ended, end_reason))
    conn.commit()


def _task(conn, tid, session, status="running", *, created=1, error=None, title=None):
    conn.execute("INSERT INTO tasks (id, title, status, assignee, created_at, last_failure_error, session_id) "
                 "VALUES (?, ?, ?, 'coder', ?, ?, ?)", (tid, title or tid, status, created, error, session))
    conn.commit()


def _get(query):
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app).get(f"{BASE}/sessions/kanban-tasks{query}")


def test_a_chat_finds_its_tasks_on_every_board_and_through_its_compressions(host):
    state, add_board = host
    _session(state, "a", started=1, ended=5, end_reason="compression")
    _session(state, "b", parent="a", started=6)
    _session(state, "other", started=1)
    main = add_board("default")
    side = add_board("research")
    _task(main, "t1", "a", "done", created=1)
    _task(side, "t2", "b", "running", created=2)
    _task(main, "t3", "other", "running", created=3)
    _task(main, "t4", "b", "archived", created=4)
    for sid in ("a", "b"):
        body = _get(f"?session_id={sid}").json()
        assert body["supported"] is True
        assert [(t["id"], t["status"], t["board"]) for t in body["tasks"]] == [
            ("t1", "done", "default"), ("t2", "running", "research")]
        assert body["tasks"][1]["board_name"] == "Research"
        assert body["tasks"][1]["assignee"] == "coder"


def test_a_blocked_task_names_why_and_a_finished_one_doesnt(host):
    state, add_board = host
    _session(state, "a")
    board = add_board("default")
    _task(board, "t1", "a", "blocked", error="worker crashed")
    _task(board, "t2", "a", "done", created=2, error="an earlier run's failure")
    tasks = _get("?session_id=a").json()["tasks"]
    assert [t["error"] for t in tasks] == ["worker crashed", None]


def test_archived_boards_are_left_out(host):
    state, add_board = host
    _session(state, "a")
    _task(add_board("old", archived=True), "t1", "a")
    body = _get("?session_id=a").json()
    assert body == {"session_id": "a", "supported": False, "tasks": []}


def test_a_hermes_that_doesnt_stamp_tasks_says_it_cant_tell(host):
    state, add_board = host
    _session(state, "a")
    add_board("default", stamped=False)
    assert _get("?session_id=a").json()["supported"] is False
    # Beside a board that does, the stamped board answers.
    _task(add_board("new"), "t1", "a")
    body = _get("?session_id=a").json()
    assert body["supported"] is True and [t["id"] for t in body["tasks"]] == ["t1"]


def test_a_hermes_without_boards_says_it_cant_tell(host, monkeypatch):
    state, _ = host
    _session(state, "a")
    monkeypatch.delattr(sys.modules["hermes_cli.kanban_db"], "list_boards")
    assert _get("?session_id=a").json() == {"session_id": "a", "supported": False, "tasks": []}


def test_a_hermes_whose_boards_name_no_path_and_no_path_helper_says_it_cant_tell(host, monkeypatch):
    state, add_board = host
    _session(state, "a")
    _task(add_board("default"), "t1", "a")
    kanban_db = sys.modules["hermes_cli.kanban_db"]
    monkeypatch.setattr(kanban_db, "list_boards", lambda include_archived=True: [{"slug": "default", "name": "Default"}])
    monkeypatch.delattr(kanban_db, "kanban_db_path")
    assert _get("?session_id=a").json() == {"session_id": "a", "supported": False, "tasks": []}


def test_an_unknown_chat_or_none_is_refused(host):
    assert _get("?session_id=nope").status_code == 404
    assert _get("").status_code == 400


def test_a_hermes_whose_sessions_predate_the_chain_columns_says_it_cant_tell(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_dashboard", None)
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('a', 'conduit')")
    conn.commit()
    conn.close()
    assert _get("?session_id=a").json() == {"session_id": "a", "supported": False, "tasks": []}


def test_a_card_with_no_lane_yet_still_shows(host):
    state, add_board = host
    _session(state, "a")
    board = add_board("default")
    board.execute("INSERT INTO tasks (id, title, status, session_id) VALUES ('t1', 't1', NULL, 'a')")
    board.commit()
    assert [t["id"] for t in _get("?session_id=a").json()["tasks"]] == ["t1"]
