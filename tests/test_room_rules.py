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
if "tools.clarify_tool" not in sys.modules:
    _tools = sys.modules.setdefault("tools", types.ModuleType("tools"))
    _tools.__path__ = getattr(_tools, "__path__", [])
    _clarify_tool = types.ModuleType("tools.clarify_tool")

    def _strip_recommended(text):
        # Same semantics as test_clarify_loop's stub, whichever module loads first.
        stripped = str(text).strip()
        suffix = "(Recommended)"
        if stripped.casefold().endswith(suffix.casefold()):
            return stripped[: -len(suffix)].strip()
        return stripped

    _clarify_tool.strip_recommended = _strip_recommended
    sys.modules["tools.clarify_tool"] = _clarify_tool
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
plugin = _load("conduit_push.__init__", ROOT / "__init__.py")

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


def _title(home, session_id, title):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
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
        room_id="room-b", side="home", with_others=True, session_id="s-home", others=("laifu",))
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
        room_id="room-b", side="home", with_others=True, session_id="s-child", others=("laifu",))
    _room_session(home, REMOTE, "room-a", end_reason="compression")
    _session(home, "s-remote-child", source="bot_room", title="Group: room-a #2", parent=REMOTE)
    assert rules.room_turn("s-remote-child", home) == rules.RoomTurn(
        room_id="room-a", side="member", with_others=True, session_id="s-remote-child")
    # A branch of a room session is a conversation of its own.
    _session(home, "s-branch", parent="s-local-parent")
    _session(home, "s-local-parent", source="bot_room", title="Group: room-b", end_reason="branched")
    assert rules.room_turn("s-branch", home) is None


def test_a_room_session_is_read_again_until_it_has_its_title(tmp_path):
    home = tmp_path / "hermes"
    _room(home / "state.db", "room-b", [FURINA, LAIFU])
    # Hermes writes a local member's row, then titles it in a second write.
    _session(home, "s-home", source="bot_room")
    _session(home, REMOTE, source="bot_room")
    assert not rules.room_turn("s-home", home).with_others
    assert rules.room_turn(REMOTE, home).room_id == ""
    _title(home, "s-home", "Group: room-b")
    _title(home, REMOTE, "Group: room-a")
    assert rules.room_turn("s-home", home) == rules.RoomTurn(
        room_id="room-b", side="home", with_others=True, session_id="s-home", others=("laifu",))
    assert rules.room_turn(REMOTE, home).room_id == "room-a"
    # A legacy Desktop group's title carries its display name: no room, so no others.
    _session(home, "s-desktop", source="bot_room", title="Group: Family chat")
    assert rules.room_turn("s-desktop", home) == rules.RoomTurn(
        room_id="", side="home", with_others=False, session_id="s-desktop")


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


def test_a_room_in_no_store_is_read_again_and_logged_once(tmp_path, caplog):
    home = tmp_path / "hermes"
    _room_session(home, "s-home", "room-b")
    # The legacy store's hosted_rooms has another shape: it doesn't have the room either.
    conn = sqlite3.connect(home / "shared-state.db")
    conn.execute("CREATE TABLE hosted_rooms (room_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO hosted_rooms VALUES ('room-b')")
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            turn = rules.room_turn("s-home", home)
            assert (turn.room_id, turn.with_others) == ("room-b", False)
    assert caplog.text.count("in any room store") == 1
    # Not remembered: once the room is there, its others are found.
    _room(home / "state.db", "room-b", [FURINA, LAIFU])
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


# --- What a room turn may do ------------------------------------------------------


@pytest.fixture
def host(tmp_path, monkeypatch):
    """This host's Hermes home, with a room that seats another owner's agent
    (room-b, Furina's session s-home), a room of this host's own agents
    (room-c, s-local), a remote room this host's agent sits in (REMOTE) and
    an ordinary chat (s-chat)."""
    home = tmp_path / "hermes"
    _room(home / "state.db", "room-b", [FURINA, LAIFU])
    _room_session(home, "s-home", "room-b")
    _room(home / "state.db", "room-c", [FURINA, NAHIDA])
    _room_session(home, "s-local", "room-c")
    _room_session(home, REMOTE, "room-a")
    _session(home, "s-chat")
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    return home


