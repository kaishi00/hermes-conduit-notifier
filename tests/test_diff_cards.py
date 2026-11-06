"""Diff cards: a patch, a patch file or two texts read into files and hunks
(card_schema.read_diff), what conduit_show's answer carries of them, the long
ones kept on the host (room_boards.KeptDiffs, GET /boards/diffs), and their
Markdown."""

import contextlib
import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_room_boards import BASE, Clock, api, room_boards
from test_show import BOARD_FEATURES, PresentStore, _call, _exchange, tool, tool_home  # noqa: F401

card_schema = tool.card_schema
DIFF_FEATURES = (*BOARD_FEATURES, card_schema.DIFF_FEATURE)

GIT_SHOW = """commit 4f2a9c1
Author: Hermes <hermes@example.com>

    Retry the login once

diff --git a/Sources/Login.swift b/Sources/Login.swift
index 1b2c3d4..5e6f7a8 100644
--- a/Sources/Login.swift
+++ b/Sources/Login.swift
@@ -40,4 +40,5 @@ func retry() {
     let delay = 2.0
-    attempts += 2
+    attempts += 1
+\tlog(attempts)

     return delay
\\ No newline at end of file
diff --git a/docs/sign-in.md b/docs/login.md
similarity index 100%
rename from docs/sign-in.md
rename to docs/login.md
diff --git a/Assets/logo.png b/Assets/logo.png
new file mode 100644
index 0000000..e69de29
Binary files /dev/null and b/Assets/logo.png differ
diff --git a/NOTES.md b/NOTES.md
deleted file mode 100644
--- a/NOTES.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# Notes
--- a line that reads like a header
diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
"""


def _one(given, notes=None):
    cards = card_schema.normalize_cards([{"type": "diff", "diff": given}], notes)
    return cards[0]["diff"] if cards else None


def test_a_git_patch_reads_into_its_files_and_hunks():
    diff = _one({"patch": GIT_SHOW})
    login, renamed, logo, notes_file = diff["files"]
    assert [file["path"] for file in diff["files"]] == ["Sources/Login.swift", "docs/login.md", "Assets/logo.png",
                                                       "NOTES.md"], "a mode change alone shows nothing"
    assert login == {"path": "Sources/Login.swift", "status": "modified", "added": 2, "removed": 1, "hunks": [
        {"old_start": 40, "new_start": 40, "context": "func retry() {",
         "lines": ["     let delay = 2.0", "-    attempts += 2", "+    attempts += 1", "+    log(attempts)", " ",
                   "     return delay"]}]}, "tabs are four spaces, a trimmed context line a space, the commit skipped"
    assert renamed == {"path": "docs/login.md", "old_path": "docs/sign-in.md", "status": "renamed", "added": 0,
                       "removed": 0}
    assert logo == {"path": "Assets/logo.png", "status": "added", "binary": True, "added": 0, "removed": 0}
    assert notes_file["status"] == "deleted" and notes_file["hunks"][0]["lines"] == [
        "-# Notes", "--- a line that reads like a header"], "a hunk is read by its lengths, not by what looks like a header"
    assert (diff["added"], diff["removed"]) == (2, 3)
    assert card_schema.normalize_cards([{"type": "diff", "diff": diff}]) == [{"type": "diff", "diff": diff}], \
        "a card read once reads the same again"


def test_diff_u_and_bare_hunks_read_too():
    plain = _one({"patch": "--- etc/nginx.conf\t2026-11-05 10:00:00\n+++ etc/nginx.conf\t2026-11-05 10:05:00\n"
                           "@@ -3 +3 @@\n-worker_processes 2;\n+worker_processes auto;\n"
                           "--- b.txt\n+++ b.txt\n@@ -1,0 +1 @@\n+new\n"})
    assert [(file["path"], file["added"], file["removed"]) for file in plain["files"]] == [
        ("etc/nginx.conf", 1, 1), ("b.txt", 1, 0)]
    bare = _one({"patch": "@@ -1 +1 @@\n-a\n+b\n", "name": "notes.txt"})
    assert bare["files"][0]["path"] == "notes.txt"
    assert _one({"patch": "@@ -1 +1 @@\n-a\n+b\n"})["files"][0]["path"] == "Changes"
    quoted = _one({"patch": 'diff --git "a/my file.txt" "b/my file.txt"\n--- "a/my file.txt"\n'
                            '+++ "b/my file.txt"\n@@ -1 +1 @@\n-a\n+b\n'})
    assert quoted["files"][0]["path"] == "my file.txt"


