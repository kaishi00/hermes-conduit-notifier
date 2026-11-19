"""Hermes' stores through whichever door it has (hermes_store.py, notifier#883-#897).

Hermes' canary of 2026-11-18 removed SessionDB and moved its group rooms' calls
from a database path to a store handle and a placement. These fakes stand in
for its storage interface (persistence_assembly and the repositories) and for
the SessionDB of a release before it; upstream/ runs both for real.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fresh(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin(name: str):
    """A module of the plugin's package, as Hermes loads it."""
    if "conduit_push" not in sys.modules:
        package = types.ModuleType("conduit_push")
        package.__path__ = [str(ROOT)]
        sys.modules["conduit_push"] = package
    return importlib.import_module(f"conduit_push.{name}")


@pytest.fixture
def store_module():
    return _fresh("conduit_hermes_store_under_test", "hermes_store.py")


# --- A fake storage interface -----------------------------------------------------------------------


@dataclass
class Record:
    row: dict

    def as_session_row(self):
        return dict(self.row)


@dataclass
class Entry:
    row: dict

    def as_list_row(self):
        return dict(self.row)


@dataclass
class Row:
    row: dict

    def as_wire_row(self):
        return dict(self.row)


@dataclass
class Outcome:
    deleted: int
    skipped_guarded: tuple
    removed_ids: tuple


@dataclass
class Store:
    """One handle on a profile's store: the verbs hermes_store calls, recording what they were given."""

    scope: types.SimpleNamespace
    read_only: bool = False
    sessions_rows: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    closed: bool = False

    def __post_init__(self):
        store = self

        class Sessions:
            def get(self, session_id):
                row = store.sessions_rows.get(session_id)
                return None if row is None else Record(row)

            def get_title(self, session_id):
                return (store.sessions_rows.get(session_id) or {}).get("title")

            def get_title_source(self, session_id):
                return types.SimpleNamespace(value="user") if session_id in store.sessions_rows else None

            def list_sessions(self, query, window):
                store.calls.append(("list_sessions", query, window))
                return tuple(Entry(row) for row in store.sessions_rows.values()
                             if row.get("session_key") == query.session_key
                             and (query.include_hidden or not row.get("hidden")))[:window.limit]

            def create(self, spec):
                store.sessions_rows.setdefault(spec.session_id, {"id": spec.session_id, "source": spec.source})
                return spec.session_id

            def end(self, session_id, reason):
                store.calls.append(("end", session_id, reason))

            def set_title(self, session_id, title):
                if any(row.get("title") == title for key, row in store.sessions_rows.items() if key != session_id):
                    raise TitleConflict(title)
                store.sessions_rows[session_id]["title"] = title
                return True

            def set_hidden(self, session_id, hidden):
                store.calls.append(("set_hidden", session_id, hidden))
                return True

            def set_archived(self, session_id, archived):
                store.calls.append(("set_archived", session_id, archived))
                return types.SimpleNamespace(changed=True, archived_ids=())

            def delete(self, session_id, *, media_root):
                store.calls.append(("delete", session_id, media_root))
                return (session_id,) if store.sessions_rows.pop(session_id, None) else ()

            def delete_many(self, session_ids, *, skip_guarded, include_compression_chain, media_root):
                store.calls.append(("delete_many", tuple(session_ids), skip_guarded, include_compression_chain,
                                    media_root))
                guarded = tuple(sid for sid in session_ids if (store.sessions_rows.get(sid) or {}).get("busy"))
                gone = tuple(sid for sid in session_ids if sid in store.sessions_rows and sid not in guarded)
                for sid in gone:
                    del store.sessions_rows[sid]
                return Outcome(len(gone), guarded, gone)

        class Messages:
            def read_transcript(self, session_id):
                return tuple(Row(row) for row in store.sessions_rows.get(session_id, {}).get("messages", ()))

            def append_batch(self, session_id, messages):
                store.sessions_rows[session_id].setdefault("messages", []).extend(m.live for m in messages)

            def append_completion_note(self, session_id, admission_id, content, display_kind, metadata):
                store.calls.append(("note", session_id, admission_id, content, display_kind, metadata))
                return 7

            def has_platform_message_id(self, session_id, platform_message_id):
                return any(m.get("platform_message_id") == platform_message_id
                           for m in store.sessions_rows.get(session_id, {}).get("messages", ()))

        class Maintenance:
            def auto_archive_lineage(self, session_id):
                store.calls.append(("auto_archive", session_id))
                return True

        class StateMeta:
            def get(self, key):
                return store.meta.get(key)

            def set(self, key, value):
                store.meta[key] = value

        self.sessions, self.messages = Sessions(), Messages()
        self.maintenance, self.state_meta = Maintenance(), StateMeta()

    def close(self):
        self.closed = True


