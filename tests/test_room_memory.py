"""Group room turns and Pulse's own turns stay out of the memory provider."""
import importlib.util
import hashlib
import json
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

        def queue_prefetch_all(self, query, *, session_id=""):
            self.saved.append(("queue_prefetch_all", query))

        def on_turn_start(self, turn_number, message, **kwargs):
            if "previous_message" in kwargs:
                self.saved.append(("on_turn_start", message, kwargs["previous_message"]))
            else:
                self.saved.append(("on_turn_start", message))

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


def test_a_helper_a_room_session_delegated_to_is_guarded_too(guard, hermes, tmp_path):
    # Hermes builds a delegated child without a memory provider (skip_memory in
    # tools/delegate_tool), so this guards a provider that Hermes doesn't run
    # there today. The child works for the room's turn, as room_rules counts it:
    # Hermes marks it (_delegate_from) and gives it its own source and title.
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, end_reason TEXT, "
                 "model_config TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", [
        ("sess_room", "bot_room", None, None, None),
        ("sess_helper", "subagent", "sess_room", None, json.dumps({"_delegate_from": "sess_room"})),
        ("sess_helper_2", "api_server", "sess_room", None, json.dumps({"_delegate_from": "sess_room"})),
        ("sess_branch", "cli", "sess_room", None, json.dumps({"_branched_from": "sess_room"})),
        # A branch off a helper keeps the helper's marks, and is still a chat of its own.
        ("sess_helper_branch", "subagent", "sess_helper", None,
         json.dumps({"_delegate_from": "sess_room", "_branched_from": "sess_helper"})),
        ("sess_chat", "cli", None, None, None),
        ("sess_chat_helper", "subagent", "sess_chat", None, json.dumps({"_delegate_from": "sess_chat"})),
    ])
    conn.commit()
    conn.close()

    for helper in ("sess_helper", "sess_helper_2"):
        manager = _bound(hermes, helper, tmp_path)
        assert _turn_end(manager, helper) == ""
        assert manager.saved == [], helper
    # Branches off the room or a helper, and a helper of an ordinary chat, are saved as before.
    for chat in ("sess_branch", "sess_helper_branch", "sess_chat_helper"):
        manager = _bound(hermes, chat, tmp_path)
        manager.sync_all("Eric: notes for me", "Furina: noted", session_id=chat)
        assert manager.saved == [("sync_all", "Eric: notes for me", chat)], chat


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


GUARDED = ("initialize_all", "on_session_switch", "sync_all", "on_session_end", "on_pre_compress", "prefetch_all",
           "queue_prefetch_all", "on_turn_start")


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
                             "on_turn_start, prefetch_all, queue_prefetch_all, sync_all") == 1
    assert vars(MemoryManager).keys().isdisjoint(GUARDED)


def test_no_hermes_memory_at_all_is_not_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.memory_manager", None)

    assert _load().install() is False


# Pulse's own turns (notifier #633) --------------------------------------------

def _pulse_modules():
    """The plugin's Pulse store and delivery, loaded as the plugin loads them."""
    if "conduit_push" not in sys.modules:
        package = types.ModuleType("conduit_push")
        package.__path__ = [str(ROOT)]
        sys.modules["conduit_push"] = package
    return importlib.import_module("conduit_push.pulse_store"), importlib.import_module("conduit_push.pulse_delivery")


class _Sent:
    ok, status, detail = True, "streaming", ""


def _pulse_sent(home, text):
    """What Pulse sent into its chat from this profile: the attempt's whole text."""
    store, delivery = _pulse_modules()
    pulse_store = store.PulseStore(Path(home) / store.DB_NAME)
    attempt = delivery.submit(pulse_store, lambda chat_id, body: _Sent(), kind="report", chat_id="sess_pulse",
                              text=lambda tag: text.format(tag=tag))
    return attempt["text"]


REPORT = "Pulse report {tag}.\n" + "Context Pulse gathered, mostly the same as last time.\n" * 50


def test_the_pulse_store_and_tag_the_guard_reads_are_pulses_own():
    store, delivery = _pulse_modules()
    room_memory = _load()

    assert room_memory.PULSE_DB == store.DB_NAME
    for item_id in ("0123abcd-f12", "89abcdef-m3", "fedc0123-brief", "0123456789abcdef0123456789abcdef"):
        assert room_memory.PULSE_TAG.fullmatch(delivery.new_tag(item_id, 2))