def test_what_isnt_a_diff_shows_nothing_and_says_why(tmp_path):
    notes = []
    assert _one({"patch": "SECRET=hunter2\nTOKEN=abc\n"}, notes) is None
    assert notes[0] == "cards[0].diff.patch: not a unified diff (no ---/+++ file lines or @@ hunks)"
    assert not any("hunter2" in note for note in notes), "nothing of the file is said back"
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n-----END OPENSSH PRIVATE KEY-----\n")
    notes = []
    assert _one({"patch_file": str(key)}, notes) is None, "a patch file must be a diff, so no other file goes on screen"
    assert notes[0].startswith("cards[0].diff.patch_file: not a unified diff")
    notes = []
    assert _one({}, notes) is None and notes[0] == "cards[0].diff: give patch, patch_file, or before and after"


def test_a_patch_file_is_read_from_the_host(tmp_path, monkeypatch):
    patch = tmp_path / "review.diff"
    patch.write_text(GIT_SHOW, encoding="utf-8")
    assert _one({"patch_file": str(patch)}) == _one({"patch": GIT_SHOW})
    for given, said in ((str(tmp_path / "gone.diff"), "no such file on the Hermes host"),
                        ("review.diff", "give the file's absolute path"),
                        (str(tmp_path), "no such file on the Hermes host")):
        notes = []
        assert _one({"patch_file": given}, notes) is None and said in notes[0], given
    binary = tmp_path / "blob.diff"
    binary.write_bytes(b"@@ -1 +1 @@\n-a\0\n+b\n")
    notes = []
    assert _one({"patch_file": str(binary)}, notes) is None and "not a unified diff" in notes[0]
    monkeypatch.setattr(card_schema, "MAX_DIFF_INPUT", 100)
    notes = []
    assert _one({"patch_file": str(patch)}, notes) is None and "over 2 MB" in notes[0]
    notes = []
    assert _one({"patch": GIT_SHOW}, notes) is None and notes[0].startswith("cards[0].diff.patch: over 2 MB")


def test_git_quoted_names_are_read_as_git_wrote_them():
    patch = ('diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n--- "a/caf\\303\\251.txt"\n'
             '+++ "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+b\n')
    assert _one({"patch": patch})["files"][0]["path"] == "caf\u00e9.txt"
    assert card_schema._unquoted('"a\\"b\\\\c"') == 'a"b\\c'


def test_read_files_with_a_name_stay_as_read():
    diff = _one({"patch": GIT_SHOW})
    assert _one({**diff, "name": "x"}) == diff


def test_a_long_diffs_markdown_closes_its_fence():
    long = _one({"patch": "@@ -1,40 +1,40 @@\n" + "".join(f"-{'x' * 900}\n+{'y' * 900}\n" for _ in range(20))})
    text = card_schema.fallback_markdown([{"type": "diff", "diff": long}])
    assert len(text) <= card_schema.MARKDOWN_DIFF_CHARS
    assert text.count("```") == 2 and "more lines_" in text


def test_a_board_cut_inside_a_code_block_closes_it():
    lines = "".join(f"-{'x' * 900}\n+{'y' * 900}\n" for _ in range(20))
    long = _one({"patch": "@@ -1,20 +1,20 @@\n" + lines})
    text = tool.clip_markdown(card_schema.fallback_markdown([{"type": "diff", "diff": long}] * 2))
    assert len(text) <= sys.modules[PresentStore.__module__].MAX_CARD_MARKDOWN_CHARS
    assert text.rstrip().endswith("```") and text.count("```") % 2 == 0


