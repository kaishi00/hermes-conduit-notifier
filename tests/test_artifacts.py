import importlib.util
import json
import pathlib
import sqlite3
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path("/nonexistent-hermes-home")
    sys.modules["hermes_constants"] = _hermes_constants

_spec = importlib.util.spec_from_file_location("conduit_artifacts_index_test", ROOT / "artifacts_index.py")
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)

_api_spec = importlib.util.spec_from_file_location("conduit_plugin_api_artifacts", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_api_spec)
_api_spec.loader.exec_module(api)


def _db(home: pathlib.Path, rows, sessions=None, hidden_column=True):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    hidden = ", hidden INTEGER NOT NULL DEFAULT 0, parent_session_id TEXT" if hidden_column else ""
    conn.execute(f"CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT{hidden})")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                 " content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    for sid, title, *rest in sessions or [("s1", "Weekend trip")]:
        if hidden_column:
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                         (sid, title, rest[0] if rest else 0, rest[1] if len(rest) > 1 else None))
        else:
            conn.execute("INSERT INTO sessions VALUES (?, ?)", (sid, title))
    for number, row in enumerate(rows, start=1):
        session, role, content, *extra = row
        tool_calls = extra[0] if extra else None
        tool_name = extra[1] if len(extra) > 1 else None
        conn.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (session, role, content, tool_calls, tool_name, 1_760_000_000 + number))
    conn.commit()
    conn.close()
    return index.open_db(home)


def _list(conn, **kwargs):
    kwargs.setdefault("stat", lambda path: None)
    return index.list_artifacts(conn, **kwargs)


def _no_gateway(monkeypatch):
    # The plain tag reading, as on a host without Hermes' gateway module.
    monkeypatch.setattr(index, "_extract_media", None)


def test_each_source_is_listed_newest_first(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    write = json.dumps([{"function": {"name": "write_file",
                                      "arguments": json.dumps({"path": "/root/out/report.pdf", "content": "x"})}}])
    conn = _db(tmp_path, [
        ("s1", "user", "@file:/root/uploads/notes.txt\nplease read"),
        ("s1", "assistant", "Here it is MEDIA:/root/out/map.png"),
        ("s1", "assistant", "", write),
        ("s1", "tool", json.dumps({"file_path": "/root/out/brief.mp3"}), None, "text_to_speech"),
        ("s1", "assistant", 'Chart below\n::preview{file="/root/out/chart.html"}'),
        ("s1", "assistant", "Book at [Curate](https://curate.example/reserve)."),
    ])
    items = _list(conn)["items"]
    assert [(i["source"], i["kind"], i.get("path") or i.get("url")) for i in items] == [
        ("link", "link", "https://curate.example/reserve"),
        ("preview", "page", "/root/out/chart.html"),
        ("written", "audio", "/root/out/brief.mp3"),
        ("written", "document", "/root/out/report.pdf"),
        ("delivered", "image", "/root/out/map.png"),
        ("attached", "document", "/root/uploads/notes.txt"),
    ]
    assert items[0]["name"] == "Curate"
    assert items[0]["host"] == "curate.example"
    assert items[1]["session_title"] == "Weekend trip"
    assert items[1]["name"] == "chart.html"
    assert items[1]["exists"] is False


def test_source_code_a_tool_writes_is_left_out(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    calls = json.dumps([{"function": {"name": "write_file",
                                      "arguments": json.dumps({"path": "/repo/app.py", "content": "x"})}}])
    conn = _db(tmp_path, [("s1", "assistant", "", calls),
                          ("s1", "tool", json.dumps({"path": "/repo/out.txt"}), None, "read_file")])
    assert _list(conn)["items"] == []


def test_a_path_mentioned_twice_is_listed_once_at_its_newest(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "MEDIA:/root/a.png"), ("s1", "assistant", "again MEDIA:/root/a.png")])
    items = _list(conn)["items"]
    assert len(items) == 1 and items[0]["message_id"] == 2


def test_code_blocks_are_examples_not_deliveries(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "Use it like:\n```\nMEDIA:/tmp/example.png\n```\n")])
    assert _list(conn)["items"] == []


