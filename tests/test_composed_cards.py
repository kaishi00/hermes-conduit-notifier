"""Composed cards, buttons and fields on any card, taps heard by Hermes, and
saved card templates (issue 1946): card_schema, card_actions,
card_templates, show_tool and POST /boards/taps."""

import contextlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

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
card_schema = tool.card_schema
card_templates = tool.card_templates
card_actions = _load("conduit_push.card_actions", ROOT / "card_actions.py")

PHONE = "phone-installation-1"
FEATURES = ("chat-cards", "info-cards", "media-cards", "live-cards", "board-cards", "composed-cards")

EMAIL = {
    "type": "composed", "title": "Email to Mei", "live": "email-mei",
    "sections": [
        {"items": [{"kind": "badge", "text": "Not sent", "tone": "warning"}]},
        {"layout": "row", "items": [{"kind": "field", "id": "to", "input": "text", "label": "To",
                                     "value": "mei@example.com"},
                                    {"kind": "field", "id": "subject", "input": "text", "label": "Subject",
                                     "value": "Friday"}]},
        {"items": [{"kind": "field", "id": "body", "input": "text", "lines": 6, "value": "See you Friday."}]}],
    "actions": [{"label": "Send", "action": "tap", "id": "send", "style": "primary", "confirm": "Send to Mei?"},
                {"label": "Discard", "action": "tap", "id": "discard", "style": "destructive"}]}


def _exchange(home, features=FEATURES):
    store_module.PresentStore(home).exchange({"installation_id": PHONE, "device": "iPhone", "app_version": "2.0",
                                              "results": [], "features": list(features)})


@pytest.fixture()
def tool_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.client, "load_state", lambda: {"credential": "c"})
    monkeypatch.setattr(tool, "_turn_source", lambda: "conduit")
    monkeypatch.setattr(tool.turn_surface, "turn_entry", lambda session: "typed")
    return tmp_path


def _call(args, session_id="s1", **kwargs):
    return json.loads(tool.handle(args, session_id=session_id, **kwargs))


# --- the card and its Markdown --------------------------------------------------

def test_a_composed_card_reads_as_markdown_with_its_buttons_as_replies():
    [card] = card_schema.normalize_cards([{**EMAIL, "actions": [
        *EMAIL["actions"], {"label": "Open Gmail", "action": "open", "target": "https://mail.google.com/"},
    ]}])
    text = card_schema.fallback_markdown([card])
    assert text.startswith("### Email to Mei\n\n[Not sent]")
    assert "**To:** mei@example.com · **Subject:** Friday" in text, "a row reads on one line"
    assert "[Open Gmail](https://mail.google.com/)" in text
    assert "Reply _Send_ or _Discard_" in text, "a tap is a reply the user can type instead"


def test_any_card_carries_buttons_and_its_markdown_shows_them():
    [card] = card_schema.normalize_cards([{"type": "callout", "callout": {"text": "Your vet visit is due."},
                                           "actions": [{"label": "Call", "action": "open",
                                                        "target": "tel:+14155550100"},
                                                       {"label": "Copy code", "action": "copy",
                                                        "target": "VET-2231"}]}])
    assert len(card["actions"]) == 2
    text = card_schema.fallback_markdown([card])
    assert "Call: tel:+14155550100" in text and "Copy code: VET-2231" in text


@pytest.mark.parametrize("name", ["password", "Your Passcode", "api_key", "apiKey", "OTP", "PIN", "card-cvv",
                                  "One time code", "auth token", "client_secret",
                                  "SSH private key", "Recovery key", "Verification code", "license_key",
                                  "Passphrase", "TOTP", "Master key", "App credentials",
                                  "2FA code", "Two-factor code", "Login code",
                                  "パスワード", "Passwort", "Contraseña", "Mot de passe", "Пароль", "Kata sandi",
                                  "비밀번호를 입력", "密码", "Access code", "Unlock code", "Security key", "Reset code",
                                  "Recovery code", "Activation code", "Auth key", "Verification key",
                                  "Bestätigungscode", "Código de verificación", "Code de vérification",
                                  "Kode verifikasi", "Doğrulama kodu", "Код подтверждения"])
def test_a_field_never_asks_for_a_secret(name):
    assert card_schema.names_secret(name)


@pytest.mark.parametrize("name", ["Spin class", "Subject", "Opinion", "Pinned note", "to", "Topic", "Key dates",
                                  "Zip code", "メモ", "Betreff", "Тема"])