def test_two_texts_are_compared():
    diff = _one({"before": "listen 80;\nroot /srv;\n", "after": "listen 443 ssl;\nroot /srv;\n", "name": "site.conf"})
    assert diff["files"] == [{"path": "site.conf", "status": "modified", "added": 1, "removed": 1, "hunks": [
        {"old_start": 1, "new_start": 1, "lines": ["-listen 80;", "+listen 443 ssl;", " root /srv;"]}]}]
    assert _one({"after": "hello"})["files"][0] == {"path": "Text", "status": "added", "added": 1, "removed": 0,
                                                    "hunks": [{"old_start": 0, "new_start": 1, "lines": ["+hello"]}]}
    notes = []
    assert _one({"before": "same", "after": "same"}, notes) is None
    assert notes[0] == "cards[0].diff: before and after are the same, nothing to show"
    notes = []
    assert _one({"before": ["old"], "after": "new"}, notes) is None
    assert notes[0] == "cards[0].diff.before: before and after are text"
    notes = []
    assert _one({"before": "x" * 20_001, "after": "y"}, notes) is None and "diff -u" in notes[0]
    for before, after in (("a\n", "a"), ("a\r\nb", "a\nb")):
        notes = []
        assert _one({"before": before, "after": after}, notes) is None
        assert "only line endings differ" in notes[0]


def test_a_diff_keeps_what_fits_and_says_what_it_left_out(monkeypatch):
    many = "".join(f"--- f{index}\n+++ f{index}\n@@ -1 +1 @@\n-a\n+b\n" for index in range(305))
    notes = []
    diff = _one({"patch": many}, notes)
    assert len(diff["files"]) == 300 and diff["more_files"] == 5 and (diff["added"], diff["removed"]) == (305, 305)
    assert "cards[0].diff: 305 files, the first 300 are listed" in notes
    monkeypatch.setattr(card_schema, "MAX_DIFF_LINES", 3)
    notes = []
    diff = _one({"patch": "--- a\n+++ a\n@@ -1,2 +1,2 @@\n-a\n-b\n+c\n+d\n--- b\n+++ b\n@@ -1 +1 @@\n-x\n+y\n"}, notes)
    first, second = diff["files"]
    assert first["hunks"][0]["lines"] == ["-a", "-b", "+c"] and first["cut"] is True and first["added"] == 2
    assert "hunks" not in second and second["cut"] is True and second["added"] == 1, "counted, not shown"
    assert "cards[0].diff: only the first 3 lines are shown" in notes
    monkeypatch.setattr(card_schema, "MAX_DIFF_LINES", 20_000)
    notes = []
    diff = _one({"patch": "@@ -1 +1 @@\n-" + "x" * 5000 + "\n+y\n"}, notes)
    line = diff["files"][0]["hunks"][0]["lines"][0]
    assert len(line) == card_schema.MAX_DIFF_LINE_CHARS + 1 and line.endswith("…")
    assert "cards[0].diff: 1 lines over 1000 characters were cut" in notes


def test_hidden_bidi_characters_in_a_patch_show_as_marks_and_leave_the_names():
    """Issue 1852 (Trojan Source): an override in a patch line would draw the
    line in another order than it runs, and Copy Changes would paste it."""
    diff = _one({"patch": "diff --git a/auth\u200b.py b/auth\u200b.py\n--- a/auth\u200b.py\n+++ b/auth\u200b.py\n"
                          "@@ -1 +1 @@ def is\u2066_admin\n-ok = role == 'admin'\n+ok = role != 'user\u202e \u2066'\n"})
    [file] = diff["files"]
    assert file["path"] == "auth.py" and file["hunks"][0]["context"] == "def is_admin", "names drop them"
    assert file["hunks"][0]["lines"][1] == "+ok = role != 'user\ufffd \ufffd'", "a line shows where each was"
    text = card_schema.fallback_markdown([{"type": "diff", "diff": diff}])
    assert "\u202e" not in text and "\u2066" not in text and "\u200b" not in text