def _tool(session_id, tool_name, **kwargs):
    return plugin._pre_tool_call(session_id=session_id, tool_name=tool_name, args={}, turn_id="t1", **kwargs)


def _blocked(result):
    return isinstance(result, dict) and result.get("action") == "block" and bool(result.get("message"))


def _choose(home, room_id, **choices):
    room_choices.ChoicesStore(home).set(room_id, choices)


EVERYDAY_TOOLS = ["terminal", "read_file", "write_file", "web_search", "web_extract", "session_search", "memory",
                  "skill_view", "conduit_location", "conduit_calendar", "tool_search", "execute_code"]


@pytest.mark.parametrize("session_id", ["s-home", REMOTE])
def test_talk_only_blocks_every_tool_in_a_room_turn(host, session_id):
    for tool_name in EVERYDAY_TOOLS:
        assert _blocked(_tool(session_id, tool_name)), tool_name
    assert "talk" in _tool(session_id, "terminal")["message"].lower()


@pytest.mark.parametrize("session_id, room_id", [("s-home", "room-b"), (REMOTE, "room-a")])
def test_look_things_up_allows_web_tools_only(host, session_id, room_id):
    _choose(host, room_id, tools="look")
    for tool_name in ("web_search", "web_extract", "x_search", "vision_analyze"):
        assert _tool(session_id, tool_name) is None, tool_name
    # Tool Search lists tools; Hermes checks the tool it calls by that tool's name.
    assert _tool(session_id, "tool_search") is None
    for tool_name in ("terminal", "read_file", "browser_navigate", "conduit_location", "session_search", "memory"):
        assert _blocked(_tool(session_id, tool_name)), tool_name
    # Use my data adds only the room-safe data tools the owner picks; none exist yet.
    _choose(host, room_id, tools="data", custom_tools=["conduit_calendar"])
    assert _tool(session_id, "web_search") is None
    assert _blocked(_tool(session_id, "conduit_calendar"))


def test_custom_allows_its_tools_but_never_memory_or_delegation(host):
    _choose(host, "room-b", tools="custom", custom_tools=["terminal", "read_file"])
    assert _tool("s-home", "terminal") is None
    assert _tool("s-home", "read_file") is None
    assert _blocked(_tool("s-home", "write_file"))
    for tool_name in ("memory", "skill_manage", "delegate_task", "cronjob_manage", "send_message",
                      "conduit_settings", "conduit_call_watch", "conduit_call_user"):
        assert _blocked(_tool("s-home", tool_name)), tool_name
    # A choice that names one anyway (a hand-edited file) still can't allow it.
    picked = room_choices.RoomChoices(tools="custom", custom_tools=("memory", "delegate_task", "terminal"))
    assert rules.tool_block("memory", picked) and rules.tool_block("delegate_task", picked)
    assert rules.tool_block("terminal", picked) is None
    assert rules.allowed_tools(picked) == {"terminal"}


def test_tools_that_get_around_the_rules_are_never_allowed(host):
    # execute_code's script calls tools without the room session's id, so the
    # rules never see those calls; message_agent, react_to_message and
    # kanban_create reach agents and chats outside the room.
    names = ("execute_code", "message_agent", "react_to_message", "kanban_create")
    picked = room_choices.RoomChoices(tools="custom", custom_tools=names + ("terminal",))
    for tool_name in names:
        assert rules.tool_block(tool_name, picked) == rules.NEVER_BLOCK.format(tool=tool_name), tool_name
    assert rules.allowed_tools(picked) == {"terminal"}


@pytest.mark.parametrize("tools, picks", [("talk", []), ("look", []), ("data", []),
                                          ("custom", ["skill_view", "skills_list", "terminal"])])