def test_ordinary_fields_are_not_secrets(name):
    assert not card_schema.names_secret(name)


def test_without_actions_keeps_only_the_buttons_asked_for():
    [card] = card_schema.normalize_cards([{**EMAIL, "actions": [
        *EMAIL["actions"], {"label": "Open Gmail", "action": "open", "target": "https://mail.google.com/"}]}])
    kept = card_schema.without_actions(card, keep=("open", "copy"))
    assert [button["label"] for button in kept["actions"]] == ["Open Gmail"]
    assert not any(element["kind"] == "field" for section in kept["sections"] for element in section["items"])
    assert card_schema.has_actions(card) and not card_schema.has_actions(card_schema.without_actions(card))
    only_fields = {"type": "composed", "sections": [{"items": [{"kind": "field", "id": "a", "input": "text"}]}]}
    assert card_schema.without_actions(only_fields) is None, "nothing else to show"


# --- conduit_show ---------------------------------------------------------------

def test_conduit_show_draws_a_composed_card_with_its_buttons(tool_home):
    _exchange(tool_home)
    answer = _call({"title": "Mei", "cards": [EMAIL]})
    assert answer["ok"] is True and answer["cards"][0]["type"] == "composed"
    assert [button["id"] for button in answer["cards"][0]["actions"]] == ["send", "discard"]


def test_an_older_conduit_gets_no_composed_card_and_no_buttons(tool_home):
    _exchange(tool_home, features=FEATURES[:-1])
    answer = _call({"title": "Vet", "cards": [EMAIL, {"type": "callout", "callout": {"text": "Due"},
                                                      "actions": [{"label": "Book", "action": "tap", "id": "b"}]}]})
    assert [card["type"] for card in answer["cards"]] == ["callout"]
    assert "actions" not in answer["cards"][0], "a tap that can't come is never offered"
    assert tool.OLD_ACTIONS_NOTE in answer["problems"] and "composed" in answer["note"]


def test_a_room_keeps_open_and_copy_buttons_only(tool_home):
    _exchange(tool_home, features=(*FEATURES, tool.ROOM_BOARDS_FEATURE))
    kept = []
    card = {**EMAIL, "actions": [*EMAIL["actions"], {"label": "Open", "action": "open",
                                                    "target": "https://mail.google.com/"}]}
    answer = _call({"title": "Mei", "cards": [card]}, session_id="room-1", is_room=lambda s: s == "room-1",
                   room_board=lambda session, board: kept.append(board) or True)
    [shown] = answer["cards"]
    assert [button["label"] for button in shown["actions"]] == ["Open"]
    assert all(element["kind"] != "field" for section in shown["sections"] for element in section["items"])
    assert answer["problems"][0] == tool.ROOM_ACTIONS_NOTE and kept


# --- templates ------------------------------------------------------------------

AGENDA = {"type": "composed", "sections": [
    {"items": [{"kind": "text", "text": "{{day}}", "role": "heading"}]},
    {"repeat": "events", "layout": "row", "items": [{"kind": "text", "text": "{{events.time}}", "role": "mono"},
                                                     {"kind": "text", "text": "{{events.what}}"}]},
    {"items": [{"kind": "progress", "progress": "{{busy}}", "label": "Booked"}]}]}


def test_a_saved_template_fills_by_name_with_repeat_sections(tool_home):
    _exchange(tool_home)
    saved = _call({"title": "Agenda", "save_templates": [{"name": "Day-Agenda", "about": "A day", "card": AGENDA}]})
    assert saved == {"ok": True, "saved": ["day-agenda: saved"], "note": tool.SAVED_NOTE}
    answer = _call({"title": "Today", "cards": [{"type": "template", "live": "today", "title": "Friday",
                                                  "template": {"name": "day-agenda", "fill": {
                                                      "day": "Fri 8 Nov", "busy": 0.5,
                                                      "events": [{"time": "09:00", "what": "Standup"},
                                                                 {"time": "13:00", "what": "Dentist"}]}}}]})
    [card] = answer["cards"]
    assert card["type"] == "composed" and card["live"] == "today" and card["title"] == "Friday"
    texts = [element.get("text") for section in card["sections"] for element in section["items"]]
    assert texts == ["Fri 8 Nov", "09:00", "Standup", "13:00", "Dentist", None]
    assert card["sections"][-1]["items"][0]["progress"] == 0.5, "a whole-slot number stays a number"
    assert "repeat" not in json.dumps(card), "Conduit never sees a template"