class TitleConflict(ValueError):
    pass


@dataclass(frozen=True)
class SessionQuery:
    session_key: Optional[str] = None
    order: object = None
    include_hidden: bool = False


@dataclass(frozen=True)
class SessionWindow:
    limit: Optional[int]


@dataclass(frozen=True)
class NewSession:
    session_id: str
    source: str


class NewMessage:
    def __init__(self, live):
        self.live = dict(live)

    @classmethod
    def from_live(cls, message):
        return cls(message)


@pytest.fixture
def interface(monkeypatch, tmp_path):
    """Hermes' storage interface, faked: every handle opened, with the scope it was opened for."""
    opened: list = []
    removed: list = []
    stores: dict = {}

    def handle(home, read_only):
        store = Store(scope=types.SimpleNamespace(profile_dir=Path(home)), read_only=read_only)
        shared = stores.setdefault(Path(home), store)
        store.sessions_rows, store.meta = shared.sessions_rows, shared.meta
        opened.append(store)
        return store

    assembly = types.ModuleType("persistence_assembly")
    assembly.profile_scope = lambda home=None: types.SimpleNamespace(profile_dir=Path(home))
    assembly.open_persistence = lambda scope: handle(scope.profile_dir, False)

    class Reader:
        def __init__(self, home):
            self.home = home

        def __enter__(self):
            self.store = handle(self.home, True)
            return self.store

        def __exit__(self, *exc):
            self.store.close()

    assembly.open_persistence_reader = Reader
    sessions = types.ModuleType("persistence.conversation.sessions")
    sessions.SessionQuery, sessions.SessionWindow, sessions.NewSession = SessionQuery, SessionWindow, NewSession
    sessions.SessionOrder = types.SimpleNamespace(LAST_ACTIVE="last_active", STARTED_AT="started_at")
    messages = types.ModuleType("persistence.conversation.messages")
    messages.NewMessage = NewMessage
    retention = types.ModuleType("session_retention")
    retention.after_sessions_removed = lambda store, home, ids, **kw: removed.append((home, ids, kw))
    media = types.ModuleType("gateway.session_ingress_media")
    media.native_inputs_root = lambda: tmp_path / "media"
    rooms = types.ModuleType("gateway.hosted_rooms")
    rooms.default_db_path = lambda home: (Path(home).parent.parent if Path(home).parent.name == "profiles"
                                          else Path(home)) / "shared-state.db"
    ids = types.ModuleType("session_rules.session_ids")
    ids.new_session_id = lambda: "20261118_120000_abcdef"
    for name, module in (("persistence_assembly", assembly), ("persistence.conversation.sessions", sessions),
                         ("persistence.conversation.messages", messages), ("session_retention", retention),
                         ("gateway.session_ingress_media", media), ("gateway.hosted_rooms", rooms),
                         ("session_rules.session_ids", ids)):
        monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(opened=opened, removed=removed, stores=stores)


# --- Sessions -----------------------------------------------------------------------------------------


def test_a_hermes_with_the_storage_interface_is_read_and_written_through_it(store_module, interface, tmp_path):
    assert store_module.has_storage_interface()
    with store_module.open_store(tmp_path) as db:
        db.ensure_session("s1", "cli")
        db.set_session_title("s1", "Bot Chat")
        db.append_messages_batch("s1", [{"role": "user", "content": "hi", "platform_message_id": "r1"}])
        db.set_meta("k", "v")
    assert interface.opened[-1].closed and not interface.opened[-1].read_only
    with store_module.open_store(tmp_path, read_only=True) as db:
        assert db.get_session_title("s1") == "Bot Chat"
        assert db.get_session_title_source("s1") == "user"
        assert db.get_session("s1")["source"] == "cli"
        assert db.get_messages("s1") == [{"role": "user", "content": "hi", "platform_message_id": "r1"}]
        assert db.has_platform_message_id("s1", "r1") and not db.has_platform_message_id("s1", "r2")
        assert db.get_meta("k") == "v"
    assert interface.opened[-1].read_only and interface.opened[-1].closed


def test_a_title_another_chat_holds_refuses_as_a_value_error_as_sessiondb_did(store_module, interface, tmp_path):
    with store_module.open_store(tmp_path) as db:
        db.ensure_session("s1", "cli")
        db.ensure_session("s2", "cli")
        db.set_session_title("s1", "Call")
        with pytest.raises(ValueError):
            db.set_session_title("s2", "Call")