def test_a_pulse_turn_is_not_saved_recalled_or_counted_by_the_provider(guard, hermes, tmp_path):
    ask = _pulse_sent(tmp_path, REPORT)
    manager = _bound(hermes, "sess_pulse", tmp_path)

    assert manager.prefetch_all(ask, session_id="sess_pulse") == ""
    manager.on_turn_start(4, ask)
    manager.sync_all(ask, "Three cards.", session_id="sess_pulse", messages=[])
    manager.queue_prefetch_all(ask, session_id="sess_pulse")
    assert manager.saved == []

    # The user's own turn in the Pulse chat goes on as any chat's.
    assert manager.prefetch_all("why the dentist?", session_id="sess_pulse") == "recalled for why the dentist?"
    manager.on_turn_start(5, "why the dentist?")
    manager.sync_all("why the dentist?", "It's Thursday.", session_id="sess_pulse")
    assert manager.saved == [("on_turn_start", "why the dentist?"), ("sync_all", "why the dentist?", "sess_pulse")]

    # Hermes 0.23 hands the provider the person's last message, stripped; after
    # a Pulse ask that is the ask, which the provider gets as no message (#652).
    manager.saved.clear()
    manager.on_turn_start(6, "and Friday?", previous_message=ask.strip())
    manager.on_turn_start(7, "thanks", previous_message="and Friday?")
    assert manager.saved == [("on_turn_start", "and Friday?", ""), ("on_turn_start", "thanks", "and Friday?")]


def test_a_user_quoting_a_pulse_tag_is_still_saved(guard, hermes, tmp_path):
    ask = _pulse_sent(tmp_path, REPORT)
    tag = guard.PULSE_TAG.search(ask).group(0)
    manager = _bound(hermes, "sess_pulse", tmp_path)

    quoted = f"What did Pulse report {tag} mean?"
    manager.sync_all(quoted, "It was the morning report.", session_id="sess_pulse")
    assert manager.saved == [("sync_all", quoted, "sess_pulse")]