def test_a_missing_slot_or_template_is_named(tool_home):
    _exchange(tool_home)
    _call({"title": "Agenda", "save_templates": [{"name": "agenda", "card": AGENDA}]})
    answer = _call({"title": "Today", "cards": [{"type": "template", "template": {"name": "agenda",
                                                                                  "fill": {"day": "Fri"}}}]})
    assert answer["ok"] is True
    assert any("fill.events: not given" in line for line in answer["problems"])
    missing = _call({"title": "Today", "cards": [{"type": "template", "template": {"name": "nope"}}]})
    assert missing["error"] == "invalid_cards"
    assert any("saved: agenda" in line for line in missing["problems"])


def test_templates_are_checked_when_saved_and_can_be_deleted(tool_home):
    store = card_templates.TemplateStore(tool_home)
    notes = []
    assert store.save([{"name": "bad name!", "card": AGENDA}, {"name": "map", "card": {"type": "map"}},
                       {"name": "empty", "card": {"type": "composed", "sections": [
                           {"items": [{"kind": "image", "url": "http://a.test/p.png"}]}]}}], notes) == []
    assert any("lowercase letters" in line for line in notes)
    assert any("a template is a composed card" in line for line in notes)
    assert any("no composed card is left" in line for line in notes)
    assert store.save([{"name": "agenda", "card": AGENDA}], []) == ["agenda: saved"]
    assert store.names() == ["agenda"]
    assert store.save([{"name": "agenda"}], []) == ["agenda: deleted"]
    assert store.names() == []


def test_the_drawing_rule_lists_saved_templates(tool_home):
    _exchange(tool_home)
    assert "templates" not in tool.board_rule("conduit")
    card_templates.TemplateStore(tool_home).save([{"name": "email-draft", "card": EMAIL}], [])
    assert tool.board_rule("conduit").endswith(tool.TEMPLATES_RULE.format(names="email-draft"))


def test_templates_keep_the_newest_fifty(tool_home):
    store = card_templates.TemplateStore(tool_home)
    for start in range(0, 52, 4):
        store.save([{"name": f"t{index}", "card": AGENDA} for index in range(start, min(start + 4, 52))], [])
    names = store.names()
    assert len(names) == card_templates.MAX_TEMPLATES and names[0] == "t2" and names[-1] == "t51"


# --- taps -----------------------------------------------------------------------

def test_a_tap_is_heard_once_with_the_card_and_its_fields(tmp_path):
    taps = card_actions.TapStore(tmp_path)
    answer = taps.save({"sessions": ["chat-1", "alias-1"], "card": EMAIL, "action": "send",
                        "values": {"to": "mei@example.com", "subject": "Friday!", "body": "See you",
                                   "unknown": "dropped"}})
    assert answer["ok"] is True
    note = taps.note("alias-1", "Send · Email to Mei")
    assert '"Send" (tap send) on "Email to Mei" (live name email-mei)' in note
    assert "OK for exactly what the card showed" in note
    assert "subject: Friday!" in note and "unknown" not in note
    assert "### Email to Mei" in note, "the card the user saw"
    assert taps.note("chat-1", "Send · Email to Mei") is None, "heard under one id, heard under them all"


def test_a_tap_must_name_one_of_the_cards_tap_buttons(tmp_path):
    taps = card_actions.TapStore(tmp_path)
    for body in ({"sessions": ["c"], "card": EMAIL, "action": "nope"},
                 {"sessions": ["c"], "card": {"type": "map"}, "action": "send"},
                 {"sessions": [], "card": EMAIL, "action": "send"}, "x"):
        with pytest.raises(card_actions.TapError):
            taps.save(body)


def test_an_unheard_tap_is_forgotten(tmp_path):
    clock = [1000.0]
    taps = card_actions.TapStore(tmp_path, clock=lambda: clock[0])
    taps.save({"sessions": ["c"], "card": EMAIL, "action": "discard"})
    clock[0] += card_actions.KEEP_S
    assert taps.note("c", "Discard") is None


_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_taps", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


@pytest.fixture()
def route_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    api.boards._tap_limiter._mints.clear()
    return tmp_path


