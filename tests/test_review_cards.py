"""Review cards: options to look at and mark (a quick pick or a design review
in rounds), kept on the host (review_store), shown by conduit_show
(show_tool), marked with a tap (card_actions), read back over
GET /boards/reviews, and in Bot Mode bound to one workstream for every round
(Eric, 2026-11-08)."""

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
review_store = _load("conduit_push.review_store", ROOT / "review_store.py")
card_actions = _load("conduit_push.card_actions", ROOT / "card_actions.py")
card_schema = tool.card_schema

PHONE = "phone-installation-1"
FEATURES = ("chat-cards", "info-cards", "media-cards", "live-cards", "board-cards", "composed-cards", "review-cards")
PAGE = "<!doctype html><html><body><h1>Option</h1></body></html>"


def _exchange(home, features=FEATURES):
    store_module.PresentStore(home).exchange({"installation_id": PHONE, "device": "iPhone", "app_version": "2.0",
                                              "results": [], "features": list(features)})


class Host(tool.ReviewHost):
    def __init__(self, root, *, workstreams=(), bot_chats=(), inside=()):
        self.root = root
        self.workstreams = set(workstreams)
        self.bot_chats = set(bot_chats)
        self.inside = set(inside)

    def store(self):
        return review_store.KeptReviews(self.root)

    def in_workstream(self, session):
        return session in self.inside

    def is_bot_chat(self, session):
        return session in self.bot_chats

    def workstream(self, ref):
        return ref in self.workstreams


@pytest.fixture()
def tool_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(tool.client, "load_state", lambda: {"credential": "c"})
    monkeypatch.setattr(tool, "_turn_source", lambda: "conduit")
    monkeypatch.setattr(tool.turn_surface, "turn_entry", lambda session: "typed")
    _exchange(tmp_path)
    return tmp_path


def _call(args, session_id="s1", **kwargs):
    return json.loads(tool.handle(args, session_id=session_id, **kwargs))


def _review(decisions=None, **extra):
    return {"type": "review", "title": "Recipe app home", "review": {"decisions": decisions or [
        {"title": "Home layout", "options": [{"label": "Cards", "html": PAGE, "recommended": True},
                                             {"label": "List", "html": PAGE},
                                             {"label": "Photo", "image": "https://img.test/photo.png"}]},
        {"title": "Save button colour", "options": [{"label": "Amber", "html": PAGE},
                                                    {"label": "Green", "html": PAGE}]}], **extra}}


# --- conduit_show ---------------------------------------------------------------

def test_a_review_keeps_its_pages_on_the_host_and_the_card_carries_labels(tool_home):
    host = Host(tool_home)
    answer = _call({"cards": [_review()]}, reviews=host)
    assert answer["ok"], answer
    [card] = answer["cards"]
    review = card["review"]
    assert review_store.REVIEW_ID.fullmatch(review["id"]) and review["round"] == 1
    assert [d["id"] for d in review["decisions"]] == ["d1", "d2"]
    assert [o["id"] for o in review["decisions"][0]["options"]] == ["A", "B", "C"]
    assert review["decisions"][0]["options"][2] == {"id": "C", "label": "Photo", "kind": "image",
                                                    "url": "https://img.test/photo.png"}
    assert "html" not in json.dumps(card), "a page stays on the host"
    kept = host.store().read(review["id"])
    assert kept["pages"]["d1/A"] == PAGE and set(kept["pages"]) == {"d1/A", "d1/B", "d2/A", "d2/B"}
    assert review["id"] in answer["note"], "the answer names the id to show the next round under"


def test_a_page_file_and_a_picture_file_are_read_on_the_host(tool_home, tmp_path):
    page = tmp_path / "a.html"
    page.write_text(PAGE, encoding="utf-8")
    picture = tmp_path / "b.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    host = Host(tool_home)
    answer = _call({"cards": [_review([{"title": "Poster", "options": [{"label": "A", "file": str(page)},
                                                                       {"label": "B", "file": str(picture)}]}])]},
                   reviews=host)
    review = answer["cards"][0]["review"]
    pages = host.store().read(review["id"])["pages"]
    assert pages["d1/A"] == PAGE and "data:image/png;base64," in pages["d1/B"]


def test_an_option_that_cant_be_shown_is_named_and_a_decision_needs_two(tool_home):
    host = Host(tool_home)
    answer = _call({"cards": [_review([{"title": "Poster", "options": [{"label": "A", "html": PAGE},
                                                                       {"label": "B", "file": "/nope/b.html"}]},
                                       {"title": "Chart", "options": [{"label": "Bars", "html": PAGE},
                                                                      {"label": "Line", "html": PAGE}]}])]},
                   reviews=host)
    assert answer["ok"]
    assert [d["title"] for d in answer["cards"][0]["review"]["decisions"]] == ["Chart"]
    assert any("no such file" in problem for problem in answer["problems"])
    assert any("needs two options" in problem for problem in answer["problems"])


