import importlib.util
import pathlib
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

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


first = _load("conduit_push.conduit_first", ROOT / "conduit_first.py")
health = _load("conduit_push.health_tool", ROOT / "health_tool.py")
location = _load("conduit_push.location_tool", ROOT / "location_tool.py")
phone = _load("conduit_push.phone_tool", ROOT / "phone_tool.py")


@pytest.fixture
def shared(monkeypatch, tmp_path):
    """Which kinds the phone shares, by the availability check each tool uses."""
    kinds = {"calendar": False, "health": False, "location": False, "phone": False, "contacts": False,
             "photos": False}
    monkeypatch.setattr(first, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(first, "_cache", {})
    monkeypatch.setattr(phone, "calendar_available", lambda: kinds["calendar"])
    monkeypatch.setattr(health, "available", lambda: kinds["health"])
    monkeypatch.setattr(location, "available", lambda: kinds["location"])
    monkeypatch.setattr(phone, "phone_available", lambda: kinds["phone"])
    monkeypatch.setattr(phone, "contacts_available", lambda: kinds["contacts"])
    monkeypatch.setattr(phone, "photos_available", lambda: kinds["photos"])
    return kinds


def test_nothing_shared_adds_nothing(shared):
    assert first.note() is None


def test_the_note_names_what_is_shared_and_says_to_use_it_first(shared):
    shared["calendar"] = shared["location"] = True
    note = first.note()
    assert note.startswith("[Conduit: the user's iPhone shares their calendar and reminders (conduit_calendar) and "
                           "their location (conduit_location) with you.")
    assert "use the Conduit tool first" in note
    assert "such as gog" in note
    assert "conduit_health" not in note


def test_every_kind_is_listed(shared):
    for kind in shared:
        shared[kind] = True
    note = first.note()
    for tool in ("conduit_calendar", "conduit_health", "conduit_location", "conduit_phone", "conduit_contacts",
                 "conduit_photos"):
        assert tool in note
    assert ", their health data (conduit_health), " in note


def test_a_turn_reuses_a_recent_look(shared):
    now = [100.0]
    assert first.note(clock=lambda: now[0]) is None
    shared["health"] = True
    now[0] += first.CACHE_S - 1
    assert first.note(clock=lambda: now[0]) is None
    now[0] += 1
    assert "conduit_health" in first.note(clock=lambda: now[0])


def test_a_failing_check_leaves_out_only_its_kind(shared, monkeypatch):
    def broken():
        raise RuntimeError("locked")
    monkeypatch.setattr(phone, "calendar_available", broken)
    assert first.note() is None
    first._cache.clear()
    shared["health"] = True
    note = first.note()
    assert "conduit_health" in note and "conduit_calendar" not in note


def test_the_cache_keeps_a_few_homes(shared, monkeypatch):
    homes = iter(range(first.MAX_HOMES + 5))
    monkeypatch.setattr(first, "get_hermes_home", lambda: f"/home/{next(homes)}")
    for _ in range(first.MAX_HOMES + 5):
        first.note()
    assert len(first._cache) == first.MAX_HOMES


def test_each_shared_tool_says_to_use_it_first():
    assert phone.CALENDAR_SCHEMA["description"].endswith(first.CALENDAR_FIRST)
    assert phone.CONTACTS_SCHEMA["description"].endswith(first.CONTACTS_FIRST)
    assert phone.PHONE_SCHEMA["description"].endswith(first.PHONE_FIRST)
    assert health.SCHEMA["description"].endswith(first.HEALTH_FIRST)
    assert location.SCHEMA["description"].endswith(first.LOCATION_FIRST)


def test_voice_calls_get_no_routing_sentence():
    # A live voice model has no other route to choose from.
    assert first.HEALTH_FIRST not in health.voice_description()
    assert first.LOCATION_FIRST not in location.voice_description()
    texts = [tool["description"] for tool in phone.voice_tools(_SharingStore()).values()]
    assert texts
    for sentence in (first.CALENDAR_FIRST, first.CONTACTS_FIRST, first.PHONE_FIRST):
        assert all(sentence not in text for text in texts)
    # Deleting is the phone's own tool too, so a call doesn't ask Hermes.
    calendar = phone.voice_tools(_SharingStore())[phone.CALENDAR_TOOL]["description"]
    assert "add or delete an event or reminder, use the phone's own add or remove tool" in calendar


class _SharingStore:
    """A phone store where one device shares everything voice can read."""

    def devices(self):
        return [{"sharing": {"calendar": "full", "reminders": True, "status": True, "focus": True, "alarms": True}}]