def test_background_review_cannot_write_memory_or_skills_in_a_room(host, monkeypatch, tools, picks):
    # The review runs after the room turn with the room session's id, and
    # whatever Hermes tells the plugin about delegation.
    _choose(host, "room-b", tools=tools, custom_tools=picks)
    _choose(host, "room-a", tools=tools, custom_tools=picks)
    for child in (False, True):
        monkeypatch.setattr(plugin, "_is_child", lambda session_id, child=child: child)
        for session_id in ("s-home", REMOTE):
            for tool_name in ("memory", "skill_manage"):
                assert _blocked(_tool(session_id, tool_name)), (tools, child, session_id, tool_name)
            assert "memory" in _tool(session_id, "memory")["message"].lower()


def test_past_chat_search_is_off_unless_chosen(host):
    _choose(host, "room-b", tools="look")
    assert "past chats" in _tool("s-home", "session_search")["message"]
    _choose(host, "room-b", search_chats=True)
    assert _tool("s-home", "session_search") is None
    # Searching is its own choice: it holds under Talk only too.
    _choose(host, "room-b", tools="talk")
    assert _tool("s-home", "session_search") is None
    assert _blocked(_tool("s-home", "web_search"))


def test_a_room_with_only_your_own_agents_keeps_its_tools(host):
    for tool_name in EVERYDAY_TOOLS + ["delegate_task", "skill_manage"]:
        assert _tool("s-local", tool_name) is None, tool_name
        assert _tool("s-chat", tool_name) is None, tool_name


def test_a_store_error_in_a_room_turn_blocks_the_tool(host, monkeypatch):
    # The room's roster can't be read: whether it has other people in it is unknown.
    _room_session(host, "s-unread", "room-unread")
    (host / "shared-state.db").write_bytes(b"this is not a database" * 64)
    result = _tool("s-unread", "terminal")
    assert _blocked(result) and "couldn't" in result["message"]
    # The owner's choices can't be read.
    def unreadable(self, room_id):
        raise OSError("disk gone")
    monkeypatch.setattr(room_choices.ChoicesStore, "get", unreadable)
    assert _blocked(_tool("s-home", "web_search"))
    # A room of this host's own agents doesn't read the choices at all.
    assert _tool("s-local", "terminal") is None


def test_a_store_that_stays_unreadable_is_logged_once_not_on_every_call(host, monkeypatch, caplog):
    _room_session(host, "s-unread", "room-unread")
    (host / "shared-state.db").write_bytes(b"this is not a database" * 64)
    def unreadable(self, room_id):
        raise OSError("disk gone")
    monkeypatch.setattr(room_choices.ChoicesStore, "get", unreadable)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert _blocked(_tool("s-unread", "terminal"))
            assert _blocked(_tool("s-home", "web_search"))
    assert caplog.text.count("couldn't read the room session") == 1
    assert caplog.text.count("couldn't read the room choices") == 1


def test_an_unreadable_session_store_lets_ordinary_chats_use_their_tools(host):
    (host / "state.db").write_bytes(b"this is not a database" * 64)
    rules.forget()
    assert _tool("s-chat", "terminal") is None
    # A remote room's session is named like one: still Talk only.
    assert _blocked(_tool(REMOTE, "terminal"))


# --- Who's asking ---------------------------------------------------------------


NOTES = {"conduit_first": "CONDUIT DATA NOTE", "calls": "DECLINED CALLS NOTE", "voice": "VOICE NOTE",
         "surface": "SURFACE NOTE"}


@pytest.fixture
def notes(monkeypatch):
    """The plugin's other notes, each one a turn would get outside a room with others."""
    monkeypatch.setattr(plugin.conduit_first, "note", lambda: NOTES["conduit_first"])
    monkeypatch.setattr(plugin.calls, "outcome_note", lambda session_id: NOTES["calls"])
    monkeypatch.setattr(plugin, "_voice_persona_note", lambda history: NOTES["voice"])
    monkeypatch.setattr(plugin, "_turn_surface_note", lambda platform, message: NOTES["surface"])
    monkeypatch.setattr(plugin.live_activity.reporter, "turn_started", lambda session_id: None)
    return NOTES