def _http():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_the_tap_route_keeps_a_tap_for_the_next_turn(route_home):
    http = _http()
    response = http.post(f"{BASE}/boards/taps", json={"sessions": ["chat-1"], "card": EMAIL, "action": "send",
                                                      "values": {"to": "mei@example.com"}})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert response.headers["cache-control"] == "no-store"
    assert "tap send" in card_actions.TapStore(route_home).note("chat-1", "Send")
    assert http.post(f"{BASE}/boards/taps", json={"sessions": ["chat-1"], "card": EMAIL,
                                                  "action": "x"}).status_code == 400
    assert http.post(f"{BASE}/boards/taps", json=[1]).status_code == 400


def test_card_taps_are_a_capability():
    assert "card-taps" in api.capabilities.ROUTE_CAPABILITIES


def test_a_taps_note_says_when_part_of_the_card_was_folded(tmp_path):
    card = {"type": "composed", "title": "Folded", "sections": [
        {"items": [{"kind": "text", "text": "Shown"}]},
        {"more": True, "items": [{"kind": "text", "text": "Hidden"}]},
    ], "actions": [{"label": "Go", "action": "tap", "id": "go"}]}
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "go"})
    assert "folded under Show more" in store.note("s1", "Go")


def test_a_tap_button_whose_label_isnt_a_plain_message_is_dropped():
    notes = []
    cards = card_schema.normalize_cards([{"type": "callout", "callout": {"text": "Hi"}, "actions": [
        {"label": "/reset", "action": "tap", "id": "r"}, {"label": "Go", "action": "tap", "id": "g"}]}], notes)
    assert [button["label"] for button in cards[0]["actions"]] == ["Go"]
    assert any("plain message" in note for note in notes)


def test_a_taps_number_out_of_bounds_goes_empty(tmp_path):
    card = {"type": "composed", "title": "Seats", "sections": [{"items": [
        {"kind": "field", "id": "seats", "input": "number", "min": 1, "max": 8, "value": "2"}]}],
        "actions": [{"label": "Book", "action": "tap", "id": "book"}]}
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "book", "values": {"seats": "12"}})
    assert "seats: (empty)" in store.note("s1", "Book · Seats")


def test_a_tap_waits_for_the_users_message_about_it(tmp_path):
    card = {"type": "callout", "title": "Vet", "callout": {"text": "Due"},
            "actions": [{"label": "Book it", "action": "tap", "id": "book"}]}
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "book"})
    assert store.note("s1", "Here's your daily report") is None
    assert "Book it" in store.note("s1", "Book it · Vet")
    assert store.note("s1", "Book it · Vet") is None


def test_a_taps_note_closes_the_fence_a_mono_element_leaves_open(tmp_path):
    card = {"type": "composed", "title": "Log", "sections": [
        {"items": [{"kind": "text", "text": "\n".join(["line ```` x"] * 400), "role": "mono"}]}],
        "actions": [{"label": "Go", "action": "tap", "id": "go"}]}
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "go"})
    markdown = store.note("s1", "Go").split("The card they saw:\n", 1)[1]
    assert not markdown.rstrip("]").rstrip().endswith("\n`````"), "inline code is no fence to close"
    assert card_actions._open_fence("````` a ```` b `````\n```\ncode") == "```"


def test_a_clipped_cards_markdown_reads_a_mono_line_as_no_fence():
    limit = store_module.MAX_CARD_MARKDOWN_CHARS
    text = ("x" * 99 + "\n") * (limit // 100 - 1) + "```` a `` b ````\n" + "y" * 500
    clipped = tool.clip_markdown(text)
    assert clipped.endswith("```` a `` b ````"), "cut after the mono line, no fence added"
    assert store_module._open_fence(clipped) is None
    assert store_module._open_fence("```` a `` b ````\n```\ncode") == "```"


def test_a_turn_with_no_message_leaves_the_taps_waiting(tmp_path):
    card = {"type": "callout", "callout": {"text": "Due"}, "actions": [{"label": "Go", "action": "tap", "id": "go"}]}
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "go"})
    assert store.note("s1", None) is None
    assert store.note("s1", "Go") is not None


def test_one_message_hears_one_tap(tmp_path):
    clock = iter([100.0, 200.0, 300.0, 400.0])
    store = card_actions.TapStore(tmp_path, clock=lambda: next(clock, 500.0))
    for label, ident in (("Send", "a"), ("Send to all", "b")):
        store.save({"sessions": ["s1"], "card": {"type": "callout", "callout": {"text": "x"},
                    "actions": [{"label": label, "action": "tap", "id": ident}]}, "action": ident})
    note = store.note("s1", "Send to all · Card")
    assert "(tap b)" in note and "(tap a)" not in note
    assert "(tap a)" in store.note("s1", "Send")