def test_pages_continue_where_the_last_stopped(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", f"MEDIA:/root/{n}.png") for n in range(1, 8)])
    first = _list(conn, limit=3)
    assert [i["name"] for i in first["items"]] == ["7.png", "6.png", "5.png"]
    second = _list(conn, limit=3, before=first["next_before"])
    assert [i["name"] for i in second["items"]] == ["4.png", "3.png", "2.png"]
    third = _list(conn, limit=3, before=second["next_before"])
    assert [i["name"] for i in third["items"]] == ["1.png"]
    assert third["next_before"] is None


def test_a_request_reads_a_bounded_window(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    rows = [("s1", "assistant", "MEDIA:/root/old.png")] + [("s1", "assistant", "nothing here")] * 20
    conn = _db(tmp_path, rows)
    page = _list(conn, window_rows=5, max_scan_rows=10)
    assert page["items"] == [] and page["next_before"] == 12
    rest = _list(conn, before=page["next_before"], window_rows=5, max_scan_rows=100)
    assert [i["name"] for i in rest["items"]] == ["old.png"] and rest["next_before"] is None


def test_one_chat_kind_and_search_filters(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [
        ("s1", "assistant", "MEDIA:/root/map.png and MEDIA:/root/plan.pdf"),
        ("s2", "assistant", "MEDIA:/root/budget.xlsx"),
    ], sessions=[("s1", "Weekend trip"), ("s2", "Budget review")])
    assert [i["name"] for i in _list(conn, session_id="s1")["items"]] == ["map.png", "plan.pdf"]
    assert [i["name"] for i in _list(conn, group="images")["items"]] == ["map.png"]
    assert [i["name"] for i in _list(conn, group="files")["items"]] == ["budget.xlsx", "plan.pdf"]
    assert [i["name"] for i in _list(conn, query="budget REVIEW")["items"]] == ["budget.xlsx"]


def test_one_chat_includes_the_sessions_it_continues_and_spawned(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [
        ("root", "assistant", "MEDIA:/root/before-compression.png"),
        ("tip", "assistant", "MEDIA:/root/after-compression.png"),
        ("sub", "tool", json.dumps({"file_path": "/root/sub.pdf"}), None, "export_report"),
        ("other", "assistant", "MEDIA:/root/elsewhere.png"),
    ], sessions=[("root", "Trip", 0, None), ("tip", None, 0, "root"), ("sub", "", 0, "tip"),
                 ("other", "Other", 0, None)])
    for asked in ("root", "tip", "sub"):
        names = [i["name"] for i in _list(conn, session_ids=[asked])["items"]]
        assert names == ["sub.pdf", "after-compression.png", "before-compression.png"], asked
    titles = {i["name"]: i["session_title"] for i in _list(conn)["items"]}
    assert titles["after-compression.png"] == "Trip" and titles["sub.pdf"] == "Trip"
    assert titles["elsewhere.png"] == "Other"


def test_one_chat_is_read_in_one_call_under_a_busy_history(tmp_path, monkeypatch):
    # A chat under thousands of newer rows from other chats (cron runs, say)
    # lists its own artifacts at once instead of paging the whole history.
    _no_gateway(monkeypatch)
    rows = [("s1", "assistant", "MEDIA:/root/old.png"), ("s1", "assistant", "MEDIA:/root/new.png")]
    rows += [("cron", "assistant", f"run {n} MEDIA:/root/run{n}.png") for n in range(300)]
    conn = _db(tmp_path, rows, sessions=[("s1", "Trip"), ("cron", "Cron")])
    page = _list(conn, session_ids=["s1"], window_rows=10, max_scan_rows=50)
    assert [i["name"] for i in page["items"]] == ["new.png", "old.png"] and page["next_before"] is None
    first = _list(conn, session_ids=["s1"], limit=1, window_rows=10, max_scan_rows=50)
    assert [i["name"] for i in first["items"]] == ["new.png"] and first["next_before"] is not None
    rest = _list(conn, session_ids=["s1"], before=first["next_before"], window_rows=10, max_scan_rows=50)
    assert [i["name"] for i in rest["items"]] == ["old.png"] and rest["next_before"] is None
    capped = _list(conn, session_ids=["s1"], window_rows=10, max_scan_rows=1)
    assert [i["name"] for i in capped["items"]] == ["new.png"] and capped["next_before"] is not None
    tail = _list(conn, session_ids=["s1"], before=capped["next_before"], window_rows=10, max_scan_rows=1)
    assert [i["name"] for i in tail["items"]] == ["old.png"]


def test_a_chat_with_no_usable_id_lists_nothing(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "MEDIA:/root/a.png")])
    assert _list(conn, session_ids=[""]) == {"items": [], "next_before": None}


def test_a_parent_loop_ends(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("a", "assistant", "MEDIA:/root/a.png")],
               sessions=[("a", "", 0, "b"), ("b", "", 0, "a")])
    assert sorted(index.lineage(conn, ["a"])) == ["a", "b"]
    assert _list(conn, session_ids=["a"])["items"][0]["session_title"] == ""


def test_hidden_chats_are_left_out_and_older_stores_still_read(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path / "a", [("s1", "assistant", "MEDIA:/root/a.png"), ("s2", "assistant", "MEDIA:/root/b.png")],
               sessions=[("s1", "Shown", 0), ("s2", "Hidden", 1)])
    assert [i["name"] for i in _list(conn)["items"]] == ["a.png"]
    old = _db(tmp_path / "b", [("s1", "assistant", "MEDIA:/root/a.png")], hidden_column=False)
    assert [i["name"] for i in _list(old)["items"]] == ["a.png"]


def test_a_bots_workstreams_are_left_out_hidden_or_not(tmp_path, monkeypatch):
    # Hermes shows a resolved workstream so Desktop's link opens it (notifier
    # #540); the profile's list still leaves it and what continues it out,
    # and the workstream's own chat still lists its artifacts.
    _no_gateway(monkeypatch)
    conn = _db(tmp_path / "a", [("s1", "assistant", "MEDIA:/root/a.png"), ("ws", "assistant", "MEDIA:/root/b.png"),
                                ("ws-tip", "assistant", "MEDIA:/root/c.png"), ("ws-tip2", "assistant", "MEDIA:/root/d.png")],
               sessions=[("s1", "Shown", 0), ("ws", "Disk", 0), ("ws-tip", "Disk", 0, "ws"),
                         ("ws-tip2", "Disk", 0, "ws-tip")])
    assert [i["name"] for i in _list(conn, workstreams=["ws"])["items"]] == ["a.png"]
    assert [i["name"] for i in _list(conn, session_id="ws", workstreams=["ws"])["items"]] == ["d.png", "c.png", "b.png"]
    assert len(_list(conn)["items"]) == 4


