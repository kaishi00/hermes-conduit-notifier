"""The shared card schema and its examples (cards/card-vectors.json), which
Conduit's RichCardSchemaTests run too."""

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("conduit_push_test_card_schema", ROOT / "card_schema.py")
card_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(card_schema)

VECTORS = json.loads((ROOT / "cards" / "card-vectors.json").read_text(encoding="utf-8"))


def test_every_shared_example_normalizes_as_written():
    assert VECTORS["version"] == card_schema.VERSION
    assert len(VECTORS["cases"]) >= 10
    for case in VECTORS["cases"]:
        assert card_schema.normalize_cards(case["input"]) == case["output"], case["name"]


def test_normalized_cards_are_stable():
    for case in VECTORS["cases"]:
        assert card_schema.normalize_cards(case["output"]) == case["output"], case["name"]


def test_the_schema_uses_only_what_both_sides_implement():
    allowed = {"type", "description", "enum", "properties", "required", "items", "minItems", "maxItems",
               "maxLength", "minimum", "maximum", "format"}

    def walk(schema, path):
        assert set(schema) <= allowed, f"{path} uses {set(schema) - allowed}"
        assert schema["type"] in ("object", "array", "string", "number", "integer", "boolean"), path
        if schema.get("format") is not None:
            assert schema["format"] in ("uri", "line"), path
        for key, inner in schema.get("properties", {}).items():
            walk(inner, f"{path}.{key}")
        if "items" in schema:
            walk(schema["items"], f"{path}[]")
        for key in schema.get("required", []):
            assert key in schema["properties"], f"{path} requires unknown {key}"

    walk(card_schema.CARD_SCHEMA, "card")


def test_fallback_markdown_reads_as_a_list_of_places():
    [card] = card_schema.normalize_cards([{
        "type": "map", "title": "Dinner and the show",
        "places": [{"name": "Kin Khao", "address": "55 Cyril Magnin St", "lat": 37.7853, "lon": -122.4089,
                    "note": "Get the *khao soi*.", "rating": {"value": 4.4, "count": 1520, "source": "Google",
                                                           "url": "https://g.test/r"},
                    "hours": {"text": "Open until 22:00", "source": "Yelp"}},
                   {"name": "Orpheum Theatre"}],
        "route": {"from": 0, "to": 1, "mode": "walking"}}])
    text = card_schema.fallback_markdown([card])
    assert text.startswith("### Dinner and the show")
    assert "**Kin Khao** · 55 Cyril Magnin St" in text
    assert "★ 4.4 (1,520) · [Google](https://g.test/r)" in text and "Open until 22:00 · Yelp" in text
    assert "_Get the \\*khao soi\\*._" in text, "Markdown in a note is escaped"
    assert "https://maps.apple.com/?q=Kin%20Khao&ll=37.785300,-122.408900" in text
    assert "[Directions to Orpheum Theatre (walking)](https://maps.apple.com/?saddr=37.785300,-122.408900" \
           "&daddr=Orpheum%20Theatre&dirflg=w)" in text


def test_fallback_markdown_escapes_titles_and_names_no_mode_it_cant_open():
    [card] = card_schema.normalize_cards([{
        "type": "map", "title": "[x](https://evil.test) **bold**", "places": [{"name": "A"}, {"name": "B"}],
        "route": {"from": 0, "to": 1, "mode": "cycling"}}])
    text = card_schema.fallback_markdown([card])
    assert text.startswith("### \\[x\\](https://evil.test) \\*\\*bold\\*\\*")
    assert "[Directions to B](https://maps.apple.com/?saddr=A&daddr=B)" in text, "Apple Maps links can't cycle"


