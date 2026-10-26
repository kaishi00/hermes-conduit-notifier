"""Disbanding a room ends its hidden chats on its home, and a member host's
owner can delete theirs later (designs/cross-owner-agents, N2; Eric,
2026-10-26)."""
import argparse
import importlib.util
import json
import logging
import pathlib
import sqlite3
import sys
import tempfile
import threading
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
SCHEMA = (ROOT / "tests" / "hermes_state_schema.sql").read_text(encoding="utf-8")
# Hermes' hosted_rooms table (gateway/hosted_rooms.py _SCHEMA_DDL, 9e01b44).
HOSTED_ROOMS_DDL = """CREATE TABLE IF NOT EXISTS hosted_rooms (
            room_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            members_json TEXT NOT NULL,
            authority_gateway_id TEXT NOT NULL,
            authority_epoch INTEGER NOT NULL DEFAULT 1 CHECK (authority_epoch >= 1),
            next_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
            event_bytes INTEGER NOT NULL DEFAULT 0 CHECK (event_bytes >= 0),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            disbanded_at REAL
        )"""

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
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


cleanup = _load("conduit_push.room_cleanup", ROOT / "room_cleanup.py")
# What a disband's cleanup runs on, before the tests below run it inline.
IN_BACKGROUND = cleanup._in_background
_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_cleanup", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)

FURINA = {"member_id": "furina", "profile": "furina", "handle": "furina", "display_name": "Furina"}
NAHIDA = {"member_id": "nahida", "profile": "nahida", "handle": "nahida", "display_name": "Nahida",
          "target": {"kind": "local"}}
LAIFU = {"member_id": "laifu", "profile": "laifu", "handle": "laifu", "display_name": "Laifu",
         "target": {"kind": "peer", "peer_id": "mona", "installation_id": "install-mona", "profile": "laifu",
                    "capability_digest": "sha256:abc"}}
# A member host's session for a room hosted elsewhere
# (gateway/platforms/api_server_room_dispatch.py).
REMOTE = "room_" + "0123456789abcdef" * 2


# --- Hermes, as far as the cleanup uses it ---------------------------------------


class _Busy(RuntimeError):
    """Hermes' RuntimeStoreError for a session whose ledger still has a turn
    (hermes_state_mutation_retirement.retire_terminal_receipts)."""

    def __init__(self, reason="session_busy"):
        super().__init__(reason)
        self.reason = reason


_UNMARKED = ("json_extract({t}.model_config, '$._branched_from') IS NULL "
             "AND json_extract({t}.model_config, '$._delegate_from') IS NULL")