def test_a_bots_hidden_bot_chat_is_listed(tmp_path, monkeypatch):
    # Bot Mode keeps a bot's Bot Chat as a hidden session titled "Bot Chat";
    # other hidden chats (a group room member's turns) stay out of the
    # profile list.
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("bot", "assistant", "MEDIA:/root/.hermes/cache/images/wallpaper.png"),
                          ("room", "assistant", "MEDIA:/root/room.png"),
                          ("more", "assistant", "MEDIA:/root/later.png"),
                          ("s1", "assistant", "MEDIA:/root/a.png")],
               sessions=[("bot", "Bot Chat", 1), ("room", "Group: Council", 1), ("more", "", 1, "bot"),
                         ("s1", "Shown", 0)])
    assert [i["name"] for i in _list(conn)["items"]] == ["a.png", "later.png", "wallpaper.png"]
    # Its own sheet asks for it by id.
    assert [i["name"] for i in _list(conn, session_ids=["bot"])["items"]] == ["later.png", "wallpaper.png"]


def test_a_chat_branched_off_a_hidden_room_doesnt_list_the_room(tmp_path, monkeypatch):
    # A chat's own list follows its lineage, but a hidden chat it was
    # branched from stays out; the chat named by id is read though hidden.
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("room", "assistant", "MEDIA:/root/room.png"),
                          ("branch", "assistant", "MEDIA:/root/branch.png")],
               sessions=[("room", "Group: Council", 1), ("branch", "Notes", 0, "room")])
    assert [i["name"] for i in _list(conn, session_ids=["branch"])["items"]] == ["branch.png"]
    assert [i["name"] for i in _list(conn, session_ids=["room"])["items"]] == ["branch.png", "room.png"]


def test_a_bot_chat_doesnt_bring_in_the_hidden_chat_it_came_from(tmp_path, monkeypatch):
    # Only what continues a Bot Chat is listed with it; a hidden room it
    # somehow descends from stays out. The title is matched trimmed.
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("room", "assistant", "MEDIA:/root/room.png"),
                          ("bot", "assistant", "MEDIA:/root/bot.png")],
               sessions=[("room", "Group: Council", 1), ("bot", " Bot Chat\n", 1, "room")])
    assert [i["name"] for i in _list(conn)["items"]] == ["bot.png"]


def test_a_file_on_disk_reports_its_size(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    target = tmp_path / "out" / "photo.jpg"
    target.parent.mkdir()
    target.write_bytes(b"12345")
    conn = _db(tmp_path / "home", [("s1", "assistant", f"MEDIA:{target}")])
    item = index.list_artifacts(conn)["items"][0]
    assert item["exists"] is True and item["size"] == 5


def test_links_need_a_web_address(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "See https://example.com/a, and ![chart](https://img.example/c.png)")])
    items = _list(conn)["items"]
    assert [(i["kind"], i["url"]) for i in items] == [
        ("image", "https://img.example/c.png"), ("link", "https://example.com/a")]


def test_the_route_reads_the_profile_store(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    _db(tmp_path, [("s1", "assistant", "MEDIA:/root/map.png")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    body = client.get(f"{BASE}/artifacts").json()
    assert body["ok"] is True and body["items"][0]["name"] == "map.png" and body["next_before"] is None
    assert client.get(f"{BASE}/artifacts?kind=bogus").status_code == 400
    one = client.get(f"{BASE}/artifacts?session_id=gone&session_id=s1").json()
    assert [i["name"] for i in one["items"]] == ["map.png"]
    assert client.get(f"{BASE}/artifacts?session_id=gone").json()["items"] == []


def test_a_profile_without_a_store_has_no_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path / "missing")
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    assert api.workspace.list_artifacts() == {"items": [], "next_before": None}


def test_a_newer_mention_the_filter_leaves_out_keeps_an_older_one(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "MEDIA:/root/plan.pdf"), ("s2", "assistant", "again MEDIA:/root/plan.pdf")],
               sessions=[("s1", "Budget review"), ("s2", "Weekend trip")])
    items = _list(conn, query="budget")["items"]
    assert [(i["name"], i["session_title"]) for i in items] == [("plan.pdf", "Budget review")]


def test_a_links_host_leaves_out_credentials(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "See https://user:secret@Example.com/")])
    item = _list(conn)["items"][0]
    assert item["host"] == "example.com" and item["name"] == "example.com"
    assert "secret" not in item["name"] and item["url"] == "https://Example.com/"