def test_fallback_markdown_shows_information_cards_and_their_sources():
    cards = card_schema.normalize_cards([
        {"type": "stats", "title": "This *week*", "source": {"name": "Health", "url": "https://h.test/s"},
         "stats": [{"label": "Steps", "value": "58,204", "change": "+12%", "detail": "daily 8,315"}]},
        {"type": "chart", "chart": {"kind": "bar", "unit": "USD", "labels": ["Mon", "Tue"],
                                    "series": [{"name": "Sales", "values": [120, 95.5]}, {"values": [1, 2]}]}},
        {"type": "weather", "source": {"name": "Open-Meteo"},
         "weather": {"place": "Berlin", "unit": "C", "now": {"temp": 18.5, "condition": "partly_cloudy"},
                     "days": [{"day": "Tue", "low": 12, "high": 21, "condition": "rain", "precip": 60}]}},
        {"type": "steps", "needs": ["2 eggs"], "steps": [{"text": "Boil water."}, {"text": "Simmer.", "timer": 390}]},
    ])
    text = card_schema.fallback_markdown(cards)
    assert "### This \\*week\\*" in text
    assert "- **Steps:** 58,204 (+12%), daily 8,315" in text and "_From [Health](https://h.test/s)_" in text
    assert "| | Sales (USD) | Value (USD) |" in text and "| Tue | 95.5 | 2 |" in text
    assert "Berlin: **18.5°C**, Partly cloudy" in text and "- **Tue:** 12–21°C, Rain (60% chance of rain)" in text
    assert "_From Open-Meteo_" in text
    assert "- 2 eggs" in text and "2. Simmer. (6 min 30 s)" in text
    comparison = card_schema.normalize_cards([{"type": "comparison", "options": [
        {"name": "A", "recommended": True, "facts": [{"label": "Price", "value": "$1"}], "pros": ["Cheap"]},
        {"name": "B", "cons": ["Slow"]}]}])
    text = card_schema.fallback_markdown(comparison)
    assert "**A** (recommended)  \nPrice: $1  \n+ Cheap" in text and "**B**  \n− Slow" in text


def test_every_type_has_its_field_and_markdown():
    fields = {"map": "places", "place": "places", "places": "places", "stats": "stats", "chart": "chart",
              "weather": "weather", "comparison": "options", "steps": "steps", "media": "media",
              "contact": "people", "status": "status", "table": "table", "timeline": "timeline",
              "checklist": "checklist", "list": "list", "progress": "progress", "callout": "callout",
              "diff": "diff", "review": "review", "composed": "sections"}
    assert set(card_schema.CARD_SCHEMA["properties"]["type"]["enum"]) == set(fields)
    for kind, field in fields.items():
        assert card_schema._TYPE_FIELDS[kind][0] == field
        assert field in card_schema.CARD_SCHEMA["properties"]
    gated = [kind for kinds in card_schema.FEATURE_TYPES.values() for kind in kinds]
    assert set(gated) == set(card_schema._INFO_MARKDOWN) == set(fields) - set(card_schema.PLACE_TYPES)


def test_fallback_markdown_shows_media_contacts_and_statuses():
    cards = card_schema.normalize_cards([
        {"type": "media", "source": {"name": "NASA"}, "media": [
            {"kind": "image", "url": "https://a.test/moon.jpg", "title": "Moon", "duration": 9},
            {"kind": "audio", "url": "https://a.test/talk.mp3", "by": "JPL", "duration": 125}]},
        {"type": "contact", "people": [{"name": "Dr *Lee*", "detail": "Dentist", "phone": "+1 (415) 555-0100",
                                        "email": "a@b.co"}, {"name": "X", "phone": "tel:1"}]},
        {"type": "status", "live": "order", "status": {"state": "Out for delivery", "progress": 0.75, "eta": "14:20",
                                                      "milestones": [{"label": "Shipped", "done": True},
                                                                     {"label": "Delivered"}]}}])
    assert "duration" not in cards[0]["media"][0], "an image has no length"
    assert "phone" not in cards[1]["people"][1], "only something that reads as a phone number"
    text = card_schema.fallback_markdown(cards)
    assert "- [Moon](https://a.test/moon.jpg)" in text and "- [Audio](https://a.test/talk.mp3) · JPL (2 min 5 s)" in text
    assert "_From NASA_" in text
    assert "**Dr \\*Lee\\*** · Dentist  \n+1 (415) 555-0100  \na@b.co" in text
    assert "**Out for delivery** · 75% · 14:20  \n✓ Shipped → Delivered" in text