def test_a_repeat_section_fills_the_cards_own_slots_too():
    template = {"type": "composed", "title": "Agenda", "sections": [
        {"repeat": "events", "items": [{"kind": "text", "text": "{{day}}: {{events.time}} {{events.what}}"}]}]}
    notes = []
    card = card_templates.expand_template(template, {"day": "Mon", "events": [{"time": "9", "what": "Vet"}]},
                                          notes, "t")
    assert card["sections"][0]["items"][0]["text"] == "Mon: 9 Vet" and notes == []


def test_a_slot_the_fill_leaves_out_says_its_element_may_go():
    template = {"type": "composed", "title": "{{title}}", "sections": [
        {"items": [{"kind": "text", "text": "{{note}}"}, {"kind": "field", "id": "n", "input": "text",
                                                         "value": "{{start}}"}]}]}
    notes = []
    card_templates.expand_template(template, {"title": "T", "start": ["a"]}, notes, "t")
    assert "t.fill.note: not given, left empty (an element left with nothing is left out)" in notes
    assert "t.fill.start: a list, which this slot can't take, left empty (an element left with nothing is left out)" \
        in notes


def test_a_field_whose_placeholder_asks_for_a_secret_is_dropped():
    notes = []
    cards = card_schema.normalize_cards([{"type": "composed", "title": "Notes", "sections": [{"items": [
        {"kind": "field", "id": "notes", "input": "text", "placeholder": "Paste your API key"},
        {"kind": "field", "id": "topic", "input": "text", "placeholder": "What it's about"}]}]}], notes)
    fields = [item["id"] for item in cards[0]["sections"][0]["items"]]
    assert fields == ["topic"] and any("never asks for a password" in line for line in notes)


def test_a_taps_note_clips_a_card_over_the_cap(tmp_path):
    card = {"type": "composed", "title": "Form", "sections": [{"items": [
        {"kind": "field", "id": f"f{n}", "input": "text", "lines": 4, "value": "word " * 390} for n in range(5)]}],
        "actions": [{"label": "Send", "action": "tap", "id": "send"}]}
    full = card_schema.fallback_markdown(card_schema.normalize_cards([card], []))
    assert len(full) > card_actions.MAX_CARD_MARKDOWN_CHARS
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "send"})
    note = store.note("s1", "Send · Form")
    assert note and len(note) < len(full)
    markdown = note.split("The card they saw:\n", 1)[1]
    assert len(markdown) <= card_actions.MAX_CARD_MARKDOWN_CHARS + 200 and note.endswith("]")


def test_a_taps_note_cut_inside_a_diff_closes_its_fence(tmp_path):
    before = "".join(f"line {n} {'a' * 160}\n" for n in range(80))
    after = "".join(f"line {n} {'b' * 160}\n" for n in range(80))
    card = {"type": "diff", "title": "Patch", "diff": {"before": before, "after": after, "name": "a.txt"},
            "actions": [{"label": "Apply", "action": "tap", "id": "apply"}]}
    full = card_schema.fallback_markdown(card_schema.normalize_cards([card], []))
    assert len(full) > card_actions.MAX_CARD_MARKDOWN_CHARS and full.count("```") >= 2
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": card, "action": "apply"})
    markdown = store.note("s1", "Apply · Patch").split("The card they saw:\n", 1)[1].rsplit("]", 1)[0]
    assert len(markdown) <= card_actions.MAX_CARD_MARKDOWN_CHARS
    assert markdown.rstrip().endswith("```") and card_actions._open_fence(markdown) is None


def test_a_number_filled_into_a_text_slot_is_shown_as_text():
    template = {"type": "composed", "title": "Count", "sections": [{"items": [
        {"kind": "text", "text": "{{count}}"}, {"kind": "progress", "progress": "{{busy}}"}]}]}
    notes = []
    card = card_templates.expand_template(template, {"count": 3, "busy": 0.5}, notes, "t")
    items = card["sections"][0]["items"]
    assert items[0]["text"] == "3" and items[1]["progress"] == 0.5 and notes == []