def test_the_markdown_lists_the_files_and_shows_the_first_lines():
    [card] = card_schema.normalize_cards([{"type": "diff", "title": "Login fix", "diff": {"patch": GIT_SHOW},
                                           "source": {"name": "PR #12", "url": "https://example.com/pull/12"}}])
    text = card_schema.fallback_markdown([card])
    assert text.startswith("### Login fix\n\n**+2 −3** in 4 files")
    assert "- Sources/Login.swift · +2 −1\n- docs/sign-in.md → docs/login.md · renamed\n" \
           "- Assets/logo.png (new) · binary\n- NOTES.md (deleted) · +0 −2" in text
    assert "```diff\n--- a/Sources/Login.swift\n+++ b/Sources/Login.swift\n@@ -40,4 +40,5 @@ func retry() {\n" in text
    assert "--- a/NOTES.md\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-# Notes" in text
    assert text.endswith("_From [PR #12](https://example.com/pull/12)_")
    ticks = _one({"before": "use ``` fences\n", "after": "use ```` fences\n"})
    assert "`````diff\n" in card_schema.fallback_markdown([{"type": "diff", "diff": ticks}]), \
        "the fence is longer than any run of backticks in the code"
    long = _one({"patch": "@@ -1,50 +1,50 @@\n" + "".join(f"-{index}\n+{index}!\n" for index in range(50))})
    text = card_schema.fallback_markdown([{"type": "diff", "diff": long}])
    assert text.count("\n-") + text.count("\n+") - 3 == card_schema.MARKDOWN_DIFF_LINES, "the file row and lines"
    assert text.endswith("_… 60 more lines_")


def test_a_short_diff_travels_whole_and_a_long_one_is_kept_on_the_host(tool_home):
    _exchange(PresentStore(tool_home), features=DIFF_FEATURES)
    kept = []

    def keep(diff):
        kept.append(diff)
        return "cd-0123456789abcdef"

    short = _call({"title": "Fix", "cards": [{"type": "diff", "diff": {"patch": GIT_SHOW}}]}, keep_diff=keep)
    assert short["ok"] is True and not kept and short["cards"][0]["diff"] == _one({"patch": GIT_SHOW})
    long_patch = "@@ -1,200 +1,200 @@\n" + "".join(f"-{index}\n+{index}!\n" for index in range(200))
    answer = _call({"title": "Big", "cards": [{"type": "diff", "diff": {"patch": long_patch}}]}, keep_diff=keep)
    [whole] = kept
    assert whole == _one({"patch": long_patch}) and len(whole["files"][0]["hunks"][0]["lines"]) == 400
    assert answer["cards"][0]["diff"] == {"files": [{"path": "Changes", "status": "modified", "added": 200,
                                                     "removed": 200}],
                                          "added": 200, "removed": 200, "id": "cd-0123456789abcdef"}
    assert "```" not in answer["markdown"], "the Markdown lists a long diff's files, never its lines"
    unkept = _call({"title": "Big", "cards": [{"type": "diff", "diff": {"patch": long_patch}}]},
                   keep_diff=lambda diff: None)
    assert "id" not in unkept["cards"][0]["diff"] and tool.DIFF_NOT_KEPT_NOTE in unkept["problems"]


def test_a_conduit_without_diffs_hears_nothing_was_shown(tool_home):
    _exchange(PresentStore(tool_home), features=BOARD_FEATURES)
    answer = _call({"title": "Fix", "cards": [{"type": "diff", "diff": {"patch": GIT_SHOW}}]})
    assert answer["ok"] is False and answer["error"] == "unsupported_cards"