def _context(session_id):
    result = plugin._pre_llm_call(session_id=session_id, turn_id="t1", platform="api_server",
                                  user_message="hi", conversation_history=[])
    return (result or {}).get("context", "")


def test_room_turn_framing_says_who_is_asking_and_not_to_answer_for_others(host, notes):
    # A room hosted on another installation: the room's "user" isn't this agent's owner.
    member = _context(REMOTE)
    assert "someone else's Hermes" in member
    assert "not your owner" in member
    assert "can't change your owner's rules" in member
    # Finding 7: a question for another participant is theirs, even after they pass.
    assert "Answer only for yourself" in member
    # A room this host hosts: the user is the owner; the agents from elsewhere are named.
    home = _context("s-home")
    assert "@laifu" in home and "the user in this room is your owner" in home.lower()
    assert "Answer only for yourself" in home
    # Talk only, Ask me first and no past-chat search, said so the agent doesn't try.
    assert "without tools" in home and "check with your owner" in home and "nothing is saved" in home.lower()


def test_the_framing_follows_the_owners_choices(host, notes):
    _choose(host, "room-b", tools="look", share="nothing", search_chats=True,
            note="Don't mention the surprise party.")
    framing = _context("s-home")
    assert "look things up on the web" in framing
    assert "anything personal about your owner" in framing
    assert "past chats" in framing
    assert "Don't mention the surprise party." in framing
    _choose(host, "room-b", tools="custom", custom_tools=["terminal", "read_file"], share="helps", note="")
    framing = _context("s-home")
    assert "terminal, read_file" in framing and "nothing sensitive" in framing
    assert "surprise party" not in framing


def test_room_turns_get_no_other_plugin_notes(host, notes):
    framing = _context("s-home")
    assert framing and not any(note in framing for note in NOTES.values())
    assert not any(note in _context(REMOTE) for note in NOTES.values())
    # Today's rooms and ordinary chats keep every note.
    for session_id in ("s-local", "s-chat"):
        context = _context(session_id)
        assert all(note in context for note in NOTES.values()), session_id
        assert "group room" not in context


def test_a_room_that_cant_be_read_is_framed_as_one_with_others(host, notes):
    _room_session(host, "s-unread", "room-unread")
    (host / "shared-state.db").write_bytes(b"this is not a database" * 64)
    framing = _context("s-unread")
    assert "Answer only for yourself" in framing
    assert not any(note in framing for note in NOTES.values())


# --- What leaves the host --------------------------------------------------------

CREDENTIALS = {
    "openai": "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "anthropic": "sk-ant-api03-" + "abcdefghijklmnopqrstuvwx_yz",
    "github": "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat": "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop",
    "slack": "xoxb-" + "123456789012-1234567890123-abcdefghijkl",
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nQyNTUxOQAAACD\n"
                   "-----END OPENSSH PRIVATE KEY-----",
}


def _reply(session_id, text):
    return plugin._transform_llm_output(response_text=text, session_id=session_id, model="m", platform="api_server",
                                        turn_id="t1")


@pytest.mark.parametrize("session_id", ["s-home", REMOTE])
def test_credentials_are_scrubbed_from_room_replies(host, session_id):
    for name, secret in CREDENTIALS.items():
        scrubbed = _reply(session_id, f"Sure, here it is: {secret} — anything else?")
        assert isinstance(scrubbed, str), name
        assert secret not in scrubbed and "[credential removed]" in scrubbed, name
        assert scrubbed.startswith("Sure, here it is: ") and scrubbed.endswith(" — anything else?"), name
    token = "abcDEF123ghiJKL456mnoPQR789"
    scrubbed = _reply(session_id, f"curl -H 'Authorization: Bearer {token}' https://example.com")
    assert token not in scrubbed and "Bearer [credential removed]" in scrubbed