def test_a_room_drawn_by_a_conduit_without_composed_cards_leaves_them_out(tool_home):
    _exchange(tool_home, features=(*FEATURES[:-1], tool.ROOM_BOARDS_FEATURE))
    room = {"session_id": "room-1", "is_room": lambda s: s == "room-1", "room_board": lambda *_: True}
    callout = {"type": "callout", "callout": {"text": "Due"}}
    answer = _call({"title": "Mei", "cards": [EMAIL, callout]}, **room)
    assert [card["type"] for card in answer["cards"]] == ["callout"]
    assert answer["problems"][0] == tool.LEFT_OUT_NOTE.format(kinds="composed")
    alone = _call({"title": "Mei", "cards": [EMAIL]}, **room)
    assert alone["ok"] is False and alone["error"] == "unsupported_cards"


def test_a_text_fields_lines_reach_hermes_as_written(tmp_path):
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": EMAIL, "action": "send",
                "values": {"body": "Hi Mei,\r\n\r\nSee you Friday.\n\n—Alex", "subject": "x" * 2100}})
    note = store.note("s1", "Send · Email to Mei")
    assert 'body: "Hi Mei,\\n\\nSee you Friday.\\n\\n—Alex"' in note
    assert "… (cut at 2,000 characters)" in note


@pytest.mark.parametrize("text", ["```\ncode", "```` a `` b ````", "```` a `` b ````\n```\ncode", "```py\nx\n```",
                                  "````\n```\n````", "plain", "``` x ```\n```"])
def test_the_answers_and_the_tap_notes_fence_reading_agree(text):
    assert store_module._open_fence(text) == card_schema.open_fence(text)


def test_a_message_hears_the_tap_on_the_card_it_names(tmp_path):
    clock = iter([100.0, 200.0])
    store = card_actions.TapStore(tmp_path, clock=lambda: next(clock, 300.0))
    for title, ident in (("Card A", "a"), ("Card B", "b")):
        store.save({"sessions": ["s1"], "card": {"type": "callout", "title": title, "callout": {"text": "x"},
                    "actions": [{"label": "Go", "action": "tap", "id": ident}]}, "action": ident})
    note = store.note("s1", "Go · Card A")
    assert '"Card A"' in note and '"Card B"' not in note, "the newer Go on Card B isn't this message's"
    assert '"Card B"' in store.note("s1", "Go · Card B")


def test_a_tap_note_on_a_card_with_no_live_name_says_so(tmp_path):
    store = card_actions.TapStore(tmp_path)
    store.save({"sessions": ["s1"], "card": {"type": "callout", "title": "Vet", "callout": {"text": "x"},
                "actions": [{"label": "Go", "action": "tap", "id": "go"}]}, "action": "go"})
    note = store.note("s1", "Go · Vet")
    assert "same live name" not in note and "it had no live name" in note


def test_a_repeat_section_keeps_to_the_cards_eight_sections():
    template = {"type": "composed", "title": "Day", "sections": [
        {"items": [{"kind": "text", "text": "Today"}]},
        {"repeat": "events", "items": [{"kind": "text", "text": "{{events.time}}"}]},
        {"items": [{"kind": "text", "text": "End"}]}]}
    notes = []
    events = [{"time": str(hour)} for hour in range(12)]
    card = card_templates.expand_template(template, {"events": events}, notes, "t")
    assert len(card["sections"]) == card_templates.MAX_SECTIONS
    assert card["sections"][-1]["items"][0]["text"] == "End", "the template's own last section keeps its place"
    assert any("6 of its 12 items fit" in note for note in notes)
    schema = json.loads((ROOT / "cards" / "card-schema.json").read_text())
    assert json.dumps(schema).count('"maxItems": 8') >= 1


def test_a_repeat_item_that_isnt_an_object_is_named():
    template = {"type": "composed", "title": "Day", "sections": [
        {"repeat": "events", "items": [{"kind": "text", "text": "{{events.time}}"}]}]}
    notes = []
    card_templates.expand_template(template, {"events": ["9:00", {"time": "13:00"}]}, notes, "t")
    assert "t.fill.events: item 1 is text, not an object of its section's slots, so it was left out" in notes
    assert not any("not given" in note for note in notes)


@pytest.mark.parametrize("label", ["인증코드", "인증 코드", "인증번호"])
def test_a_korean_verification_code_field_is_dropped(label):
    assert card_schema.names_secret(label)
