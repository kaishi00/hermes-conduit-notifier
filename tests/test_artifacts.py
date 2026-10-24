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
    monkeypatch.setattr(api, "_artifacts_index", lambda: index)
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
    monkeypatch.setattr(api, "_artifacts_index", lambda: index)
    assert api.list_artifacts() == {"items": [], "next_before": None}


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
    monkeypatch.setattr(api, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert api._artifacts_slots.acquire(blocking=False) and api._artifacts_slots.acquire(blocking=False)
    try:
        assert client.get(f"{BASE}/artifacts").status_code == 503
    finally:
        api._artifacts_slots.release()
        api._artifacts_slots.release()
    # Every finished read gave its slot back.
    for _ in range(3):
        assert client.get(f"{BASE}/artifacts").status_code == 200
    assert api._artifacts_slots.acquire(blocking=False) and api._artifacts_slots.acquire(blocking=False)
    api._artifacts_slots.release()
    api._artifacts_slots.release()


def test_one_lineage_budget_covers_every_chain(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    sessions = [(f"a{n}", "", 0, f"a{n + 1}" if n < 299 else None) for n in range(300)]
    conn = _db(tmp_path, [("a0", "assistant", "MEDIA:/root/a.png")], sessions=sessions)
    assert len(index.lineage(conn, ["a0", "a5"])) <= index.MAX_LINEAGE


def test_unusable_chat_ids_list_nothing(tmp_path, monkeypatch):
    _no_gateway(monkeypatch)
    _db(tmp_path, [("s1", "assistant", "MEDIA:/root/map.png")]).close()
    monkeypatch.setattr(sys.modules["hermes_constants"], "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_artifacts_index", lambda: index)
    assert api.list_artifacts(session_ids=["x" * 500, "  "]) == {"items": [], "next_before": None}


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
    monkeypatch.setattr(api, "_artifacts_index", lambda: index)
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    client = TestClient(app)
    assert [i["name"] for i in client.get(f"{BASE}/artifacts?missing=show").json()["items"]] == ["gone.png"]
    assert client.get(f"{BASE}/artifacts?missing=hide").json()["items"] == []
    assert client.get(f"{BASE}/artifacts?missing=bogus").status_code == 400