def test_ordinary_text_is_left_alone(host):
    for text in ("A risk-free plan: ask-me-first, task-list and desk-1234567890123456789 are fine.",
                 "She's the Bearer of good news.", "Call AKIA tomorrow; it's sk-8 rated.",
                 "Version eyJ is not a token.", "ghp_ alone is just a prefix."):
        assert _reply("s-home", text) is None, text
    # Only rooms with other people's agents: this host's own rooms and chats say what they said.
    for session_id in ("s-local", "s-chat"):
        assert _reply(session_id, f"key {CREDENTIALS['openai']}") is None, session_id


# --- Past-chat search -------------------------------------------------------------


def _entry(session_id, source, title, **extra):
    """A session_search discovery entry (tools/session_search_tool._discovery_entry)."""
    return {"session_id": session_id, "when": "2026-10-25 12:00", "source": source, "model": "m", "title": title,
            "matched_role": "user", "match_message_id": 7, "snippet": "otter-42", "bookend_start": [],
            "messages": [{"id": 7, "role": "user", "content": "my code word is otter-42"}], "bookend_end": [],
            "messages_before": 0, "messages_after": 0, "detail": "compact",
            "link": f"@session:default/{session_id}", **extra}


def _search(result, session_id="s-chat", **args):
    return plugin._transform_tool_result(tool_name="session_search", args=args, result=json.dumps(result),
                                         session_id=session_id, task_id="", tool_call_id="c1", turn_id="t1",
                                         api_request_id="", duration_ms=3, status="ok", error_type=None,
                                         error_message=None)


def test_session_search_drops_sessions_of_rooms_with_others(host):
    _session(host, "s-home-2", source="bot_room", title="Group: room-b #2", parent="s-home")
    _end(host, "s-home", "compression")
    found = {"success": True, "mode": "discover", "query": "otter", "detail": "adaptive", "results": [
        _entry("s-home", "bot_room", "Group: room-b"), _entry("s-local", "bot_room", "Group: room-c"),
        _entry("s-chat", "tui", "Bike plans"), _entry(REMOTE, "bot_room", "Group: room-a"),
        _entry("s-home-2", "bot_room", "Group: room-b #2", parent_session_id="s-home")],
        "count": 5, "sessions_searched": 5, "link_hint": "..."}
    kept = json.loads(_search(found, query="otter"))
    assert [entry["session_id"] for entry in kept["results"]] == ["s-local", "s-chat"]
    assert kept["count"] == 2 and kept["mode"] == "discover" and kept["link_hint"] == "..."
    # Browsing recent sessions, the same.
    browsed = {"success": True, "mode": "browse", "count": 3, "message": "Showing 3 most recent sessions.",
               "results": [{"session_id": sid, "link": f"@session:default/{sid}", "title": title, "source": source,
                            "started_at": 1.0, "last_active": 1.0, "message_count": 2, "preview": "hi"}
                           for sid, source, title in (("s-chat", "tui", "Bike plans"),
                                                      (REMOTE, "bot_room", "Group: room-a"),
                                                      ("s-home", "bot_room", "Group: room-b"))]}
    assert [entry["session_id"] for entry in json.loads(_search(browsed))["results"]] == ["s-chat"]
    # A search that found none of them is passed on as Hermes wrote it.
    plain = {**found, "results": [_entry("s-chat", "tui", "Bike plans")], "count": 1}
    assert _search(plain, query="otter") is None