def test_pulse_turns_are_left_out_of_what_the_provider_reads_at_the_end_and_before_compressing(
        hermes, tmp_path, monkeypatch):
    seen = []

    class MemoryManager:
        def initialize_all(self, session_id, **kwargs):
            pass

        def sync_all(self, user_content, assistant_content, *, session_id="", messages=None):
            seen.append(("sync_all", messages))

        def on_session_end(self, messages):
            seen.append(("on_session_end", messages))

        def on_pre_compress(self, messages, *, evidence_messages=None, require_checkpoint=False):
            seen.append(("on_pre_compress", messages, evidence_messages))
            return "summary"

    _hermes_with(monkeypatch, MemoryManager)
    room_memory = _load()
    assert room_memory.install()
    try:
        probe = _pulse_sent(tmp_path, "Pulse delivery check {tag}. Nothing to do: reply with one short line.")
        report = _pulse_sent(tmp_path, REPORT)
        user = [{"role": "user", "content": "why the dentist?"}, {"role": "assistant", "content": "It's Thursday."}]
        pulse_turn = [{"role": "user", "content": report, "display_kind": "hidden"},
                      {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                      {"role": "tool", "content": "calendar"},
                      {"role": "assistant", "content": "Three cards."}]
        probe_turn = [{"role": "user", "content": probe}, {"role": "assistant", "content": "Arrived."}]
        chat = [{"role": "system", "content": "You are Hermes."}, *probe_turn, *user, *pulse_turn]

        manager = MemoryManager()
        manager.initialize_all(session_id="sess_pulse", hermes_home=str(tmp_path))
        manager.on_session_end(chat)
        assert manager.on_pre_compress(chat, evidence_messages=pulse_turn + user, require_checkpoint=True) == "summary"
        manager.sync_all("why the dentist?", "It's Thursday.", session_id="sess_pulse", messages=chat)
        kept = [chat[0], *user]
        assert seen == [("on_session_end", kept), ("on_pre_compress", kept, user), ("sync_all", kept)]

        # Nothing of the user's: nothing for the provider to read.
        seen.clear()
        manager.on_session_end([chat[0], *pulse_turn, *probe_turn])
        assert manager.on_pre_compress([chat[0], *pulse_turn], require_checkpoint=True) == ""
        assert seen == []
    finally:
        room_memory.uninstall()


def test_an_unreadable_pulse_store_leaves_turns_to_hermes(guard, hermes, tmp_path):
    (tmp_path / guard.PULSE_DB).write_text("not a database")
    manager = _bound(hermes, "sess_pulse", tmp_path)

    ask = "Pulse report p0123abcd-1. Context."
    manager.sync_all(ask, "Cards.", session_id="sess_pulse")
    assert manager.saved == [("sync_all", ask, "sess_pulse")]


def test_a_finished_report_cut_to_its_first_line_is_still_told_from_a_user_quoting_it(guard, hermes, tmp_path):
    store, _ = _pulse_modules()
    ask = _pulse_sent(tmp_path, REPORT)
    pulse_store = store.PulseStore(tmp_path / store.DB_NAME)
    attempt = pulse_store.attempt_by_tag(guard.PULSE_TAG.search(ask).group(0))
    pulse_store.update_attempt(attempt["attempt_id"], state=store.DELIVERED)
    pulse_store.trim_finished("report")
    heading = ask.splitlines()[0]
    assert pulse_store.attempt_by_tag(attempt["tag"])["text"] == heading

    quoted = f"{heading}\nWhat did the second card mean?"
    # Hermes hands a provider the last message stripped (#652): REPORT ends in a newline.
    assert ask != ask.strip()
    assert guard.pulse_asks(tmp_path, [ask, ask.strip(), quoted, heading]) == [True, True, False, False]
    # An attempt stored before 0.153.4 has the digest of its whole text, newline included.
    with sqlite3.connect(tmp_path / store.DB_NAME) as conn:
        conn.execute("UPDATE attempts SET text_sha=?", (hashlib.sha256(ask.encode()).hexdigest(),))
    assert guard.pulse_asks(tmp_path, [ask, ask.strip(), quoted, heading]) == [True, True, False, False]
    chat = [{"role": "user", "content": ask}, {"role": "assistant", "content": "Three cards."},
            {"role": "user", "content": quoted}, {"role": "assistant", "content": "The dentist."}]
    assert guard.without_pulse(tmp_path, chat) == chat[2:]


def test_an_unreadable_pulse_store_is_a_warning_once_not_every_turn(guard, hermes, tmp_path, caplog):
    (tmp_path / guard.PULSE_DB).write_text("not a database")
    manager = _bound(hermes, "sess_pulse", tmp_path)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            manager.sync_all("Pulse report p0123abcd-1. Context.", "Cards.", session_id="sess_pulse")
    assert caplog.text.count("couldn't read Pulse's store") == 1


def test_attempts_from_before_digests_get_one_unless_their_text_may_be_cut(guard, tmp_path):
    store, _ = _pulse_modules()
    report = _pulse_sent(tmp_path, REPORT)
    running = _pulse_sent(tmp_path, REPORT)
    delivered = _pulse_sent(tmp_path, REPORT)  # finished, not cut yet
    probe = _pulse_sent(tmp_path, "Pulse delivery check {tag}. Nothing to do: reply with one short line.")
    path = tmp_path / store.DB_NAME
    pulse_store = store.PulseStore(path)
    tag = guard.PULSE_TAG.search(report).group(0)
    pulse_store.update_attempt(pulse_store.attempt_by_tag(tag)["attempt_id"], state=store.DELIVERED)
    pulse_store.trim_finished("report")
    later = guard.PULSE_TAG.search(delivered).group(0)
    pulse_store.update_attempt(pulse_store.attempt_by_tag(later)["attempt_id"], state=store.DELIVERED)
    with sqlite3.connect(path) as conn:  # as a store from before text_sha has them
        conn.execute("UPDATE attempts SET text_sha=''")

    store.PulseStore(path)

    heading = report.splitlines()[0]
    assert guard.pulse_asks(tmp_path, [f"{heading}\nWhat did it mean?", running, probe, delivered]) == [
        False, True, True, True]
    assert pulse_store.attempt_by_tag(tag)["text_sha"] == store.UNKNOWN_DIGEST


def test_a_user_quoting_a_whole_delivery_check_line_is_still_saved(guard, hermes, tmp_path):
    probe = _pulse_sent(tmp_path, "Pulse delivery check {tag}. Nothing to do: reply with one short line.")
    manager = _bound(hermes, "sess_pulse", tmp_path)

    quoted = f'Why did you get "{probe}"?'
    manager.sync_all(probe, "Arrived.", session_id="sess_pulse")
    manager.sync_all(quoted, "That was Pulse checking in.", session_id="sess_pulse")
    assert manager.saved == [("sync_all", quoted, "sess_pulse")]


def test_a_pulse_store_caught_mid_checkpoint_is_read_again(guard, hermes, tmp_path, monkeypatch):
    ask = _pulse_sent(tmp_path, REPORT)
    real, calls = guard._sent, []

    def flaky(path, wanted):
        calls.append(path)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(path, wanted)

    monkeypatch.setattr(guard, "_sent", flaky)
    monkeypatch.setattr(guard, "READ_RETRY_PAUSE", 0)
    assert guard.pulse_asks(tmp_path, [ask]) == [True]
    assert len(calls) == 2


# --- A host that keeps the plugin to Conduit's rooms (notifier #443) ----------


def _titled_db(home, rows, *, conduit_rooms_only):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, end_reason TEXT, "
                 "title TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    (home / "config.yaml").write_text(
        "plugins:\n  entries:\n    conduit_push:\n      settings:\n"
        f"        conduit_rooms_only: {'true' if conduit_rooms_only else 'false'}\n", encoding="utf-8")
    settings = sys.modules.get("conduit_push_room_settings")
    if settings is not None:
        settings._clear()
    return home