def test_fallback_markdown_shows_the_text_to_send():
    [card] = card_schema.normalize_cards([{"type": "contact", "people": [
        {"name": "Mei", "phone": "+86 139 6261 8677", "message": "Landed *safe*"}]}])
    assert "**Mei**  \n+86 139 6261 8677  \nText to send: Landed \\*safe\\*" in card_schema.fallback_markdown([card])


def test_a_done_status_reads_as_complete_whatever_progress_it_carries():
    cards = card_schema.normalize_cards([{"type": "status", "status": {"state": "Delivered", "phase": "done",
                                                                       "progress": 0.4}}])
    assert "**Delivered** (Done) · 100%" in card_schema.fallback_markdown(cards), "as Conduit draws its bar full"


def test_a_live_name_stays_on_the_first_card_that_has_it():
    status = {"type": "status", "live": "deploy", "status": {"state": "Building"}}
    first, second = card_schema.normalize_cards([status, status])
    assert first["live"] == "deploy" and "live" not in second


def test_a_url_that_could_end_a_link_is_invalid():
    for url in ("https://a.test/x)[y](javascript:alert(1))", "https://a.test/a b", "https://a.test/<x>",
                'https://a.test/"x"', "https://a.test/[x]"):
        [card] = card_schema.normalize_cards([{"type": "place", "places": [
            {"name": "A", "hours": {"text": "9–5", "source": "Yelp", "url": url}}]}])
        assert card["places"][0]["hours"] == {"text": "9–5", "source": "Yelp"}, url


def test_long_lists_stop_once_full():
    places = [{"name": f"P{i}"} for i in range(10_000)]
    cards = card_schema.normalize_cards([{"type": "places", "places": places}] * 1_000)
    assert len(cards) == card_schema.MAX_CARDS
    assert all(len(card["places"]) == 10 for card in cards)


def test_the_tool_parameter_wraps_the_card():
    parameter = card_schema.tool_parameter("Cards.")
    items = parameter.pop("items")
    assert parameter == {"type": "array", "maxItems": card_schema.MAX_CARDS, "description": "Cards."}
    assert items["properties"]["diff"] == card_schema.DEFINITION["inputs"]["diff"], "a diff is given as its input"
    assert items["properties"]["review"] == card_schema.DEFINITION["inputs"]["review"], \
        "a review is given as its input"
    assert {**items, "properties": {**items["properties"], "diff": card_schema.CARD_SCHEMA["properties"]["diff"],
                                    "review": card_schema.CARD_SCHEMA["properties"]["review"]}} \
        == card_schema.CARD_SCHEMA, "the rest is the card as is"


def test_fallback_markdown_shows_the_board_kinds():
    cards = card_schema.normalize_cards([
        {"type": "table", "title": "Top", "table": {"columns": ["Company", "Cap"],
                                                    "rows": [["Apple", "$3T"], ["Nvidia", 2.9]]}},
        {"type": "timeline", "timeline": [{"label": "Day 1", "title": "Valley", "stops": [
            {"time": "09:00", "text": "Mist Trail", "place": "Vernal Fall, Yosemite", "note": "Go early"}]}]},
        {"type": "checklist", "checklist": [{"text": "Socks", "group": "Clothes"}, {"text": "Passport"}]},
        {"type": "list", "ranked": True, "list": [{"title": "Apple", "value": "$3T", "url": "https://a.test"}]},
        {"type": "progress", "progress": [{"label": "Savings", "value": 500, "goal": 2000, "unit": "USD"}]},
        {"type": "callout", "callout": {"text": "Book *early*", "tone": "warning"}}])
    text = card_schema.fallback_markdown(cards)
    assert "| Company | Cap |\n|---|---|\n| Apple | $3T |\n| Nvidia | 2.9 |" in text
    assert "**Day 1** · Valley\n- 09:00: Mist Trail ([Vernal Fall, Yosemite]" \
           "(https://maps.apple.com/?q=Vernal%20Fall,%20Yosemite)). _Go early_" in text
    assert "**Clothes**\n- [ ] Socks\n\n- [ ] Passport" in text
    assert "1. **[Apple](https://a.test)** · $3T" in text
    assert "- **Savings:** 500 of 2,000 USD (25%)" in text
    assert "> **Warning:** Book \\*early\\*" in text