class _Hermes:
    """Stand-ins for gateway.hosted_rooms.disband_room, SessionDB.delete_sessions
    and the session-store registry (c73cbcc4 semantics), over real sqlite stores."""

    def __init__(self, root):
        self.root = root
        self.guarded = {}  # session id -> deletes a turn's lease still refuses
        self.busy = set()  # session ids whose ledger has a turn not finished
        self.fails = set()  # session ids Hermes' delete fails on (a row of theirs it can't read)
        self.broken = False
        self.prune = False  # the disband prunes the room's row in its transaction
        self.refuse = False  # the disband loses its authority fence
        self.deletes = []
        self.held = 0
        self.block = None

    def disband_room(self, db_path, *, room_id, expected_gateway_id, expected_epoch, now=None):
        if self.refuse:
            raise RuntimeError("stale hosted room authority")
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT disbanded_at FROM hosted_rooms WHERE room_id = ?", (room_id,)).fetchone()
            if row is None:
                raise LookupError("hosted room not found")
            if row[0] is not None:
                return {"room_id": room_id, "disbanded_at": row[0], "idempotent": True}
            conn.execute("UPDATE hosted_rooms SET disbanded_at = 5.0 WHERE room_id = ?", (room_id,))
            if self.prune:
                conn.execute("DELETE FROM hosted_rooms WHERE room_id = ?", (room_id,))
            conn.commit()
        finally:
            conn.close()
        return {"room_id": room_id, "disbanded_at": 5.0, "idempotent": False}

    def delete_sessions(self, path, session_ids, sessions_dir=None, exclude_active_write_guards=False,
                        skipped_ids=None, include_compression_chain=False):
        self.deletes.append((path, list(session_ids), {
            "sessions_dir": sessions_dir, "exclude_active_write_guards": exclude_active_write_guards,
            "include_compression_chain": include_compression_chain}))
        if self.block is not None:
            self.block.wait(5)
        if self.broken:
            raise sqlite3.OperationalError("disk I/O error")
        if self.fails & set(session_ids):
            raise ValueError("malformed JSON")
        conn = sqlite3.connect(path)
        try:
            existing = [sid for sid in session_ids
                        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone()]
            covered = {sid: self._covered(conn, sid, include_compression_chain) for sid in existing}
            if exclude_active_write_guards:
                refused = [sid for sid in existing if any(self.guarded.get(x, 0) > 0 for x in covered[sid])]
                for sid in refused:
                    for x in covered[sid]:
                        if self.guarded.get(x, 0) > 0:
                            self.guarded[x] -= 1
                existing = [sid for sid in existing if sid not in refused]
                if skipped_ids is not None:
                    skipped_ids.extend(sorted(refused))
            doomed = [x for sid in existing for x in covered[sid]]
            if any(x in self.busy for x in doomed):
                raise _Busy()
            for x in doomed:
                conn.execute("UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?", (x,))
                conn.execute("DELETE FROM messages WHERE session_id = ?", (x,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (x,))
            conn.commit()
            return len(existing)
        finally:
            conn.close()

    @staticmethod
    def _covered(conn, session_id, chain):
        """The rows Hermes deletes for one id: its compression chain both ways
        (when asked), and every delegated child below them."""
        found = [session_id]
        frontier = [session_id]
        while chain and frontier:
            step = []
            for sid in frontier:
                rows = conn.execute(
                    "SELECT child.id FROM sessions parent JOIN sessions child ON child.parent_session_id = parent.id "
                    f"WHERE parent.id = ? AND parent.end_reason = 'compression' AND {_UNMARKED.format(t='child')}",
                    (sid,)).fetchall()
                rows += conn.execute(
                    "SELECT parent.id FROM sessions child JOIN sessions parent ON parent.id = child.parent_session_id "
                    f"WHERE child.id = ? AND parent.end_reason = 'compression' AND {_UNMARKED.format(t='child')}",
                    (sid,)).fetchall()
                step += [row[0] for row in rows if row[0] not in found and row[0] not in step]
            found += step
            frontier = step
        frontier = list(found)
        while frontier:
            marks = ",".join("?" for _ in frontier)
            children = [row[0] for row in conn.execute(
                f"SELECT id FROM sessions WHERE json_extract(model_config, '$._delegate_from') IN ({marks})",
                frontier).fetchall() if row[0] not in found]
            found += children
            frontier = children
        return found


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    fake = _Hermes(root)

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    rooms = types.ModuleType("gateway.hosted_rooms")
    rooms.disband_room = fake.disband_room
    gateway.hosted_rooms = rooms
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.hosted_rooms", rooms)

    class SessionDB:
        def __init__(self, db_path):
            self.db_path = pathlib.Path(db_path)

        def delete_sessions(self, session_ids, sessions_dir=None, exclude_active_write_guards=False,
                            skipped_ids=None, include_compression_chain=False):
            return fake.delete_sessions(self.db_path, session_ids, sessions_dir, exclude_active_write_guards,
                                        skipped_ids, include_compression_chain)

    state = types.ModuleType("hermes_state")
    state.SessionDB = SessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", state)

    registry = types.ModuleType("hermes_state_registry")

    def acquire(db_path=None):
        fake.held += 1
        return SessionDB(db_path)

    def release_or_close(db):
        fake.held -= 1

    registry.acquire, registry.release_or_close = acquire, release_or_close
    monkeypatch.setitem(sys.modules, "hermes_state_registry", registry)

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    profiles = types.ModuleType("hermes_cli.profiles")

    def get_profile_dir(name):
        if name == "default":
            return root
        if not cleanup._PROFILE.fullmatch(name):
            raise ValueError(f"invalid profile name {name!r}")
        return root / "profiles" / name

    def list_profile_names():
        # Hermes' listing: default, then each named profile's directory.
        named = sorted(path.name for path in (root / "profiles").glob("*") if path.is_dir())
        return ["default", *named]

    profiles.get_profile_dir, profiles.list_profile_names = get_profile_dir, list_profile_names
    config = types.ModuleType("hermes_cli.config_effective")

    def load_user_config_effective(config_path=None, *, fail_closed=False):
        import yaml

        path = pathlib.Path(config_path)
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}

    config.load_user_config_effective = load_user_config_effective
    hermes_cli.profiles, hermes_cli.config_effective = profiles, config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    monkeypatch.setitem(sys.modules, "hermes_cli.config_effective", config)
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: root, raising=False)

    # The cleanup after a disband runs here, not on a thread, unless a test
    # asks for the real one; the load's sweep waits for a test to run it.
    monkeypatch.setattr(cleanup, "_in_background", lambda work, name: work())
    monkeypatch.setattr(cleanup, "RETRY_AFTER_S", 0.0)
    monkeypatch.setattr(cleanup, "SWEEP_AFTER_S", 3600.0)
    fake.rooms = rooms
    yield fake
    cleanup.uninstall()


@pytest.fixture
def guard(hermes):
    assert cleanup.install()
    return hermes


def _home(hermes, profile):
    return hermes.root if profile == "default" else hermes.root / "profiles" / profile


def _store(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def _session(home, session_id, *, source="tui", title=None, parent=None, end_reason=None, model_config=None):
    conn = sqlite3.connect(_store(home / "state.db"))
    conn.execute("INSERT INTO sessions (id, source, title, parent_session_id, end_reason, model_config, started_at, "
                 "hidden) VALUES (?, ?, ?, ?, ?, ?, 1.0, ?)",
                 (session_id, source, title, parent, end_reason,
                  json.dumps(model_config) if model_config is not None else None, int(source == "bot_room")))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', 1.0)",
                 (session_id,))
    conn.commit()
    conn.close()


def _room_session(home, session_id, room_id, **kwargs):
    _session(home, session_id, source="bot_room", title=f"Group: {room_id}", **kwargs)


def _room(path, room_id, members, *, disbanded_at=None):
    _store(path) if path.name == "state.db" else path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(HOSTED_ROOMS_DDL)
    conn.execute("INSERT INTO hosted_rooms (room_id, name, members_json, authority_gateway_id, created_at, "
                 "updated_at, disbanded_at) VALUES (?, ?, ?, 'install:home', 1.0, 1.0, ?)",
                 (room_id, room_id, json.dumps(members), disbanded_at))
    conn.commit()
    conn.close()