def test_the_next_round_keeps_the_review_and_lists_what_was_settled(tool_home):
    host = Host(tool_home)
    first = _call({"cards": [_review()]}, reviews=host)["cards"][0]["review"]
    host.store().save_marks(first["id"], 1, {"decisions": [{"decision": "d1", "mark": "change", "option": "B"},
                                                          {"decision": "d2", "mark": "approve", "option": "A"}]},
                            ["s1"])
    second = _call({"cards": [_review([{"id": "d1", "title": "Home layout", "options": [
        {"label": "List, roomier", "html": PAGE}, {"label": "List, photos", "html": PAGE}]}], id=first["id"])]},
        reviews=host)["cards"][0]["review"]
    assert second["id"] == first["id"] and second["round"] == 2
    assert second["settled"] == [{"title": "Save button colour", "mark": "approve", "choice": "Amber", "round": 1}]


def test_an_unknown_review_id_is_refused(tool_home):
    answer = _call({"cards": [_review(id="rv-0000000000000000")]}, reviews=Host(tool_home))
    assert not answer["ok"] and any("no review rv-0000000000000000" in p for p in answer["problems"])


@pytest.mark.parametrize("where", ["room", "workstream", "spoken", "no host"])
def test_a_review_is_shown_only_in_a_typed_chat_or_bot_chat(tool_home, monkeypatch, where):
    host = Host(tool_home, inside={"s1"} if where == "workstream" else ())
    kwargs = {"reviews": None if where == "no host" else host}
    if where == "room":
        kwargs["is_room"] = lambda session: True
    if where == "spoken":
        monkeypatch.setattr(tool.turn_surface, "turn_entry", lambda session: "spoken")
    answer = _call({"cards": [_review()]}, **kwargs)
    assert not answer.get("cards")
    note = {"room": tool.ROOM_REVIEW_NOTE, "workstream": tool.WORKSTREAM_REVIEW_NOTE,
            "spoken": tool.SPOKEN_REVIEW_NOTE, "no host": tool.NO_REVIEWS_NOTE}[where]
    assert note in answer["problems"]


# --- Bot Mode: one review, one workstream, every round ----------------------------

def test_a_bot_chat_shows_a_review_for_its_workstream(tool_home):
    host = Host(tool_home, workstreams={"W3"}, bot_chats={"bot"})
    answer = _call({"cards": [_review(**{"for": "W3"})]}, session_id="bot", reviews=host)
    review = answer["cards"][0]["review"]
    assert review["for"] == "W3" and host.store().get(review["id"])["for"] == "W3"
    assert "go to W3" in answer["note"]


def test_for_is_only_a_bot_chats_and_only_its_own_workstreams(tool_home):
    host = Host(tool_home, workstreams={"W3"}, bot_chats={"bot"})
    elsewhere = _call({"cards": [_review(**{"for": "W3"})]}, session_id="s1", reviews=host)
    assert "for" not in elsewhere["cards"][0]["review"], "a chat that isn't the Bot Chat keeps it here"
    unknown = _call({"cards": [_review(**{"for": "W9"})]}, session_id="bot", reviews=host)
    assert not unknown["ok"] and any("W9 isn't one of this bot's workstreams" in p for p in unknown["problems"])


def test_every_round_stays_with_the_workstream_that_started_it(tool_home):
    host = Host(tool_home, workstreams={"W3", "W4"}, bot_chats={"bot"})
    first = _call({"cards": [_review(**{"for": "W3"})]}, session_id="bot", reviews=host)["cards"][0]["review"]
    # A round with no for still goes to W3.
    second = _call({"cards": [_review(id=first["id"])]}, session_id="bot", reviews=host)["cards"][0]["review"]
    assert second["for"] == "W3" and second["round"] == 2
    # Another workstream's round is refused.
    other = _call({"cards": [_review(id=first["id"], **{"for": "W4"})]}, session_id="bot", reviews=host)
    assert not other["ok"]
    assert any("belongs to W3" in p for p in other["problems"])
    assert host.store().get(first["id"])["for"] == "W3" and len(host.store().get(first["id"])["rounds"]) == 2