def test_a_session_key_lists_its_rows_newest_activity_first(store_module, interface, tmp_path):
    with store_module.open_store(tmp_path) as db:
        db.ensure_session("s1", "api_server")
        interface.stores[tmp_path].sessions_rows["s1"].update(session_key="agent:main:x", title="Trip")
        rows = db.list_sessions_rich(session_key="agent:main:x", limit=1, order_by_last_active=True)
    assert [row["title"] for row in rows] == ["Trip"]
    _, query, window = next(call for call in interface.opened[-1].calls if call[0] == "list_sessions")
    assert (query.session_key, query.order, window.limit) == ("agent:main:x", "last_active", 1)


def test_a_hidden_bot_chat_reads_its_title_by_id_and_by_gateway_key(interface, tmp_path):
    """notifier#881: a Bot Chat's row is hidden and its id isn't its gateway
    key. Its title reads either way (the Work tools' Bot Chat check passes the
    id, approval hooks the key); an unknown id or key reads none."""
    titles = _plugin("session_titles")
    with _plugin("hermes_store").open_store(tmp_path) as db:
        db.ensure_session("20261118_090000_aaaaaa", "api_server")
        db.ensure_session("20261118_090500_bbbbbb", "api_server")
    rows = interface.stores[tmp_path].sessions_rows
    rows["20261118_090000_aaaaaa"].update(session_key="agent:main:api_server:dm:bot", title="Bot Chat", hidden=True)
    rows["20261118_090500_bbbbbb"].update(session_key="agent:main:api_server:dm:trip", title="Trip")
    assert titles.read_or_raise(tmp_path, "20261118_090000_aaaaaa") == "Bot Chat"
    assert titles.read_or_raise(tmp_path, "agent:main:api_server:dm:bot") == "Bot Chat"
    assert titles.read_or_raise(tmp_path, "20261118_090500_bbbbbb") == "Trip"
    assert titles.read_or_raise(tmp_path, "nope") == ""


def test_a_session_key_listing_leaves_hidden_rows_out_unless_asked(store_module, interface, tmp_path):
    with store_module.open_store(tmp_path) as db:
        db.ensure_session("s1", "api_server")
        interface.stores[tmp_path].sessions_rows["s1"].update(session_key="k", title="Bot Chat", hidden=True)
        assert db.list_sessions_rich(session_key="k") == []
        assert [r["title"] for r in db.list_sessions_rich(session_key="k", include_hidden=True)] == ["Bot Chat"]


def test_deleting_runs_what_hermes_own_delete_runs_after_it(store_module, interface, tmp_path):
    with store_module.open_store(tmp_path) as db:
        for sid in ("a", "b", "c"):
            db.ensure_session(sid, "api_server")
        interface.stores[tmp_path].sessions_rows["b"]["busy"] = True
        skipped: list = []
        deleted = db.delete_sessions(["a", "b"], sessions_dir=tmp_path / "sessions", exclude_active_write_guards=True,
                                     skipped_ids=skipped, include_compression_chain=True)
        assert db.delete_session("c")
    assert (deleted, skipped) == (1, ["b"])
    assert ("delete_many", ("a", "b"), True, True, str(tmp_path / "media")) in interface.opened[-1].calls
    assert [(ids, kw["sessions_dir"]) for _, ids, kw in interface.removed] == [
        (("a",), tmp_path / "sessions"), (("c",), tmp_path / "sessions")]


def test_a_note_and_an_archive_go_through_the_stores_own_verbs(store_module, interface, tmp_path):
    with store_module.open_store(tmp_path) as db:
        db.append_completion_note("chat", "key", "text", "internal_notification", {"a": 1})
        assert db._auto_archive_lineage("chat") and db.unarchive_auto_archived("chat")
        db.set_session_hidden("chat", False)
        db.end_session("chat", "voice_call_ended")
    calls = interface.opened[-1].calls
    assert ("note", "chat", "key", "text", "internal_notification", {"a": 1}) in calls
    assert all(call in calls for call in (("auto_archive", "chat"), ("set_archived", "chat", False),
                                          ("set_hidden", "chat", False), ("end", "chat", "voice_call_ended")))


def test_can_answers_for_the_door_this_hermes_has(store_module, interface, monkeypatch):
    assert store_module.can("append_completion_note") and not store_module.can("no_such_verb")
    monkeypatch.delitem(sys.modules, "persistence_assembly")
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)
    state = types.ModuleType("hermes_state")
    state.SessionDB = type("SessionDB", (), {"get_meta": lambda self, key: None})
    monkeypatch.setitem(sys.modules, "hermes_state", state)
    assert not store_module.has_storage_interface()
    assert store_module.can("get_meta") and not store_module.can("append_completion_note")


