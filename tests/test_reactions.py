"""The user's reactions reach the bot on its next turn; the bot reacts back (reactions, issue 3439)."""

import contextlib
import importlib.util
import json
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(os.environ.get("HERMES_HOME", "/nonexistent"))
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


_load("conduit_push.hermes_store", ROOT / "hermes_store.py")
reactions = _load("conduit_push.reactions", ROOT / "reactions.py")


class _Store:
    """Hermes' store as the note reads it: each reaction surfaces once."""

    def __init__(self, pending):
        self.pending = list(pending)
        self.asked = []

    def take_unseen_reactions(self, session_id, author="user"):
        self.asked.append((session_id, author))
        taken, self.pending = self.pending, []
        return taken


def _opener(store):
    @contextlib.contextmanager
    def opener(_home):
        yield store
    return opener


def test_the_next_turn_hears_each_reaction_once(monkeypatch):
    monkeypatch.setattr(reactions.hermes_store, "can", lambda method: True)
    store = _Store([
        {"row_id": 7, "role": "assistant", "emoji": "❤️", "text": "Here's the second\ndesign."},
        {"row_id": 9, "role": "assistant", "emoji": "👎", "text": ""},
        {"row_id": 4, "role": "user", "emoji": "😂", "text": "lol"},
    ])
    note = reactions.note("s1", opener=_opener(store))
    assert note == "\n".join([
        reactions.NOTE_HEAD,
        '- ❤️ on your message: "Here\'s the second design."',
        "- 👎 on your earlier message",
        '- 😂 on their own message: "lol"',
    ])
    assert store.asked == [("s1", "user")]
    # Stamped seen as read: the turn after hears nothing again.
    assert reactions.note("s1", opener=_opener(store)) is None


def test_a_long_message_is_quoted_short_and_many_reactions_counted():
    pending = [{"role": "assistant", "emoji": "👍", "text": "x" * 300}] * (reactions.MAX_LISTED + 3)
    lines = reactions.format_note(pending).splitlines()
    assert lines[1].endswith("x" * reactions.SNIPPET_CHARS + '…"')
    assert lines[-1] == "- and 3 more"
    assert len(lines) == reactions.MAX_LISTED + 2


def test_no_reactions_no_note_and_a_broken_store_never_breaks_the_turn(monkeypatch):
    monkeypatch.setattr(reactions.hermes_store, "can", lambda method: True)
    assert reactions.note("s1", opener=_opener(_Store([]))) is None
    assert reactions.note("", opener=_opener(_Store([{"emoji": "❤️"}]))) is None

    @contextlib.contextmanager
    def broken(_home):
        raise RuntimeError("locked")
        yield
    assert reactions.note("s1", opener=broken) is None
    # An older Hermes without the call has nothing to read.
    monkeypatch.setattr(reactions.hermes_store, "can", lambda method: False)
    assert reactions.note("s1", opener=_opener(_Store([{"emoji": "❤️", "role": "assistant"}]))) is None


def test_the_tool_runs_hermes_own_reaction():
    calls = []

    def react(emoji, row_id, back):
        calls.append((emoji, row_id, back))
        return json.dumps({"success": True, "row_id": 3, "reactions": []})
    answer = json.loads(reactions.handle({"emoji": " ✅ ", "messages_back": 1}, session_id="s1", react=react))
    assert answer["ok"] is True
    assert calls == [("✅", None, 1)]
    # An empty emoji takes it back.
    assert json.loads(reactions.handle({"emoji": ""}, session_id="s1", react=react))["ok"] is True
    assert calls[-1] == ("", None, None)


def test_the_tool_refuses_what_it_cant_do():
    def react(*_):
        raise AssertionError("not called")
    assert json.loads(reactions.handle({"emoji": "❤️"}, session_id="", react=react))["error"] == "no_chat"
    assert json.loads(reactions.handle({"emoji": "❤️"}, session_id="kid", react=react,
                                       is_child=lambda s: True))["error"] == "subagent"
    assert json.loads(reactions.handle({"emoji": 1}, session_id="s", react=react))["error"] == "bad_emoji"
    assert json.loads(reactions.handle({"emoji": "great job"}, session_id="s", react=react))["error"] == "bad_emoji"
    assert json.loads(reactions.handle({"emoji": "❤️", "messages_back": -1}, session_id="s",
                                       react=react))["error"] == "bad_messages_back"
    assert json.loads(reactions.handle({"emoji": "❤️", "messages_back": True}, session_id="s",
                                       react=react))["error"] == "bad_messages_back"


def test_a_failed_save_says_so():
    answer = json.loads(reactions.handle({"emoji": "❤️"}, session_id="s",
                                         react=lambda *_: json.dumps({"error": "No user message to react to yet."})))
    assert answer["ok"] is False and "No user message to react to yet." in answer["note"]


def test_the_tool_registers_and_rooms_never_get_it():
    registered = []
    ctx = types.SimpleNamespace(register_tool=lambda **kwargs: registered.append(kwargs))
    reactions.register(ctx, is_child=lambda s: False)
    assert registered[0]["name"] == "conduit_react" and registered[0]["toolset"] == "conduit"
    assert "conduit_react" in (ROOT / "plugin.yaml").read_text()
    room_choices = _load("conduit_push.room_choices", ROOT / "room_choices.py")
    assert "conduit_react" in room_choices.NEVER


def test_a_strict_bot_chat_can_still_react():
    work_tool_source = (ROOT / "work_tool.py").read_text()
    shows = work_tool_source[work_tool_source.index("SHOWS_OR_ASKS = "):].splitlines()[0]
    assert '"conduit_react"' in shows


def test_an_older_hermes_without_the_display_fields_still_gets_the_tool():
    registered = []

    def register_tool(name, toolset, schema, handler):
        registered.append(name)
    reactions.register(types.SimpleNamespace(register_tool=register_tool), is_child=lambda s: False)
    assert registered == ["conduit_react"]


def test_every_chat_turn_gets_the_note_but_never_a_room_or_a_subagent():
    source = (ROOT / "__init__.py").read_text()
    hook = source[source.index("def _pre_llm_call("):]
    hook = hook[:hook.index("\ndef ", 1)]
    # After the room's own framing returns, among the owner's notes, and only for the chat itself.
    assert hook.index("room_framing = _room_framing(") < hook.index("reactions.note(session_id)")
    assert "None if child else reactions.note(session_id)" in hook


def test_an_entry_of_no_known_role_is_named_by_no_one():
    assert reactions.format_note([{"emoji": "❤️", "text": "hi"}]).endswith('- ❤️ on a message: "hi"')
    assert reactions.format_note([{"emoji": "❤️"}]).endswith("- ❤️ on an earlier message")