def _ids(home):
    path = home / "state.db"
    if not path.exists():
        return set()
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT id FROM sessions")}
    finally:
        conn.close()


def _disband(hermes, room_id, store=None):
    # As Hermes' group controls call it (gateway/session_group_controls.py,
    # tui_gateway/methods_groups.py): looked up on the module at call time.
    return hermes.rooms.disband_room(store or hermes.root / "state.db", room_id=room_id,
                                     expected_gateway_id="install:home", expected_epoch=1)


# --- Disbanding ends the room's hidden chats --------------------------------------


def test_disband_deletes_your_agents_hidden_chats_in_every_room(guard):
    furina, nahida = _home(guard, "furina"), _home(guard, "nahida")
    _room(guard.root / "state.db", "with-laifu", [FURINA, NAHIDA, LAIFU])
    _room(guard.root / "state.db", "just-mine", [FURINA, NAHIDA])
    _room(guard.root / "state.db", "still-going", [FURINA])
    _room_session(furina, "f-with", "with-laifu")
    _room_session(furina, "f-mine", "just-mine")
    _room_session(furina, "f-going", "still-going")
    _room_session(furina, REMOTE, "hosted-elsewhere")
    _session(furina, "f-private", title="Weekend plans")
    _room_session(nahida, "n-with", "with-laifu")
    _room_session(nahida, "n-mine", "just-mine")
    _session(guard.root, "d-private", title="Groceries")
    # A peer member's profile is a name on its own host: this host's profile
    # of that name isn't in the room.
    namesake = _home(guard, "laifu")
    _room_session(namesake, "l-namesake", "with-laifu")

    result = _disband(guard, "with-laifu")

    assert result == {"room_id": "with-laifu", "disbanded_at": 5.0, "idempotent": False}
    assert _ids(furina) == {"f-mine", "f-going", REMOTE, "f-private"}
    assert _ids(nahida) == {"n-mine"}
    # A room of only your own agents ends the same way (Eric, 2026-10-26).
    _disband(guard, "just-mine")
    assert _ids(furina) == {"f-going", REMOTE, "f-private"}
    assert _ids(nahida) == set()
    assert _ids(guard.root) == {"d-private"}
    assert _ids(namesake) == {"l-namesake"}
    # Each chat goes the way Hermes' own delete takes it, and every store it opened is let go.
    assert {(path, tuple(ids)) for path, ids, _ in guard.deletes} == {
        (furina / "state.db", ("f-with",)), (nahida / "state.db", ("n-with",)),
        (furina / "state.db", ("f-mine",)), (nahida / "state.db", ("n-mine",))}
    assert all(kwargs == {"sessions_dir": path.parent / "sessions", "exclude_active_write_guards": True,
                          "include_compression_chain": True} for path, _, kwargs in guard.deletes)
    assert guard.held == 0


def test_a_compacted_room_chat_goes_with_its_chain(guard):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    # Hermes' compaction moves the title to the continuation (c73cbcc4).
    _session(furina, "c-root", source="bot_room", end_reason="compression")
    _room_session(furina, "c-tip", "with-laifu", parent="c-root")
    _session(furina, "c-helper", source="subagent", title="Subagent: check the forecast", parent="c-tip",
             model_config={"_delegate_from": "c-tip"})
    _session(furina, "c-branch", title="Branch of the room", parent="c-tip", model_config={"_branched_from": "c-tip"})

    _disband(guard, "with-laifu")

    # A branch is a conversation of its own, so it stays, as Hermes' delete leaves one.
    assert _ids(furina) == {"c-branch"}


def test_only_the_rooms_own_hidden_chats_go(guard):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _session(furina, "numbered", source="bot_room", title="Group: with-laifu #2")
    _session(furina, "desktop", source="desktop", title="Group: with-laifu")
    _session(furina, "longer", source="bot_room", title="Group: with-laifu-2")
    _session(furina, "not-numbered", source="bot_room", title="Group: with-laifu #two")
    _session(furina, "other-case", source="bot_room", title="group: with-laifu")

    _disband(guard, "with-laifu")

    # Hermes numbers a taken title " #N" (hermes_state_titles.next_title_in_lineage).
    assert _ids(furina) == {"desktop", "longer", "not-numbered", "other-case"}


def test_a_room_hermes_prunes_as_it_disbands_still_ends_its_chats(guard):
    # Past its tombstone or event-size caps Hermes prunes disbanded rooms in
    # the disband's own transaction (_prune_disbanded_rooms_locked).
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    guard.prune = True

    _disband(guard, "with-laifu")

    assert _ids(furina) == set()


def test_the_legacy_room_service_is_cleaned_up_too(guard):
    # The dashboard's own room service keeps its rooms in the root's shared-state.db (.52).
    furina = _home(guard, "furina")
    legacy = guard.root / "shared-state.db"
    _room(legacy, "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")

    _disband(guard, "with-laifu", store=legacy)

    assert _ids(furina) == set()


def test_a_member_seated_from_a_configured_home_is_found(guard, tmp_path):
    # A group seats the homes hosted_rooms.profiles names (gateway/session_hosted_members.py).
    elsewhere = tmp_path / "elsewhere" / "lumine"
    (guard.root / "config.yaml").write_text(f"hosted_rooms:\n  profiles:\n    lumine: {elsewhere}\n", encoding="utf-8")
    lumine = {"member_id": "lumine", "profile": "lumine", "handle": "lumine", "target": {"kind": "local"}}
    _room(guard.root / "state.db", "with-laifu", [lumine, LAIFU])
    _room_session(elsewhere, "l-with", "with-laifu")
    _room_session(elsewhere, "l-other", "another-room")

    _disband(guard, "with-laifu")

    assert _ids(elsewhere) == {"l-other"}