def test_a_release_before_the_storage_interface_opens_its_sessiondb(store_module, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)
    opened = []

    class SessionDB:
        def __init__(self, db_path, read_only=False):
            opened.append((db_path, read_only))

    state = types.ModuleType("hermes_state")
    state.SessionDB = SessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", state)
    assert isinstance(store_module.open_store(tmp_path), SessionDB)
    store_module.open_store(tmp_path, read_only=True)
    assert opened == [(tmp_path / "state.db", False), (tmp_path / "state.db", True)]


def test_a_new_session_id_is_hermes_own_where_it_has_one(store_module, interface, monkeypatch):
    assert store_module.new_session_id() == "20261118_120000_abcdef"
    monkeypatch.setitem(sys.modules, "session_rules.session_ids", None)
    monkeypatch.setitem(sys.modules, "hermes_state_ids", None)
    assert store_module.new_session_id() is None


# --- Group rooms ------------------------------------------------------------------------------------


def test_a_room_call_takes_a_store_and_placement_where_hermes_does(store_module, interface, tmp_path):
    seen = []

    def read_events(store, *, placement, room_id):
        seen.append((store.scope.profile_dir, placement, room_id))
        return {"events": []}

    def old_read_events(db_path, *, room_id):
        seen.append((db_path, room_id))
        return {"events": []}

    work = tmp_path / "profiles" / "work"
    store_module.room_call(read_events, tmp_path / "shared-state.db", room_id="r1")
    store_module.room_call(read_events, work / "state.db", room_id="r2")
    store_module.room_call(old_read_events, tmp_path / "shared-state.db", room_id="r3")
    assert seen == [(tmp_path, "shared", "r1"), (work, "profile", "r2"), (tmp_path / "shared-state.db", "r3")]
    assert all(store.closed for store in interface.opened)


def test_a_room_service_is_called_on_its_own_store_where_it_has_one(store_module, interface, tmp_path):
    # DeepSeek's review of #904: a room service's log is read from the store
    # it holds, not from a handle opened again on its file.
    seen = []

    def read_events(store, *, placement, room_id):
        seen.append((store, placement, room_id))
        return {"events": []}

    def old_read_events(db_path, *, room_id):
        seen.append((db_path, room_id))
        return {"events": []}

    own = types.SimpleNamespace(scope=types.SimpleNamespace(profile_dir=tmp_path))
    service = types.SimpleNamespace(store=own, placement="shared", db_path=None)
    store_module.service_call(read_events, service, room_id="r1")
    old = types.SimpleNamespace(db_path=tmp_path / "shared-state.db")
    store_module.service_call(old_read_events, old, room_id="r2")
    store_module.service_call(read_events, old, room_id="r3")  # a path only: opened on it
    assert seen[:2] == [(own, "shared", "r1"), (tmp_path / "shared-state.db", "r2")]
    assert seen[2][1:] == ("shared", "r3") and seen[2][0] is not own
    assert not interface.opened[:-1] and all(store.closed for store in interface.opened)


def test_copies_read_a_rooms_log_from_its_services_store(interface, monkeypatch, tmp_path):
    copies = _plugin("room_copies")
    seen = []

    def read_events(store, *, placement, **fields):
        seen.append((store, placement, fields["room_id"]))
        return {"events": []}

    hermes = copies.Hermes()
    monkeypatch.setattr(hermes, "_calls", {"read_events": read_events, "ingest_page": None, "ReplicaError": None})
    own = types.SimpleNamespace(scope=types.SimpleNamespace(profile_dir=tmp_path))
    hermes.read_events(types.SimpleNamespace(store=own, placement="profile"), room_id="r1", since_seq=0)
    assert seen == [(own, "profile", "r1")]
    # GLM's review of #904: room_reading reads a log by its database's path.
    hermes.read_events(tmp_path / "shared-state.db", room_id="r2", since_seq=0)
    assert seen[1][1:] == ("shared", "r2") and all(store.closed for store in interface.opened)
    old = []
    monkeypatch.setattr(hermes, "_calls", {"read_events": lambda db_path, **fields: old.append(db_path) or {},
                                            "ingest_page": None, "ReplicaError": None})
    hermes.read_events(tmp_path / "shared-state.db", room_id="r3")
    hermes.read_events(types.SimpleNamespace(store=own, placement="shared", db_path=tmp_path / "x.db"), room_id="r4")
    assert old == [tmp_path / "shared-state.db", tmp_path / "x.db"]


