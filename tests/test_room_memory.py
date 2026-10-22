"""Group room turns stay out of the memory provider (designs/cross-owner-agents)."""
import importlib.util
import logging
import sqlite3
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOM = "room_" + "0123456789abcdef" * 2


def _load(name="conduit_room_memory"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "room_memory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manager_class():
    """Hermes' MemoryManager as far as the guard touches it, with one
    provider that records what reached it."""

    class MemoryManager:
        def __init__(self):
            self.saved = []

        def initialize_all(self, session_id, **kwargs):
            self.session_id = session_id

        def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, **kwargs):
            self.session_id = new_session_id

        def sync_all(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
            self.saved.append(("sync_all", user_content, session_id))

        def on_session_end(self, messages):
            self.saved.append(("on_session_end", len(messages)))

        def on_pre_compress(self, messages, *, evidence_messages=None, require_checkpoint=False,
                            checkpoint_api_version=2):
            self.saved.append(("on_pre_compress", require_checkpoint))
            return "what the provider adds to the summary"

        def prefetch_all(self, query, *, session_id=""):
            return f"recalled for {query}"

    return MemoryManager


@pytest.fixture
def hermes(monkeypatch):
    manager_class = _manager_class()
    memory_manager = types.ModuleType("agent.memory_manager")
    memory_manager.MemoryManager = manager_class
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_manager", memory_manager)
    return manager_class


@pytest.fixture
def guard(hermes):
    room_memory = _load()
    assert room_memory.install()
    yield room_memory
    room_memory.uninstall()


def _state_db(home, rows):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, end_reason TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return home


def _bound(manager_class, session_id, home=None):
    manager = manager_class()
    # Hermes passes everything by keyword (MemoryManager.initialize_for_agent).
    manager.initialize_all(session_id=session_id, hermes_home=str(home) if home else None, platform="api_server")
    return manager


def _turn_end(manager, session_id):
    manager.sync_all("Mona: Saturday works", "Laifu: Saturday it is", session_id=session_id, messages=[])
    manager.on_session_end([{"role": "user", "content": "Mona: Saturday works"}])
    return manager.on_pre_compress([], require_checkpoint=True)


def test_a_room_members_turn_is_not_saved_into_the_memory_provider(guard, hermes):
    manager = _bound(hermes, REMOTE_ROOM)

    assert _turn_end(manager, REMOTE_ROOM) == ""
    assert manager.saved == []
    assert manager.session_id == REMOTE_ROOM  # Hermes still binds the provider


def test_recall_still_runs_in_a_room(guard, hermes):
    manager = _bound(hermes, REMOTE_ROOM)

    assert manager.prefetch_all("the zoo", session_id=REMOTE_ROOM) == "recalled for the zoo"


def test_other_chats_are_saved_as_before(guard, hermes, tmp_path):
    home = _state_db(tmp_path, [("20261022_chat", "telegram", None, None)])
    manager = _bound(hermes, "20261022_chat", home)

    assert _turn_end(manager, "20261022_chat") == "what the provider adds to the summary"
    assert manager.saved == [("sync_all", "Mona: Saturday works", "20261022_chat"), ("on_session_end", 1),
                             ("on_pre_compress", True)]


def test_a_local_members_room_session_is_found_by_its_stored_source(guard, hermes, tmp_path):
    home = _state_db(tmp_path, [("sess_local_furina", "bot_room", None, None)])
    manager = _bound(hermes, "sess_local_furina", home)

    assert _turn_end(manager, "sess_local_furina") == ""
    assert manager.saved == []


def test_a_compacted_room_session_stays_guarded_under_its_new_id(guard, hermes, tmp_path):
    home = _state_db(tmp_path, [
        ("sess_room", "bot_room", None, "compression"),
        ("sess_room_2", "api_server", "sess_room", "compression"),
        ("sess_room_3", "api_server", "sess_room_2", None),
    ])
    rebuilt = _bound(hermes, "sess_room_3", home)
    assert _turn_end(rebuilt, "sess_room_3") == ""
    assert rebuilt.saved == []

    # A manager bound before the compaction keeps its mark when the id moves
    # (Hermes switches the live manager to the child).
    earlier = _bound(hermes, REMOTE_ROOM)
    earlier.on_session_switch("sess_after_compaction", parent_session_id=REMOTE_ROOM, reset=False,
                              reason="compression")
    earlier.sync_all("Mona: and lunch?", "Laifu: noon", session_id="sess_after_compaction")
    assert earlier.saved == []


def test_a_manager_moved_to_another_chat_follows_that_chat(guard, hermes, tmp_path):
    home = _state_db(tmp_path, [("sess_room", "bot_room", None, None), ("sess_mine", "cli", None, None)])
    manager = _bound(hermes, "sess_room", home)

    # /new or /resume out of the room: the chat it lands in is saved again...
    manager.on_session_switch("sess_mine", reset=True)
    manager.sync_all("Eric: notes for me", "Furina: noted", session_id="sess_mine")
    assert manager.saved == [("sync_all", "Eric: notes for me", "sess_mine")]

    # ...and resuming the room stops it again.
    manager.on_session_switch("sess_room")
    manager.sync_all("Mona: Saturday works", "Laifu: Saturday it is", session_id="sess_room")
    assert manager.saved == [("sync_all", "Eric: notes for me", "sess_mine")]


def test_a_branch_off_a_room_is_a_chat_of_its_own(guard, hermes, tmp_path):
    home = _state_db(tmp_path, [
        ("sess_room", "bot_room", None, "branched"),
        ("sess_branch", "cli", "sess_room", None),
    ])
    manager = _bound(hermes, "sess_branch", home)

    manager.sync_all("Eric: notes for me", "Furina: noted", session_id="sess_branch")
    assert manager.saved == [("sync_all", "Eric: notes for me", "sess_branch")]


def test_an_unreadable_session_store_counts_as_not_a_room(guard, hermes, tmp_path):
    (tmp_path / "state.db").write_bytes(b"not a database")

    assert guard.is_room_session("sess_unknown", tmp_path) is False
    assert guard.is_room_session("sess_unknown", tmp_path / "missing") is False
    assert guard.is_room_session("", tmp_path) is False
    manager = _bound(hermes, "sess_unknown", tmp_path)
    manager.sync_all("hi", "hello", session_id="sess_unknown")
    assert manager.saved == [("sync_all", "hi", "sess_unknown")]


def test_a_store_that_stays_unreadable_is_a_warning_once_not_every_turn(guard, tmp_path, caplog):
    (tmp_path / "state.db").write_bytes(b"not a database")

    with caplog.at_level(logging.DEBUG):
        assert guard.is_room_session("sess_first", tmp_path) is False
        assert guard.is_room_session("sess_second", tmp_path) is False
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "sess_first" in warnings[0].getMessage()


def test_a_store_caught_mid_checkpoint_is_read_again(guard, hermes, tmp_path, monkeypatch):
    # A WAL writer's checkpoint can show a read-only reader "disk I/O error"
    # for a few milliseconds; Hermes' own reads retry it (hermes_state).
    _state_db(tmp_path, [("sess_local_room", "bot_room", None, None)])
    failures = [1]

    class MidCheckpoint(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            if failures[0]:
                failures[0] -= 1
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(*args, **kwargs)

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=MidCheckpoint, **kwargs))
    manager = _bound(hermes, "sess_local_room", tmp_path)

    assert _turn_end(manager, "sess_local_room") == ""
    assert manager.saved == []
    assert failures == [0]