def test_a_turn_finishing_at_disband_is_tried_again_shortly(guard):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    guard.guarded["f-with"] = 1

    _disband(guard, "with-laifu")

    assert _ids(furina) == set()
    assert [ids for _, ids, _ in guard.deletes] == [["f-with"], ["f-with"]]


def test_a_refused_delete_is_retried_at_load(guard, caplog):
    furina, nahida = _home(guard, "furina"), _home(guard, "nahida")
    _room(guard.root / "state.db", "with-laifu", [FURINA, NAHIDA, LAIFU])
    _room(guard.root / "state.db", "still-going", [FURINA])
    # Disbanded before this version of the plugin.
    _room(guard.root / "state.db", "long-gone", [FURINA, LAIFU], disbanded_at=2.0)
    _room_session(furina, "f-with", "with-laifu")
    _room_session(nahida, "n-with", "with-laifu")
    _room_session(furina, "f-going", "still-going")
    _room_session(furina, "f-gone", "long-gone")
    _session(furina, "f-private", title="Weekend plans")
    guard.guarded["f-with"] = 2
    guard.busy.add("n-with")

    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        _disband(guard, "with-laifu")

    assert _ids(furina) == {"f-with", "f-going", "f-gone", "f-private"}
    assert _ids(nahida) == {"n-with"}
    assert "still finishing a turn" in caplog.text and "next loads" in caplog.text
    assert guard.held == 0

    guard.busy.clear()
    assert cleanup.sweep() == 3
    assert _ids(furina) == {"f-going", "f-private"}
    assert _ids(nahida) == set()
    assert cleanup.sweep() == 0


def test_disband_still_succeeds_when_cleanup_fails(guard, caplog):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    guard.broken = True

    with caplog.at_level(logging.WARNING, logger=cleanup.__name__):
        result = _disband(guard, "with-laifu")

    assert result["idempotent"] is False
    assert "couldn't delete every hidden chat of room with-laifu" in caplog.text
    assert _ids(furina) == {"f-with"}
    assert guard.held == 0