def test_session_search_cant_read_a_room_session_with_others_directly(host):
    for mode, session_id in (("read", "s-home"), ("scroll", REMOTE)):
        read = {"success": True, "mode": mode, "session_id": session_id, "link": f"@session:default/{session_id}",
                "session_meta": {"when": "2026-10-25 12:00", "source": "bot_room", "model": "m", "title": "Group: x"},
                "messages": [{"id": 1, "role": "user", "content": "my code word is otter-42"}]}
        refused = json.loads(_search(read, session_id=session_id))
        assert refused["success"] is False and "otter" not in json.dumps(refused)
        assert f"session_id not found: {session_id}" in refused["error"]
    local = {"success": True, "mode": "read", "session_id": "s-local",
             "session_meta": {"source": "bot_room", "title": "Group: room-c"}, "messages": []}
    assert _search(local, session_id="s-local") is None


def test_session_search_of_another_profile_reads_that_profiles_rooms(host):
    nahida = host / "profiles" / "nahida"
    _room_session(nahida, "s-nahida", "room-b")
    found = {"success": True, "mode": "discover", "results": [_entry("s-nahida", "bot_room", "Group: room-b")],
             "count": 1}
    assert json.loads(_search(found, query="otter", profile="nahida"))["results"] == []
    assert json.loads(_search(found, query="otter", session_id="nahida/s-nahida"))["results"] == []
    # That profile's own rooms are read from its store: a room of this host's agents stays findable.
    _room_session(nahida, "s-nahida-local", "room-c")
    local = {**found, "results": [_entry("s-nahida-local", "bot_room", "Group: room-c")]}
    assert _search(local, query="otter", profile="nahida") is None


def test_session_search_hides_a_room_session_it_cant_check(host):
    _room_session(host, "s-unread", "room-unread")
    (host / "shared-state.db").write_bytes(b"this is not a database" * 64)
    found = {"success": True, "mode": "discover", "results": [_entry("s-unread", "bot_room", "Group: room-unread"),
                                                              _entry("s-chat", "tui", "Bike plans")], "count": 2}
    assert [entry["session_id"] for entry in json.loads(_search(found, query="x"))["results"]] == ["s-chat"]


def test_a_search_result_it_cant_read_is_withheld_when_it_mentions_rooms(host, caplog):
    # Shapes this plugin doesn't know (a newer Hermes, say) can't be checked.
    room = _entry("s-home", "bot_room", "Group: room-b")
    unknown = [{"success": True, "mode": "timeline", "sessions": [room]},
               {"success": True, "mode": "read", "messages": room["messages"], "session_meta": {"source": "bot_room"}},
               {"success": True, "mode": "discover", "results": [json.dumps(room)]},
               {"mode": "discover", "results": [room]},
               [room]]
    with caplog.at_level(logging.WARNING):
        for result in unknown:
            assert json.loads(_search(result)) == {"success": False, "error": "Past-chat search failed; try again."}
        assert json.loads(plugin._transform_tool_result(
            tool_name="session_search", args={}, result="bot_room " + REMOTE, session_id="s-chat"))["success"] is False
    assert caplog.text.count("couldn't read a past-chat search") == 1
    # Hermes' own errors are passed on as they are.
    assert _search({"success": False, "error": f"session_id not found: {REMOTE}"}) is None
    assert _search({"error": f"Error executing session_search for {REMOTE}"}) is None


def test_a_search_check_that_fails_withholds_room_results_and_logs_once(host, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(rules, "filter_search", broken)
    found = {"success": True, "mode": "discover", "results": [_entry("s-home", "bot_room", "Group: room-b")]}
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert json.loads(_search(found)) == {"success": False, "error": "Past-chat search failed; try again."}
    assert caplog.text.count("couldn't check a past-chat search") == 1
    assert _search({"success": True, "mode": "discover", "results": [_entry("s-chat", "tui", "Bike")]}) is None


def test_other_tools_and_unreadable_results_pass_through(host):
    assert plugin._transform_tool_result(tool_name="web_search", args={}, result='{"results": []}',
                                         session_id="s-chat") is None
    for result in ("not json", '["a"]', '{"success": false, "error": "x"}'):
        assert plugin._transform_tool_result(tool_name="session_search", args={}, result=result,
                                             session_id="s-chat") is None
