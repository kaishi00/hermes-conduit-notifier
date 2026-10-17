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
            assert schema["format"] == "uri", path
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


def test_the_tool_parameter_wraps_the_card():
    parameter = card_schema.tool_parameter("Cards.")
    assert parameter == {"type": "array", "maxItems": card_schema.MAX_CARDS, "description": "Cards.",
                         "items": card_schema.CARD_SCHEMA}