def test_a_chat_hermes_cant_delete_keeps_only_itself(guard, caplog):
    furina, nahida = _home(guard, "furina"), _home(guard, "nahida")
    _room(guard.root / "state.db", "with-laifu", [FURINA, NAHIDA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    _session(furina, "f-with-2", source="bot_room", title="Group: with-laifu #2")
    _room_session(nahida, "n-with", "with-laifu")
    guard.fails.add("f-with")

    with caplog.at_level(logging.WARNING, logger=cleanup.__name__):
        assert _disband(guard, "with-laifu")["idempotent"] is False

    assert _ids(furina) == {"f-with"}
    assert _ids(nahida) == set()
    # Tried again shortly, as a chat a turn is finishing in is, then at the next load.
    assert [ids for _, ids, _ in guard.deletes].count(["f-with"]) == 2
    assert "couldn't delete" in caplog.text and "next loads" in caplog.text
    assert guard.held == 0
    guard.fails.clear()
    assert cleanup.sweep() == 1
    assert _ids(furina) == set()


def test_a_home_whose_store_fails_is_tried_again_shortly(guard, monkeypatch):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    registry = sys.modules["hermes_state_registry"]
    acquire, opened = registry.acquire, []

    def locked_at_first(db_path=None):
        opened.append(db_path)
        if len(opened) == 1:
            raise sqlite3.OperationalError("database is locked")
        return acquire(db_path)

    monkeypatch.setattr(registry, "acquire", locked_at_first)
    _disband(guard, "with-laifu")

    assert len(opened) == 2
    assert _ids(furina) == set()


def test_a_cleanup_that_cant_start_leaves_the_disband_done(guard, monkeypatch, caplog):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")

    def no_thread(work, name):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(cleanup, "_in_background", no_thread)
    with caplog.at_level(logging.WARNING, logger=cleanup.__name__):
        assert _disband(guard, "with-laifu")["idempotent"] is False

    assert "room with-laifu" in caplog.text and "next loads" in caplog.text
    assert _ids(furina) == {"f-with"}
    assert cleanup.sweep() == 1
    assert _ids(furina) == set()


def _damage(store, room_id, members_json):
    conn = sqlite3.connect(store)
    conn.execute("UPDATE hosted_rooms SET members_json = ? WHERE room_id = ?", (members_json, room_id))
    conn.commit()
    conn.close()


def test_a_roster_it_cant_read_ends_the_rooms_chats_in_every_home(guard, tmp_path, caplog):
    furina, nahida = _home(guard, "furina"), _home(guard, "nahida")
    elsewhere = tmp_path / "elsewhere" / "lumine"
    (guard.root / "config.yaml").write_text(f"hosted_rooms:\n  profiles:\n    lumine: {elsewhere}\n", encoding="utf-8")
    store = guard.root / "state.db"
    _room(store, "with-laifu", [FURINA, LAIFU])
    _damage(store, "with-laifu", "not json")
    _room_session(furina, "f-with", "with-laifu")
    _room_session(nahida, "n-with", "with-laifu")
    _room_session(elsewhere, "l-with", "with-laifu")
    _room_session(furina, "f-other", "other-room")
    _session(furina, "f-private", title="Weekend plans")

    with caplog.at_level(logging.WARNING, logger=cleanup.__name__):
        assert _disband(guard, "with-laifu")["idempotent"] is False

    assert "couldn't read room with-laifu's members" in caplog.text and "every agent's home" in caplog.text
    assert _ids(furina) == {"f-other", "f-private"}
    assert _ids(nahida) == set()
    assert _ids(elsewhere) == set()
    assert guard.held == 0


def test_a_store_locked_at_disband_still_ends_the_chats_hermes_prunes(guard, monkeypatch, caplog):
    # The read before the disband times out, and Hermes prunes the room's row
    # in the disband's own transaction, so no later sweep can find it.
    furina, nahida = _home(guard, "furina"), _home(guard, "nahida")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    _session(nahida, "n-private", title="Weekend plans")
    guard.prune = True
    select = cleanup._select

    def locked_roster(path, sql, params):
        if sql.startswith("SELECT members_json"):
            raise sqlite3.OperationalError("database is locked")
        return select(path, sql, params)

    monkeypatch.setattr(cleanup, "_select", locked_roster)
    with caplog.at_level(logging.WARNING, logger=cleanup.__name__):
        _disband(guard, "with-laifu")

    assert "couldn't read room with-laifu's members" in caplog.text
    assert _ids(furina) == set()
    assert _ids(nahida) == {"n-private"}
    assert cleanup.sweep() == 0


def test_hermes_refusing_the_disband_deletes_nothing(guard):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    guard.refuse = True

    with pytest.raises(RuntimeError, match="stale hosted room authority"):
        _disband(guard, "with-laifu")
    assert _ids(furina) == {"f-with"}
    assert guard.deletes == []


def test_disbanding_again_deletes_what_was_left(guard):
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    guard.guarded["f-with"] = 2
    _disband(guard, "with-laifu")
    assert _ids(furina) == {"f-with"}

    assert _disband(guard, "with-laifu")["idempotent"] is True
    assert _ids(furina) == set()


def test_the_disband_doesnt_wait_for_the_cleanup(guard, monkeypatch):
    monkeypatch.setattr(cleanup, "_in_background", IN_BACKGROUND)
    furina = _home(guard, "furina")
    _room(guard.root / "state.db", "with-laifu", [FURINA, LAIFU])
    _room_session(furina, "f-with", "with-laifu")
    guard.block = threading.Event()

    _disband(guard, "with-laifu")
    assert _ids(furina) == {"f-with"}

    guard.block.set()
    for thread in threading.enumerate():
        if thread.name == "conduit-room-cleanup":
            thread.join(5)
    assert _ids(furina) == set()


# --- Installing -----------------------------------------------------------------


def test_cleanup_installs_once_and_uninstalls(hermes):
    own = hermes.rooms.disband_room
    assert cleanup.install()
    assert cleanup.install()
    assert hermes.rooms.disband_room is not own
    assert hermes.rooms.disband_room.__wrapped__ is own

    cleanup.uninstall()
    assert hermes.rooms.disband_room is own


def test_a_second_load_wraps_hermes_own_function_not_the_first_guard(hermes, monkeypatch):
    own = hermes.rooms.disband_room
    second_package = types.ModuleType("conduit_push_second")
    second_package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "conduit_push_second", second_package)
    assert cleanup.install()
    first = hermes.rooms.disband_room
    second = _load("conduit_push_second.room_cleanup", ROOT / "room_cleanup.py")
    try:
        assert second.install()
        assert hermes.rooms.disband_room.__wrapped__ is own
        # The first load's uninstall leaves the second load's guard in place.
        cleanup.uninstall()
        assert hermes.rooms.disband_room is not first and hermes.rooms.disband_room.__wrapped__ is own
    finally:
        second.uninstall()
        for name in [name for name in sys.modules if name.startswith("conduit_push_second.")]:
            sys.modules.pop(name)
    assert hermes.rooms.disband_room is own


def test_a_hermes_without_disband_room_gets_one_log_line(hermes, monkeypatch, caplog):
    monkeypatch.delattr(hermes.rooms, "disband_room")
    monkeypatch.setattr(cleanup, "_logged", set())
    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        assert not cleanup.install()
        assert not cleanup.install()
    lines = [record for record in caplog.records if record.name == cleanup.__name__]
    assert len(lines) == 1
    assert "disband_room" in lines[0].getMessage() and "hidden chats" in lines[0].getMessage()


def test_a_hermes_that_cant_delete_a_whole_chain_gets_one_log_line(hermes, monkeypatch, caplog):
    def delete_sessions(self, session_ids, sessions_dir=None, exclude_active_write_guards=False, skipped_ids=None):
        raise AssertionError("never called")

    monkeypatch.setattr(sys.modules["hermes_state"].SessionDB, "delete_sessions", delete_sessions)
    monkeypatch.setattr(cleanup, "_logged", set())
    own = hermes.rooms.disband_room
    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        assert not cleanup.install()
        assert not cleanup.install()
    assert hermes.rooms.disband_room is own
    assert len([record for record in caplog.records if record.name == cleanup.__name__]) == 1


def test_no_hermes_rooms_at_all_is_not_an_error(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.hosted_rooms", None)
    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        assert not cleanup.install()
    assert not [record for record in caplog.records if record.levelno >= logging.INFO]


def test_the_load_sweep_runs_once_a_load_off_the_start_path(hermes, monkeypatch):
    furina = _home(hermes, "furina")
    _room(hermes.root / "state.db", "long-gone", [FURINA, LAIFU], disbanded_at=2.0)
    _room_session(furina, "f-gone", "long-gone")
    monkeypatch.setattr(cleanup, "SWEEP_AFTER_S", 0.2)

    assert cleanup.install()
    # Off the plugin's load: nothing is deleted while Hermes starts.
    assert _ids(furina) == {"f-gone"}
    cleanup._sweeper.join(5)
    assert _ids(furina) == set()

    # The dashboard's copy, loaded in the same process, doesn't sweep again.
    second_package = types.ModuleType("conduit_push_second")
    second_package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "conduit_push_second", second_package)
    second = _load("conduit_push_second.room_cleanup", ROOT / "room_cleanup.py")
    try:
        assert second.install()
        assert second._sweeper is None

        # Loaded again after Hermes unloaded it (a reload): it sweeps what
        # the unload cut short.
        cleanup.uninstall()
        _room_session(furina, "f-gone-2", "long-gone")
        assert cleanup.install()
        cleanup._sweeper.join(5)
        assert _ids(furina) == set()
    finally:
        second.uninstall()
        for name in [name for name in sys.modules if name.startswith("conduit_push_second.")]:
            sys.modules.pop(name)


def test_unloading_stops_a_sweep_that_hasnt_run(hermes, monkeypatch):
    furina = _home(hermes, "furina")
    _room(hermes.root / "state.db", "long-gone", [FURINA, LAIFU], disbanded_at=2.0)
    _room_session(furina, "f-gone", "long-gone")
    monkeypatch.setattr(cleanup, "SWEEP_AFTER_S", 30.0)

    assert cleanup.install()
    sweeper = cleanup._sweeper
    cleanup.uninstall()
    sweeper.join(5)

    assert not sweeper.is_alive()
    assert _ids(furina) == {"f-gone"}


def test_the_sweep_reads_every_store_of_this_host(hermes):
    furina, nahida = _home(hermes, "furina"), _home(hermes, "nahida")
    # Rooms hosted by a profile (its own state.db) and by the legacy service.
    _room(nahida / "state.db", "nahidas-room", [NAHIDA, FURINA, LAIFU], disbanded_at=2.0)
    _room(hermes.root / "shared-state.db", "legacy-room", [FURINA], disbanded_at=3.0)
    _room_session(furina, "f-nahidas", "nahidas-room")
    _room_session(nahida, "n-nahidas", "nahidas-room")
    _room_session(furina, "f-legacy", "legacy-room")
    _room_session(furina, "f-live", "live-room")

    assert cleanup.sweep() == 3
    assert _ids(furina) == {"f-live"}
    assert _ids(nahida) == set()


def test_the_sweep_looks_in_every_home_for_rooms_whose_roster_it_cant_read(hermes):
    furina, nahida = _home(hermes, "furina"), _home(hermes, "nahida")
    store = hermes.root / "state.db"
    _room(store, "damaged", [FURINA], disbanded_at=2.0)
    _room(store, "too-deep", [FURINA], disbanded_at=3.0)
    _room(store, "with-laifu", [FURINA, LAIFU], disbanded_at=4.0)
    _damage(store, "damaged", "not json")
    # Deeper than json.loads recurses: RecursionError, not a ValueError.
    _damage(store, "too-deep", "[" * 100_000 + "]" * 100_000)
    _room_session(furina, "f-damaged", "damaged")
    _room_session(nahida, "n-damaged", "damaged")
    _room_session(nahida, "n-deep", "too-deep")
    _room_session(furina, "f-with", "with-laifu")
    _room_session(furina, "f-live", "live-room")

    assert cleanup.sweep() == 4
    assert _ids(furina) == {"f-live"}
    assert _ids(nahida) == set()


def test_a_store_the_sweep_cant_read_is_a_warning_and_the_others_are_swept(hermes, caplog):
    furina = _home(hermes, "furina")
    _room(hermes.root / "state.db", "gone", [FURINA, LAIFU], disbanded_at=2.0)
    _room_session(furina, "f-gone", "gone")
    (hermes.root / "shared-state.db").write_bytes(b"not a database " * 100)

    with caplog.at_level(logging.WARNING, logger=cleanup.__name__):
        assert cleanup.sweep() == 1
    assert _ids(furina) == set()
    assert "couldn't read the disbanded rooms in" in caplog.text and "shared-state.db" in caplog.text
    assert "next loads" in caplog.text


def test_the_sweep_looks_rooms_up_in_batches(hermes, monkeypatch):
    monkeypatch.setattr(cleanup, "ROOMS_PER_QUERY", 2)
    furina = _home(hermes, "furina")
    for index, room_id in enumerate(("gone-a", "gone-b", "gone-c")):
        _room(hermes.root / "state.db", room_id, [FURINA, LAIFU], disbanded_at=2.0 + index)
        _room_session(furina, f"f-{room_id}", room_id)
    _room_session(furina, "f-live", "live-room")

    assert cleanup.sweep() == 3
    assert _ids(furina) == {"f-live"}


# --- A member host's owner forgets a room -------------------------------------------


def test_forget_deletes_this_profiles_hidden_chats_for_the_room(hermes):
    furina, nahida = _home(hermes, "furina"), _home(hermes, "nahida")
    # Member side: a room hosted on someone else's Hermes.
    _room_session(furina, REMOTE, "theirs")
    _session(furina, "f-theirs-2", source="bot_room", title="Group: theirs #2")
    _room_session(furina, "f-another", "another-room")
    _session(furina, "f-private", title="Weekend plans")
    _room_session(nahida, "n-theirs", "theirs")

    assert cleanup.forget_room("theirs", furina) == {"room_id": "theirs", "deleted": 2, "kept": 0}
    assert _ids(furina) == {"f-another", "f-private"}
    assert _ids(nahida) == {"n-theirs"}

    # Home side: what a disband left behind.
    _room(hermes.root / "state.db", "ended-here", [FURINA, LAIFU], disbanded_at=2.0)
    _room_session(furina, "f-ended", "ended-here")
    assert cleanup.forget_room("ended-here", furina)["deleted"] == 1
    assert _ids(furina) == {"f-another", "f-private"}
    assert hermes.held == 0


def test_forget_with_nothing_to_delete_opens_no_store(hermes):
    nobody = _home(hermes, "nobody")
    nobody.mkdir(parents=True)

    assert cleanup.forget_room("theirs", nobody) == {"room_id": "theirs", "deleted": 0, "kept": 0}
    assert not (nobody / "state.db").exists()
    assert hermes.deletes == []


@pytest.mark.parametrize("held", ["leased", "in its ledger"])
def test_forget_keeps_a_chat_whose_turn_is_still_finishing(hermes, held):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")
    if held == "leased":
        hermes.guarded[REMOTE] = 1
    else:
        hermes.busy.add(REMOTE)

    assert cleanup.forget_room("theirs", furina) == {"room_id": "theirs", "deleted": 0, "kept": 1}
    assert _ids(furina) == {REMOTE}


def test_forget_finds_the_rooms_chats_among_many(hermes, monkeypatch):
    monkeypatch.setattr(cleanup, "MAX_ROOM_SESSIONS", 2)
    furina = _home(hermes, "furina")
    for room_id in ("room-a", "room-b", "room-c"):
        _room_session(furina, f"f-{room_id}", room_id)
    _room_session(furina, REMOTE, "theirs")

    assert cleanup.forget_room("theirs", furina)["deleted"] == 1
    assert _ids(furina) == {"f-room-a", "f-room-b", "f-room-c"}


def test_forget_says_when_hermes_couldnt_delete_a_chat(hermes):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")
    _session(furina, "f-theirs-2", source="bot_room", title="Group: theirs #2")
    hermes.fails.add(REMOTE)

    with pytest.raises(cleanup.CleanupError) as failed:
        cleanup.forget_room("theirs", furina)
    assert failed.value.status == 503 and "try again" in str(failed.value)
    assert _ids(furina) == {REMOTE}
    assert hermes.held == 0


# What opening Hermes' session store can raise (SessionDB.__init__): sqlite's
# errors, StateDbReplacedError (a RuntimeError) and RuntimeStoreError (a ValueError).
STORE_OPEN_ERRORS = [sqlite3.OperationalError("unable to open database file"),
                     RuntimeError("state.db was replaced while it was open"),
                     ValueError("runtime store unavailable")]


@pytest.mark.parametrize("error", STORE_OPEN_ERRORS, ids=lambda error: type(error).__name__)
def test_forget_says_try_again_when_the_session_store_wont_open(hermes, monkeypatch, error):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")

    def acquire(db_path=None):
        raise error

    monkeypatch.setattr(sys.modules["hermes_state_registry"], "acquire", acquire)
    with pytest.raises(cleanup.CleanupError) as failed:
        cleanup.forget_room("theirs", furina)
    assert failed.value.status == 503 and "try again" in str(failed.value)
    assert _ids(furina) == {REMOTE}


def test_forget_refuses_a_room_still_going_here(hermes):
    furina = _home(hermes, "furina")
    _room(hermes.root / "state.db", "still-going", [FURINA, LAIFU])
    _room_session(furina, "f-going", "still-going")

    with pytest.raises(cleanup.CleanupError) as refused:
        cleanup.forget_room("still-going", furina)
    assert refused.value.status == 409 and "disband" in str(refused.value)
    assert _ids(furina) == {"f-going"}


@pytest.mark.parametrize("room_id", ["", "../x", "Group: x", "a b", "%", "_", None, 3, "x" * 129])
def test_forget_refuses_what_isnt_a_room_id(hermes, room_id):
    with pytest.raises(cleanup.CleanupError) as refused:
        cleanup.forget_room(room_id, _home(hermes, "furina"))
    assert refused.value.status == 400


def test_forget_on_a_hermes_that_cant_delete_a_whole_chain(hermes, monkeypatch):
    monkeypatch.delattr(sys.modules["hermes_state"].SessionDB, "delete_sessions")
    with pytest.raises(cleanup.CleanupError) as refused:
        cleanup.forget_room("theirs", _home(hermes, "furina"))
    assert refused.value.status == 501


# --- Route ------------------------------------------------------------------------


@pytest.fixture
def client(hermes, monkeypatch):
    monkeypatch.setattr(api, "_calls_home", lambda: _home(hermes, "furina"))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_forget_route_deletes_and_says_what_it_kept(client, hermes):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")
    _session(furina, "f-theirs-2", source="bot_room", title="Group: theirs #2")
    hermes.guarded["f-theirs-2"] = 1

    response = client.delete(f"{BASE}/rooms/sessions", params={"room_id": "theirs"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "room_id": "theirs", "deleted": 1, "kept": 1}
    assert _ids(furina) == {"f-theirs-2"}


def test_the_forget_route_says_try_again_when_the_session_store_fails(client, hermes):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")
    hermes.broken = True

    response = client.delete(f"{BASE}/rooms/sessions", params={"room_id": "theirs"})

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "try again" in response.json()["detail"]
    assert _ids(furina) == {REMOTE}


def test_the_forget_route_says_try_again_when_the_session_store_wont_open(client, hermes, monkeypatch):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")

    def acquire(db_path=None):
        raise RuntimeError("state.db was replaced while it was open")

    monkeypatch.setattr(sys.modules["hermes_state_registry"], "acquire", acquire)
    response = client.delete(f"{BASE}/rooms/sessions", params={"room_id": "theirs"})

    assert response.status_code == 503
    assert "try again" in response.json()["detail"]
    assert _ids(furina) == {REMOTE}


@pytest.mark.parametrize("params", [{}, {"room_id": ""}, {"room_id": "../x"}, {"room_id": "Group: theirs"}])
def test_the_forget_route_refuses_what_isnt_a_room_id(client, params):
    response = client.delete(f"{BASE}/rooms/sessions", params=params)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_the_forget_route_refuses_a_room_still_going_here(client, hermes):
    _room(hermes.root / "state.db", "still-going", [FURINA, LAIFU])
    response = client.delete(f"{BASE}/rooms/sessions", params={"room_id": "still-going"})
    assert response.status_code == 409


def test_room_forget_is_a_listed_capability(client):
    assert "room-forget" in client.get(f"{BASE}/capabilities").json()["capabilities"]


def test_the_dashboard_routes_guard_it_as_they_load(hermes):
    # The dashboard hosts the rooms of a Hermes whose gateway doesn't (.52).
    own = hermes.rooms.disband_room
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_room_cleanup_load", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        assert hermes.rooms.disband_room is not own
        assert hermes.rooms.disband_room.__wrapped__ is own
    finally:
        module._room_cleanup_module.uninstall()
    assert hermes.rooms.disband_room is own


# --- CLI ------------------------------------------------------------------------


def _forget_args(room="", yes=False):
    values = {"tools": None, "tool": None, "share": None, "search": None, "takes_part": None, "limit": None,
              "note": None}
    return argparse.Namespace(conduit_push_action="rooms", rooms_action="forget", room=room, yes=yes, **values)


@pytest.fixture
def cli(hermes, monkeypatch):
    module = _load("conduit_push.cli", ROOT / "cli.py")
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: _home(hermes, "furina"))
    return module


def test_cli_forget_warns_and_asks_first(cli, hermes, monkeypatch, capsys):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert cli.dispatch(_forget_args("theirs")) == 0
    said = capsys.readouterr().out
    assert "starts over" in said and "Cancelled." in said
    assert _ids(furina) == {REMOTE}

    assert cli.dispatch(_forget_args("theirs")) == 0
    assert "Deleted 1 hidden chat for room theirs." in capsys.readouterr().out
    assert _ids(furina) == set()


def test_cli_forget_with_yes_doesnt_ask(cli, hermes, monkeypatch, capsys):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))

    assert cli.dispatch(_forget_args("theirs", yes=True)) == 0
    assert "starts over" in capsys.readouterr().out
    assert _ids(furina) == set()


