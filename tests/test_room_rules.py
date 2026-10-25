"""Owner rules for turns in rooms with other people's agents (designs/cross-owner-agents, M1)."""
import importlib.util
import json
import logging
import pathlib
import sqlite3
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "tests" / "hermes_state_schema.sql").read_text(encoding="utf-8")
# Hermes' hosted_rooms table (gateway/hosted_rooms.py _SCHEMA_DDL, 9e01b44): the
# canonical service keeps it in an authority's state.db, the legacy one in the
# root's shared-state.db.
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


room_choices = _load("conduit_push.room_choices", ROOT / "room_choices.py")
rules = _load("conduit_push.room_rules", ROOT / "room_rules.py")

REMOTE = "room_" + "0123456789abcdef" * 2
FURINA = {"member_id": "furina", "profile": "furina", "handle": "furina", "display_name": "Furina"}
NAHIDA = {"member_id": "nahida", "profile": "nahida", "handle": "nahida", "display_name": "Nahida",
          "target": {"kind": "local"}}
LAIFU = {"member_id": "laifu", "profile": "laifu", "handle": "laifu", "display_name": "Laifu",
         "target": {"kind": "peer", "peer_id": "mona", "installation_id": "install-mona", "profile": "laifu",
                    "capability_digest": "sha256:abc"}}


@pytest.fixture(autouse=True)
def fresh_cache():
    rules.forget()
    yield
    rules.forget()