def test_a_store_and_placement_name_the_room_database_hermes_keeps_them_in(store_module, interface, tmp_path):
    work = tmp_path / "profiles" / "work"
    store = types.SimpleNamespace(scope=types.SimpleNamespace(profile_dir=work))
    assert store_module.room_db_path(store, "profile") == work / "state.db"
    assert store_module.room_db_path(store, "shared") == tmp_path / "shared-state.db"
    assert store_module.room_db_path(tmp_path / "shared-state.db") == tmp_path / "shared-state.db"
    assert store_module.room_db_path(str(work / "state.db"), None) == work / "state.db"


# --- What uses it -----------------------------------------------------------------------------------


def test_the_disband_guard_cleans_up_after_a_room_hermes_names_by_its_store(monkeypatch, tmp_path):
    cleanup = _plugin("room_cleanup")
    rooms = types.ModuleType("gateway.hosted_rooms")
    rooms.default_db_path = lambda home: Path(home) / "shared-state.db"
    monkeypatch.setitem(sys.modules, "gateway.hosted_rooms", rooms)
    seen = []
    monkeypatch.setattr(cleanup, "cleans", lambda room_id, home: seen.append(("cleans", home)) or True)
    monkeypatch.setattr(cleanup, "_before", lambda db_path, room_id: seen.append(("before", db_path)) or ("default",))
    monkeypatch.setattr(cleanup, "_in_background", lambda work, name: seen.append(("after", work.args[2])))

    def disband_room(store, *, placement, room_id, expected_gateway_id, expected_epoch):
        seen.append(("hermes", store, placement))
        return {"room_id": room_id}

    store = types.SimpleNamespace(scope=types.SimpleNamespace(profile_dir=tmp_path))
    guard = cleanup._guard(disband_room)
    assert guard(store, placement="shared", room_id="room_abc", expected_gateway_id="g", expected_epoch=1) == {
        "room_id": "room_abc"}
    assert seen == [("cleans", tmp_path), ("before", tmp_path / "shared-state.db"), ("hermes", store, "shared"),
                    ("after", tmp_path / "shared-state.db")]


def test_the_cut_off_guard_reads_routes_from_the_runtimes_store(monkeypatch):
    cutoff = _fresh("conduit_room_cutoff_store_test", "room_cutoff.py")
    asked = []

    def load_room_links_tolerant(store, *, placement):
        asked.append((store, placement))
        return (types.SimpleNamespace(room_id="r1", member_id="m1", status=cutoff.CUT_OFF),), ()

    links = types.ModuleType(cutoff.LINKS)
    links.load_room_links_tolerant = load_room_links_tolerant
    monkeypatch.setitem(sys.modules, cutoff.LINKS, links)
    runtime = types.SimpleNamespace(store="the store", placement="profile")
    assert cutoff.cut_off(runtime, "r1", "m1") and not cutoff.cut_off(runtime, "r1", "m2")
    assert asked == [("the store", "profile")] * 2


def test_copies_move_to_their_own_store_under_the_storage_interface(interface, tmp_path):
    copies = _plugin("room_copies")
    folder = tmp_path / "conduit-rooms"
    folder.mkdir()
    with sqlite3.connect(folder / copies.FILE_NAME) as conn:
        conn.execute("CREATE TABLE conduit_room_copies (room_id TEXT)")
        conn.execute("INSERT INTO conduit_room_copies VALUES ('room_abc')")
    (folder / (copies.FILE_NAME + "-wal")).write_bytes(b"")
    moved = copies.path(tmp_path)
    assert moved == folder / "copies" / "shared-state.db" and not (folder / copies.FILE_NAME).exists()
    assert (folder / "copies" / "shared-state.db-wal").exists()
    with sqlite3.connect(moved) as conn:
        assert conn.execute("SELECT room_id FROM conduit_room_copies").fetchall() == [("room_abc",)]
    assert copies.path(tmp_path) == moved


def test_a_voice_chat_comes_back_through_the_stores_unarchive(interface, tmp_path):
    archive = _fresh("conduit_voice_archive_store_test", "voice_archive.py")
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("CREATE TABLE sessions (id TEXT, archived INTEGER, auto_archived INTEGER)")
        conn.execute("CREATE TABLE state_meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1', 1, 1)")
        conn.execute("INSERT INTO state_meta VALUES (?, ?)", (archive.VOICE_TAGS_KEY, '{"s1": {"kind": "call"}}'))
    assert archive.bring_back(tmp_path, "s1")
    assert ("set_archived", "s1", False) in interface.opened[-1].calls and interface.opened[-1].closed
    os.remove(tmp_path / "state.db")