def test_cli_forget_without_a_terminal_cancels(cli, hermes, monkeypatch, capsys):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")

    def no_terminal(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert cli.dispatch(_forget_args("theirs")) == 0
    assert "Cancelled." in capsys.readouterr().out
    assert _ids(furina) == {REMOTE}


def test_cli_forget_says_what_it_couldnt_do(cli, hermes, capsys):
    furina = _home(hermes, "furina")
    assert cli.dispatch(_forget_args("")) == 2
    assert "--room" in capsys.readouterr().out
    assert cli.dispatch(_forget_args("../x", yes=True)) == 1
    assert capsys.readouterr().out.startswith("Couldn't delete it:")

    assert cli.dispatch(_forget_args("theirs", yes=True)) == 0
    assert "no hidden chat for room theirs" in capsys.readouterr().out

    _room_session(furina, REMOTE, "theirs")
    hermes.guarded[REMOTE] = 1
    assert cli.dispatch(_forget_args("theirs", yes=True)) == 1
    assert "still finishing" in capsys.readouterr().out
    assert _ids(furina) == {REMOTE}

    hermes.fails.add(REMOTE)
    assert cli.dispatch(_forget_args("theirs", yes=True)) == 1
    assert "try again shortly" in capsys.readouterr().out
    assert _ids(furina) == {REMOTE}


def test_cli_forget_says_try_again_when_the_session_store_wont_open(cli, hermes, monkeypatch, capsys):
    furina = _home(hermes, "furina")
    _room_session(furina, REMOTE, "theirs")

    def acquire(db_path=None):
        raise RuntimeError("state.db was replaced while it was open")

    monkeypatch.setattr(sys.modules["hermes_state_registry"], "acquire", acquire)
    assert cli.dispatch(_forget_args("theirs", yes=True)) == 1
    assert "try again shortly" in capsys.readouterr().out
    assert _ids(furina) == {REMOTE}