def test_a_room_session_is_found_while_another_connection_commits(guard, hermes, tmp_path):
    # Where WAL isn't available Hermes falls back to a rollback journal, and a
    # reader waits out another process's commit; Hermes gives its reads 5 s.
    _state_db(tmp_path, [("sess_local_room", "bot_room", None, None)])
    writer = sqlite3.connect(tmp_path / "state.db", check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    commit = threading.Timer(1.5, writer.commit)
    commit.start()
    try:
        manager = _bound(hermes, "sess_local_room", tmp_path)
    finally:
        commit.join()
        writer.close()

    assert _turn_end(manager, "sess_local_room") == ""
    assert manager.saved == []


GUARDED = ("initialize_all", "on_session_switch", "sync_all", "on_session_end", "on_pre_compress")


def test_installing_twice_wraps_once_and_uninstall_puts_hermes_methods_back(hermes):
    own = {name: hermes.__dict__[name] for name in GUARDED}
    room_memory = _load()

    assert room_memory.install() and room_memory.install()
    assert all(hermes.__dict__[name].__wrapped__ is method for name, method in own.items())
    room_memory.uninstall()
    assert {name: hermes.__dict__[name] for name in own} == own


def test_a_reloaded_plugin_wraps_hermes_own_method_not_the_old_guard(hermes):
    own_sync = hermes.__dict__["sync_all"]
    first, second = _load("conduit_room_memory_first"), _load("conduit_room_memory_second")

    assert first.install() and second.install()
    assert hermes.__dict__["sync_all"].__wrapped__ is own_sync
    # The old load unloading afterwards leaves the new load's guard on.
    newer = hermes.__dict__["sync_all"]
    first.uninstall()
    assert hermes.__dict__["sync_all"] is newer
    second.uninstall()
    assert hermes.__dict__["sync_all"] is own_sync


def test_the_guard_hands_on_whatever_hermes_sends(monkeypatch, tmp_path):
    class MemoryManager:
        """A Hermes whose methods take other shapes than today's."""

        def __init__(self):
            self.saved = []

        def initialize_all(self, **kwargs):
            self.bound = kwargs

        def on_session_switch(self, **kwargs):
            pass

        def sync_all(self, *, user, assistant, session_id="", extra=None):
            self.saved.append((user, extra))

        def on_session_end(self, messages, reason):
            self.saved.append(("end", reason))

        def on_pre_compress(self, messages, budget, **kwargs):
            return f"summary within {budget}"

    memory_manager = types.ModuleType("agent.memory_manager")
    memory_manager.MemoryManager = MemoryManager
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_manager", memory_manager)
    room_memory = _load()
    assert room_memory.install()
    try:
        chat = MemoryManager()
        chat.initialize_all(session_id="sess_chat", hermes_home=str(tmp_path), platform="cli")
        chat.sync_all(user="hi", assistant="hello", session_id="sess_chat", extra=1)
        chat.on_session_end([], "closed")
        assert chat.on_pre_compress([], 500) == "summary within 500"
        assert chat.saved == [("hi", 1), ("end", "closed")]
        assert chat.bound == {"session_id": "sess_chat", "hermes_home": str(tmp_path), "platform": "cli"}

        room = MemoryManager()
        room.initialize_all(session_id=REMOTE_ROOM)
        room.sync_all(user="Mona: hi", assistant="Laifu: hello", session_id=REMOTE_ROOM)
        room.on_session_end([], "closed")
        assert room.on_pre_compress([], 500) == ""
        assert room.saved == []
    finally:
        room_memory.uninstall()


def test_a_room_like_session_id_hermes_names_differently_is_logged_once(guard, caplog):
    with caplog.at_level(logging.WARNING):
        assert guard.is_room_session("room_NOT-A-DIGEST") is False
        assert guard.is_room_session("room_another") is False
    assert caplog.text.count("looks like a group room's") == 1


def _hermes_with(monkeypatch, manager_class):
    memory_manager = types.ModuleType("agent.memory_manager")
    memory_manager.MemoryManager = manager_class
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_manager", memory_manager)


def test_a_hermes_lacking_some_methods_has_the_rest_guarded_and_the_gap_logged(monkeypatch, caplog):
    class MemoryManager:
        def __init__(self):
            self.saved = []

        def initialize_all(self, session_id, **kwargs):
            pass

        def sync_all(self, user_content, assistant_content, **kwargs):
            self.saved.append(user_content)

    _hermes_with(monkeypatch, MemoryManager)
    own_sync = MemoryManager.__dict__["sync_all"]
    room_memory = _load()

    with caplog.at_level(logging.WARNING):
        assert room_memory.install()
    try:
        assert "on_pre_compress, on_session_end, on_session_switch" in caplog.text
        room = MemoryManager()
        room.initialize_all(session_id=REMOTE_ROOM)
        room.sync_all("Mona: hi", "Laifu: hello", session_id=REMOTE_ROOM)
        assert room.saved == []
    finally:
        room_memory.uninstall()
    assert MemoryManager.__dict__["sync_all"] is own_sync


def test_a_hermes_with_none_of_the_methods_is_left_alone(monkeypatch, caplog):
    class MemoryManager:
        pass

    _hermes_with(monkeypatch, MemoryManager)

    with caplog.at_level(logging.WARNING):
        assert _load().install() is False
    assert caplog.text.count("has no initialize_all, on_pre_compress, on_session_end, on_session_switch, "
                             "sync_all") == 1
    assert vars(MemoryManager).keys().isdisjoint(GUARDED)


def test_no_hermes_memory_at_all_is_not_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.memory_manager", None)

    assert _load().install() is False
