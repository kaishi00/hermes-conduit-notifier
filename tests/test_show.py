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
    assert tool.SCHEMA["parameters"]["required"] == ["title"]
    assert "voice call" in tool.SCHEMA["description"]
    assert properties["cards"]["items"] == tool.card_schema.CARD_SCHEMA, "the card schema comes from the one file"


MAP = {"type": "map", "places": [{"name": "Kin Khao", "lat": 37.7853, "lon": -122.4089,
                                  "rating": {"value": 4.4}}], "theme": "dark"}
CLEAN_MAP = {"type": "map", "places": [{"name": "Kin Khao", "lat": 37.7853, "lon": -122.4089}]}


def test_cards_go_to_the_call_screen_and_come_back_normalized(tool_home):
    _exchange(PresentStore(tool_home))
    answer = _call({"title": "Dinner", "cards": [MAP]})
    assert answer["ok"] is True and answer["cards"] == [CLEAN_MAP], "the answer keeps them in the chat's history"
    [card] = _exchange(PresentStore(tool_home))["cards"]
    assert card["cards"] == [CLEAN_MAP]
    assert "**Kin Khao**" in card["markdown"] and "maps.apple.com" in card["markdown"], \
        "a screen that doesn't draw cards shows their Markdown"
    assert "4.4" not in card["markdown"], "an unsourced rating is shown nowhere"
    assert card["fallback"] is True, "a phone that draws the cards leaves their Markdown out"


def test_given_markdown_is_kept_beside_cards(tool_home):
    _exchange(PresentStore(tool_home))
    _call({"title": "Dinner", "markdown": TABLE, "cards": [MAP]})
    [card] = _exchange(PresentStore(tool_home))["cards"]
    assert card["markdown"] == TABLE and card["cards"] == [CLEAN_MAP]
    assert "fallback" not in card, "Markdown Hermes wrote shows beside the cards"


def test_cards_show_in_a_chat_without_a_call_screen(tool_home):
    assert tool.available() is False
    _exchange(PresentStore(tool_home), features=("maps-navigate", "chat-cards"))
    assert tool.available() is True, "a Conduit that draws chat cards gets the tool"
    answer = _call({"title": "Dinner", "cards": [MAP]})
    assert answer == {"ok": True, "cards": [CLEAN_MAP], "note": tool.IN_CHAT_NOTE}
    assert _call({"title": "Forecast", "markdown": TABLE})["error"] == "no_device", \
        "Markdown alone still needs a call screen"


def test_cards_that_are_all_invalid_are_refused(tool_home):
    _exchange(PresentStore(tool_home))
    answer = _call({"title": "Dinner", "cards": [{"type": "weather", "places": [{"name": "x"}]}]})
    assert answer["ok"] is False and answer["error"] == "invalid_cards"
    assert "cards" not in _exchange(PresentStore(tool_home))


STATS = {"type": "stats", "stats": [{"label": "Steps", "value": "8,412"}], "source": {"name": "Health"}}


def test_information_cards_go_only_to_a_conduit_that_draws_them(tool_home):
    _exchange(PresentStore(tool_home), features=("chat-cards",))
    answer = _call({"title": "Today", "cards": [STATS]})
    assert answer["ok"] is False and answer["error"] == "unsupported_cards", "an older Conduit would draw nothing"
    answer = _call({"title": "Today", "cards": [STATS, MAP]})
    assert answer["ok"] is True and answer["cards"] == [CLEAN_MAP], "the cards it can draw still go"
    assert "stats card(s) weren't shown" in answer["note"], "Hermes hears what was left out"
    answer = _call({"title": "Today", "markdown": "**Steps** 8,000", "cards": [STATS]})
    assert answer["error"] == "no_device", "Markdown Hermes wrote still goes to a call screen, without the cards"
    _exchange(PresentStore(tool_home), features=("chat-cards", "info-cards"))
    answer = _call({"title": "Today", "cards": [STATS]})
    assert answer == {"ok": True, "cards": [STATS], "note": tool.IN_CHAT_NOTE}


def test_a_phone_without_information_cards_gets_only_what_it_draws(store):
    _exchange(store, features=("show-cards", "info-cards"))
    store.show("Today", TABLE, cards=[STATS, CLEAN_MAP], markdown_is_fallback=True)
    store.show("Steps", TABLE, cards=[STATS], markdown_is_fallback=True)
    old = {"installation_id": "older-phone", "device": "iPhone", "app_version": "1.9", "results": [],
           "features": ["show-cards"]}
    mixed, info_only = store.exchange(old)["cards"]
    assert mixed["cards"] == [CLEAN_MAP], "an older build gets the cards it draws"
    assert "fallback" not in mixed, "and the Markdown, which covers what was left out"
    assert "cards" not in info_only and "fallback" not in info_only, "and the Markdown when nothing else is left"
    assert [card["cards"] for card in _exchange(store, features=("show-cards", "info-cards"))["cards"]] == \
        [[STATS, CLEAN_MAP], [STATS]]


def test_cards_are_refused_where_conduit_isnt_drawing_them(tool_home, monkeypatch):
    _exchange(PresentStore(tool_home), features=("chat-cards",))
    monkeypatch.setattr(tool, "_turn_source", lambda: "messaging")
    answer = _call({"title": "Dinner", "cards": [MAP]})
    assert answer["ok"] is False and answer["error"] == "not_conduit"
    monkeypatch.setattr(tool, "_turn_source", lambda: "conduit")
    assert _call({"title": "Dinner", "cards": [MAP]})["ok"] is True


def test_long_fallback_markdown_is_cut_at_a_line(tool_home, monkeypatch):
    monkeypatch.setattr(tool.card_schema, "fallback_markdown", lambda cards: ("line\n" * 3000))
    text = tool._fallback([CLEAN_MAP])
    assert len(text) <= store_module.MAX_CARD_MARKDOWN_CHARS and text.endswith("line")


def test_a_card_on_disk_keeps_only_valid_rich_cards(store):
    _exchange(store)
    store.show("Dinner", TABLE, cards=[CLEAN_MAP])
    state = json.loads(store.path.read_text())
    state["cards"][0]["cards"].append({"type": "weather"})
    state["cards"][0]["fallback"] = "yes"
    store.path.write_text(json.dumps(state))
    [card] = _exchange(store)["cards"]
    assert card["cards"] == [CLEAN_MAP]
    assert "fallback" not in card, "only a true flag is kept"


def test_a_turn_source_lookup_that_fails_draws_no_cards(tool_home, monkeypatch):
    _exchange(PresentStore(tool_home), features=("chat-cards",))

    def broken(*args):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(tool.reply_sources, "turn_source", broken)
    assert _call({"title": "Dinner", "cards": [MAP]})["error"] == "not_conduit"


def test_cards_are_left_out_when_the_schema_is_missing(store, monkeypatch):
    monkeypatch.setattr(store_module, "_card_schema_module", store_module._CARD_SCHEMA_MISSING)
    _exchange(store)
    card = store.show("Dinner", TABLE, cards=[CLEAN_MAP])
    assert "cards" not in card
    [shown] = _exchange(store)["cards"]
    assert shown["markdown"] == TABLE, "everything else still works"
