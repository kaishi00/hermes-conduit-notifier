import importlib.util
import json
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


store_module = _load("conduit_push.present_store", ROOT / "present_store.py")
tool = _load("conduit_push.show_tool", ROOT / "show_tool.py")

PresentStore = store_module.PresentStore
PresentError = store_module.PresentError

PHONE = "phone-installation-1"
TABLE = "| Day | High |\n|---|---|\n| Mon | 20 |"


def _exchange(store, features=("show-cards",)):
    return store.exchange({"installation_id": PHONE, "device": "iPhone", "app_version": "2.0", "results": [],
                           "features": list(features)})


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(tmp_path, clock):
    return PresentStore(tmp_path, clock=clock)


def test_a_card_reaches_the_phone_until_it_expires(store, clock):
    _exchange(store)
    card = store.show("  Forecast ", f"\n{TABLE}\n\n", session_id="s1")
    [shown] = _exchange(store)["cards"]
    assert shown == card
    assert shown["title"] == "Forecast" and shown["markdown"] == TABLE and shown["session_id"] == "s1"
    assert _exchange(store)["cards"] == [shown], "every exchange lists it; the phone shows each card once"
    clock.now += store_module.CARD_TTL_S
    assert "cards" not in _exchange(store)
    assert json.loads(store.path.read_text())["cards"] == [], "an expired card isn't kept"


def test_a_card_reaches_only_a_phone_that_shows_cards(store):
    _exchange(store)
    store.show("Forecast", TABLE)
    other = store.exchange({"installation_id": "ipad-installation-2", "results": [], "features": ["maps-navigate"]})
    assert "cards" not in other
    assert len(_exchange(store)["cards"]) == 1


def test_a_phone_not_seen_for_hours_doesnt_count(store, clock):
    _exchange(store)
    clock.now += store_module.SHOW_DEVICE_RECENT_S
    assert store.can_show() is False
    with pytest.raises(PresentError) as error:
        store.show("Forecast", TABLE)
    assert error.value.code == "no_device"
    _exchange(store)
    assert store.can_show() is True


def test_only_a_phone_that_shows_cards_gets_them(store):
    with pytest.raises(PresentError) as error:
        store.show("Forecast", TABLE)
    assert error.value.code == "no_device"
    _exchange(store, features=("maps-navigate",))
    with pytest.raises(PresentError):
        store.show("Forecast", TABLE)
    _exchange(store)
    assert store.show("Forecast", TABLE)["id"]


def test_cards_are_checked_capped_and_rate_limited(store, clock):
    _exchange(store)
    for title, markdown, code in (("", TABLE, "no_title"), ("x" * 81, TABLE, "too_long"),
                                  ("Forecast", "  \n ", "no_markdown"), ("Forecast", "x" * 8001, "too_long"),
                                  (None, TABLE, "no_title"), ("Forecast", 5, "no_markdown")):
        with pytest.raises(PresentError) as error:
            store.show(title, markdown)
        assert error.value.code == code
    for index in range(store_module.CARDS_PER_HOUR):
        store.show(f"Card {index}", TABLE)
    with pytest.raises(PresentError) as error:
        store.show("One more", TABLE)
    assert error.value.code == "rate_limited"
    cards = _exchange(store)["cards"]
    assert len(cards) == store_module.MAX_CARDS and cards[-1]["title"] == f"Card {store_module.CARDS_PER_HOUR - 1}"
    clock.now += 3600
    assert store.show("Next hour", TABLE)


def test_a_hand_edited_card_is_cleaned_or_dropped(store, clock):
    _exchange(store)
    state = json.loads(store.path.read_text())
    state["cards"] = [
        {"id": "good-card-1", "title": "Ok\u0007", "markdown": "a\u0000b", "created_at": 1_000_000,
         "expires_at": 1_000_600},
        {"id": "x", "title": "Bad id", "markdown": "m", "created_at": 1, "expires_at": 1_000_600},
        {"id": "no-text-01", "title": "", "markdown": "m", "created_at": 1, "expires_at": 1_000_600},
        {"id": "year-long-1", "title": "Long", "markdown": "m", "created_at": 1_000_000, "expires_at": 40_000_000},
        "not a card",
    ]
    store.path.write_text(json.dumps(state))
    [card] = _exchange(store)["cards"]
    assert card["id"] == "good-card-1" and card["title"] == "Ok" and card["markdown"] == "a b"


@pytest.fixture()
def tool_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.client, "load_state", lambda: {"credential": "c"})
    return tmp_path


def _call(args, session_id="s1", **kwargs):
    return json.loads(tool.handle(args, session_id=session_id, **kwargs))


def test_the_tool_is_offered_once_a_phone_that_shows_cards_exchanged(tool_home, monkeypatch):
    assert tool.available() is False
    _exchange(PresentStore(tool_home), features=("maps-navigate",))
    assert tool.available() is False
    _exchange(PresentStore(tool_home))
    assert tool.available() is True
    monkeypatch.setattr(tool.client, "load_state", lambda: None)
    assert tool.available() is False


def test_show_keeps_the_card_for_the_phone(tool_home):
    _exchange(PresentStore(tool_home))
    answer = _call({"title": "Forecast", "markdown": TABLE})
    assert answer["ok"] is True and "on the user's screen" in answer["note"].lower()
    [card] = _exchange(PresentStore(tool_home))["cards"]
    assert card["id"] == answer["id"] and card["session_id"] == "s1"


def test_show_says_when_it_cant(tool_home):
    assert _call({"title": "Forecast", "markdown": TABLE})["error"] == "no_device"
    _exchange(PresentStore(tool_home))
    refused = _call({"title": "Forecast"})
    assert refused["ok"] is False and refused["error"] == "no_markdown"
    assert _call("not an object")["ok"] is False


def test_a_subagent_cannot_show(tool_home):
    _exchange(PresentStore(tool_home))
    answer = _call({"title": "Forecast", "markdown": TABLE}, session_id="child", is_child=lambda s: s == "child")
    assert answer["error"] == "subagent"
    assert "cards" not in _exchange(PresentStore(tool_home))


def test_a_broken_store_never_raises(tool_home, monkeypatch):
    def broken():
        raise OSError("disk")
    monkeypatch.setattr(tool, "_store", broken)
    assert _call({"title": "Forecast", "markdown": TABLE})["error"] == "unavailable"


def test_the_schema_matches_the_store():
    properties = tool.SCHEMA["parameters"]["properties"]
    assert properties["title"]["maxLength"] == store_module.MAX_CARD_TITLE_CHARS
    assert properties["markdown"]["maxLength"] == store_module.MAX_CARD_MARKDOWN_CHARS
    assert tool.SCHEMA["parameters"]["required"] == ["title", "markdown"]
    assert "voice call" in tool.SCHEMA["description"]
