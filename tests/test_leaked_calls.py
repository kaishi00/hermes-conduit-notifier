"""conduit_show calls a model wrote out as text in its reply (leaked_calls),
and the plugin showing them in their place (Eric, 2026-11-09: Furina wrote
her review card out as markup, twice)."""

import importlib.util
import json
import pathlib
import re
import sys

from test_desktop_boards import _plugin
from test_show import MAP

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("conduit_leaked_calls", ROOT / "leaked_calls.py")
leaked = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = leaked
spec.loader.exec_module(leaked)

# As Furina wrote it, cut short where the reply ended.
FURINA = ("I keep hitting a parser issue with deeply nested JSON. Let me show them the simpler way."
          "<tool_call> <function=conduit_show> <args> <title>Conduit website concepts</title> "
          "<markdown>**Concept 1 — Minimal Dark** (recommended)\nFile: /root/scratch/concept-1-minimal-dark.html\n\n"
          "---\n**Concept 2 — Vibrant Gradient**\nFile: /root/scratch/concept-2-vibrant-gradient.html</markdown> "
          "</args> </")
REVIEW_XML = ("<tool_call><function=conduit_show><args><title>W17</title><cards><item><type>review</type>"
              "<title>Concepts</title><review><for>W17</for><decisions><item><title>Pick one</title><options>"
              "<item><label>A</label><file>/a.html</file></item><item><label>B</label><file>/b.html</file></item>"
              "</options></item></decisions></review></item></cards></args></function></tool_call>")


def test_each_shape_a_model_writes_is_read():
    [leak] = leaked.shows(FURINA)
    assert leak.end == len(FURINA) and FURINA[leak.start:].startswith("<tool_call>"), "to the end of a cut reply"
    assert leak.args["title"] == "Conduit website concepts"
    assert leak.args["markdown"].startswith("**Concept 1") and leak.args["markdown"].endswith(".html")
    [leak] = leaked.shows("Here." + REVIEW_XML + " Pick one.")
    assert leak.args["cards"] == [{"type": "review", "title": "Concepts", "review": {"for": "W17", "decisions": [
        {"title": "Pick one", "options": [{"label": "A", "file": "/a.html"}, {"label": "B", "file": "/b.html"}]}]}}]
    bridged = ('<tool_call>{"name": "tool_call", "arguments": {"calls": [{"name": "conduit_show", '
               '"arguments": {"from_work": "W17", "title": "Concepts"}}]}}</tool_call>')
    assert [leak.args for leak in leaked.shows(bridged)] == [{"from_work": "W17", "title": "Concepts"}]
    qwen = ("<tool_call>\n<function=tool_call>\n<parameter=name>conduit_show</parameter>\n"
            '<parameter=arguments>{"from_work": "W17"}</parameter>\n</function>\n</tool_call>')
    assert [leak.args for leak in leaked.shows(qwen)] == [{"from_work": "W17"}]
    direct = '<tool_call>{"name": "conduit_show", "arguments": "{\\"title\\": \\"T\\", \\"cards\\": []}"}</tool_call>'
    assert [leak.args for leak in leaked.shows(direct)] == [{"title": "T", "cards": []}]


def test_other_calls_and_plain_replies_are_left_alone():
    assert leaked.shows("No calls here, <b>bold</b>.") == []
    assert leaked.shows("<tool_call><function=work_send><args><ref>W1</ref></args></function></tool_call>") == []
    assert leaked.shows("<tool_call>not a call</tool_call>") == []
    assert leaked.shows(None) == [] and leaked.shows("<tool_call>" + "x" * leaked.MAX_REPLY) == []
    # Markup in a value stays its text; a tag left open closes with its parent.
    [leak] = leaked.shows("<tool_call><function=conduit_show><args><title>A <b>bold</b> one</title>"
                          "<markdown>x</args></function></tool_call>")
    assert leak.args["markdown"] == "x"


def test_a_written_out_call_is_shown_and_its_line_takes_its_place(tmp_path, monkeypatch):
    plugin = _plugin()
    from conduit_push import room_boards, room_identity, show_tool
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(show_tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(room_identity, "root_home", lambda home=None: tmp_path)
    monkeypatch.setattr(show_tool.client, "load_state", lambda: {"credential": "c"})
    monkeypatch.setattr(show_tool, "_turn_source", lambda: "conduit")
    monkeypatch.setattr(plugin, "_in_room_with_others", lambda session_id, platform=None: False)
    monkeypatch.setattr(plugin, "_is_child", lambda session_id: False)
    monkeypatch.setattr(show_tool.PresentStore, "has_feature_everywhere", lambda self, feature: True)
    monkeypatch.setattr(show_tool.PresentStore, "has_feature", lambda self, feature: True)
    cards = ("<tool_call>\n<function=conduit_show>\n<parameter=title>Dinner</parameter>\n<parameter=cards>"
             + json.dumps([MAP]) + "</parameter>\n</function>\n</tool_call>")
    out = plugin._transform_llm_output(response_text="Here you go." + cards, session_id="s1", model="m",
                                       platform="api_server")
    match = re.fullmatch(r'Here you go\.\n\n::conduit-board\{id="(cb-[0-9a-f]{12})"\}', out or "")
    assert match, out
    board = room_boards.DesktopBoards(tmp_path).get(match.group(1))
    assert board["cards"][0]["type"] == "map", "kept for Conduit to draw where the line is"
    # Markdown alone is the reply's own text, without the markup.
    out = plugin._transform_llm_output(response_text=FURINA, session_id="s1", model="m", platform="api_server")
    assert "<tool_call>" not in out and "</" not in out.replace("**", "")[-3:]
    assert out.startswith("I keep hitting") and "File: /root/scratch/concept-2-vibrant-gradient.html" in out
    assert plugin._transform_llm_output(response_text="Plain.", session_id="s1", model="m",
                                        platform="api_server") is None