def test_the_route_turns_a_request_away_while_both_reads_run(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    _db(tmp_path, [("s1", "assistant", "MEDIA:/root/map.png")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert api.workspace._artifacts_slots.acquire(blocking=False) and api.workspace._artifacts_slots.acquire(blocking=False)
    try:
        assert client.get(f"{BASE}/artifacts").status_code == 503
    finally:
        api.workspace._artifacts_slots.release()
        api.workspace._artifacts_slots.release()
    # Every finished read gave its slot back.
    for _ in range(3):
        assert client.get(f"{BASE}/artifacts").status_code == 200
    assert api.workspace._artifacts_slots.acquire(blocking=False) and api.workspace._artifacts_slots.acquire(blocking=False)
    api.workspace._artifacts_slots.release()
    api.workspace._artifacts_slots.release()


def test_one_lineage_budget_covers_every_chain(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    sessions = [(f"a{n}", "", 0, f"a{n + 1}" if n < 299 else None) for n in range(300)]
    conn = _db(tmp_path, [("a0", "assistant", "MEDIA:/root/a.png")], sessions=sessions)
    assert len(index.lineage(conn, ["a0", "a5"])) <= index.MAX_LINEAGE


def test_unusable_chat_ids_list_nothing(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    _db(tmp_path, [("s1", "assistant", "MEDIA:/root/map.png")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    assert api.workspace.list_artifacts(session_ids=["x" * 500, "  "]) == {"items": [], "next_before": None}


def test_a_file_reference_in_a_code_block_is_not_an_attachment(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "user", "Use it like:\n```\n@file:/root/example.txt\n```\n")])
    assert _list(conn)["items"] == []


def test_a_malformed_link_or_timestamp_skips_only_its_own_row(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "MEDIA:/root/a.png"), ("s1", "assistant", "see https://[::1 in the log")])
    conn.close()
    raw = sqlite3.connect(tmp_path / "state.db")
    raw.execute("UPDATE messages SET timestamp = 'soon' WHERE id = 1")
    raw.commit()
    raw.close()
    items = _list(index.open_db(tmp_path))["items"]
    assert [i["name"] for i in items] == ["a.png"] and items[0]["timestamp"] == 0.0


def test_tool_results_under_every_producer_key_are_read(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [
        ("s1", "tool", json.dumps({"saved_to": "/root/b.wav"}), None, "text_to_speech"),
        ("s1", "tool", json.dumps({"files_created": ["/root/a.png"]}), None, "generate_image"),
        ("s1", "tool", json.dumps({"output_file": "/root/c.pdf"}), None, "export_report"),
    ])
    assert sorted(i["name"] for i in _list(conn)["items"]) == ["a.png", "b.wav", "c.pdf"]


def test_image_generate_pictures_are_listed(tmp_path, monkeypatch):
    # image_generate reports its picture under "image": a cached file, or a
    # hosted URL from a provider that doesn't download it.
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [
        ("s1", "tool", json.dumps({"success": True, "image": "/root/.hermes/cache/images/cat.png",
                                   "model": "gpt-image-2", "prompt": "a cat"}), None, "image_generate"),
        ("s1", "tool", json.dumps({"success": True, "image": "https://fal.media/files/dog_",
                                   "model": "flux"}), None, "image_generate"),
        ("s1", "tool", json.dumps({"success": False, "image": None, "error": "no provider"}), None,
         "image_generate"),
    ])
    items = {i.get("path") or i.get("url"): i for i in _list(conn)["items"]}
    assert set(items) == {"/root/.hermes/cache/images/cat.png", "https://fal.media/files/dog_"}
    assert items["/root/.hermes/cache/images/cat.png"]["kind"] == "image"
    assert items["https://fal.media/files/dog_"]["kind"] == "image"
    assert items["/root/.hermes/cache/images/cat.png"]["source"] == "written"
    assert items["https://fal.media/files/dog_"]["source"] == "link"


def test_one_message_cannot_overflow_the_page(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    links = " ".join(f"https://example.com/{n}" for n in range(5000))
    conn = _db(tmp_path, [("s1", "assistant", links)])
    page = index.list_artifacts(conn, limit=10)
    assert len(page["items"]) <= index.MAX_LIMIT, len(page["items"])


def test_one_message_cannot_make_a_page_stat_thousands_of_files(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    tags = "\n".join(f"MEDIA:/root/out/{n}.png" for n in range(5000))
    conn = _db(tmp_path, [("s1", "assistant", tags)])
    statted = []
    page = index.list_artifacts(conn, limit=10, stat=lambda path: statted.append(path) or 1)
    assert len(page["items"]) <= index.MAX_LIMIT
    assert len(statted) <= index.MAX_LIMIT, len(statted)


def test_missing_files_can_be_left_out_and_links_stay(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    kept = tmp_path / "out" / "kept.pdf"
    kept.parent.mkdir()
    kept.write_bytes(b"pdf")
    conn = _db(tmp_path / "home", [("s1", "assistant", f"MEDIA:{kept}"),
                                   ("s1", "assistant", f"MEDIA:{tmp_path / 'gone.pdf'}"),
                                   ("s1", "assistant", "See https://example.com/a")])
    every = index.list_artifacts(conn)["items"]
    assert [i["name"] for i in every] == ["example.com/a", "gone.pdf", "kept.pdf"]
    present = index.list_artifacts(conn, include_missing=False)["items"]
    assert [i["name"] for i in present] == ["example.com/a", "kept.pdf"]


def test_a_missing_file_is_statted_once_while_left_out(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    conn = _db(tmp_path, [("s1", "assistant", "MEDIA:/root/gone.pdf"), ("s2", "assistant", "again MEDIA:/root/gone.pdf")])
    statted = []
    page = index.list_artifacts(conn, include_missing=False, stat=lambda path: statted.append(path) or None)
    assert page["items"] == [] and statted == ["/root/gone.pdf"]


def test_the_route_hides_missing_files_on_request(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    _db(tmp_path, [("s1", "assistant", f"MEDIA:{tmp_path / 'gone.png'}")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert [i["name"] for i in client.get(f"{BASE}/artifacts?missing=show").json()["items"]] == ["gone.png"]
    assert client.get(f"{BASE}/artifacts?missing=hide").json()["items"] == []
    assert client.get(f"{BASE}/artifacts?missing=bogus").status_code == 400


# --- A file a reply names (hermes-conduit#1809) -----------------------------


def _read_call(path):
    return json.dumps([{"function": {"name": "read_file", "arguments": json.dumps({"path": path})}}])


def test_a_bare_name_resolves_to_the_path_the_chat_read_it_at(tmp_path):
    real = tmp_path / "yelan-audits" / "hermes-conduit" / "Conduit" / "Services"
    real.mkdir(parents=True)
    (real / "AppState.swift").write_text("x")
    (tmp_path / "MyAppState.swift").write_text("x")
    conn = _db(tmp_path / "home", [
        ("s1", "assistant", "", _read_call(str(tmp_path / "MyAppState.swift"))),
        ("s1", "assistant", "", _read_call(str(real / "AppState.swift"))),
        ("s1", "assistant", "The fix is in `AppState.swift`."),
    ])
    found = index.resolve_name(conn, ["s1"], "AppState.swift", exists=lambda p: pathlib.Path(p).is_file())
    assert found == {"path": str(real / "AppState.swift"), "via": "chat"}
    rel = index.resolve_name(conn, ["s1"], "./Services/AppState.swift", exists=lambda p: pathlib.Path(p).is_file())
    assert rel["path"] == str(real / "AppState.swift")
    # A name that only ends the same way isn't the file.
    assert index.resolve_name(conn, ["s1"], "pState.swift", exists=lambda p: True) == {"path": None}


def test_the_newest_existing_mention_wins_and_tool_output_counts(tmp_path):
    old = tmp_path / "old" / "report.json"
    new = tmp_path / "new" / "report.json"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    gone = tmp_path / "gone" / "report.json"
    conn = _db(tmp_path / "home", [
        ("s1", "tool", f"{old}:12: match", None, "terminal"),
        ("s1", "tool", json.dumps({"output": f"wrote {new}"}), None, "terminal"),
        ("s1", "assistant", f"Also tried {gone}."),
    ])
    found = index.resolve_name(conn, ["s1"], "report.json", exists=lambda p: pathlib.Path(p).is_file())
    assert found["path"] == str(new)


def test_the_chats_folder_comes_first_and_lineage_is_read(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, cwd TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                 " content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions VALUES ('parent', 't', NULL, ?)", (str(tmp_path / "work"),))
    conn.execute("INSERT INTO sessions VALUES ('child', 't', 'parent', NULL)")
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('parent', 'assistant', ?)",
                 (f"see {tmp_path}/elsewhere/notes.md",))
    conn.commit()
    conn.close()
    (tmp_path / "work" / "docs").mkdir(parents=True)
    (tmp_path / "work" / "docs" / "notes.md").write_text("x")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "notes.md").write_text("x")
    db = index.open_db(home)
    check = lambda p: pathlib.Path(p).is_file()  # noqa: E731
    assert index.resolve_name(db, ["child"], "docs/notes.md", exists=check) == {
        "path": str(tmp_path / "work" / "docs" / "notes.md"), "via": "folder"}
    assert index.resolve_name(db, ["child"], "notes.md", exists=check) == {
        "path": str(tmp_path / "elsewhere" / "notes.md"), "via": "chat"}


def _folders_db(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, cwd TEXT,"
                 " git_repo_root TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                 " content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    conn.executemany("INSERT INTO sessions VALUES (?, 't', NULL, ?, ?)", rows)
    conn.commit()
    conn.close()
    return index.open_db(home)


def test_a_project_chat_in_a_subfolder_finds_a_link_from_the_repos_top(tmp_path):
    # hermes-conduit#1809: Hermes puts a chat in a project when its folder is
    # anywhere inside it, and replies name files from the repository's top.
    repo = tmp_path / "acme"
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / "plan.md").write_text("x")
    (repo / "app").mkdir()
    (repo / "app" / "README.md").write_text("x")
    (repo / "README.md").write_text("x")
    db = _folders_db(tmp_path / "home", [("s1", str(repo / "app"), str(repo))])
    check = lambda p: pathlib.Path(p).is_file()  # noqa: E731
    assert index.resolve_name(db, ["s1"], "docs/specs/plan.md", exists=check) == {
        "path": str(repo / "docs" / "specs" / "plan.md"), "via": "folder"}
    # The chat's own folder still comes first.
    assert index.resolve_name(db, ["s1"], "README.md", exists=check)["path"] == str(repo / "app" / "README.md")


def test_a_chat_with_no_folder_joins_the_terminal_folder(tmp_path):
    work = tmp_path / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "todo.md").write_text("x")
    db = _folders_db(tmp_path / "home", [("s1", None, None), ("s2", str(tmp_path / "other"), None)])
    check = lambda p: pathlib.Path(p).is_file()  # noqa: E731
    assert index.resolve_name(db, ["s1"], "notes/todo.md", exists=check, default_folder=str(work)) == {
        "path": str(work / "notes" / "todo.md"), "via": "folder"}
    # A chat with a folder of its own doesn't fall back to the terminal's,
    # as Hermes' file tools don't.
    assert index.resolve_name(db, ["s2"], "notes/todo.md", exists=check, default_folder=str(work)) == {"path": None}
    # Nor does a relative or blank one count.
    assert index.resolve_name(db, ["s1"], "notes/todo.md", exists=check, default_folder="work") == {"path": None}
    assert index.resolve_name(db, ["s1"], "notes/todo.md", exists=check, default_folder="") == {"path": None}


def test_the_terminal_folder_is_read_from_a_local_terminals_config(tmp_path, monkeypatch):
    config = types.ModuleType("hermes_cli.config")
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    work = tmp_path / "work"
    config.load_config = lambda: {"terminal": {"backend": "local", "cwd": str(work)}}
    assert api.workspace._terminal_folder() == str(work)
    config.load_config = lambda: {"terminal": {"backend": "docker", "cwd": "/workspace"}}
    assert api.workspace._terminal_folder() is None
    config.load_config = lambda: {"terminal": {"cwd": "."}}
    assert api.workspace._terminal_folder() is None
    config.load_config = lambda: {}
    assert api.workspace._terminal_folder() is None
    # Without Hermes' config (an older Hermes), there's no folder to add.
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    assert api.workspace._terminal_folder() is None


def test_nothing_resolves_outside_the_chat_or_past_the_folder(tmp_path):
    conn = _db(tmp_path / "home", [("s2", "assistant", "/root/secret/plan.md")],
               sessions=[("s1", "a"), ("s2", "b")])
    assert index.resolve_name(conn, ["s1"], "plan.md", exists=lambda p: True) == {"path": None}
    assert index.resolve_name(conn, ["s2"], "../plan.md", exists=lambda p: True) == {"path": None}
    assert index.resolve_name(conn, [], "plan.md", exists=lambda p: True) == {"path": None}
    assert index.resolve_name(conn, ["s2"], "", exists=lambda p: True) == {"path": None}
    # A file the chat named that is gone is reported as not found.
    assert index.resolve_name(conn, ["s2"], "plan.md", exists=lambda p: False) == {"path": None}
    # Without a check (an SSH profile), the newest mention is taken.
    assert index.resolve_name(conn, ["s2"], "plan.md")["path"] == "/root/secret/plan.md"


def test_an_absolute_path_is_only_checked(tmp_path):
    conn = _db(tmp_path / "home", [])
    (tmp_path / "a.md").write_text("x")
    check = lambda p: pathlib.Path(p).is_file()  # noqa: E731
    assert index.resolve_name(conn, ["s1"], str(tmp_path / "a.md"), exists=check)["path"] == str(tmp_path / "a.md")
    assert index.resolve_name(conn, ["s1"], str(tmp_path / "b.md"), exists=check)["path"] is None


def _hermes_file_routes(monkeypatch):
    """Hermes' own file routing, as a local install has it."""
    import types
    files = types.ModuleType("hermes_cli.web_routers.files")
    files._fs_backend = lambda profile: None
    files._fs_profile_home = lambda profile: None
    files._is_sensitive_path = lambda path: False
    monkeypatch.setitem(sys.modules, "hermes_cli.web_routers.files", files)


def test_the_resolve_route_reads_the_profile_store(tmp_path, monkeypatch):
    _hermes_file_routes(monkeypatch)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.xlsx").write_text("x")
    _db(tmp_path, [("s1", "tool", json.dumps({"file_path": str(tmp_path / "out" / "report.xlsx")}), None,
                    "execute_code")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    body = client.get(f"{BASE}/workspace/resolve", params={"path": "report.xlsx", "session_id": ["gone", "s1"]}).json()
    assert body == {"ok": True, "path": str(tmp_path / "out" / "report.xlsx"), "via": "chat"}
    missing = client.get(f"{BASE}/workspace/resolve", params={"path": "nope.xlsx", "session_id": "s1"}).json()
    assert missing == {"ok": True, "path": None}


def test_a_hidden_room_the_chat_came_from_is_not_read(tmp_path):
    conn = _db(tmp_path / "home", [("room", "assistant", "/srv/room/plan.md"),
                                   ("chat", "assistant", "See `plan.md`.")],
               sessions=[("room", "Room", 1), ("chat", "Chat", 0, "room")])
    assert index.resolve_name(conn, ["chat"], "plan.md", exists=lambda p: True) == {"path": None}
    # Asked for by its own id, the hidden session is read.
    assert index.resolve_name(conn, ["room"], "plan.md", exists=lambda p: True)["path"] == "/srv/room/plan.md"


def test_a_windows_path_in_a_tool_call_resolves(tmp_path):
    conn = _db(tmp_path / "home", [
        ("s1", "assistant", "", _read_call("C:\\proj\\Conduit\\AppState.swift")),
        ("s1", "tool", "C:\\proj\\Conduit\\AppStateTests.swift:12: ok", None, "terminal"),
    ])
    assert index.resolve_name(conn, ["s1"], "AppState.swift", exists=lambda p: True) == {
        "path": "C:/proj/Conduit/AppState.swift", "via": "chat"}
    assert index.resolve_name(conn, ["s1"], "Conduit\\AppState.swift", exists=lambda p: True)["path"] == (
        "C:/proj/Conduit/AppState.swift")


def test_the_resolve_route_never_answers_with_a_secret_file(tmp_path, monkeypatch):
    _hermes_file_routes(monkeypatch)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("x")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / ".env").write_text("x")
    _db(tmp_path, [("s1", "tool", f"cat {tmp_path / 'out' / '.env'}", None, "terminal")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    for params in ({"path": str(tmp_path / ".ssh" / "id_rsa")}, {"path": ".env", "session_id": "s1"}):
        assert client.get(f"{BASE}/workspace/resolve", params=params).json() == {"ok": True, "path": None}


def test_the_chats_own_folder_wins_over_a_continuations(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, cwd TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                 " content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions VALUES ('sub', 't', 'main', '/tree')")
    conn.execute("INSERT INTO sessions VALUES ('main', 't', NULL, 'C:\\proj')")
    conn.commit()
    conn.close()
    db = index.open_db(home)
    assert index.resolve_name(db, ["main"], "README.md", exists=lambda p: True) == {
        "path": "C:/proj/README.md", "via": "folder"}


def test_without_hermes_routing_the_resolve_route_refuses(tmp_path, monkeypatch):
    _db(tmp_path, []).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_routers.files", None)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert client.get(f"{BASE}/workspace/resolve", params={"path": "a.md", "session_id": "s1"}).status_code == 501


def test_a_chat_without_a_folder_skips_the_join(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT, cwd TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                 " content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 't', NULL, NULL)")
    conn.execute("INSERT INTO sessions VALUES ('s2', 't', NULL, '/')")
    conn.execute("INSERT INTO sessions VALUES ('s3', 't', NULL, 'C:\\')")
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('s1', 'tool', '/srv/app/README.md')")
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('s2', 'tool', '/srv/app/README.md')")
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('s3', 'tool', '/srv/app/README.md')")
    conn.commit()
    conn.close()
    db = index.open_db(home)
    # Unchecked (an SSH profile): the chat's own path, never /README.md.
    for sid in ("s1", "s2", "s3"):
        assert index.resolve_name(db, [sid], "README.md") == {"path": "/srv/app/README.md", "via": "chat"}


def test_a_url_ending_in_the_name_is_not_a_file(tmp_path):
    conn = _db(tmp_path / "home", [("s1", "assistant", "Mirror: https://mirror.example/AppState.swift")])
    assert index.resolve_name(conn, ["s1"], "AppState.swift", exists=lambda p: True) == {"path": None}
    assert index.resolve_name(conn, ["s1"], "//mirror.example/AppState.swift", exists=lambda p: True) == {"path": None}


def test_a_quoted_path_with_spaces_or_brackets_is_read_whole(tmp_path):
    conn = _db(tmp_path / "home", [
        ("s1", "assistant", "", _read_call("/Users/me/My Project/AppState.swift")),
        ("s1", "tool", 'saved "/Users/me/Reports (2026)/Report (final).pdf"', None, "terminal"),
        ("s1", "assistant", "Wrote `/Users/me/My Project/notes v2.md`."),
    ])
    check = lambda p: True  # noqa: E731
    # Never a piece of it (/Project/AppState.swift).
    assert index.resolve_name(conn, ["s1"], "AppState.swift", exists=check) == {
        "path": "/Users/me/My Project/AppState.swift", "via": "chat"}
    # A quote that only starts with a path still names it.
    said = _db(tmp_path / "home2", [("s1", "assistant", '"/srv/app/plan.md has been updated"')])
    assert index.resolve_name(said, ["s1"], "plan.md", exists=check)["path"] == "/srv/app/plan.md"
    # A quote holding two paths is two paths, even unchecked (an SSH profile).
    moved = _db(tmp_path / "home3", [("s1", "tool", json.dumps({"message": "/srv/old.md moved to /srv/new.md"}),
                                      None, "terminal")])
    assert index.resolve_name(moved, ["s1"], "new.md")["path"] == "/srv/new.md"
    assert index.resolve_name(moved, ["s1"], "old.md")["path"] == "/srv/old.md"
    joined = _db(tmp_path / "home5", [("s1", "tool", json.dumps({"detail": "/srv/other.md,/b/plan.md"}), None,
                                       "terminal")])
    assert index.resolve_name(joined, ["s1"], "plan.md")["path"] == "/b/plan.md"
    # Unchecked (an SSH profile), quoted prose is never taken for a path.
    prose = _db(tmp_path / "home6", [("s1", "tool", json.dumps({"detail": "/srv/app moved to backup/old.md"}),
                                      None, "terminal")])
    assert index.resolve_name(prose, ["s1"], "old.md") == {"path": None}
    # Checked, a quoted run the host doesn't have falls back to its pieces,
    # and a piece of a folder's name (/Project/AppState.swift) isn't one.
    spaced = _db(tmp_path / "home7", [("s1", "tool", '"/srv/app moved to /w/AppState.swift"', None, "terminal"),
                                      ("s1", "tool", '"/Users/me/My Project/AppState.swift"', None, "terminal")])
    assert index.resolve_name(spaced, ["s1"], "AppState.swift",
                              exists=lambda p: p == "/w/AppState.swift")["path"] == "/w/AppState.swift"
    renamed = _db(tmp_path / "home4", [("s1", "tool", json.dumps({"detail": "/srv/old.md renamed to backup/old.md"}),
                                        None, "terminal")])
    assert index.resolve_name(renamed, ["s1"], "old.md")["path"] == "/srv/old.md"
    assert index.resolve_name(conn, ["s1"], "Report (final).pdf", exists=check)["path"] == (
        "/Users/me/Reports (2026)/Report (final).pdf")
    assert index.resolve_name(conn, ["s1"], "notes v2.md", exists=check)["path"] == "/Users/me/My Project/notes v2.md"


def test_a_name_ending_in_punctuation_still_matches(tmp_path):
    conn = _db(tmp_path / "home", [
        ("s1", "tool", "wrote /notes/report_ and /notes/why?", None, "terminal"),
        ("s1", "tool", "then /logs/build_. And /q/wow!.", None, "terminal"),
        ("s1", "assistant", "See /srv/app/plan.md."),
    ])
    check = lambda p: True  # noqa: E731
    assert index.resolve_name(conn, ["s1"], "report_", exists=check)["path"] == "/notes/report_"
    assert index.resolve_name(conn, ["s1"], "why?", exists=check)["path"] == "/notes/why?"
    assert index.resolve_name(conn, ["s1"], "build_", exists=check)["path"] == "/logs/build_"
    assert index.resolve_name(conn, ["s1"], "wow!", exists=check)["path"] == "/q/wow!"
    # A sentence's full stop is still not the name's.
    assert index.resolve_name(conn, ["s1"], "plan.md", exists=check)["path"] == "/srv/app/plan.md"


def _resolve_client(tmp_path, monkeypatch, rows):
    _hermes_file_routes(monkeypatch)
    _db(tmp_path / "home", rows).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(api.workspace, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_a_hosted_install_never_answers_outside_its_locked_root(tmp_path, monkeypatch):
    import types
    root = tmp_path / "root"
    (root / "out").mkdir(parents=True)
    (root / "out" / "a.md").write_text("x")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "b.md").write_text("x")
    (root / "link.md").symlink_to(tmp_path / "outside" / "b.md")
    server_files = types.ModuleType("hermes_cli.web_server_files")
    server_files._managed_files_policy = lambda *a, **k: types.SimpleNamespace(locked_root=root)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_files", server_files)
    client = _resolve_client(tmp_path, monkeypatch, [
        ("s1", "tool", f"{root / 'out' / 'a.md'} {tmp_path / 'outside' / 'b.md'} {root / 'link.md'}", None,
         "terminal")])

    def resolve(name):
        return client.get(f"{BASE}/workspace/resolve", params={"path": name, "session_id": "s1"}).json()["path"]

    assert resolve("a.md") == str(root / "out" / "a.md")
    assert resolve("b.md") is None
    assert resolve(str(tmp_path / "outside" / "b.md")) is None
    # A link inside the root to a file outside it is outside.
    assert resolve("link.md") is None


def test_a_docker_profiles_paths_are_checked_where_the_host_keeps_them(tmp_path, monkeypatch):
    import types
    host = tmp_path / "sandbox"
    (host / "workspace").mkdir(parents=True)
    (host / "workspace" / "out.csv").write_text("x")
    server_files = types.ModuleType("hermes_cli.web_server_files")

    def agent_fs_path(path, profile_home=None):
        text = str(path)
        return pathlib.Path(str(host) + text) if text.startswith("/workspace/") else pathlib.Path(text)

    server_files._agent_fs_path = agent_fs_path
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_files", server_files)
    client = _resolve_client(tmp_path, monkeypatch, [
        ("s1", "tool", "wrote /workspace/out.csv and /workspace/gone.csv", None, "terminal")])
    body = client.get(f"{BASE}/workspace/resolve", params={"path": "out.csv", "session_id": "s1"}).json()
    # Answered in the agent's own path, which the Folder maps again.
    assert body["path"] == "/workspace/out.csv"
    gone = client.get(f"{BASE}/workspace/resolve", params={"path": "gone.csv", "session_id": "s1"}).json()
    assert gone["path"] is None


def test_an_ssh_profile_never_takes_quoted_prose_for_a_path(tmp_path, monkeypatch):
    client = _resolve_client(tmp_path, monkeypatch, [
        ("s1", "tool", json.dumps({"message": "/srv/app moved to backup/old.md"}), None, "terminal"),
        ("s1", "tool", '"/srv/My Project/notes.md"', None, "terminal")])
    # The profile's files are on its SSH machine: nothing here checks them.
    monkeypatch.setattr(sys.modules["hermes_cli.web_routers.files"], "_fs_backend", lambda profile: object())

    def resolve(name):
        return client.get(f"{BASE}/workspace/resolve", params={"path": name, "session_id": "s1"}).json()["path"]

    assert resolve("old.md") is None
    # Unchecked, a quoted path with a space is left out too.
    assert resolve("notes.md") is None


def test_a_quoted_path_with_an_apostrophe_is_read_whole(tmp_path):
    conn = _db(tmp_path / "home", [
        ("s1", "assistant", "", _read_call("/Users/me/John's Projects/AppState.swift")),
        ("s1", "assistant", "Saved `/Users/me/Ann's/notes.md`."),
    ])
    check = lambda p: True  # noqa: E731
    assert index.resolve_name(conn, ["s1"], "AppState.swift", exists=check)["path"] == (
        "/Users/me/John's Projects/AppState.swift")
    assert index.resolve_name(conn, ["s1"], "notes.md", exists=check)["path"] == "/Users/me/Ann's/notes.md"


def test_an_ssh_profile_never_opens_a_piece_of_a_quoted_bracket_path(tmp_path, monkeypatch):
    client = _resolve_client(tmp_path, monkeypatch, [
        ("s1", "tool", '"/Users/me/Reports (2026)/AppState.swift"', None, "terminal")])
    monkeypatch.setattr(sys.modules["hermes_cli.web_routers.files"], "_fs_backend", lambda profile: object())
    body = client.get(f"{BASE}/workspace/resolve", params={"path": "AppState.swift", "session_id": "s1"}).json()
    assert body["path"] is None, "never /AppState.swift"


def test_a_quoted_run_with_a_path_in_brackets_still_names_it(tmp_path):
    conn = _db(tmp_path / "home", [("s1", "tool", '"/a/b.md (/c/d.md)"', None, "terminal")])
    assert index.resolve_name(conn, ["s1"], "d.md", exists=lambda p: True)["path"] == "/c/d.md"


def test_a_quoted_path_whose_folder_ends_in_a_separator_is_tried_whole(tmp_path):
    real = {"/data/a;/b/old.md", "/b/old.md", "/x/old.md"}
    conn = _db(tmp_path / "home", [
        ("s1", "tool", '"/data/a;/b/old.md"', None, "terminal"),
        ("s3", "assistant", 'Moved it to "/data/a;/b/old.md."'),
        ("s2", "tool", '"/x/old.md, /y/old.md"', None, "terminal"),
    ])
    assert index.resolve_name(conn, ["s1"], "old.md", exists=real.__contains__)["path"] == "/data/a;/b/old.md"
    assert index.resolve_name(conn, ["s3"], "old.md", exists=real.__contains__)["path"] == "/data/a;/b/old.md"
    # Two paths: the whole run isn't a file, so its pieces still answer.
    assert index.resolve_name(conn, ["s2"], "old.md", exists=real.__contains__)["path"] == "/x/old.md"


def test_the_resolve_route_finds_a_link_in_the_terminal_folder(tmp_path, monkeypatch):
    # The route hands resolve_name the profile's terminal folder; the helper's
    # store keeps no cwd column, so no chat has a folder of its own.
    work = tmp_path / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "todo.md").write_text("x")
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"terminal": {"backend": "local", "cwd": str(work)}}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    client = _resolve_client(tmp_path, monkeypatch, [("s1", "assistant", "Wrote `notes/todo.md`.")])
    body = client.get(f"{BASE}/workspace/resolve", params={"path": "notes/todo.md", "session_id": "s1"}).json()
    assert body == {"ok": True, "path": str(work / "notes" / "todo.md"), "via": "folder"}