def _store(path):
    """A Hermes state.db with the real sessions schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def _session(home, session_id, *, source="tui", title=None, parent=None, end_reason=None):
    conn = sqlite3.connect(_store(home / "state.db"))
    conn.execute("INSERT INTO sessions (id, source, title, parent_session_id, end_reason, started_at, hidden) "
                 "VALUES (?, ?, ?, ?, ?, 1.0, ?)", (session_id, source, title, parent, end_reason,
                                                     int(source == "bot_room")))
    conn.commit()
    conn.close()


def _end(home, session_id, reason):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("UPDATE sessions SET end_reason = ? WHERE id = ?", (reason, session_id))
    conn.commit()
    conn.close()


def _room(path, room_id, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(HOSTED_ROOMS_DDL)
    conn.execute("INSERT INTO hosted_rooms (room_id, name, members_json, authority_gateway_id, created_at, "
                 "updated_at) VALUES (?, ?, ?, 'install:home', 1.0, 1.0)", (room_id, room_id, json.dumps(members)))
    conn.commit()
    conn.close()


def _room_session(home, session_id, room_id, **kwargs):
    _session(home, session_id, source="bot_room", title=f"Group: {room_id}", **kwargs)


# --- Telling room turns apart ------------------------------------------------------


def test_a_room_session_with_others_is_told_apart_on_each_side(tmp_path):
    home = tmp_path / "hermes"
    # A member of a room hosted on another installation runs in room_<digest>.
    _room_session(home, REMOTE, "room-a")
    assert rules.room_turn(REMOTE, home) == rules.RoomTurn(
        room_id="room-a", side="member", with_others=True, session_id=REMOTE)
    # A local member of a room with a peer member, the room in the canonical store.
    _room(home / "state.db", "room-b", [FURINA, LAIFU])
    _room_session(home, "s-home", "room-b")
    assert rules.room_turn("s-home", home) == rules.RoomTurn(
        room_id="room-b", side="home", with_others=True, session_id="s-home")
    # Only this host's own agents: today's rooms.
    _room(home / "state.db", "room-c", [FURINA, NAHIDA])
    _room_session(home, "s-local", "room-c")
    assert rules.room_turn("s-local", home) == rules.RoomTurn(
        room_id="room-c", side="home", with_others=False, session_id="s-local")
    # An ordinary chat isn't a room turn.
    _session(home, "s-chat")
    assert rules.room_turn("s-chat", home) is None
    assert rules.room_turn("", home) is None
    assert rules.room_turn("s-unknown", home) is None


def test_a_room_is_found_in_the_legacy_store_and_in_another_profiles(tmp_path):
    root = tmp_path / "hermes"
    # The dashboard's legacy room service keeps rooms in the root's shared-state.db.
    _room(root / "shared-state.db", "room-legacy", [FURINA, LAIFU])
    _room_session(root, "s-legacy", "room-legacy")
    assert rules.room_turn("s-legacy", root).with_others
    # A canonical room is in its owner's state.db; a member profile's session is in its own.
    nahida = root / "profiles" / "nahida"
    _room(root / "state.db", "room-owned", [FURINA, NAHIDA, LAIFU])
    _room_session(nahida, "s-nahida", "room-owned")
    turn = rules.room_turn("s-nahida", nahida)
    assert (turn.room_id, turn.side, turn.with_others) == ("room-owned", "home", True)


def test_a_compacted_room_session_keeps_its_room(tmp_path):
    home = tmp_path / "hermes"
    _room(home / "state.db", "room-b", [FURINA, LAIFU])
    _room_session(home, "s-home", "room-b", end_reason="compression")
    # Hermes titles the continuation after its parent's.
    _session(home, "s-child", source="bot_room", title="Group: room-b #2", parent="s-home")
    assert rules.room_turn("s-child", home) == rules.RoomTurn(
        room_id="room-b", side="home", with_others=True, session_id="s-child")
    _room_session(home, REMOTE, "room-a", end_reason="compression")
    _session(home, "s-remote-child", source="bot_room", title="Group: room-a #2", parent=REMOTE)
    assert rules.room_turn("s-remote-child", home) == rules.RoomTurn(
        room_id="room-a", side="member", with_others=True, session_id="s-remote-child")
    # A branch of a room session is a conversation of its own.
    _session(home, "s-branch", parent="s-local-parent")
    _session(home, "s-local-parent", source="bot_room", title="Group: room-b", end_reason="branched")
    assert rules.room_turn("s-branch", home) is None


def test_room_turns_are_remembered_per_session(tmp_path):
    home = tmp_path / "hermes"
    _room(home / "state.db", "room-b", [FURINA, LAIFU])
    _room_session(home, "s-home", "room-b")
    assert rules.room_turn("s-home", home).with_others
    (home / "state.db").unlink()
    assert rules.room_turn("s-home", home).with_others


def test_a_room_store_that_cant_be_read_is_an_error_not_a_guess(tmp_path):
    home = tmp_path / "hermes"
    _room_session(home, "s-home", "room-b")
    (home / "shared-state.db").write_bytes(b"this is not a database" * 64)
    with pytest.raises(rules.RoomStoreError):
        rules.room_turn("s-home", home)
    # Not remembered: the next turn reads again.
    (home / "shared-state.db").unlink()
    _room(home / "shared-state.db", "room-b", [FURINA, LAIFU])
    assert rules.room_turn("s-home", home).with_others


def test_an_unreadable_session_store_counts_as_no_room(tmp_path, caplog):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "state.db").write_bytes(b"this is not a database" * 64)
    with caplog.at_level(logging.WARNING):
        assert rules.room_turn("s-chat", home) is None
    assert "couldn't read the session store" in caplog.text
    # A remote room member's session is named like one, so it's still a room turn.
    turn = rules.room_turn(REMOTE, home)
    assert (turn.side, turn.with_others, turn.room_id) == ("member", True, "")


def test_a_remote_room_session_is_a_room_turn_before_its_row_is_found(tmp_path):
    home = tmp_path / "hermes"
    _session(home, "s-chat")
    turn = rules.room_turn(REMOTE, home)
    assert (turn.side, turn.with_others, turn.room_id) == ("member", True, "")
    # Not remembered without its row: once it's there, the room is named.
    _room_session(home, REMOTE, "room-a")
    assert rules.room_turn(REMOTE, home).room_id == "room-a"