def test_conduit_rooms_only_leaves_other_rooms_turns_to_the_provider(guard, hermes, tmp_path):
    other_remote = "room_" + "fedcba9876543210" * 2
    home = _titled_db(tmp_path, [
        ("sess_desktop", "bot_room", None, "compression", "Group: desktop-room"),
        ("sess_desktop_2", "api_server", "sess_desktop", None, None),
        ("sess_conduit", "bot_room", None, None, "Group: conduit-0f8e"),
        (REMOTE_ROOM, "api_server", None, None, "Group: theirs #2"),
        (other_remote, "api_server", None, None, "Group: conduit-1a2b"),
    ], conduit_rooms_only=True)

    for session_id in ("sess_desktop", "sess_desktop_2", REMOTE_ROOM):
        manager = _bound(hermes, session_id, home)
        manager.sync_all("Mona: Saturday works", "Laifu: Saturday it is", session_id=session_id)
        assert manager.saved == [("sync_all", "Mona: Saturday works", session_id)], session_id
    for session_id in ("sess_conduit", other_remote):
        manager = _bound(hermes, session_id, home)
        assert _turn_end(manager, session_id) == ""
        assert manager.saved == [], session_id


def test_a_change_to_conduit_rooms_only_holds_without_a_rebind(guard, hermes, tmp_path):
    home = _titled_db(tmp_path, [("sess_desktop", "bot_room", None, None, "Group: desktop-room")],
                      conduit_rooms_only=True)
    manager = _bound(hermes, "sess_desktop", home)
    manager.sync_all("Mona: Saturday works", "Laifu: Saturday it is", session_id="sess_desktop")
    assert manager.saved == [("sync_all", "Mona: Saturday works", "sess_desktop")]

    (tmp_path / "config.yaml").write_text(
        "plugins:\n  entries:\n    conduit_push:\n      settings:\n        conduit_rooms_only: false\n",
        encoding="utf-8")
    sys.modules["conduit_push_room_settings"]._clear()
    manager.sync_all("Mona: Sunday then", "Laifu: Sunday", session_id="sess_desktop")
    assert manager.saved == [("sync_all", "Mona: Saturday works", "sess_desktop")]  # guarded from the next turn


def test_a_room_it_cant_name_yet_is_still_guarded(guard, hermes, tmp_path):
    home = _titled_db(tmp_path, [("sess_untitled", "bot_room", None, None, None)], conduit_rooms_only=True)

    # A remote room's session with no row yet, and a local one Hermes hasn't titled.
    for session_id in (REMOTE_ROOM, "sess_untitled"):
        manager = _bound(hermes, session_id, home)
        manager.sync_all("Mona: Saturday works", "Laifu: Saturday it is", session_id=session_id)
        assert manager.saved == [], session_id


def test_every_room_is_guarded_by_default(guard, hermes, tmp_path):
    home = _titled_db(tmp_path, [("sess_desktop", "bot_room", None, None, "Group: desktop-room")],
                      conduit_rooms_only=False)

    manager = _bound(hermes, "sess_desktop", home)
    assert _turn_end(manager, "sess_desktop") == ""
    assert manager.saved == []


def test_a_helpers_title_isnt_its_rooms_and_a_desktop_groups_isnt_conduits(guard, hermes, tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, end_reason TEXT, "
                 "model_config TEXT, title TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", [
        ("sess_room", "bot_room", None, None, None, "Group: conduit-0f8e"),
        ("sess_helper", "subagent", "sess_room", None, json.dumps({"_delegate_from": "sess_room"}),
         "Subagent: find a zoo"),
        ("sess_family", "bot_room", None, None, None, "Group: Family chat"),
    ])
    conn.commit()
    conn.close()
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  entries:\n    conduit_push:\n      settings:\n        conduit_rooms_only: true\n", encoding="utf-8")
    settings = sys.modules.get("conduit_push_room_settings")
    if settings is not None:
        settings._clear()

    helper = _bound(hermes, "sess_helper", tmp_path)
    helper.sync_all("Mona: Saturday works", "Laifu: Saturday it is", session_id="sess_helper")
    assert helper.saved == []
    family = _bound(hermes, "sess_family", tmp_path)
    family.sync_all("Mum: dinner at 7", "Furina: noted", session_id="sess_family")
    assert family.saved == [("sync_all", "Mum: dinner at 7", "sess_family")]