def test_a_review_started_outside_a_workstream_stays_in_its_chat(tool_home):
    host = Host(tool_home, workstreams={"W3"}, bot_chats={"bot"})
    first = _call({"cards": [_review()]}, session_id="bot", reviews=host)["cards"][0]["review"]
    later = _call({"cards": [_review(id=first["id"], **{"for": "W3"})]}, session_id="bot", reviews=host)
    assert "for" not in later["cards"][0]["review"]
    assert any("stay in this chat" in p for p in later["problems"])


# --- marks, as a tap ----------------------------------------------------------------

def _shown(tool_home, host):
    return _call({"cards": [_review()]}, reviews=host)["cards"][0]


def _marks(*entries, note=None):
    body = {"round": 1, "marks": [dict(zip(("decision", "mark", "option", "note"), entry)) for entry in entries]}
    for mark in body["marks"]:
        for key in [key for key, value in mark.items() if value is None]:
            del mark[key]
    if note:
        body["note"] = note
    return body


def test_marks_are_kept_with_the_round_and_heard_once(tool_home):
    host = Host(tool_home)
    card = _shown(tool_home, host)
    taps = card_actions.TapStore(tool_home, reviews=host.store())
    taps.save({"sessions": ["s1"], "card": card, "action": "review",
               "review": _marks(("d1", "change", "B", "More photos"), ("d2", "approve", "A"), note="Warmer")})
    kept = host.store().get(card["review"]["id"])["rounds"][0]["marks"]
    assert kept["decisions"][0] == {"decision": "d1", "mark": "change", "option": "B", "note": "More photos"}
    assert kept["note"] == "Warmer"
    note = taps.note("s1", "Sent review · Recipe app home")
    assert '- Home layout: wants changes to B · List (note: "More photos")' in note
    assert "- Save button colour: approved A · Amber" in note
    assert f"review id {card['review']['id']}" in note
    assert taps.note("s1", "Sent review · Recipe app home") is None, "heard once"


def test_a_quick_pick_is_heard_as_picked_and_settles(tool_home):
    host = Host(tool_home)
    answer = _call({"cards": [_review([{"title": "Chart", "options": [{"label": "Bars", "html": PAGE},
                                                                      {"label": "Line", "html": PAGE}]}])]},
                   reviews=host)
    card = answer["cards"][0]
    taps = card_actions.TapStore(tool_home, reviews=host.store())
    taps.save({"sessions": ["s1"], "card": card, "action": "review", "review": _marks(("d1", "approve", "B"))})
    note = taps.note("s1", "Picked B · Line")
    assert "approved B · Line" in note and "They picked: go ahead" in note
    assert host.store().get(card["review"]["id"])["done"] is True
    assert host.store().open_reviews("s1") == []


@pytest.mark.parametrize("review", [
    None, {"round": 1, "marks": []}, {"round": 1, "marks": [{"decision": "d9", "mark": "skip"}]},
    {"round": 1, "marks": [{"decision": "d1", "mark": "love"}]},
    {"round": 1, "marks": [{"decision": "d1", "mark": "approve"}]},
    {"round": 1, "marks": [{"decision": "d1", "mark": "approve", "option": "Z"}]},
    {"round": 1, "marks": [{"decision": "d1", "mark": "skip"}, {"decision": "d1", "mark": "skip"}]}])
def test_marks_are_checked_against_the_round(tool_home, review):
    host = Host(tool_home)
    card = _shown(tool_home, host)
    with pytest.raises(card_actions.TapError):
        card_actions.TapStore(tool_home, reviews=host.store()).save(
            {"sessions": ["s1"], "card": card, "action": "review", "review": review})


def test_the_app_names_the_words_its_message_starts_with(tool_home):
    host = Host(tool_home)
    card = _shown(tool_home, host)
    taps = card_actions.TapStore(tool_home, reviews=host.store())
    taps.save({"sessions": ["s1"], "card": card, "action": "review",
               "review": {**_marks(("d1", "skip")), "label": "Bewertung gesendet"}})
    assert taps.note("s1", "Sent review · Recipe app home") is None
    assert "skipped" in taps.note("s1", "Bewertung gesendet · Recipe app home")


def test_marks_on_an_older_round_are_refused(tool_home):
    host = Host(tool_home)
    card = _shown(tool_home, host)
    _call({"cards": [_review(id=card["review"]["id"])]}, reviews=host)
    with pytest.raises(card_actions.TapError, match="newer round"):
        card_actions.TapStore(tool_home, reviews=host.store()).save(
            {"sessions": ["s1"], "card": card, "action": "review", "review": _marks(("d1", "skip"))})