def test_table_cells_keep_their_places():
    [card] = card_schema.normalize_cards([{"type": "table", "table": {
        "columns": ["A", "", 3], "rows": [[1, 2.5, None], ["x", " ", True], [1], [1e16, -0.25, "y"]]}}])
    assert card["table"] == {"columns": ["A", "\u2013", "3"],
                             "rows": [["1", "2.5", "\u2013"], ["x", "\u2013", "\u2013"], ["\u2013", "-0.25", "y"]]}


def test_notes_say_what_was_dropped_and_why():
    notes = []
    cards = card_schema.normalize_cards([
        {"type": "table", "table": {"columns": ["A", "B"], "rows": [["1", "2"], ["3"]]}},
        {"type": "progress", "progress": [{"label": "X", "value": 1, "goal": 0}]},
        {"type": "chart", "chart": {"kind": "doughnut", "labels": ["a"], "series": [{"values": [1]}]}},
        {"type": "stats", "stats": [{"label": "Steps", "value": "1"}], "places": []}], notes)
    assert [card["type"] for card in cards] == ["table", "stats"]
    assert 'cards[0].table.rows: the row starting "3" has 1 cells for 2 columns, left out' in notes
    assert "cards[1].progress: a goal must be above 0, those without were left out" in notes
    assert "cards[2].chart.kind: not one of line, bar, area, pie" in notes and "cards[2]: dropped" in notes
    assert "cards[3].places: not used on a stats card, left out" not in notes, "an empty list never survives"
    assert card_schema.normalize_cards([{"type": "stats", "stats": []}]) == []


def test_notes_never_change_what_is_kept():
    for case in VECTORS["cases"]:
        assert card_schema.normalize_cards(case["input"], []) == case["output"], case["name"]


def test_follow_ups_are_plain_messages():
    notes = []
    kept = card_schema.normalize_next(["Make it $1,500", "make it $1,500", "/new", "!ls", "Read @notes.md",
                                       "Two\nlines", "Run `ls`", "  Add a day in Sequoia ", 4, "x" * 300], notes)
    assert kept == ["Make it $1,500", "Two lines", "Add a day in Sequoia", "x" * 200]
    assert 'next[2]: "/new" isn\'t a plain message, left out' in notes
    assert 'next[1]: "make it $1,500" is there twice, kept once' in notes


def test_a_list_photo_that_isnt_https_is_said_to_be_left_out():
    notes = []
    card = {"type": "list", "list": [{"detail": "no title"},
                                     {"title": "X", "photo": {"url": "http://a.test/x.jpg", "source": "S"}}]}
    assert card_schema.normalize_cards([card], notes) == [{"type": "list", "list": [{"title": "X"}]}]
    assert notes[-1] == 'cards[0].list "X".photo: not https, left out', "named by title, not a moved place"


def test_older_kinds_say_what_their_rules_left_out():
    notes = []
    weather = {"type": "weather", "source": {"name": "Met"},
               "weather": {"unit": "C", "days": [{"day": "Mon", "low": 5, "high": 9}, {"day": "Tue", "low": 20, "high": 10}]}}
    contact = {"type": "contact", "people": [{"name": "Mei", "email": "not-an-email", "message": "Hi"}]}
    card_schema.normalize_cards([weather, contact], notes)
    assert "cards[0].weather.days: a day's low is above its high, those were left out" in notes
    assert 'cards[1].people "Mei".email: not an email address, left out' in notes
    assert 'cards[1].people "Mei".message: a text needs their phone number, left out' in notes
    both = {"type": "comparison", "options": [{"name": "A", "recommended": True}, {"name": "B", "recommended": True}]}
    notes = []
    card_schema.normalize_cards([both], notes)
    assert 'cards[0].options "B".recommended: one option at most, left out' in notes
    assert card_schema.normalize_next(None) == [] and card_schema.normalize_next("Hi") == []
    assert card_schema.normalize_next(["a", "b", "c", "d", "e"]) == ["a", "b", "c", "d"]


def test_layout_is_tabs_or_the_default():
    assert card_schema.normalize_layout("TABS") == "tabs"
    assert card_schema.normalize_layout("stack") is None and card_schema.normalize_layout("grid") is None