def test_a_room_drawn_by_a_conduit_without_diffs_leaves_the_diff_out(tool_home, monkeypatch):
    monkeypatch.setattr(tool, "_turn_source", lambda: "conduit")
    room = {"session_id": "room-1", "is_room": lambda s: s == "room-1", "room_board": lambda *_: True}
    callout = {"type": "callout", "callout": {"text": "Merged"}}
    _exchange(PresentStore(tool_home), features=(*BOARD_FEATURES, tool.ROOM_BOARDS_FEATURE))
    answer = _call({"title": "Fix", "cards": [{"type": "diff", "diff": {"patch": GIT_SHOW}}, callout]}, **room)
    assert [card["type"] for card in answer["cards"]] == ["callout"]
    assert answer["problems"][0] == tool.LEFT_OUT_NOTE.format(kinds="diff")
    alone = _call({"title": "Fix", "cards": [{"type": "diff", "diff": {"patch": GIT_SHOW}}]}, **room)
    assert alone["ok"] is False and alone["error"] == "unsupported_cards"
    _exchange(PresentStore(tool_home), features=(*DIFF_FEATURES, tool.ROOM_BOARDS_FEATURE))
    shown = _call({"title": "Fix", "cards": [{"type": "diff", "diff": {"patch": GIT_SHOW}}]}, **room)
    assert shown["ok"] is True and shown["cards"][0]["type"] == "diff"


def test_the_tool_offers_the_diffs_input():
    diff = tool.SCHEMA["parameters"]["properties"]["cards"]["items"]["properties"]["diff"]
    assert set(diff["properties"]) == {"patch", "patch_file", "before", "after", "name"}
    assert "diff" in tool.SCHEMA["description"] and "patch_file" in tool.SCHEMA["description"]


def _ids():
    count = iter(range(1, 10_000))
    return lambda: f"cd-{next(count):016x}"


def test_a_kept_diff_is_read_by_its_id_for_a_month(tmp_path, monkeypatch):
    store = room_boards.KeptDiffs(tmp_path, clock=Clock(), new_id=_ids())
    first = store.add({"files": [], "n": 1})
    assert first == "cd-0000000000000001" and store.get(first) == {"files": [], "n": 1}
    path = tmp_path / room_boards.DIFF_DIRECTORY / f"{first}.json"
    assert (path.stat().st_mode & 0o777) == 0o600 and (path.parent.stat().st_mode & 0o777) == 0o700
    assert store.get("cd-00000000000000ff") is None
    for bad in ("", "cd-1", "../x", "cb-000000000001", None, 7):
        with pytest.raises(ValueError):
            store.get(bad)
    store.clock.now += room_boards.KEEP_S
    assert store.get(first) is None
    monkeypatch.setattr(room_boards, "MAX_KEPT_DIFFS", 3)
    ids = [store.add({"n": index}) for index in range(5)]
    assert sorted(path.name for path in (tmp_path / room_boards.DIFF_DIRECTORY).glob("cd-*.json")) == \
        sorted(f"{diff_id}.json" for diff_id in ids[-3:]), "the newest stay"


def test_the_route_reads_a_kept_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(api.core, "_calls_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    identity = api.core._plugin_package_module("room_identity")
    monkeypatch.setattr(identity, "root_home", lambda home=None: tmp_path)
    diff = {"files": [{"path": "a", "status": "modified", "added": 1, "removed": 0}], "added": 1, "removed": 0}
    diff_id = api.core._plugin_package_module("room_boards").KeptDiffs(tmp_path).add(diff)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    http = TestClient(app)
    response = http.get(f"{BASE}/boards/diffs", params={"id": diff_id})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"diff": diff}
    assert http.get(f"{BASE}/boards/diffs", params={"id": "cd-00000000000000ff"}).status_code == 404
    assert http.get(f"{BASE}/boards/diffs", params={"id": "../../etc/passwd"}).status_code == 400
    assert "diff-cards" in api.capabilities.ROUTE_CAPABILITIES
    assert json.loads(json.dumps(response.json())) == {"diff": diff}