def test_a_workstreams_marks_go_to_it_and_the_bot_chat_is_told_not_to_resend(tool_home):
    host = Host(tool_home, workstreams={"W3"}, bot_chats={"bot"})
    card = _call({"cards": [_review(**{"for": "W3"})]}, session_id="bot", reviews=host)["cards"][0]
    taps = card_actions.TapStore(tool_home, reviews=host.store())
    taps.save({"sessions": ["bot"], "card": card, "action": "review",
               "review": _marks(("d1", "change", "A", "Bigger photos"), ("d2", "skip"))})
    sent = []
    note = taps.note("bot", "Sent review · Recipe app home",
                     forward=lambda ref, text: sent.append((ref, text)) or {"ok": True})
    [(ref, text)] = sent
    assert ref == "W3" and "Bigger photos" in text and card["review"]["id"] in text
    assert "already went to W3" in note and "for W3" in note


def test_marks_that_couldnt_be_forwarded_say_to_send_them(tool_home):
    host = Host(tool_home, workstreams={"W3"}, bot_chats={"bot"})
    card = _call({"cards": [_review(**{"for": "W3"})]}, session_id="bot", reviews=host)["cards"][0]
    taps = card_actions.TapStore(tool_home, reviews=host.store())
    taps.save({"sessions": ["bot"], "card": card, "action": "review", "review": _marks(("d1", "skip"))})
    note = taps.note("bot", "Sent review · Recipe app home",
                     forward=lambda ref, text: {"ok": False, "note": "W3 is open in another Hermes window"})
    assert "work_send" in note and "another Hermes window" in note


def test_the_open_reviews_note_names_ids_and_workstreams(tool_home):
    host = Host(tool_home, workstreams={"W3"}, bot_chats={"bot"})
    card = _call({"cards": [_review(**{"for": "W3"})]}, session_id="bot", reviews=host)["cards"][0]
    note = tool.open_reviews_note(host.store().open_reviews("bot"))
    assert card["review"]["id"] in note and "belongs to W3" in note
    assert tool.open_reviews_note([]) is None


# --- the store ------------------------------------------------------------------------

def test_a_round_too_big_is_refused(tmp_path):
    store = review_store.KeptReviews(tmp_path)
    review_id = store.start("T", "s1")
    with pytest.raises(review_store.ReviewError):
        store.add_round(review_id, "T", [], {"d1/A": "x" * (review_store.MAX_ROUND_BYTES + 1)})


def test_reviews_untouched_for_a_month_are_gone(tmp_path):
    now = [1000.0]
    store = review_store.KeptReviews(tmp_path, clock=lambda: now[0])
    review_id = store.start("T", "s1")
    store.add_round(review_id, "T", [], {})
    now[0] += review_store.KEEP_S
    assert store.get(review_id) is None and store.read(review_id) is None


@pytest.mark.parametrize("option", [{}, {"html": PAGE, "image": "https://a.test/x.png"},
                                    {"image": "http://a.test/x.png"}, {"file": "relative.html"},
                                    {"file": "/etc/passwd"}, {"html": "x" * (review_store.MAX_HTML_CHARS + 1)}])
def test_an_option_needs_one_page_or_picture_it_can_show(option):
    with pytest.raises(review_store.ReviewError):
        review_store.read_page(option)


# --- GET /boards/reviews ----------------------------------------------------------------

_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_reviews", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


@pytest.fixture()
def route_home(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    api.boards._kept_reviews_limiter._mints.clear()
    return tmp_path


def test_the_route_reads_a_round_with_its_pages(route_home):
    store = review_store.KeptReviews(route_home)
    review_id = store.start("Poster", "s1")
    decisions = [{"id": "d1", "title": "Poster", "options": [{"id": "A", "label": "Bold", "kind": "page"},
                                                             {"id": "B", "label": "Photo", "kind": "page"}]}]
    store.add_round(review_id, "Poster", decisions, {"d1/A": PAGE, "d1/B": PAGE})
    store.add_round(review_id, "Poster", decisions, {"d1/A": "<p>two</p>", "d1/B": PAGE})
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    latest = http.get(f"{BASE}/boards/reviews", params={"id": review_id})
    assert latest.status_code == 200 and latest.headers["cache-control"] == "no-store"
    body = latest.json()["review"]
    assert body["round"] == 2 and body["rounds"] == 2 and body["pages"]["d1/A"] == "<p>two</p>"
    assert http.get(f"{BASE}/boards/reviews", params={"id": review_id, "round": "1"}).json()["review"]["pages"][
        "d1/A"] == PAGE
    assert http.get(f"{BASE}/boards/reviews", params={"id": review_id, "round": "7"}).status_code == 400
    assert http.get(f"{BASE}/boards/reviews", params={"id": "nope"}).status_code == 400
    assert http.get(f"{BASE}/boards/reviews", params={"id": "rv-0000000000000000"}).status_code == 404
